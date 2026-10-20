# -*- coding: utf-8 -*-
"""Web 服务。

预览读取每路共享的后台画框缓存；原图选项直接转发 ffmpeg JPEG。
HTTP 不执行识别、跟踪或编码，后台处理过慢时继续发送当前原画面。
"""
import ctypes
import copy
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import sys
import time
import urllib.parse
from contextlib import nullcontext

import cv2
import numpy as np
from flask import Flask, Response, g, jsonify, request, send_from_directory, session

from . import __version__, discovery
from .config import Config, records_root
from . import local_camera


def web_dir():
    base = getattr(sys, "_MEIPASS", "")
    if base:
        p = os.path.join(base, "web")
        if os.path.isdir(p):
            return p
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


WEB = web_dir()

# ---------------- 占位图（离线/连接中也要有画面，杜绝黑屏） ----------------
_PW, _PH = 320, 180
_PLACE = {}


def _make_placeholder(text, sub=""):
    img = np.zeros((_PH, _PW, 3), np.uint8)
    img[:] = (30, 28, 26)
    cv2.putText(img, text, (14, 92), cv2.FONT_HERSHEY_SIMPLEX, 0.85,
                (225, 225, 230), 2, cv2.LINE_AA)
    if sub:
        cv2.putText(img, sub[:38], (14, 124), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, (150, 150, 158), 1, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return buf.tobytes() if ok else b""


def placeholder(kind, sub=""):
    key = kind + "|" + sub
    if key not in _PLACE:
        _PLACE[key] = _make_placeholder(kind, sub)
    return _PLACE[key]


def _frame(buf):
    return (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(buf)).encode() + b"\r\n\r\n" + buf + b"\r\n")


# ---------------- 内存 ----------------

def mem_mb():
    try:
        if os.name == "nt":
            class PMC(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_ulong),
                            ("PageFaultCount", ctypes.c_ulong),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]
            # 不声明 argtypes/restype 时，64 位伪句柄会被截断成 0 → 内存永远读 0.0
            k32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_ulong]
            psapi.GetProcessMemoryInfo.restype = ctypes.c_int
            c = PMC()
            c.cb = ctypes.sizeof(PMC)
            if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(),
                                          ctypes.byref(c), c.cb):
                return round(c.WorkingSetSize / 1048576.0, 1)
            return None
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    except Exception:
        return None


