# -*- coding: utf-8 -*-
"""单路摄像头。

一条常驻 ffmpeg：RTSP → 小尺寸 MJPEG 字节流。
这一份字节同时供 ①网页预览 ②人形检测 ③抽帧存图 使用 —— 只拉一次、只解一次。

检测到人时另起一条 ffmpeg 用 -c copy 原样封装落盘（优先避免重新编码），人消失即停。
"""
import os
import subprocess
import threading
import time

import cv2
import numpy as np

from .config import records_root
from .detect import Detector
from .ffmpeg import (cmd_record, cmd_stream, ffmpeg_exe, iter_jpegs, popen,
                     probe, run as run_ffmpeg, stop_process)
from .safety import redact_urls

# 正在做「faststart 重封装」的文件：回放列表要把它们当成「未完成」排除掉，
# 避免浏览器拿到重写到一半的 mp4。
_REMUXING = set()
_REMUX_LK = threading.Lock()


def remuxing_paths():
    """返回正在重封装的录像绝对路径集合。"""
    with _REMUX_LK:
        return set(_REMUXING)


def _is_fragmented(path):
    """分片 MP4（+frag_keyframe+empty_moov）浏览器 seek 很卡、时长读不出。

    判断依据：文件头里有 moof 分片盒子。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
        return b"moof" in head
    except Exception:
        return False


def _faststart(src, log=None):
    """把 mp4 重封装成 moov 在头部的 progressive 格式 —— 秒拖动、秒起播。

    纯 copy 不重编码，几十 MB 也就 1~2 秒。先写 .fs.mp4 再原子替换，
    中途被打断也不会破坏原文件。
    """
    log = log or (lambda *a: None)
    tmp = src[:-4] + ".fs.mp4"      # 必须以 .mp4 结尾，否则 ffmpeg 认不出封装格式
    try:
        cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
               "-i", src, "-c", "copy", "-movflags", "+faststart", tmp]
        p = run_ffmpeg(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=900)
        if p.returncode == 0 and os.path.exists(tmp) \
                and os.path.getsize(tmp) > 1024:
            os.replace(tmp, src)
            log("[录像] %s 已转为可秒播格式" % os.path.basename(src))
            return True
        log("[录像] %s 重封装失败 rc=%s" % (os.path.basename(src),
                                            p.returncode))
    except Exception as e:
        log("[录像] %s 重封装异常: %s" % (os.path.basename(src), e))
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
    return False


def schedule_faststart(path, log=None):
    """后台重封装，期间该文件在回放列表里隐藏（视为「写入中」）。"""
    if not path or not os.path.exists(path):
        return
    if not _is_fragmented(path):
        return                       # 已经是 progressive 了，不用动
    with _REMUX_LK:
        if path in _REMUXING:
            return
        _REMUXING.add(path)

    def run():
        try:
            _faststart(path, log)
        finally:
            with _REMUX_LK:
                _REMUXING.discard(path)

    threading.Thread(target=run, daemon=True).start()


class Cam(threading.Thread):
    def __init__(self, spec, cfg, log=None):
        threading.Thread.__init__(self)
        self.daemon = True
        self.spec = spec
        self.cfg = cfg
        self.log = log or (lambda *a: None)

        self.id = spec.get("id") or "cam"
        self.name = spec.get("name") or self.id
        self.url = spec.get("url") or ""
        self.enabled = bool(spec.get("enabled", True))
        self.detect_on = bool(spec.get("detect", True))

        self.stream_url = self.url      # 常驻取流（最小分辨率）
        self.record_url = self.url      # 事件录像（最大分辨率，copy 不解码）
        self.stream_wh = (0, 0)
        self._picked = False

        self.state = "idle"
        self.last_error = ""
        self.running = False
        # Thread.join() calls Thread._stop(); do not shadow that method.
        self._stop_event = threading.Event()
        self._proc_lock = threading.Lock()
        self._rec_lock = threading.RLock()
        self._connect_t0 = 0.0

        self.proc = None
        self.lock = threading.Lock()
        self.last_jpeg = b""
        self.last_ts = 0.0
        self.seq = 0
        self.fps = 0.0
        self._fps_n = 0
        self._fps_t0 = 0.0

        # 检测状态（由 Hub 的串行调度器读写）
        self.det = None
        self.hit = 0
        self.person = False
        self.last_person = 0.0
        self.last_event_end = 0.0

        # 录制
        self.rec_proc = None
        self.rec_path = ""
        self.rec_t0 = 0.0
        self.rec_transcode = False

        # 解码缓存（检测用，避免同一帧重复解码）
        self._dec = None
        self._dec_seq = -1

        # 抽帧（独立定时器线程，跟取流帧率/突发无关）
        self._tl_thread = None
        self._watch = None
        self._det_opts = None

    # ---------------- 生命周期 ----------------

    def start(self):
        if not self.running:
            self.running = True
            self._stop_event.clear()
            threading.Thread.start(self)
            self._tl_thread = threading.Thread(target=self._timelapse_loop,
                                              daemon=True)
            self._tl_thread.start()
            self._watch = threading.Thread(target=self._watchdog, daemon=True)
            self._watch.start()

    def stop(self):
        self._stop_event.set()
        self._close_proc()
        self.stop_record()
        for thread in (self, self._tl_thread, self._watch):
            if thread is not None and thread is not threading.current_thread() \
                    and thread.is_alive():
                thread.join(timeout=3)

    def run(self):
        retry = max(0.1, float(self.cfg.get("stream", "retry_sec", default=5) or 5))
        backoff = retry
        while not self._stop_event.is_set():
            if not self.enabled or not self.url:
                self.state = "idle"
                self._stop_event.wait(1.0)
                continue
            try:
                self._pick_stream()
                if not self._stop_event.is_set():
                    self._pump()
            except Exception as e:
                self.last_error = redact_urls(e)[:200]
            self._close_proc()
            if self._stop_event.is_set():
                break
            if self.last_ts and self.last_ts >= self._connect_t0:
                backoff = retry
            self.stop_record()
            self.state = "offline"
            self.log("[取流] %s 断开（%s），%.0fs 后重连" %
                     (self.name, self.last_error, backoff))
            if self._stop_event.wait(backoff):
                break
            backoff = min(backoff * 1.6, max(20.0, retry))
        self.stop_record()
        self.state = "idle"
        self.person = False
        self.hit = 0
        self.running = False

    # ---------------- 码流优选（只做一次） ----------------

    def _pick_stream(self):
        if self._picked or not self.cfg.get("stream", "auto_pick", default=True):
            return
        self._picked = True
        try:
            from .ffmpeg import probe_all
            res = probe_all(self.url,
                            timeout=float(self.cfg.get("stream", "timeout_sec",
                                                       default=10)) * 0.5,
                            transport=self.cfg.get("stream", "transport",
                                                   default="tcp"),
                            log=self.log, cancel=self._stop_event.is_set)
        except Exception:
            res = []
        if not res:
            return
        res.sort(key=lambda x: x[1] * x[2])
        lo, hi = res[0], res[-1]
        self.stream_url, self.stream_wh = lo[0], (lo[1], lo[2])
        self.record_url = hi[0]
        # H265 等浏览器解不了的编码，录像时才转码
        self.rec_transcode = ("hevc" in (hi[3] or "").lower()
                              or "265" in (hi[3] or "").lower())
        self.log("[码流] %s 取流 %dx%d / 录像 %dx%d(%s)" %
                 (self.name, lo[1], lo[2], hi[1], hi[2], hi[3] or ""))

    # ---------------- 取流主循环 ----------------

    def _pump(self):
        s = self.cfg.get("stream")
        cmd = cmd_stream(self.stream_url,
                         fps=int(s.get("fps", 8) or 8),
                         width=int(s.get("width", 480) or 480),
                         quality=int(s.get("quality", 6) or 6),
                         transport=s.get("transport", "tcp") or "tcp",
                         timeout=float(s.get("timeout_sec", 10) or 10))
        with self._proc_lock:
            if self._stop_event.is_set():
                return
            self.state = "connecting"
            self._connect_t0 = time.time()
            proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.proc = proc
        self._fps_n = 0
        self._fps_t0 = time.time()
        got = False

        for jpeg in iter_jpegs(proc.stdout):
            if self._stop_event.is_set():
                return
            now = time.time()
            with self.lock:
                self.last_jpeg = jpeg
                self.last_ts = now
                self.seq += 1
            got = True
            if not self.state == "online":
                self.state = "online"
                self.last_error = ""
            self._tick_fps(now)
            if proc.poll() is not None:
                raise RuntimeError("取流进程退出 rc=%s" % proc.returncode)

        if got:
            raise RuntimeError("取流进程结束（流已断）")
        raise RuntimeError("连接超时/无数据")

    def _tick_fps(self, now):
        self._fps_n += 1
        dt = now - self._fps_t0
        if dt >= 1.0:
            self.fps = round(self._fps_n / dt, 1)
            self._fps_n = 0
            self._fps_t0 = now
        elif dt >= 0.25 and self._fps_n >= 2:
            # 窗口不足 1 秒也给个读数（文件源/爆帧时不会一直显示 0）
            self.fps = round(self._fps_n / dt, 1)

    def _close_proc(self):
        with self._proc_lock:
            p = self.proc
            self.proc = None
        if p is None:
            return
        try:
            stop_process(p, wait=2.0)
        except Exception:
            pass

    # ---------------- 断流看门狗 ----------------
    def _watchdog(self):
        """ffmpeg 进程活着但长期不出帧（假死）时，强制关掉让它重连。"""
        stall = float(self.cfg.get("stream", "stall_sec", default=25) or 25)
        connect_timeout = max(0.1, float(self.cfg.get("stream", "timeout_sec", default=10) or 10))
        while not self._stop_event.is_set():
            if self._stop_event.wait(min(5.0, max(0.1, min(stall, connect_timeout) / 2))):
                return
            if self.state in ("connecting", "online") and self.proc is not None:
                reference = max(self.last_ts, self._connect_t0)
                limit = connect_timeout if self.state == "connecting" else stall
                if reference and time.time() - reference > limit:
                    self.last_error = "取流超时/长期无新帧"
                    self.log("[看门狗] %s 超过 %.0fs 无新帧，强制重连" %
                             (self.name, limit))
                    self._close_proc()

    # ---------------- 抽帧存图（独立定时器，零编码） ----------------

    def _timelapse_loop(self):
        """独立定时器：每 timelapse_sec 秒抓一帧存盘。

        关键：不依赖取流帧的到达时刻。ffmpeg 在文件/生成类源上会"成批发帧"，
        若按帧时刻判断会整批漏掉、把全天帧数砍半。改为挂钟定时抓取最新帧，
        任何来源都精确每 N 秒 1 帧（默认 720 → 全天 120 帧 = 2 分钟视频）。
        """
        while not self._stop_event.is_set():
            sec = float(self.cfg.get("record", "timelapse_sec", default=720))
            if sec <= 0:
                if self._stop_event.wait(1.0):
                    return
                continue
            if self._stop_event.wait(sec):
                return
            if self.state != "online":
                continue
            with self.lock:
                jpeg = self.last_jpeg
            if not jpeg:
                continue
            self._save_tl(jpeg)

    def _save_tl(self, jpeg):
        try:
            now = time.time()
            day = time.strftime("%Y-%m-%d", time.localtime(now))
            d = os.path.join(records_root(self.cfg), self.id, day, "tl")
            os.makedirs(d, exist_ok=True)
            n = len([x for x in os.listdir(d) if x.endswith(".jpg")])
            with open(os.path.join(d, "%06d.jpg" % (n + 1)), "wb") as f:
                f.write(jpeg)
        except Exception as e:
            self.log("[抽帧] %s 失败: %s" % (self.name, e))

    # ---------------- 事件录像（有人 → 开，人走 → 停） ----------------

    def start_record(self):
        with self._rec_lock:
            self._start_record()

    def _start_record(self):
        if self._stop_event.is_set() or self.recording() or not self.url:
            return
        try:
            day = time.strftime("%Y-%m-%d")
            d = os.path.join(records_root(self.cfg), self.id, day, "event")
            os.makedirs(d, exist_ok=True)
            # Include subsecond time to avoid collisions on quick restarts.
            stamp = time.time_ns()
            p = os.path.join(d, time.strftime("%H%M%S") + "_%09d.mp4" % (stamp % 1000000000))
            cmd = cmd_record(self.record_url, p,
                             transport=self.cfg.get("stream", "transport",
                                                    default="tcp"),
                             timeout=float(self.cfg.get("stream", "timeout_sec",
                                                        default=10) or 10),
                             transcode=self.rec_transcode)
            import subprocess
            self.rec_proc = popen(cmd, stdin=subprocess.PIPE,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
            self.rec_path = p
            self.rec_t0 = time.time()
            self.log("[事件] %s 检测到人 → 开始录像" % self.name)
        except Exception as e:
            self.log("[事件] %s 启动录像失败: %s" % (self.name, redact_urls(e)))
            self.rec_proc = None

    def stop_record(self):
        with self._rec_lock:
            self._stop_record()

    def _stop_record(self):
        p, self.rec_proc = self.rec_proc, None
        if p is not None:
            stop_process(p, wait=4.0)
        if self.rec_path:
            try:
                # 太小的多半是空壳，丢掉免得占目录
                if os.path.getsize(self.rec_path) < 20 * 1024:
                    os.remove(self.rec_path)
                else:
                    self.log("[事件] %s 人已离开 → 录像 %s（%.0fs）" %
                             (self.name, os.path.basename(self.rec_path),
                              time.time() - self.rec_t0))
                    # 录像时用的是分片 MP4（可降低异常中断影响，但不保证断电不丢），但浏览器 seek 很卡、时长读不出；
                    # 收完号后台转成 moov 在头部的格式，回放才能拖动/秒切。
                    schedule_faststart(self.rec_path, self.log)
            except Exception:
                pass
        self.rec_path = ""
        self.rec_t0 = 0.0

    def recording(self):
        with self._rec_lock:
            if self.rec_proc is not None and self.rec_proc.poll() is not None:
                self._stop_record()
            return self.rec_proc is not None

    def rec_too_long(self):
        mx = float(self.cfg.get("record", "max_event_min", default=30) or 30)
        return mx > 0 and self.rec_t0 and (time.time() - self.rec_t0) > mx * 60

    # ---------------- 供外部读取 ----------------

    def snapshot(self):
        """最新 JPEG 字节（预览直接转发，不再编码）。"""
        with self.lock:
            return self.last_jpeg, self.seq

    def frame_bgr(self):
        """解码后的小图（检测用，同帧只解一次）。"""
        with self.lock:
            jpeg, seq = self.last_jpeg, self.seq
        if not jpeg:
            return None
        if seq == self._dec_seq and self._dec is not None:
            return self._dec
        try:
            img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            img = None
        self._dec, self._dec_seq = img, seq
        return img

    def ensure_detector(self):
        opts = self.cfg.get("detect")
        relevant = {key: opts.get(key) for key in ("mode", "min_area")}
        if self.det is None or self._det_opts != relevant:
            self.det = Detector(opts)
            self._det_opts = relevant
        return self.det

    def info(self):
        st = self.state
        return {
            "id": self.id, "name": self.name, "url": self.url,
            "enabled": self.enabled, "detect": self.detect_on,
            "state": st, "fps": self.fps,
            "stream": self.stream_url, "wh": "%dx%d" % self.stream_wh,
            "person": bool(self.person), "rec": self.recording(),
            "error": self.last_error,
        }
