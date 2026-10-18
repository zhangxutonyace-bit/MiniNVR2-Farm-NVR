# -*- coding: utf-8 -*-
"""网段扫描：找 RTSP 摄像头。

三段式，越往后越贵：
  ① 自动推导本机网段（不写死任何 IP，换台电脑自己适配）
  ② 并发 TCP 探端口，踢掉不存在的主机（毫秒级）
  ③ 对存活主机做 RTSP DESCRIBE 握手，试路径 × 账号（毫秒级）
"""
import base64
import ipaddress
import re
import socket
import subprocess
import threading
import time
from urllib.parse import quote

from .ffmpeg import PATH_CANDIDATES, run as run_hidden
from .safety import redact_urls

DEFAULT_PATHS = (
    "av0_1", "av0_0", "1/1", "1/2", "0/1", "0",
    "Streaming/Channels/101", "Streaming/Channels/102",
    "cam/realmonitor?channel=1&subtype=0",
    "h264/ch1/main/av_stream", "h264/ch1/sub/av_stream",
    "live/ch0", "ch0_0", "11", "12", "stream1", "video1",
)


# ---------------- 本机网段 ----------------

def lan_ip():
    """拿本机出口网卡 IP（UDP connect 不发实际数据包）。"""
    for target in (("8.8.8.8", 80), ("223.5.5.5", 80), ("1.1.1.1", 80)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(0.5)
                s.connect(target)
                ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            continue
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return ""


def lan_suggest():
    """返回 {'lan_ip':..., 'range':...}，range 可直接填进扫描框。"""
    ip = lan_ip()
    if not ip or ip.startswith("127."):
        return {"lan_ip": "", "range": ""}
    return {"lan_ip": ip, "range": ip.rsplit(".", 1)[0] + ".1-254"}


# ---------------- 范围展开 ----------------

def expand(spec):
    spec = (spec or "").strip()
    if not spec:
        return []
    out = []
    if "/" in spec:
        network = ipaddress.IPv4Network(spec, strict=False)
        if network.num_addresses > 4096:
            raise ValueError("单次扫描范围不能超过 4096 个地址")
        return [str(h) for h in network.hosts()]
    m = re.match(r"^(\d+\.\d+\.\d+)\.(\d+)\s*-\s*(\d+)$", spec)
    if m:
        pre, a, b = m.group(1), int(m.group(2)), int(m.group(3))
        if b < a:
            a, b = b, a
        ipaddress.IPv4Address(pre + ".0")
        for i in range(max(1, a), min(254, b) + 1):
            out.append("%s.%d" % (pre, i))
        return out
    m = re.match(r"^(\d+\.\d+\.\d+)\.(\d+)\s*-\s*(\d+\.\d+\.\d+\.\d+)$", spec)
    if m:
        a, b = m.group(1) + "." + m.group(2), m.group(3)
        try:
            ia = int(ipaddress.IPv4Address(a))
            ib = int(ipaddress.IPv4Address(b))
            if ib < ia:
                ia, ib = ib, ia
            if ib - ia + 1 > 4096:
                raise ValueError("单次扫描范围不能超过 4096 个地址")
            for v in range(ia, ib + 1):
                out.append(str(ipaddress.IPv4Address(v)))
        except ipaddress.AddressValueError as e:
            raise ValueError("无效的 IP 范围") from e
        return out
    out.append(str(ipaddress.IPv4Address(spec)))
    return out


# ---------------- RTSP 握手 ----------------

def _basic(user, pwd):
    raw = ("%s:%s" % (user, pwd)).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def rtsp_ok(ip, port, path, user="", pwd="", timeout=1.5):
    """DESCRIBE 握手；200 = 可用，401/403 = 要换账号，其它 = 路径不存在。"""
    url = "rtsp://%s:%d/%s" % (ip, port, path)
    req = ["DESCRIBE %s RTSP/1.0" % url, "CSeq: 1",
           "Accept: application/sdp", "User-Agent: NVR/2.0"]
    if user:
        req.append("Authorization: Basic %s" % _basic(user, pwd))
    data = ("\r\n".join(req) + "\r\n\r\n").encode("utf-8")
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(data)
            buf = b""
            deadline = time.monotonic() + timeout
            while b"\r\n\r\n" not in buf and len(buf) < 8192:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return 0
                s.settimeout(remaining)
                chunk = s.recv(2048)
                if not chunk:
                    break
                buf += chunk
        head = buf.decode("utf-8", "replace").split("\r\n\r\n")[0]
        m = re.match(r"RTSP/1\.\d\s+(\d+)", head)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


# ---------------- 端口存活 ----------------

def port_open(ip, port, timeout=0.35):
    try:
        s = socket.create_connection((ip, int(port)), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def lan_subnets():
    """返回本机所有网卡的 /24 网段（如 ['192.168.188.1-254','192.168.31.1-254']）。

    摄像头往往接在**第二张网卡**上（比如 PC 同时有以太网+WiFi），
    只取默认路由那张网卡的网段会漏掉它。这里把每张卡的网段都列出来。
    """
    subs = []
    try:
        out = run_hidden(["ipconfig"], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, encoding="gbk",
                         errors="ignore", timeout=10).stdout
        mip = re.finditer(r"IPv4[^:\r\n]*:\s*(\d+\.\d+\.\d+\.\d+)", out)
        for a in mip:
            ip = a.group(1)
            if ip.startswith("127.") or ip.startswith("169.254."):
                continue
            # Deliberately suggest the local /24 rather than expanding a
            # corporate /16; the user may enter a wider bounded range.
            subs.append("%s.1-254" % ip.rsplit(".", 1)[0])
    except Exception:
        pass
    seen = set()
    out = []
    for s in subs:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def scan(range_spec, port=554, users=None, pwds=None, timeout=5,
         log=None, progress=None, cancel=None, max_paths=8):
    """扫描。返回 [{'ip','port','url','user','pwd','path','w','h','codec'}...]

    两阶段：
      ① 并发 TCP 探端口，踢掉不存在的主机（毫秒级）
      ② 对开放主机用 ffmpeg 直接拉流验证（ffmpeg 自动处理 Basic/Digest，
         与真正取流同一引擎，比手写 DESCRIBE 握手可靠得多——
         天地伟业这类相机是 Digest 认证，DESCRIBE 永远 401）。
    """
    from . import ffmpeg
    log = log or (lambda *a: None)
    progress = progress or (lambda *a: None)
    users = [u.strip() for u in ("admin" if users is None else users).split(",")] or [""]
    pwds = ("admin123,123456,admin" if pwds is None else pwds).split(",")
    if not pwds:
        pwds = [""]

    ips = expand(range_spec)
    total = len(ips)
    if not total:
        return []

    # ① 端口存活
    alive = []
    lk = threading.Lock()
    done = [0]

    def w(ip):
        if cancel and cancel():
            return
        ok = port_open(ip, port)
        with lk:
            done[0] += 1
            if ok:
                alive.append(ip)
        progress(done[0], total)

    pool = [threading.Thread(target=w, args=(ip,), daemon=True) for ip in ips]
    for i in range(0, len(pool), 120):
        if cancel and cancel():
            return []
        for t in pool[i:i + 120]:
            t.start()
        for t in pool[i:i + 120]:
            t.join(1.5)
    alive.sort(key=lambda x: [int(p) for p in x.split(".")])
    log("[扫描] 共 %d 个地址，%d 台开放 %d 端口：%s" %
        (total, len(alive), port, ",".join(alive) or "无"))

    # ② 用 ffmpeg 逐主机验证
    results = []
    paths = list(DEFAULT_PATHS)[:max_paths] + \
        [p for p in ffmpeg.PATH_CANDIDATES if p not in DEFAULT_PATHS][:4]

    def build_urls(ip):
        urls = []
        for path in paths:
            for u in users:
                for pw in pwds:
                    if u:
                        urls.append(("rtsp://%s:%s@%s:%d/%s" %
                                     (quote(u, safe=""), quote(pw, safe=""), ip, int(port), path), u, pw, path))
                    else:
                        urls.append(("rtsp://%s:%d/%s" %
                                     (ip, int(port), path), "", "", path))
        return urls

    def probe_host(ip):
        if cancel and cancel():
            return
        # 快速门禁：先探一下是否有 RTSP 服务（避免对非 RTSP 的开放端口空跑 ffmpeg）
        if rtsp_ok(ip, port, "av0_1", "", "", timeout=2.0) == 0:
            return
        for (url, u, pw, path) in build_urls(ip):
            if cancel and cancel():
                return
            r = ffmpeg.probe(url, timeout=timeout, transport="tcp", cancel=cancel)
            if r and r[0] >= 160:
                with lk:
                    results.append({"ip": ip, "port": int(port), "url": url,
                                    "user": u, "pwd": pw, "path": path,
                                    "w": r[0], "h": r[1], "codec": r[2]})
                log("[扫描] 发现 %s  %s  (%dx%d %s)" %
                    (ip, redact_urls(url), r[0], r[1], r[2]))
                return
        log("[扫描] %s 已开放 %d 端口，但所有路径/账号均未能拉流" % (ip, port))

    ths = [threading.Thread(target=probe_host, args=(ip,), daemon=True)
           for ip in alive]
    for i in range(0, len(ths), 4):
        if cancel and cancel():
            break
        for t in ths[i:i + 4]:
            t.start()
        for t in ths[i:i + 4]:
            t.join()
    progress(total, total)
    return results
