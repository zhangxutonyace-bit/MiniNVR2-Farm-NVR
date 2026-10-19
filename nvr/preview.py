# -*- coding: utf-8 -*-
"""Isolated camera viewers with short-lived, single-camera capabilities."""
import hashlib
import ipaddress
import math
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from .local_camera import source_identity, worker_identity
from collections import OrderedDict

from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.serving import ThreadedWSGIServer, WSGIRequestHandler

from . import discovery


TOKEN_SECONDS = 10 * 60
MAX_TICKETS = 128
FRAME_MAX_AGE = 10
_LAN_NETWORKS = tuple(ipaddress.IPv4Network(n) for n in
                      ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def is_private_ipv4(address):
    """Only RFC1918, rather than the broader ipaddress.is_private category."""
    try:
        ip = ipaddress.IPv4Address(address)
        return any(ip in network for network in _LAN_NETWORKS)
    except (ValueError, TypeError):
        return False


def validate_options(options):
    if not isinstance(options, dict):
        raise ValueError("局域网预览设置必须为对象")
    enabled = options.get("enabled", False)
    address = options.get("address", "")
    port = options.get("port", 8803)
    if not isinstance(enabled, bool):
        raise ValueError("局域网预览启用状态必须为是或否")
    if not isinstance(address, str):
        raise ValueError("请填写本机的局域网 IPv4 地址，或留空自动选择")
    address = address.strip()
    if address and not is_private_ipv4(address):
        raise ValueError("预览地址只能填写本机的私网 IPv4，例如 192.168.1.10")
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("预览端口必须是 1024 到 65535 之间的整数")
    return {"enabled": enabled, "address": address, "port": port}


def _bindable_address(address):
    if not is_private_ipv4(address):
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((address, 0))
        return True
    except OSError:
        return False


def validate_public_origin(value):
    """Accept only a clean HTTPS origin; this is never a server fetch target."""
    message = "远程预览地址无效，请停止后重新开启远程预览。"
    if (not isinstance(value, str) or not value or len(value) > 2048
            or not value.isascii() or any(char.isspace() for char in value)
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError(message)
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
        if (parsed.scheme != "https" or parsed.username is not None
                or parsed.password is not None or not parsed.netloc
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or "?" in value or "#" in value or "\\" in value
                or parsed.netloc.endswith(":")
                or not host or len(host) > 253 or host.endswith(".")
                or "." not in host or host == "localhost"
                or host.endswith((".localhost", ".local"))
                or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                       for label in host.split("."))
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", host.rsplit(".", 1)[-1])
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError(message)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # Numeric browser shorthand (for example 2130706433 or 127.1)
            # must not be treated as a public DNS name either.
            if all(label.isdecimal() for label in host.split(".")):
                raise ValueError(message)
        else:
            raise ValueError(message)
    except (TypeError, ValueError):
        raise ValueError(message) from None
    host = host.lower()
    return "https://" + host + (":" + str(port) if port not in (None, 443) else "")


def _web_dir():
    base = getattr(sys, "_MEIPASS", "")
    if base and os.path.isdir(os.path.join(base, "web")):
        return os.path.join(base, "web")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


class _ViewerServer(ThreadedWSGIServer):
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class _QuietHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-"):
        # Capabilities are never in URLs; avoid unnecessary viewer access logs too.
        pass


class PreviewService:
    _disabled_message = "请先在电脑设置中启用局域网预览。"
    _unavailable_message = "局域网预览尚未启动，请检查电脑设置。"

    def __init__(self, cfg, camera_getter, log=None):
        self.cfg = cfg
        self.camera_getter = camera_getter
        self.log = log or (lambda message: None)
        self._lock = threading.RLock()
        self._lifecycle = threading.RLock()
        self._tickets = OrderedDict()
        self._server = None
        self._thread = None
        self._running = False
        self._address = ""
        self._port = 8803
        self._error = ""
        self._configuration = None
        self._identity = None
        self.app = self._create_app()

    def _options(self):
        return validate_options(self.cfg.get("preview"))

    @staticmethod
    def _options_key(options):
        return options["enabled"], options["address"], options["port"]

    def _current_identity(self):
        allowed = self.cfg.get("clawbot", "allowed_users", default=[])
        allowed = tuple(sorted(str(item) for item in allowed)) if isinstance(allowed, list) else ()
        credential = str(self.cfg.get("clawbot", "token", default="") or "")
        return (bool(self.cfg.get("clawbot", "enabled", default=False)),
                str(self.cfg.get("clawbot", "user_id", default="") or ""), allowed,
                hashlib.sha256(credential.encode("utf-8")).hexdigest())

    @staticmethod
    def _camera_key(camera):
        return camera.get("id"), bool(camera.get("enabled", True)), source_identity(camera)

    def _start_listener(self, port):
        server = _ViewerServer("0.0.0.0", port, self.app, handler=_QuietHandler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.2),
                                  name="nvr-lan-preview", daemon=True)
        try:
            thread.start()
        except Exception:
            server.server_close()
            raise
        return server, thread

    @staticmethod
    def _close_listener(server, thread):
        if server is None:
            return

        def close():
            try:
                server.shutdown()
            finally:
                server.server_close()
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=3)

        if thread is threading.current_thread():
            threading.Thread(target=close, daemon=True).start()
        else:
            close()

    def revoke(self):
        with self._lock:
            self._tickets.clear()

    def stop(self):
        with self._lifecycle:
            with self._lock:
                server, thread = self._server, self._thread
                self._server = self._thread = None
                self._running = False
                self._tickets.clear()
            self._close_listener(server, thread)

    def sync(self):
        """Apply settings without exposing any management routes on this listener."""
        with self._lifecycle:
            try:
                options = self._options()
            except ValueError as exc:
                self.stop()
                with self._lock:
                    self._error = str(exc)
                return
            key = self._options_key(options)
            identity = self._current_identity()
            address = ""
            valid_address = False
            if options["enabled"]:
                address = options["address"] or str(discovery.lan_ip() or "").strip()
                valid_address = is_private_ipv4(address) and _bindable_address(address)
            with self._lock:
                if identity != self._identity:
                    self._tickets.clear()
                    self._identity = identity
                healthy = (self._running and self._thread is not None
                           and self._thread.is_alive())
                if (key == self._configuration and healthy and valid_address
                        and address == self._address):
                    return
            self.stop()
            with self._lock:
                self._configuration = key
                self._port = options["port"]
                self._address = ""
                self._error = ""
            if not options["enabled"]:
                return
            if not valid_address:
                with self._lock:
                    self._error = "没有可用的本机私网 IPv4，请在设置中填写电脑的 Wi-Fi IPv4 地址。"
                return
            try:
                server, thread = self._start_listener(options["port"])
            except (OSError, SystemExit):
                with self._lock:
                    self._error = "预览端口无法启动，请检查端口是否已被其他程序占用。"
                self.log("[预览] 独立局域网预览端口启动失败：%d" % options["port"])
                return
            with self._lock:
                self._server, self._thread = server, thread
                self._address = address
                self._running = True
            self.log("[预览] 局域网只读预览已启动：%s:%d" % (address, options["port"]))

    def status(self):
        with self._lock:
            running = bool(self._running and self._thread is not None and self._thread.is_alive())
            return {"enabled": bool(self.cfg.get("preview", "enabled", default=False)),
                    "running": running, "address": self._address,
                    "port": self._port, "error": self._error}

    def _expire(self, now):
        for key in list(self._tickets):
            if self._tickets[key]["expires"] <= now:
                del self._tickets[key]

    def _link_origin(self):
        return "http://%s:%d" % (self._address, self._port)

    def make_link(self, cid):
        self.sync()
        options = self._options()
        if not options["enabled"]:
            raise ValueError(self._disabled_message)
        if not self._current_identity()[0]:
            raise ValueError("微信功能已停用，请先在电脑设置中启用。")
        if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cid):
            raise ValueError("摄像头序号无效，请重新发送“列表”选择。")
        camera = self.cfg.get_camera(cid)
        if not camera or not camera.get("enabled", True):
            raise ValueError("摄像头不存在或已停用，请重新发送“列表”选择。")
        with self._lock:
            if not self._running or self._thread is None or not self._thread.is_alive():
                raise ValueError(self._error or self._unavailable_message)
            origin = self._link_origin()
            now = time.monotonic()
            self._expire(now)
            while len(self._tickets) >= MAX_TICKETS:
                self._tickets.popitem(last=False)
            token = secrets.token_urlsafe(32)
            self._tickets[hashlib.sha256(token.encode("ascii")).hexdigest()] = {
                "cid": cid, "camera": self._camera_key(camera), "expires": now + TOKEN_SECONDS}
            return "%s/preview#t=%s" % (origin, token)

    @staticmethod
    def _error_response(message, status, code):
        return jsonify({"ok": False, "msg": message, "code": code}), status

    def _authorize(self, cid=None):
        value = request.headers.get("Authorization", "")
        if not re.fullmatch(r"Bearer [A-Za-z0-9_-]{43}", value):
            return None, self._error_response("链接无效，请回微信重新发送预览。", 401, "invalid")
        key = hashlib.sha256(value[7:].encode("ascii")).hexdigest()
        now = time.monotonic()
        with self._lock:
            try:
                options = self._options()
            except ValueError:
                options = None
            identity = self._current_identity()
            if (not options or not options["enabled"] or not identity[0]
                    or self._options_key(options) != self._configuration
                    or identity != self._identity or not self._running):
                self._tickets.clear()
                return None, self._error_response("预览已停用或设置已变化，请回微信重新发送预览。", 410, "revoked")
            ticket = self._tickets.get(key)
            if ticket and ticket["expires"] <= now:
                del self._tickets[key]
                return None, self._error_response("链接已过期，请回微信重新发送预览。", 410, "expired")
            self._expire(now)
            if not ticket:
                return None, self._error_response("链接无效，请回微信重新发送预览。", 401, "invalid")
            if cid is not None and cid != ticket["cid"]:
                return None, self._error_response("这个链接不能查看其他摄像头。", 403, "wrong_camera")
            camera = self.cfg.get_camera(ticket["cid"])
            if not camera or self._camera_key(camera) != ticket["camera"] or not camera.get("enabled", True):
                del self._tickets[key]
                return None, self._error_response("摄像头已停用或变化，请回微信重新发送预览。", 410, "camera_changed")
            return dict(ticket, spec=dict(camera)), None

    @staticmethod
    def _camera_matches(camera, ticket):
        # Configuration is saved before Hub replaces its asynchronous worker.
        # A new capability must not expose the previous worker's source.
        return (camera is not None and getattr(camera, "id", None) == ticket["cid"]
                and worker_identity(camera) == ticket["camera"][2]
                and getattr(camera, "enabled", False))

    @classmethod
    def _read_camera(cls, camera, ticket):
        if not cls._camera_matches(camera, ticket):
            return b"", "connecting", 0
        lock = getattr(camera, "lock", None)
        if lock is not None and hasattr(camera, "last_jpeg"):
            with lock:
                if not cls._camera_matches(camera, ticket):
                    return b"", "connecting", 0
                return camera.last_jpeg, camera.state, camera.last_ts
        # Test adapters and other camera implementations may expose only snapshot().
        buf = camera.snapshot()[0]
        if lock is not None:
            with lock:
                if not cls._camera_matches(camera, ticket):
                    return b"", "connecting", 0
                return buf, camera.state, camera.last_ts
        if not cls._camera_matches(camera, ticket):
            return b"", "connecting", 0
        return buf, camera.state, camera.last_ts

    def _source_error(self):
        address = request.remote_addr or ""
        try:
            loopback = ipaddress.IPv4Address(address).is_loopback
        except (ValueError, TypeError):
            loopback = False
        if not (loopback or is_private_ipv4(address)):
            return self._error_response("此预览仅供同一局域网访问。", 403, "lan_only")
        return None

    def _create_app(self):
        app = Flask("mininvr_lan_preview", static_folder=None)
        app.config["MAX_CONTENT_LENGTH"] = 1024

        @app.before_request
        def source_only():
            return self._source_error()

        @app.after_request
        def headers(response):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
                "connect-src 'self'; img-src blob:; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'")
            return response

        @app.route("/preview")
        def page():
            return send_from_directory(_web_dir(), "preview.html")

        @app.route("/preview.js")
        def script():
            return send_from_directory(_web_dir(), "preview.js")

        @app.route("/info")
        def info():
            ticket, error = self._authorize()
            if error is not None:
                return error
            try:
                camera = self.camera_getter(ticket["cid"])
                state = (str(getattr(camera, "state", "connecting"))
                         if self._camera_matches(camera, ticket) else "connecting")
            except Exception:
                state = "offline"
            return jsonify({"ok": True, "camera_id": ticket["cid"],
                            "name": str(ticket["spec"].get("name") or ticket["cid"]),
                            "state": state,
                            "expires_in": max(0, math.ceil(ticket["expires"] - time.monotonic()))})

        @app.route("/frame/<cid>")
        def frame(cid):
            ticket, error = self._authorize(cid)
            if error is not None:
                return error
            try:
                buf, state, stamp = self._read_camera(self.camera_getter(cid), ticket)
                age = time.time() - float(stamp)
                fresh = math.isfinite(age) and 0 <= age <= FRAME_MAX_AGE
            except Exception:
                buf, state, fresh = b"", "offline", False
            if state != "online" or not buf or not fresh:
                message = ("摄像头正在连接，请稍后重试。" if state == "connecting"
                           else "摄像头离线或画面已中断，请检查电脑上的摄像头状态。")
                return self._error_response(message, 503, "unavailable")
            return Response(bytes(buf), mimetype="image/jpeg")

        return app


