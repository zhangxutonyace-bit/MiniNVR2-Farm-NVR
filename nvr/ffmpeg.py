# -*- coding: utf-8 -*-
"""ffmpeg 封装：定位二进制 / 切 JPEG 流 / 探测分辨率 / 优选码流 / 录制命令。

所有和 ffmpeg 打交道的地方都在这里，方便统一处理打包环境、超时和隐藏窗口。
"""
import os
import re
import subprocess
import sys
import threading
import time

from .safety import redact_urls

# Windows 下隐藏子进程黑框
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_POPEN_KW = {"creationflags": _CREATE_NO_WINDOW} if os.name == "nt" else {}

_EXE_CACHE = {"path": None}


def ffmpeg_exe():
    """按优先级找 ffmpeg：exe 同级 → 包内 → imageio-ffmpeg → PATH。"""
    if _EXE_CACHE["path"]:
        return _EXE_CACHE["path"]

    cands = []
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
        else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cands.append(os.path.join(base, "ffmpeg.exe"))
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        cands.append(os.path.join(meipass, "ffmpeg.exe"))
    try:
        import imageio_ffmpeg
        cands.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass
    cands.append("ffmpeg")

    for c in cands:
        try:
            if not c:
                continue
            if os.path.sep in c and not os.path.exists(c):
                continue
            p = subprocess.run([c, "-version"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=8, **_POPEN_KW)
            if p.returncode == 0:
                _EXE_CACHE["path"] = c
                return c
        except Exception:
            continue
    _EXE_CACHE["path"] = "ffmpeg"
    return "ffmpeg"


def popen(args, **kw):
    k = dict(_POPEN_KW)
    k.update(kw)
    k.setdefault("stdout", subprocess.PIPE)
    k.setdefault("stderr", subprocess.PIPE)
    k.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.Popen(args, **k)


def run(args, **kw):
    """Run a bounded helper with the same hidden-window policy as streams."""
    k = dict(_POPEN_KW)
    k.update(kw)
    return subprocess.run(args, **k)


# ---------------- 从 pipe 里切出一张张 JPEG ----------------

def iter_jpegs(stream, bufsize=65536):
    """从字节流里按 FFD8..FFD9 切出完整 JPEG。

    MJPEG 压缩数据里的 FF 会被转义成 FF00，所以 FFD9 只会出现在帧尾，安全。
    """
    buf = b""
    while True:
        # BufferedReader.read(size) may wait for a whole 64 KiB even though a
        # complete small JPEG is already available. read1 keeps preview live.
        chunk = getattr(stream, "read1", stream.read)(bufsize)
        if not chunk:
            return
        buf += chunk
        while True:
            i = buf.find(b"\xff\xd8")
            if i < 0:
                buf = b"\xff" if buf.endswith(b"\xff") else b""
                break
            if i > 0:
                buf = buf[i:]
            j = buf.find(b"\xff\xd9", 2)
            if j < 0:
                break
            frame = buf[:j + 2]
            buf = buf[j + 2:]
            if len(frame) > 256:
                yield frame
        # 防止没帧时 buf 无限增长
        if len(buf) > 4 * 1024 * 1024:
            buf = b""


# ---------------- 探测：拿到一条 URL 的真实分辨率 ----------------

_RES_RE = re.compile(r"Stream #\d+:\d+.*?Video:.*?,\s*(\d{2,5})x(\d{2,5})")


def probe(url, timeout=4.0, transport="tcp", cancel=None):
    """用 ffmpeg 抓 1 帧拿分辨率。返回 (w, h, codec) 或 None。"""
    # 注意：本机 imageio-ffmpeg(v7.1) 不认 -rw_timeout / -stimeout，
    # 加这俩会让 RTSP 命令在选项解析阶段直接失败（"Option not found"）。
    # 所以这里不加 ffmpeg 层超时，改由 subprocess.run(timeout=) 控制。
    cmd = [ffmpeg_exe(), "-hide_banner"]
    if str(url).lower().startswith("rtsp://"):
        cmd += ["-rtsp_transport", transport]
    cmd += ["-i", url, "-frames:v", "1", "-f", "null", "-"]
    p = None
    try:
        if cancel and cancel():
            return None
        p = popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + timeout + 3
        while True:
            if (cancel and cancel()) or time.monotonic() >= deadline:
                return None
            try:
                _, stderr = p.communicate(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                continue
        if p.returncode != 0:
            return None
        err = (stderr or b"").decode("utf-8", "replace")
        m = _RES_RE.search(err)
        if not m:
            return None
        w, h = int(m.group(1)), int(m.group(2))
        cm = re.search(r"Video:\s*([a-z0-9_]+)", err)
        codec = cm.group(1) if cm else ""
        return (w, h, codec)
    except Exception:
        return None
    finally:
        if p is not None:
            stop_process(p, wait=0.5)


# ---------------- 码流路径候选 ----------------

# 与品牌无关的通用候选；探测后自动选分辨率最小的那条
PATH_CANDIDATES = (
    "av0_1", "av0_0", "av0_2",
    "1/1", "1/2", "0/1", "0/0",
    "Streaming/Channels/101", "Streaming/Channels/102",
    "cam/realmonitor?channel=1&subtype=0", "cam/realmonitor?channel=1&subtype=1",
    "h264/ch1/main/av_stream", "h264/ch1/sub/av_stream",
    "live/ch0", "live/ch1", "live/0", "live/1",
    "ch01/1", "ch01/2", "ch0_0", "ch0_1",
    "stream1", "stream2", "video1", "video2",
    "11", "12", "0", "1", "2",
    "0/0/0", "0/0/1",
)


def split_url(url):
    """拆成 (前缀 rtsp://user:pwd@host:port/, 路径, 查询串)。"""
    m = re.match(r"^(rtsp://[^/]*?)/(.*)$", url or "", re.I)
    if not m:
        return "", "", ""
    rest = m.group(2)
    if "?" in rest:
        path, query = rest.split("?", 1)
        return m.group(1) + "/", path, "?" + query
    return m.group(1) + "/", rest, ""


def candidates(url):
    """基于给定 URL 生成候选码流地址（原地址排第一）。"""
    pre, path, query = split_url(url)
    if not pre:
        return [url] if url else []
    out = [url]
    for p in PATH_CANDIDATES:
        if p == path:
            continue
        out.append(pre + p + query)
    return out


def pick_best(url, timeout=4.0, transport="tcp", max_probe=14, log=None):
    """探测所有候选，返回 (最佳URL, w, h)；全部失败则回退原 URL。

    选「分辨率最小」的那条 —— 预览/抽帧/检测根本用不到主码流，
    用子码流是 CPU 能否压住的关键。
    """
    cands = candidates(url)[:max_probe]
    found = []

    def work(u):
        r = probe(u, timeout=timeout, transport=transport)
        if r and r[0] >= 160:      # 太小的（如 160x120 图标流）不要
            return (u, r[0], r[1], r[2])
        return None

    ths = []
    box = []
    lk = threading.Lock()

    def runner(u):
        r = work(u)
        with lk:
            if r:
                box.append(r)

    for u in cands:
        t = threading.Thread(target=runner, args=(u,), daemon=True)
        t.start()
        ths.append(t)
    for t in ths:
        t.join(timeout + 4)

    if not box:
        if log:
            log("[码流] %s 探测无结果，沿用原地址" % redact_urls(url))
        return url, 0, 0

    box.sort(key=lambda x: x[1] * x[2])
    best = box[0]
    if log:
        log("[码流] 选中 %dx%d → %s" % (best[1], best[2], redact_urls(best[0])))
    return best[0], best[1], best[2]


# ---------------- 命令构造 ----------------

def probe_all(url, timeout=4.0, transport="tcp", max_probe=14, log=None, cancel=None):
    """并发探测所有候选码流，返回 [(url, w, h, codec), ...]（失败的不出现在结果里）。"""
    cands = candidates(url)[:max_probe]
    box = []
    lk = threading.Lock()

    def runner(u):
        r = probe(u, timeout=timeout, transport=transport, cancel=cancel)
        if r and r[0] >= 160:
            with lk:
                box.append((u, r[0], r[1], r[2]))

    ths = []
    for u in cands:
        t = threading.Thread(target=runner, args=(u,), daemon=True)
        t.start()
        ths.append(t)
    for t in ths:
        t.join(timeout + 4)
    if not box and log:
        log("[码流] %s 探测无结果，沿用原地址" % redact_urls(url))
    return box


def cmd_stream(url, fps=8, width=480, quality=6, transport="tcp", timeout=10):
    """常驻取流：RTSP → 小尺寸 MJPEG 字节流（预览 / 检测 / 抽帧 三用）。"""
    vf = []
    if fps and fps > 0:
        vf.append("fps=%d" % int(fps))
    if width and width > 0:
        vf.append("scale=%d:-2" % int(width))
    # 注意：本机 imageio-ffmpeg(v7.1) 不认 -rw_timeout / -stimeout，
    # 加这俩会让 RTSP 命令在选项解析阶段直接失败。超时改由调用方用
    # subprocess 层控制（长连接依赖 camera.py/hub 的自愈重连）。
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error"]
    if str(url).lower().startswith("rtsp://"):
        cmd += ["-rtsp_transport", transport]
    else:
        # 文件/HTTP 等非实时源必须 -re，否则 ffmpeg 一瞬把整段帧灌进管道，
        # 取流线程会"瞬间取完→判定断流→重连"死循环，fps 也永远攒不出 1 秒窗口。
        # RTSP 本身实时，加 -re 反而拖慢，故只对非 rtsp 加。
        cmd += ["-re"]
    cmd += ["-i", url, "-an", "-sn"]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += ["-c:v", "mjpeg", "-q:v", str(int(quality)),
            "-f", "image2pipe", "pipe:1"]
    return cmd


def cmd_record(url, out, transport="tcp", timeout=10, transcode=False):
    """事件录像：优先 -c copy 原样封装（优先避免重新编码）；编码不兼容时才转码。"""
    # 注意：本机 imageio-ffmpeg(v7.1) 不认 -rw_timeout / -stimeout，
    # 加这俩会让 RTSP 命令在选项解析阶段直接失败。超时改由调用方用
    # subprocess 层控制（长连接依赖 camera.py/hub 的自愈重连）。
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error"]
    if str(url).lower().startswith("rtsp://"):
        cmd += ["-rtsp_transport", transport]
    else:
        cmd += ["-re"]
    cmd += ["-i", url, "-an", "-sn"]
    if transcode:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-movflags", "+frag_keyframe+empty_moov", "-f", "mp4", out]
    return cmd


def cmd_local_stream(device, fps=8, width=480, quality=6):
    """Open one video-only DirectShow device; audio is never requested."""
    if not isinstance(device, str) or not device.startswith("@device_") \
            or len(device) > 2048 or any(c in device for c in ('"', '\r', '\n', '\x00')):
        raise ValueError("电脑摄像头标识无效，请刷新设备列表后重新添加。")
    vf = "fps=%d,scale=%d:-2" % (max(1, min(60, int(fps))), max(160, min(3840, int(width))))
    return [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "dshow",
            "-rtbufsize", "16M", "-i", "video=" + device, "-an", "-sn",
            "-vf", vf, "-c:v", "mjpeg", "-q:v", str(int(quality)),
            "-f", "image2pipe", "pipe:1"]


def cmd_jpeg_record(out, fps=8):
    """Encode shared JPEGs with arrival-time PTS, including slow writer gaps.

    image2pipe honors wall-clock timestamps; raw mjpeg generates frame-count PTS
    instead. Direct AVIO and minimal probing avoid accumulating a burst before
    assigning timestamps. VFR output preserves gaps without a catch-up queue.
    """
    return [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
            "-use_wallclock_as_timestamps", "1", "-avioflags", "direct",
            "-probesize", "32", "-analyzeduration", "0", "-fpsprobesize", "0",
            "-f", "image2pipe", "-framerate", str(int(fps)), "-c:v", "mjpeg",
            "-i", "pipe:0", "-an", "-sn", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-pix_fmt", "yuv420p", "-fps_mode", "vfr", "-movflags", "+faststart", "-f", "mp4", out]


def write_concat(files, listf, fps=1):
    """生成 concat 列表（不依赖文件名连续性，Windows 上也稳）。

    路径一律转成绝对并统一成 /：ffmpeg 的 concat demuxer 会按「列表文件所在目录」
    解析相对路径，给相对路径就会拼错（实测出现过目录被重复拼接）。
    """
    dur = 1.0 / float(fps or 1)
    lines = []
    for p in files:
        q = os.path.abspath(p).replace("\\", "/").replace("'", "'\\''")
        lines.append("file '%s'" % q)
        lines.append("duration %.6f" % dur)
    if files:
        q = os.path.abspath(files[-1]).replace("\\", "/").replace("'", "'\\''")
        lines.append("file '%s'" % q)      # 末尾帧重复一次才显示得出来
    with open(listf, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return listf


def cmd_merge(listf, out, fps=1):
    """抽帧合成：一堆 JPEG → 1fps 全天视频。

    加 -movflags +faststart：把 moov 原子移到文件头，浏览器拖动/边下边播更顺。
    """
    return [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", listf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]


def stop_process(proc, wait=4.0):
    """优雅停 ffmpeg（写 'q' 让它收尾写 moov），不行再强杀。"""
    if proc is None:
        return
    try:
        if proc.poll() is None and proc.stdin:
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
            except Exception:
                pass
            try:
                proc.stdin.close()
            except Exception:
                pass
    except Exception:
        pass
    try:
        try:
            # A stream has no interactive stdin, so there is no graceful quit
            # to wait for; terminate immediately to unblock its reader.
            if proc.poll() is None and not proc.stdin:
                proc.terminate()
            proc.wait(timeout=wait)
        except Exception:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
    finally:
        for name in ("stdin", "stdout", "stderr"):
            pipe = getattr(proc, name, None)
            if pipe:
                try:
                    pipe.close()
                except Exception:
                    pass
