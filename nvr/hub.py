# -*- coding: utf-8 -*-
"""调度中心：管路摄像头、串行人形检测、每日定时任务。

三条铁律
  * 增删改成请求只置标志（request_sync），真正的重建在后台线程做 —— 减少阻塞网页请求。
  * 人形检测全局串行，同一时刻只算 1 路 —— CPU 峰值可控。
  * 定时抽帧仍会写盘；事件录像按检测状态启停。
"""
import os
import shutil
import threading
import time

from . import discovery, mailer
from .camera import Cam
from .config import log_path, records_root
from .safety import redact_urls


class Hub(object):
    def __init__(self, cfg, log=None):
        self.cfg = cfg
        self.log = log or self._default_log
        self.cams = {}
        self.lock = threading.RLock()

        # 异步同步（web 请求只置标志）
        self._need_sync = False
        self._sync_cv = threading.Condition()
        self._stop = threading.Event()

        # 扫描
        self.scan_state = {"running": False, "done": 0, "total": 0,
                           "results": [], "error": "", "elapsed": 0, "t0": 0,
                           "log": []}
        self._scan_cancel = threading.Event()
        self._scan_thread = None
        self._scan_lock = threading.Lock()
        self._push_retry_at = 0.0

        # 微信 ClawBot（懒加载，没启用就不占资源）
        self.clawbot = None
        self.preview_service = None
        self._preview_lock = threading.RLock()

        self._start_threads()
        self._boot_clawbot()
        self.sync_preview()

    # ---------------- 单摄像头局域网预览 ----------------

    def get_preview_service(self):
        with self._preview_lock:
            if self._stop.is_set():
                raise ValueError("程序正在退出，无法开启预览。")
            if self.preview_service is None:
                from .preview import PreviewService
                self.preview_service = PreviewService(self.cfg, self.get, log=self.log)
            return self.preview_service

    def sync_preview(self):
        with self._preview_lock:
            if self._stop.is_set():
                return
            if self.preview_service is None and not self.cfg.get("preview", "enabled", default=False):
                return
            try:
                service = self.get_preview_service()
                service.sync()
                if self._stop.is_set():
                    service.stop()
            except Exception as exc:
                self.log("[预览] 局域网服务未就绪（%s），请检查预览设置。" % type(exc).__name__)

    def preview_status(self):
        if self.preview_service is not None:
            return self.preview_service.status()
        return {"enabled": bool(self.cfg.get("preview", "enabled", default=False)),
                "running": False, "address": self.cfg.get("preview", "address", default=""),
                "port": self.cfg.get("preview", "port", default=8803), "error": ""}

    def preview_link(self, cid):
        with self._preview_lock:
            if self._stop.is_set():
                raise ValueError("程序正在退出，无法开启预览。")
            service = self.get_preview_service()
            link = service.make_link(cid)
            if self._stop.is_set():
                service.stop()
                raise ValueError("程序正在退出，无法开启预览。")
            return link

    def revoke_preview(self):
        if self.preview_service is not None:
            self.preview_service.revoke()

    # ---------------- 微信 ClawBot ----------------

    def _boot_clawbot(self):
        """已登录过就随程序自动接上（token 过期会在循环里报 expired）。"""
        try:
            c = self.cfg.get("clawbot")
            if not c.get("enabled", True) or not c.get("auto_start", True):
                return
            if not c.get("token"):
                return
            self.get_clawbot(restart=True).start_loop()
        except Exception as e:
            self.log("[clawbot] 启动异常: %s" % e)

    def get_clawbot(self, restart=False):
        if self.clawbot is None or restart:
            from .clawbot import ClawBot
            if self.clawbot is not None:
                try:
                    self.clawbot.stop()
                except Exception:
                    pass
            self.clawbot = ClawBot(self.cfg, hub=self, log=self.log)
        return self.clawbot

    # ---------------- 日志 ----------------

    def _default_log(self, msg):
        try:
            msg = redact_urls(msg)
            line = time.strftime("%H:%M:%S") + " " + msg
            print(line, flush=True)
            with open(log_path(), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d ") + line + "\n")
        except Exception:
            pass

    # ---------------- 线程 ----------------

    def _start_threads(self):
        self._threads = [threading.Thread(target=target, daemon=True)
                         for target in (self._sync_worker, self._detect_loop,
                                        self._daily_loop)]
        for thread in self._threads:
            thread.start()

    def stop_all(self):
        self._stop.set()
        # Share the lock with the whole link/sync operation: an in-flight
        # make_link() may start a listener, so shutdown must finish after it.
        with self._preview_lock:
            if self.preview_service is not None:
                self.preview_service.stop()
        self.stop_scan()
        with self._sync_cv:
            self._sync_cv.notify_all()
        with self.lock:
            cams = list(self.cams.values())
        for c in cams:
            try:
                c.stop()
            except Exception:
                pass
        if self.clawbot is not None:
            try:
                self.clawbot.stop()
            except Exception:
                pass
        for thread in self._threads + ([self._scan_thread] if self._scan_thread else []):
            if thread is not threading.current_thread() and thread.is_alive():
                thread.join(timeout=3)
        self.log("[系统] 已停止")

    # ---------------- 同步（异步） ----------------

    def request_sync(self):
        with self._sync_cv:
            self._need_sync = True
            self._sync_cv.notify_all()

    def _sync_worker(self):
        while not self._stop.is_set():
            with self._sync_cv:
                self._sync_cv.wait_for(lambda: self._need_sync or self._stop.is_set(), 1.0)
                if self._stop.is_set():
                    return
                if not self._need_sync:
                    continue
                self._need_sync = False
            try:
                self.sync()
            except Exception as e:
                self.log("[同步] 异常: %s" % e)

    def sync(self):
        """按配置重建摄像头集合（同步执行，别在请求线程里调）。"""
        with self.lock:
            if self._stop.is_set():
                return
            specs = {c.get("id"): c for c in (self.cfg.cameras or [])
                     if c.get("id")}
            # 停掉已删除 / 已禁用的
            for cid, cam in list(self.cams.items()):
                s = specs.get(cid)
                if s is None or not s.get("enabled", True):
                    cam.stop()
                    del self.cams[cid]
                    self.log("[连接] 移除 %s" % cam.name)
                    continue
                cam.name = s.get("name") or cam.name
                cam.detect_on = bool(s.get("detect", True))
                if cam.url != s.get("url"):
                    cam.stop()
                    self.cams.pop(cid, None)
            # 新增加 / 重新启用
            for cid, s in specs.items():
                if not s.get("enabled", True):
                    continue
                if cid in self.cams:
                    continue
                cam = Cam(s, self.cfg, log=self.log)
                self.cams[cid] = cam
                cam.start()
                self.log("[连接] 启动 %s" % (s.get("name") or cid))

    def get(self, cid):
        with self.lock:
            return self.cams.get(cid)

    def recording_paths(self):
        """当前正在写入（事件录像中）的文件绝对路径集合 —— 回放列表据此排除「写入中」。"""
        out = set()
        with self.lock:
            for c in self.cams.values():
                # rec_proc is cleared before FFmpeg finishes stopping. Keep
                # rec_path hidden until stop_record hands it to the remux set.
                if c.rec_path:
                    out.add(c.rec_path)
        # Read remux state after camera paths: stop_record registers remux before
        # clearing rec_path, so a concurrent handoff is present in either set.
        try:
            from .camera import remuxing_paths
            out |= remuxing_paths()
        except Exception:
            pass
        return out

    def info(self):
        with self.lock:
            return [c.info() for c in self.cams.values()]

    # ---------------- 人形检测（全局串行） ----------------

    def _detect_loop(self):
        while not self._stop.is_set():
            interval = 0.5
            try:
                d = self.cfg.get("detect")
                interval = max(0.05, float(d.get("interval", 0.5) or 0.5))
                confirm = max(1, int(d.get("confirm", 2) or 2))
                gone = max(1.0, float(d.get("gone_sec", 8) or 8))
                cool = max(0.0, float(d.get("cooldown", 10)))
                enabled = bool(d.get("enabled", True))

                with self.lock:
                    cams = list(self.cams.values())
                for cam in cams:
                    if self._stop.is_set():
                        return
                    if not enabled or not cam.detect_on:
                        cam.person = False
                        cam.hit = 0
                        if cam.recording():
                            cam.stop_record()
                            cam.last_event_end = time.time()
                        continue
                    if cam.state != "online":
                        cam.person = False
                        cam.hit = 0
                        if cam.recording():
                            cam.stop_record()
                            cam.last_event_end = time.time()
                        continue
                    img = cam.frame_bgr()
                    seq = getattr(cam, "seq", None)
                    if img is None or (seq is not None and seq == getattr(cam, "_last_detection_seq", None)):
                        if cam.person and time.time() - cam.last_person > gone:
                            cam.person = False
                            cam.hit = 0
                            cam.stop_record()
                            cam.last_event_end = time.time()
                        continue
                    cam._last_detection_seq = seq
                    try:
                        has = cam.ensure_detector().process(img)
                    except Exception:
                        has = False
                    if has:
                        cam.hit += 1
                        cam.last_person = time.time()
                        if cam.hit >= confirm:
                            cam.person = True
                            if not cam.recording() and (time.time() - cam.last_event_end) >= cool:
                                cam.start_record()
                    else:
                        cam.hit = 0
                        if cam.person and (time.time() - cam.last_person) > gone:
                            cam.person = False
                            cam.stop_record()
                            cam.last_event_end = time.time()
                    if cam.person and cam.rec_too_long():
                        cam.stop_record()
                        cam.last_event_end = time.time()
            except Exception as e:
                self.log("[检测] 异常: %s" % e)
            if self._stop.wait(interval):
                return

    # ---------------- 每日任务：推送 + 清理 ----------------

    def _daily_loop(self):
        while not self._stop.is_set():
            try:
                self._check_merge()
                self._check_push()
                self._check_clean()
            except Exception as e:
                self.log("[定时] 异常: %s" % e)
            if self._stop.wait(60):
                return

    # ---------------- 抽帧录像自动合成（让回放有「生成完的录像」可看） ----------------

    def _check_merge(self):
        """把已结束那天的抽帧自动合成为 mp4（仅对 < 今天的日期，且还没合过）。

        每天最多合 4 个，分摊 CPU，避免一次占满；幂等（已合过直接跳过）。
        """
        from . import mailer
        today = time.strftime("%Y-%m-%d")
        try:
            root = records_root(self.cfg)
            todo = []
            for c in (self.cfg.cameras or []):
                if not c.get("enabled", True):
                    continue
                cid = c.get("id")
                cdir = os.path.join(root, cid)
                if not os.path.isdir(cdir):
                    continue
                for name in os.listdir(cdir):
                    if len(name) == 10 and name < today:   # 仅已结束的日期
                        mp4 = os.path.join(root, cid,
                                          "timelapse_%s.mp4" % name)
                        if not (os.path.exists(mp4)
                                and os.path.getsize(mp4) > 512):
                            todo.append((cid, name))
            for cid, day in todo[:4]:
                try:
                    mailer.merge_day(self.cfg, cid, day, log=self.log)
                except Exception as e:
                    self.log("[合成] %s %s 异常: %s" % (cid, day, e))
        except Exception as e:
            self.log("[合成] 异常: %s" % e)

    def _check_push(self):
        m = self.cfg.get("mail")
        if not m.get("enabled", False):
            return
        want = (m.get("time") or "08:00").strip()
        now = time.localtime()
        hhmm = time.strftime("%H:%M", now)
        today = time.strftime("%Y-%m-%d", now)
        if hhmm < want:
            return
        if (m.get("last_sent") or "") == today:
            return
        if time.time() < self._push_retry_at:
            return
        self._push_retry_at = time.time() + 600
        try:
            ok, msg = mailer.push_yesterday(self.cfg, log=self.log)
            if ok:
                self.cfg.set("mail", "last_sent", today)
            self.log("[推送] %s" % msg)
        except Exception as e:
            self.log("[推送] 异常: %s" % e)

    def _check_clean(self):
        days = float(self.cfg.get("record", "keep_days", default=7) or 7)
        if days <= 0:
            return
        root = records_root(self.cfg)
        try:
            today = time.strftime("%Y-%m-%d")
            limit = time.time() - days * 86400
            for cid in os.listdir(root):
                cdir = os.path.join(root, cid)
                if not os.path.isdir(cdir):
                    continue
                for name in os.listdir(cdir):
                    p = os.path.join(cdir, name)
                    try:
                        if name.startswith("timelapse_"):
                            day = name[len("timelapse_"):-4]
                            if day < today and os.path.getmtime(p) < limit:
                                os.remove(p)
                                self.log("[清理] 删除 %s" % name)
                        elif os.path.isdir(p) and len(name) == 10:
                            if time.mktime(time.strptime(name, "%Y-%m-%d")) < limit:
                                shutil.rmtree(p, ignore_errors=True)
                                self.log("[清理] 删除目录 %s/%s" % (cid, name))
                    except Exception:
                        continue
        except Exception:
            pass

    # ---------------- 扫描 ----------------

    def start_scan(self, range_spec=None, port=None, users=None,
                   pwds=None, timeout=None):
        with self._scan_lock:
            return self._start_scan(range_spec, port, users, pwds, timeout)

    def _start_scan(self, range_spec=None, port=None, users=None,
                    pwds=None, timeout=None):
        st = self.scan_state
        if st["running"] or self._stop.is_set():
            return False
        s = self.cfg.get("scan")
        rng = (range_spec or "").strip() or (s.get("range") or "").strip()
        # 没填范围 → 自动取本机所有网卡网段（摄像头常接在副网卡上）
        if not rng:
            subs = discovery.lan_subnets()
            if not subs:
                sg = discovery.lan_suggest().get("range")
                if sg:
                    subs = [sg]
            if not subs:
                st["error"] = "无法确定网段，请在扫描框里填 IP 范围"
                return False
            rng = ",".join(subs)
            st["_ranges"] = subs
        else:
            st["_ranges"] = [part.strip() for part in rng.split(",") if part.strip()]
        # 记住这次用的范围（换电脑后仍然记住）
        if rng != (s.get("range") or ""):
            self.cfg.set("scan", "range", rng)
        port = int(port or s.get("port", 554) or 554)
        users = users or s.get("users", "admin")
        pwds = pwds or s.get("pwds", "admin123,123456,admin")
        timeout = float(timeout or s.get("timeout", 5) or 5)

        st.update({"running": True, "done": 0, "total": 0, "results": [],
                   "error": "", "elapsed": 0, "t0": time.time(), "log": []})
        # A cancellation belongs to one scan. Keep running true until its
        # worker exits, preventing old workers from overwriting a new scan.
        cancel_event = threading.Event()
        self._scan_cancel = cancel_event

        def logcap(msg):
            msg = redact_urls(msg)
            try:
                st["log"].append(msg)
                if len(st["log"]) > 200:
                    st["log"] = st["log"][-200:]
            except Exception:
                pass
            self.log(msg)

        def prog(done, total):
            st["done"] = done
            st["total"] = total

        # 跨网段累计进度
        try:
            grand = sum(len(discovery.expand(r)) for r in st["_ranges"])
        except Exception:
            grand = 0

        def work():
            try:
                allres = []
                base = [0]
                for sub in st.get("_ranges", [rng]):
                    if cancel_event.is_set():
                        break
                    logcap("[扫描] 开始网段 %s" % sub)
                    def prog2(done, total):
                        st["done"] = base[0] + done
                        if grand:
                            st["total"] = grand
                    res = discovery.scan(sub, port, users, pwds, timeout,
                                         log=logcap, progress=prog2,
                                         cancel=cancel_event.is_set)
                    base[0] += len(discovery.expand(sub))
                    allres += res
                st["results"] = allres
            except Exception as e:
                st["error"] = redact_urls(e)[:200]
                logcap("[扫描] 异常: %s" % e)
            finally:
                with self._scan_lock:
                    st["elapsed"] = round(time.time() - st["t0"], 1)
                    st["running"] = False

        self._scan_thread = threading.Thread(target=work, daemon=True)
        self._scan_thread.start()
        return True

    def stop_scan(self):
        with self._scan_lock:
            self._scan_cancel.set()
        return True
