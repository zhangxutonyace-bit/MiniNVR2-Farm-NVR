"""A bounded camera-only bridge on the explicitly selected NetBird adapter.

NetBird Windows normally targets its adapter IP rather than localhost. Keep the
capability service on loopback and bridge only its four read-only routes.
"""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import os
import re
import socket
import threading

_NETBIRD_RANGE = ipaddress.IPv4Network("100.64.0.0/10")
_PATH = re.compile(r"/(?:preview|preview\.js|info|frame/[A-Za-z0-9_-]{1,64})")
_MAX_RESPONSE = 4 * 1024 * 1024


def is_overlay_address(address):
    try:
        return ipaddress.IPv4Address(address) in _NETBIRD_RANGE
    except (ValueError, TypeError):
        return False


def validate_adapter(address):
    if not is_overlay_address(address):
        raise ValueError("没有可用的 NetBird IPv4，请先在电脑客户端完成连接。")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((address, 0))
    except OSError:
        raise ValueError("NetBird 网卡尚未就绪，请确认已连接后重试。") from None
    return address


class _BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = os.name != "nt"
    request_queue_size = 8

    def __init__(self, address, origin_port):
        self.origin_port = origin_port
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, _BridgeHandler)

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def process_request(self, request, client_address):
        # Bound worker creation before ThreadingMixIn starts a new thread.
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class _BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "MiniNVR2"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass

    def _empty(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_GET(self):
        if not is_overlay_address(self.client_address[0]):
            self._empty(403)
            return
        if not _PATH.fullmatch(self.path) or self.headers.get("Transfer-Encoding"):
            self._empty(404)
            return
        if self.headers.get("Content-Length", "0") != "0":
            self._empty(400)
            return
        authorization = self.headers.get("Authorization", "")
        if len(authorization) > 128:
            self._empty(401)
            return
        headers = {"Connection": "close"}
        if authorization:
            headers["Authorization"] = authorization
        # No input can change the upstream host, port, method or allowed paths.
        connection = http.client.HTTPConnection("127.0.0.1", self.server.origin_port, timeout=3)
        try:
            connection.request("GET", self.path, headers=headers)
            response = connection.getresponse()
            body = response.read(_MAX_RESPONSE + 1)
            if len(body) > _MAX_RESPONSE:
                self._empty(502)
                return
            self.send_response(response.status)
            for key in ("Content-Type", "Cache-Control", "Pragma", "Referrer-Policy",
                        "X-Content-Type-Options", "X-Frame-Options", "Content-Security-Policy"):
                value = response.getheader(key)
                if value:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (OSError, http.client.HTTPException):
            self._empty(503)
        finally:
            connection.close()
            self.close_connection = True

    def do_POST(self):
        self._empty(405)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_POST


class PreviewBridge:
    def __init__(self, address, origin_port):
        self.address = validate_adapter(address)
        if isinstance(origin_port, bool) or not isinstance(origin_port, int) or not 1 <= origin_port <= 65535:
            raise ValueError("预览本机入口端口无效。")
        self.origin_port = origin_port
        self.server = self.thread = None
        self._lifecycle = threading.RLock()

    def start(self):
        with self._lifecycle:
            if self.status():
                return self.server.server_port
            self.stop()
            server = _BridgeServer((validate_adapter(self.address), 0), self.origin_port)
            thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.2),
                                      name="nvr-netbird-preview-bridge", daemon=True)
            try:
                thread.start()
            except Exception:
                server.server_close()
                raise
            self.server, self.thread = server, thread
            return server.server_port

    def status(self):
        with self._lifecycle:
            return self.server is not None and self.thread is not None and self.thread.is_alive()

    def stop(self):
        with self._lifecycle:
            server, thread = self.server, self.thread
            self.server = self.thread = None
            if server is not None:
                server.shutdown()
                server.server_close()
                if thread is not threading.current_thread():
                    thread.join(timeout=2)