def create_app(cfg, hub, on_quit=None):
    app = Flask(__name__, static_folder=None)
    app.config["JSON_AS_ASCII"] = False
    app.secret_key = secrets.token_bytes(32)
    app.config.update(SESSION_COOKIE_NAME="mininvr_session",
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                      MAX_CONTENT_LENGTH=1024 * 1024)

    def token_value():
        return str(cfg.get("server", "token") or "")

    def token_fingerprint(token):
        return hmac.new(app.secret_key, token.encode("utf-8"), hashlib.sha256).hexdigest()

    def local_request():
        try:
            return (ipaddress.ip_address(request.remote_addr or "").is_loopback
                    and urllib.parse.urlsplit(request.host_url).hostname
                    in ("localhost", "127.0.0.1", "::1"))
        except ValueError:
            return False

    @app.before_request
    def protect_private_routes():
        if not request.path.startswith(("/api/", "/live/", "/snap/")):
            return None
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            if ((origin and origin.rstrip("/") != request.host_url.rstrip("/"))
                    or request.headers.get("Sec-Fetch-Site") == "cross-site"):
                return jsonify({"ok": False, "msg": "不允许跨来源操作"}), 403
            if not request.is_json:
                return jsonify({"ok": False, "msg": "请使用 JSON 请求"}), 415
            if not isinstance(request.get_json(silent=True), dict):
                return jsonify({"ok": False, "msg": "请求必须是 JSON 对象"}), 400
        token = token_value()
        # Bind long-lived responses to the credential generation actually
        # checked here, including password changes before the route handler.
        g.authorized_token_generation = token_fingerprint(token)
        if not token:
            if not local_request():
                return jsonify({"ok": False, "msg": "请先在本机设置访问口令，再启用局域网访问"}), 403
            return None
        if request.path == "/api/auth" and request.method == "POST":
            return None
        supplied = request.headers.get("X-Token", "")
        if supplied and hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
            return None
        fingerprint = session.get("auth", "")
        if isinstance(fingerprint, str) and hmac.compare_digest(fingerprint, token_fingerprint(token)):
            return None
        return jsonify({"ok": False, "msg": "请输入访问口令", "auth_required": True}), 401

    @app.after_request
    def private_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.path.startswith(("/api/", "/live/", "/snap/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    def need_auth(fn):
        # Authentication is applied centrally to reads, writes and media alike.
        return fn

    @app.route("/api/auth", methods=["POST", "DELETE"])
    def api_auth():
        if request.method == "DELETE":
            session.clear()
            return jsonify({"ok": True})
        d = request.get_json(silent=True) or {}
        supplied = request.headers.get("X-Token") or d.get("token") or ""
        token = token_value()
        if not isinstance(supplied, str) or (token and not hmac.compare_digest(
                supplied.encode("utf-8"), token.encode("utf-8"))):
            return jsonify({"ok": False, "msg": "口令错误", "auth_required": True}), 401
        session.clear()
        if token:
            session["auth"] = token_fingerprint(token)
        return jsonify({"ok": True})

    def redact_url(value):
        return re.sub(r"(?i)(rtsps?://)[^/@\s]+@", r"\1***@", str(value or ""))

    def public_camera(camera):
        item = dict(camera)
        item.pop("device", None)
        for field in ("url", "stream", "name", "error"):
            if field in item:
                item[field] = redact_url(item[field])
        return item

    def public_scan():
        state = copy.deepcopy(hub.scan_state)
        state["log"] = [redact_url(line) for line in state.get("log", [])]
        state["results"] = [dict(public_camera({k: v for k, v in row.items()
                                                if k not in ("pwd", "user")}), scan_index=i)
                            for i, row in enumerate(state.get("results", []))]
        return state

    def camera_spec(data):
        if not isinstance(data, dict):
            raise ValueError("摄像头参数必须为对象")
        spec = dict(data)
        if "scan_index" in spec:
            index = spec["scan_index"]
            results = hub.scan_state.get("results") or []
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(results):
                raise ValueError("扫描结果已失效，请重新扫描")
            spec["url"] = results[index].get("url") or ""
        kind = spec.get("source_type", "rtsp")
        if kind not in ("rtsp", "local"):
            raise ValueError("不支持的摄像头来源类型")
        if kind == "local":
            if any(key in spec and not isinstance(spec[key], bool) for key in ("enabled", "detect")):
                raise ValueError("电脑摄像头启用和检测开关必须为是或否")
            if "scan_index" in spec:
                raise ValueError("扫描结果不能作为电脑摄像头")
            device_id = spec.get("device_id")
            if not isinstance(device_id, str) or not re.fullmatch(r"[a-f0-9]{64}", device_id):
                raise ValueError("请刷新并选择电脑摄像头")
            devices = local_camera.enumerate_devices()
            selected = next((d for d in devices if d["device_id"] == device_id), None)
            if selected is None:
                raise ValueError("电脑摄像头已断开或设备列表已变化，请刷新后重新选择")
            spec.update(device=selected["device"], device_name=selected["name"], url="")
            if not spec.get("name"):
                spec["name"] = selected["name"]
        elif not isinstance(spec.get("url"), str) or not spec["url"].strip():
            raise ValueError("请填 RTSP 地址")
        if "name" in spec and (not isinstance(spec["name"], str) or len(spec["name"]) > 160):
            raise ValueError("摄像头名称应为不超过 160 字的文字")
        cid = spec.get("id")
        if cid is not None and (not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cid)):
            raise ValueError("摄像头 ID 不合法")
        return spec

    # ---------------- 页面 ----------------
    @app.route("/")
    def index():
        return send_from_directory(WEB, "index.html")

    @app.route("/app.js")
    def appjs():
        return send_from_directory(WEB, "app.js")

    def preview_view(cid, camera, boxes):
        """A lightweight read boundary; no image/model work or held Hub lock.

        Config is saved before its asynchronous camera replacement. Bind the
        JPEG read to both the active worker and its capture/model generation,
        then compare the same view again before sending any of its bytes.
        """
        if camera is None or hub.get(cid) is not camera:
            return None
        with cfg.lock:
            spec = next((c for c in cfg.cameras if c.get("id") == cid), None)
            if spec is None or not spec.get("enabled", True):
                return None
            source = local_camera.source_identity(spec)
        with (getattr(camera, "lock", None) or nullcontext()):
            stopped = getattr(camera, "_stop_event", None)
            if (not getattr(camera, "enabled", True)
                    or (stopped is not None and stopped.is_set())
                    or local_camera.worker_identity(camera) != source):
                return None
            epoch = getattr(camera, "_capture_epoch", None)
        context = getattr(camera, "_preview_context", None)
        revision = context() if boxes and callable(context) else None
        return source, epoch, revision

    # ---------------- 预览：共享缓存，HTTP 不做图像处理 ----------------
    @app.route("/live/<cid>")
    def live(cid):
        boxes = request.args.get("boxes", "1") != "0"
        authorized_token = g.authorized_token_generation

        def gen():
            last_seq = -1
            last_buf = b""      # 保留最近一帧：在线但瞬时无新帧时冻结，绝不弹占位图
            last_ph = 0.0
            last_emit = 0.0
            worker = None
            worker_view = None
            while True:
                # The response generator outlives the request context. Keep a
                # fingerprint, not credentials/session, and revoke open streams
                # as soon as the configured access token changes.
                if not hmac.compare_digest(authorized_token, token_fingerprint(token_value())):
                    return
                cam = hub.get(cid)
                view = preview_view(cid, cam, boxes)
                if view is None:
                    cam = None
                if cam is not worker or view != worker_view:
                    worker, worker_view = cam, view
                    last_seq, last_buf, last_emit = -1, b"", 0.0
                if cam is None:
                    yield _frame(placeholder("未启用"))
                    time.sleep(1.0)
                    continue
                buf, seq = (cam.preview_snapshot(boxes=boxes)
                            if hasattr(cam, "preview_snapshot") else cam.snapshot())
                current_view = preview_view(cid, cam, boxes)
                if not hmac.compare_digest(authorized_token, token_fingerprint(token_value())):
                    return
                if current_view != view:
                    # Discard both this read and the previous heartbeat cache.
                    # A replacement capture may restart its sequence at zero.
                    worker, worker_view = None, None
                    last_seq, last_buf, last_emit = -1, b"", 0.0
                    time.sleep(.02)
                    continue
                if buf and cam.state == "online":
                    if last_seq != -1 and seq < last_seq:
                        time.sleep(.02)
                        continue
                    changed = seq != last_seq or buf != last_buf
                    now = time.monotonic()
                    # Poll the shared cache lightly without resending identical
                    # JPEGs at capture cadence. This avoids phase-aliasing a
                    # briefly ready frame; keepalive still carries real pixels.
                    if not changed and now - last_emit < .5:
                        time.sleep(.02)
                        continue
                    if changed:
                        last_seq = seq
                        last_buf = buf
                    last_emit = now
                    yield _frame(last_buf)
                    time.sleep(.02)
                    continue
                # 仅非在线态才下发状态提示图（连接中 / 离线 / 未启用）——这是真状态，不是多余画面
                now = time.time()
                if now - last_ph < 1.0:
                    time.sleep(0.2)
                    continue
                last_ph = now
                st = cam.state
                if st == "connecting":
                    yield _frame(placeholder("连接中…"))
                elif st == "offline":
                    yield _frame(placeholder("离线", cam.last_error))
                else:
                    yield _frame(placeholder("未启用"))
                time.sleep(0.1)

        return Response(gen(),
                        mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.route("/snap/<cid>")
    def snap(cid):
        cam = hub.get(cid)
        if request.args.get("boxes") == "1":
            authorized_token = g.authorized_token_generation
            if not hmac.compare_digest(authorized_token, token_fingerprint(token_value())):
                return jsonify({"ok": False, "msg": "请输入访问口令", "auth_required": True}), 401
            view = preview_view(cid, cam, True)
            if view is None:
                return jsonify({"ok": False, "msg": "摄像头已停用或画面来源已变化"}), 503
            buf = (cam.preview_snapshot(boxes=True)[0]
                   if hasattr(cam, "preview_snapshot") else cam.snapshot()[0])
            current_view = preview_view(cid, cam, True)
            if not hmac.compare_digest(authorized_token, token_fingerprint(token_value())):
                return jsonify({"ok": False, "msg": "请输入访问口令", "auth_required": True}), 401
            if current_view != view:
                return jsonify({"ok": False, "msg": "摄像头或算法已变化，请重试"}), 503
        else:
            buf = cam.snapshot()[0] if cam else b""
        if not buf:
            buf = placeholder("无画面")
        return Response(buf, mimetype="image/jpeg")

    # ---------------- 状态 ----------------
    @app.route("/api/status")
    def api_status():
        active = {c.get("id"): c for c in hub.info()}
        cams = [public_camera(dict(c, **{k: v for k, v in active.get(c.get("id"), {}).items()
                                        if k not in c or k not in ("enabled", "detect")}))
                for c in (cfg.cameras or [])]
        return jsonify({"ok": True, "cams": cams,
                        "detection_enabled": bool(cfg.get("detect", "enabled", default=True)),
                        "mem_mb": mem_mb(), "version": __version__,
                        "scan": public_scan()})

    @app.route("/api/net")
    def api_net():
        sug = discovery.lan_suggest()
        sug["subnets"] = discovery.lan_subnets()
        return jsonify(sug)

    # ---------------- 摄像头 ----------------
    @app.route("/api/local-cameras")
    def api_local_cameras():
        try:
            devices = local_camera.enumerate_devices()
        except ValueError as exc:
            return jsonify({"ok": False, "devices": [], "msg": str(exc)}), 400
        used = {c.get("device_id") for c in cfg.cameras if c.get("source_type") == "local"}
        return jsonify({"ok": True, "devices": [
            {"device_id": d["device_id"], "name": d["name"], "added": d["device_id"] in used}
            for d in devices], "msg": "" if devices else "未发现电脑摄像头，请检查连接与驱动后刷新。"})

    @app.route("/api/cameras", methods=["POST"])
    @need_auth
    def api_cam_save():
        d = request.get_json(silent=True) or {}
        try:
            item = cfg.upsert_camera(camera_spec(d))
        except ValueError as e:
            return jsonify({"ok": False, "msg": str(e)}), 400
        hub.request_sync()
        return jsonify({"ok": True, "id": item["id"]})

    @app.route("/api/cameras/batch", methods=["POST"])
    @need_auth
    def api_cam_batch():
        d = request.get_json(silent=True) or {}
        items = d.get("items") or []
        if not isinstance(items, list):
            return jsonify({"ok": False, "msg": "items 必须为列表"}), 400
        try:
            items = [camera_spec(it) for it in items]
        except ValueError as e:
            return jsonify({"ok": False, "msg": str(e)}), 400
        if any(it.get("source_type") == "local" for it in items):
            return jsonify({"ok": False, "msg": "请逐台选择并添加电脑摄像头"}), 400
        exist = {(c.get("url") or ""): c for c in cfg.cameras}
        added = 0
        for it in items:
            url = (it.get("url") or "").strip()
            if not url:
                continue
            old = exist.get(url)
            cfg.upsert_camera({
                "id": (old or {}).get("id"),
                "name": it.get("name") or (old or {}).get("name") or it.get("ip") or url,
                "url": url, "enabled": True, "detect": True,
            })
            added += 1
        hub.request_sync()
        return jsonify({"ok": True, "added": added})

    @app.route("/api/cameras/<cid>", methods=["DELETE"])
    @need_auth
    def api_cam_del(cid):
        cfg.remove_camera(cid)
        hub.request_sync()
        return jsonify({"ok": True})

    @app.route("/api/cameras/<cid>/toggle", methods=["POST"])
    @need_auth
    def api_cam_toggle(cid):
        with cfg.lock:
            c = cfg.get_camera(cid)
            if not c:
                return jsonify({"ok": False, "msg": "不存在"}), 404
            c["enabled"] = not bool(c.get("enabled", True))
            cfg.save()
            enabled = c["enabled"]
        hub.request_sync()
        return jsonify({"ok": True, "enabled": enabled})

    @app.route("/api/cameras/<cid>/detect", methods=["POST"])
    @need_auth
    def api_cam_detect(cid):
        d = request.get_json(silent=True) or {}
        with cfg.lock:
            c = cfg.get_camera(cid)
            if not c:
                return jsonify({"ok": False, "msg": "不存在"}), 404
            c["detect"] = bool(d.get("on", True))
            cfg.save()
            detect = c["detect"]
        hub.request_sync()
        return jsonify({"ok": True, "detect": detect})

    # ---------------- 扫描 ----------------
    @app.route("/api/scan", methods=["POST"])
    @need_auth
    def api_scan():
        d = request.get_json(silent=True) or {}
        ok = hub.start_scan(d.get("range"), d.get("port"), d.get("users"),
                            d.get("pwds"), d.get("timeout"))
        return jsonify({"ok": ok,
                        "msg": "已启动" if ok else (hub.scan_state.get("error")
                                                   or "扫描进行中")})

    @app.route("/api/scan/status")
    def api_scan_status():
        return jsonify(public_scan())

    @app.route("/api/scan/stop", methods=["POST"])
    @need_auth
    def api_scan_stop():
        hub.stop_scan()
        return jsonify({"ok": True})

    # ---------------- 设置 ----------------
    _SEC = ("site", "server", "stream", "detect", "algorithms", "record", "mail", "scan",
            "clawbot", "preview", "remote_preview", "system")
    _SECRETS = {"server": ("token",), "mail": ("pass",), "scan": ("pwds",),
                "clawbot": ("token",)}

    @app.route("/api/settings")
    def api_settings_get():
        data = {k: copy.deepcopy(cfg.get(k)) for k in _SEC}
        for section, fields in _SECRETS.items():
            for field in fields:
                data[section][field + "_configured"] = bool(data[section].get(field))
                data[section][field] = ""
        data["cameras"] = [public_camera(c) for c in (cfg.cameras or [])]
        return jsonify(data)

    @app.route("/api/settings", methods=["POST"])
    @need_auth
    def api_settings_save():
        d = request.get_json(silent=True)
        if not isinstance(d, dict):
            return jsonify({"ok": False, "msg": "设置必须为对象"}), 400
        if "algorithms" in d:
            return jsonify({"ok": False, "msg": "请到算法商城勾选目标并点击应用"}), 400
        if "detect" in d:
            mode = d["detect"].get("mode") if isinstance(d["detect"], dict) else None
            if (not isinstance(d["detect"], dict) or mode not in (None, "auto", "haar", "motion", "market")
                    or (mode == "market" and cfg.get("detect", "mode") != "market")):
                return jsonify({"ok": False, "msg": "请先到算法商城应用模型；旧模式可在这里选择"}), 400
        if "record" in d:
            from .config import validate_target_options
            try:
                if not isinstance(d["record"], dict):
                    raise ValueError("录像设置必须为对象")
                proposed_record = dict(cfg.get("record"))
                proposed_record.update(d["record"])
                validate_target_options(proposed_record)
            except ValueError as exc:
                return jsonify({"ok": False, "msg": str(exc)}), 400
        if "remote_preview" in d:
            remote = d["remote_preview"]
            if (not isinstance(remote, dict) or set(remote) - {"enabled"}
                    or not isinstance(remote.get("enabled"), bool)):
                return jsonify({"ok": False, "msg": "远程预览设置只能包含启用状态（是或否）"}), 400
        if "preview" in d:
            from .preview import validate_options
            try:
                if not isinstance(d["preview"], dict):
                    raise ValueError("局域网预览设置必须为对象")
                proposed = dict(cfg.get("preview"))
                proposed.update(d["preview"])
                d["preview"] = validate_options(proposed)
            except ValueError as exc:
                return jsonify({"ok": False, "msg": str(exc)}), 400
        new_token = (d.get("server") or {}).get("token") if isinstance(d.get("server"), dict) else None
        if new_token is not None and not isinstance(new_token, str):
            return jsonify({"ok": False, "msg": "访问口令必须为文字"}), 400
        with getattr(hub, "algorithm_lock", nullcontext()):
            with cfg.lock:
                if (isinstance(d.get("detect"), dict) and d["detect"].get("mode") == "market"
                        and cfg.get("detect", "mode") != "market"):
                    return jsonify({"ok": False, "msg": "请先到算法商城应用模型"}), 400
                before = cfg.data
                old_mode = cfg.get("detect", "mode")
                cfg.data = copy.deepcopy(before)
                for sec, vals in d.items():
                    if sec in _SEC and isinstance(vals, dict):
                        for k, v in vals.items():
                            if k.endswith("_configured"):
                                continue
                            if k in _SECRETS.get(sec, ()) and not v:
                                continue
                            cfg.set(sec, k, v, save=False)
                try:
                    cfg.save()
                except OSError:
                    cfg.data = before
                    return jsonify({"ok": False, "msg": "设置保存失败，原设置未改变"}), 400
            if (isinstance(d.get("detect"), dict) and "mode" in d["detect"]
                    and hasattr(hub, "_algorithm_apply_generation")):
                hub._algorithm_apply_generation += 1
            if old_mode != cfg.get("detect", "mode") and hasattr(hub, "reset_algorithm_events"):
                hub.reset_algorithm_events()
        if new_token:
            session.clear()
            session["auth"] = token_fingerprint(new_token)
        if not cfg.get("clawbot", "enabled", default=False) and getattr(hub, "clawbot", None) is not None:
            hub.clawbot.stop()
        hub.request_sync()
        if hasattr(hub, "sync_preview"):
            hub.sync_preview()
        return jsonify({"ok": True})

    # Algorithms are management-only routes, protected by the existing origin,
    # JSON, local-only/token guards. Preview listeners never expose these APIs.
    @app.route("/api/algorithms")
    def api_algorithms():
        from .algorithm_catalog import public_capabilities, public_catalog, validate_persisted_options
        status = hub.model_store.status()
        error = ""
        with cfg.lock:
            raw = copy.deepcopy(cfg.data.get("algorithms"))
            mode = cfg.get("detect", "mode")
        try:
            active = validate_persisted_options(raw)
        except ValueError as exc:
            error = str(exc)
            active = {}
        return jsonify(dict(status, ok=True, catalog=public_catalog(),
                            configuration_error=error,
                            capabilities=public_capabilities(),
                            active=dict(active, mode=mode)))

    @app.route("/api/algorithms/download", methods=["POST"])
    @need_auth
    def api_algorithm_download():
        value = request.get_json()
        try:
            if not isinstance(value, dict) or set(value) != {"model_id"}:
                raise ValueError("下载只能指定商城模型 ID")
            hub.model_store.start(value["model_id"])
        except ValueError as exc:
            return jsonify({"ok": False, "msg": str(exc)}), 400
        return jsonify({"ok": True})

    @app.route("/api/algorithms/cancel", methods=["POST"])
    @need_auth
    def api_algorithm_cancel():
        if request.get_json():
            return jsonify({"ok": False, "msg": "取消下载无需附加参数"}), 400
        hub.model_store.cancel()
        return jsonify({"ok": True})

    @app.route("/api/algorithms/apply", methods=["POST"])
    @need_auth
    def api_algorithm_apply():
        try:
            options = hub.apply_algorithms(request.get_json())
        except (ValueError, OSError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else "算法文件不可用，请重新下载"
            return jsonify({"ok": False, "msg": message}), 400
        return jsonify({"ok": True, "active": dict(options, mode="market")})

    @app.route("/api/preview")
    def api_preview_status():
        if hasattr(hub, "preview_status"):
            return jsonify(dict(hub.preview_status(), ok=True))
        return jsonify({"ok": True, "enabled": False, "running": False,
                        "address": "", "port": 8803, "error": ""})

    @app.route("/api/remote-preview")
    def api_remote_preview_status():
        return jsonify(dict(hub.remote_preview_status(), ok=True))

    @app.route("/api/remote-preview/start", methods=["POST"])
    @need_auth
    def api_remote_preview_start():
        if request.get_json().get("confirm_relay") is not True:
            return jsonify({"ok": False, "msg": "请先确认画面会通过 NetBird 代理转发，再开启远程预览。"}), 400
        try:
            return jsonify(dict(hub.start_remote_preview(), ok=True))
        except ValueError as exc:
            return jsonify({"ok": False, "msg": str(exc)}), 400

    @app.route("/api/remote-preview/stop", methods=["POST"])
    @need_auth
    def api_remote_preview_stop():
        return jsonify(dict(hub.stop_remote_preview(), ok=True))

    @app.route("/api/mail/test", methods=["POST"])
    @need_auth
    def api_mail_test():
        from . import mailer
        d = request.get_json(silent=True) or {}
        day = (d.get("day") or "").strip()
        if not day:
            day = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        files = mailer.build_attachments(cfg, day, log=hub.log)
        if not files:
            files = []
            # 没有素材也发一封，验证 SMTP 通不通
        ok, msg = mailer.send_mail(cfg, "[轻量录像机] 测试邮件",
                                   "这是一封测试邮件。", files, log=hub.log)
        return jsonify({"ok": ok, "msg": msg})

    # ---------------- 微信 ClawBot（腾讯 iLink 协议） ----------------
    # 形态：客户在微信里发一句话 → Bot 回抽帧录像。
    # ⚠️ iLink 不允许 Bot 主动推送，只能「一问一答」，定时推送请走邮件。

    @app.route("/api/clawbot")
    def api_clawbot_status():
        try:
            b = hub.get_clawbot()
        except Exception as e:
            return jsonify({"ok": False, "msg": str(e)[:200]})
        c = cfg.get("clawbot")
        return jsonify({
            "ok": True,
            "enabled": bool(c.get("enabled", True)),
            "state": b.state,            # idle/wait/scaned/confirmed/expired/error
            "logged": b.logged_in(),
            "qr_url": b.qr_url,
            "error": b.error,
            "remain_h": round(b.remain_sec() / 3600.0, 1),
            "receiving": bool(getattr(b, "_running", False)),
            "help": b.HELP,
        })

    @app.route("/api/clawbot/login", methods=["POST"])
    @need_auth
    def api_clawbot_login():
        if not cfg.get("clawbot", "enabled", default=False):
            return jsonify({"ok": False, "error": "请先启用微信功能并保存设置"}), 400
        b = hub.get_clawbot(restart=True)
        ok, url = b.begin_login()
        return jsonify({"ok": ok, "qr_url": url, "error": b.error})

    @app.route("/api/clawbot/qr.png")
    def api_clawbot_qr():
        """把二维码画成 PNG，网页直接 <img> 显示。"""
        b = hub.get_clawbot()
        content = b.qr_url or ""
        if not content:
            return Response(b"", 404)
        try:
            import qrcode
            img = qrcode.make(content)
            import io
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return Response(buf.getvalue(), 200,
                            headers={"Content-Type": "image/png",
                                     "Cache-Control": "no-cache"})
        except Exception as e:
            # 没装 qrcode 也不影响：前端会退化成显示二维码链接
            return jsonify({"ok": False, "msg": "二维码生成失败：%s" % e}), 500

    @app.route("/api/clawbot/logout", methods=["POST"])
    @need_auth
    def api_clawbot_logout():
        hub.get_clawbot().logout()
        return jsonify({"ok": True})

    @app.route("/api/clawbot/save", methods=["POST"])
    @need_auth
    def api_clawbot_save():
        d = request.get_json(silent=True) or {}
        for k in ("enabled", "auto_start", "max_video_mb", "bot_type",
                  "base_url", "cdn_url"):
            if k in d:
                cfg.set("clawbot", k, d[k], save=False)
        cfg.save()
        if not cfg.get("clawbot", "enabled", default=False) and getattr(hub, "clawbot", None) is not None:
            hub.clawbot.stop()
        return jsonify({"ok": True})

    @app.route("/api/system/quit", methods=["POST"])
    @need_auth
    def api_quit():
        def later():
            time.sleep(0.4)
            try:
                hub.stop_all()
            except Exception:
                pass
            if on_quit:
                on_quit()
        import threading
        threading.Thread(target=later, daemon=True).start()
        return jsonify({"ok": True})

    # ---------------- 录像回放 ----------------
    # 只列「已生成完」的录像；正在写入中的（事件录像进行中 / 当天的抽帧）一律不显示。

    def _rec_title(rtype, fname, date):
        if rtype == "timelapse":
            return "%s 抽帧录像" % date
        hh, mm, ss = fname[:2], fname[2:4], fname[4:6]
        return "%s %s:%s:%s 事件录像" % (date, hh, mm, ss)

    def _rec_item(cid, cname, date, rtype, fname, path):
        url = "/api/rec/%s/%s/%s/%s" % (
            cid, date, rtype, urllib.parse.quote(fname))
        return {"cam": cid, "cam_name": cname, "date": date, "type": rtype,
                "name": fname, "title": _rec_title(rtype, fname, date),
                "size": os.path.getsize(path), "mtime": os.path.getmtime(path),
                "url": url}

    @app.route("/api/recordings")
    def api_recordings():
        cid = (request.args.get("cam") or "").strip()
        date = (request.args.get("date") or "").strip()
        rtype = (request.args.get("type") or "").strip()
        try:
            in_prog = set(hub.recording_paths()) if hub else set()
        except Exception:
            in_prog = set()
        root = records_root(cfg)
        items = []
        try:
            for c in (cfg.cameras or []):
                ccid = c.get("id")
                if not isinstance(ccid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ccid):
                    continue
                if cid and ccid != cid:
                    continue
                cname = c.get("name") or ccid
                cdir = os.path.join(root, ccid)
                if not os.path.isdir(cdir):
                    continue
                for name in os.listdir(cdir):
                    # 抽帧全天视频：records/<cam>/timelapse_<date>.mp4
                    if name.startswith("timelapse_") and name.endswith(".mp4"):
                        d = name[len("timelapse_"):-4]
                        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                            continue  # Skip temporary .part.mp4 files still being merged.
                        if date and d != date:
                            continue
                        if rtype and rtype != "timelapse":
                            continue
                        p = os.path.join(cdir, name)
                        if not (os.path.getsize(p) > 512):
                            continue
                        if d == time.strftime("%Y-%m-%d"):
                            continue  # 当天还在生成 → 不显示
                        items.append(_rec_item(ccid, cname, d,
                                               "timelapse", name, p))
                        continue
                    # 事件录像：records/<cam>/<date>/event/HHMMSS.mp4
                    if len(name) != 10:
                        continue
                    if date and name != date:
                        continue
                    edir = os.path.join(cdir, name, "event")
                    if not os.path.isdir(edir):
                        continue
                    for fn in os.listdir(edir):
                        if not fn.endswith(".mp4") or fn.endswith((".fs.mp4", ".part.mp4")):
                            continue
                        if rtype and rtype != "event":
                            continue
                        p = os.path.join(edir, fn)
                        if p in in_prog:
                            continue  # 写入中 → 不显示
                        if os.path.getsize(p) < 1024:
                            continue
                        items.append(_rec_item(ccid, cname, name,
                                               "event", fn, p))
        except Exception as e:
            return jsonify({"ok": False, "msg": str(e)[:200]}), 500
        items.sort(key=lambda x: x["mtime"], reverse=True)
        cams = [{"id": c.get("id"), "name": (c.get("name") or c.get("id"))}
                for c in (cfg.cameras or [])]
        dates = sorted({it["date"] for it in items}, reverse=True)
        return jsonify({"ok": True, "items": items, "cams": cams,
                        "dates": dates, "count": len(items)})

    @app.route("/api/rec/<cid>/<date>/<rtype>/<name>")
    def rec_file(cid, date, rtype, name):
        if not re.match(r"^[A-Za-z0-9_-]+$", cid) \
           or not re.match(r"^\d{4}-\d{2}-\d{2}$", date) \
           or rtype not in ("event", "timelapse") \
           or not name.endswith(".mp4") \
           or name.endswith((".part.mp4", ".fs.mp4")) \
           or "/" in name or "\\" in name or ".." in name:
            return jsonify({"ok": False, "msg": "参数非法"}), 400
        root = os.path.abspath(records_root(cfg))
        if rtype == "event":
            p = os.path.abspath(os.path.join(root, cid, date, "event", name))
        else:
            p = os.path.abspath(os.path.join(root, cid,
                                            "timelapse_%s.mp4" % date))
        if not p.startswith(root + os.sep) or not os.path.isfile(p):
            return jsonify({"ok": False, "msg": "文件不存在"}), 404
        return _send_range(p, "video/mp4")

    def _send_range(path, mime):
        """支持 HTTP Range 的流式发送 —— 浏览器拖动进度条 / 切换视频都秒响应。"""
        size = os.path.getsize(path)
        rh = request.headers.get("Range")
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", (rh or "").strip()) if rh else None
        if rh and (not m or not any(m.groups()) or size == 0):
            return Response(b"", 416, headers={"Content-Range": "bytes */%d" % size})
        if m:
            s, e = m.group(1), m.group(2)
            if not s:
                suffix = int(e)
                if suffix <= 0:
                    return Response(b"", 416, headers={"Content-Range": "bytes */%d" % size})
                start, end = max(0, size - suffix), size - 1
            else:
                start = int(s)
                end = int(e) if e else size - 1
            if end >= size:
                end = size - 1
            if start > end:
                return Response(b"", 416,
                                headers={"Content-Range": "bytes */%d" % size})
            length = end - start + 1

            def gen():
                with open(path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(65536, remaining))
                        if not chunk:
                            break
                        yield chunk
                        remaining -= len(chunk)

            headers = {"Content-Range": "bytes %d-%d/%d" % (start, end, size),
                       "Accept-Ranges": "bytes",
                       "Content-Length": str(length),
                       "Content-Type": mime, "Cache-Control": "no-cache"}
            return Response(gen(), 206, headers=headers)

        def gen_full():
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(262144)
                    if not chunk:
                        break
                    yield chunk

        headers = {"Accept-Ranges": "bytes", "Content-Length": str(size),
                   "Content-Type": mime, "Cache-Control": "no-cache"}
        return Response(gen_full(), 200, headers=headers)

    return app