class RemotePreviewService(PreviewService):
    """A session-only loopback origin for an explicitly owned HTTPS relay.

    This instance does not discover network addresses, start a relay, or open
    itself from make_link()/sync(). The controller owns the external process.
    """
    _disabled_message = "请先在电脑设置中启用远程预览。"
    _unavailable_message = "远程预览尚未开启，请在电脑设置中手动开启。"

    def __init__(self, cfg, camera_getter, log=None):
        self._public_url = ""
        super().__init__(cfg, camera_getter, log=log)
        self._port = 0

    def _options(self):
        enabled = self.cfg.get("remote_preview", "enabled", default=False)
        if not isinstance(enabled, bool):
            raise ValueError("远程预览启用状态必须为是或否")
        return {"enabled": enabled, "address": self._public_url, "port": self._port}

    def _source_error(self):
        if request.remote_addr != "127.0.0.1":
            return self._error_response("远程预览入口仅接受本机转发。", 403, "origin_only")
        return None

    def _start_listener(self, port):
        # The relay is allowed to forward only this dedicated, loopback origin.
        server = _ViewerServer("127.0.0.1", 0, self.app, handler=_QuietHandler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.2),
                                  name="nvr-remote-preview-origin", daemon=True)
        try:
            thread.start()
        except Exception:
            server.server_close()
            raise
        return server, thread

    def start_origin(self):
        with self._lifecycle:
            self.sync()
            options = self._options()
            if not options["enabled"]:
                raise ValueError(self._disabled_message)
            with self._lock:
                if self._running and self._thread is not None and self._thread.is_alive():
                    return self._port
            self.stop()
            try:
                server, thread = self._start_listener(0)
            except (OSError, SystemExit):
                with self._lock:
                    self._error = "远程预览的本机入口无法启动，请稍后重试。"
                self.log("[预览] 远程预览本机入口启动失败。")
                raise ValueError(self._error) from None
            try:
                options = self._options()
                if not options["enabled"]:
                    raise ValueError(self._disabled_message)
            except ValueError:
                self._close_listener(server, thread)
                raise
            with self._lock:
                self._server, self._thread = server, thread
                self._port = server.server_port
                self._address = "127.0.0.1"
                self._running = True
                self._error = ""
                self._identity = self._current_identity()
                self._configuration = self._options_key(self._options())
            return self._port

    def sync(self):
        # Only explicit start_origin() may create a listener.
        with self._lifecycle:
            try:
                options = self._options()
            except ValueError as exc:
                self.stop()
                with self._lock:
                    self._error = str(exc)
                return
            if not options["enabled"]:
                self.stop()
                return
            with self._lock:
                identity = self._current_identity()
                if identity != self._identity:
                    self._tickets.clear()
                    self._identity = identity
                failed = self._running and (self._thread is None or not self._thread.is_alive())
            if failed:
                self.stop()
                with self._lock:
                    self._error = "远程预览的本机入口已停止，请重新开启。"

    def set_public_url(self, value):
        origin = validate_public_origin(value)
        with self._lifecycle:
            self.sync()
            with self._lock:
                if not self._running or self._thread is None or not self._thread.is_alive():
                    raise ValueError(self._error or self._unavailable_message)
                if origin != self._public_url:
                    self._tickets.clear()
                    self._public_url = origin
                    self._configuration = self._options_key(self._options())

    def _link_origin(self):
        if not self._public_url:
            raise ValueError("远程连接尚未就绪，请等待电脑设置中显示已连接。")
        return self._public_url

    def stop(self):
        with self._lifecycle:
            super().stop()
            with self._lock:
                self._public_url = ""
                self._address = ""
                self._port = 0
                self._configuration = None

    def status(self):
        self.sync()
        with self._lock:
            running = bool(self._running and self._thread is not None and self._thread.is_alive())
            return {"enabled": self.cfg.get("remote_preview", "enabled", default=False) is True,
                    "running": running, "address": self._address, "port": self._port,
                    "base_url": self._public_url if running else "",
                    "ready": bool(running and self._public_url), "error": self._error}
