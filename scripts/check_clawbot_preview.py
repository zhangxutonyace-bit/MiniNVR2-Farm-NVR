"""Exercise the real app's LAN preview link with disposable local fixtures.

Usage: python scripts/check_clawbot_preview.py [dist/MiniNVR2.exe]

This starts an independent, short-lived LAN listener. Run it only when that
local listening test is authorized. It neither changes firewall rules nor uses
a real camera, WeChat account, login, recipient or existing configuration.
Only an assigned RFC1918 IPv4 address and preselected local ports are used;
there is no external HTTP destination, proxy or redirect. A real phone opening
the link in WeChat remains a separate acceptance check.
"""
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nvr import __version__ as EXPECTED_VERSION
OWNER = "synthetic-preview-owner-no-real-account"
TOKEN = "synthetic-preview-token-no-real-account"
CONTEXT = "synthetic-preview-context-no-real-conversation"
CAMERA_ID = "sample"
CAMERA_NAME = "Synthetic local preview camera"
FLAGS = {"creationflags": 0x08000000} if os.name == "nt" else {}
LAN_NETWORKS = tuple(ipaddress.IPv4Network(value) for value in
                     ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def _private_ipv4(address):
    try:
        value = ipaddress.IPv4Address(address)
        return any(value in network for network in LAN_NETWORKS)
    except (TypeError, ValueError):
        return False


def _assigned_lan_address():
    """Resolve the local computer name, then prove the candidate is assigned."""
    try:
        results = socket.getaddrinfo(socket.gethostname(), 0, socket.AF_INET,
                                     socket.SOCK_STREAM)
    except OSError as exc:
        raise RuntimeError("Cannot enumerate this computer's IPv4 addresses") from exc
    addresses = sorted({item[4][0] for item in results if _private_ipv4(item[4][0])})
    for address in addresses:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind((address, 0))
            return address
        except OSError:
            continue
    raise RuntimeError("No assigned, bindable RFC1918 IPv4 address; LAN check cannot pass")


def _free_port(address, excluded):
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((address, 0))
            port = probe.getsockname()[1]
        if port >= 1024 and port not in excluded:
            return port
    raise RuntimeError("Cannot reserve distinct local fixture ports")


def _approved_url(url, destinations, fragment=False):
    """Permit only literal, previously selected local host/port pairs."""
    parsed = urllib.parse.urlsplit(url)
    assert parsed.scheme == "http", "Unexpected URL scheme"
    assert parsed.hostname in destinations, "Unexpected HTTP destination"
    assert parsed.port in destinations[parsed.hostname], "Unexpected HTTP port"
    assert parsed.netloc == "%s:%d" % (parsed.hostname, parsed.port), "Invalid HTTP authority"
    assert not parsed.username and not parsed.password, "Unexpected URL credentials"
    assert not parsed.query, "Unexpected URL query"
    assert fragment or not parsed.fragment, "Capabilities must not be sent in request URLs"
    assert not parsed.path or parsed.path.startswith("/"), "Invalid HTTP path"
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise AssertionError("The local preview fixture must never redirect HTTP")


class _FixtureState:
    def __init__(self):
        self.lock = threading.Lock()
        self.message_issued = False
        self.calls = []
        self.messages = []
        self.links = []
        self.errors = []
        self.link_received = threading.Event()
        self.stopping = threading.Event()


def _handler(state):
    class SyntheticHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _respond(self, body, status=200):
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            try:
                assert self.client_address[0] == "127.0.0.1", "Nonlocal fake iLink client"
                host = "127.0.0.1:%d" % self.server.server_port
                assert self.headers.get("Host") == host, "Unexpected fake iLink Host"
                _approved_url("http://" + host + self.path,
                              {"127.0.0.1": {self.server.server_port}})
                assert self.headers.get("Authorization") == "Bearer " + TOKEN
                assert self.headers.get("AuthorizationType") == "ilink_bot_token"
                assert self.headers.get_content_type() == "application/json"
                length = int(self.headers.get("Content-Length", "0"))
                assert 0 < length <= 65536, "Unexpected fake iLink request size"
                raw = self.rfile.read(length)
                assert len(raw) == length
                body = json.loads(raw)
                assert isinstance(body, dict)
                path = urllib.parse.urlsplit(self.path).path
                endpoint = path.removeprefix("/ilink/bot/")
                assert path == "/ilink/bot/" + endpoint
                with state.lock:
                    state.calls.append(endpoint)
                if endpoint == "getupdates":
                    with state.lock:
                        issue = not state.message_issued
                        state.message_issued = True
                    if issue:
                        messages = [{"message_type": 1, "from_user_id": OWNER,
                                     "context_token": CONTEXT,
                                     "item_list": [{"type": 1, "text_item": {
                                         "text": "预览1号"}}]}]
                    else:
                        time.sleep(0.15)
                        messages = []
                    self._respond({"ret": 0, "get_updates_buf": "synthetic-cursor",
                                   "msgs": messages})
                elif endpoint == "sendmessage":
                    message = body["msg"]
                    assert message["to_user_id"] == OWNER
                    assert message["context_token"] == CONTEXT
                    assert message["message_type"] == 2
                    assert message["message_state"] == 2
                    items = message["item_list"]
                    assert items and all(item.get("type") == 1 for item in items), \
                        "Preview reply must be text, without a video/CDN upload"
                    text = "\n".join(item["text_item"]["text"] for item in items)
                    links = re.findall(r"http://[^\s<>]+", text)
                    with state.lock:
                        state.messages.append(message)
                        state.links.extend(links)
                    self._respond({"ret": 0})
                    if links:
                        state.link_received.set()
                elif endpoint == "getconfig":
                    assert body["ilink_user_id"] == OWNER
                    self._respond({"ret": 0, "typing_ticket": "synthetic-typing-ticket"})
                elif endpoint == "sendtyping":
                    assert body["ilink_user_id"] == OWNER
                    assert body["typing_ticket"] == "synthetic-typing-ticket"
                    assert body["status"] in (1, 2)
                    self._respond({"ret": 0})
                else:
                    raise AssertionError("Unexpected fake iLink endpoint")
            except (BrokenPipeError, ConnectionResetError):
                if not state.stopping.is_set():
                    with state.lock:
                        state.errors.append("Fake iLink connection ended unexpectedly")
            except Exception as exc:
                with state.lock:
                    state.errors.append(type(exc).__name__)
                try:
                    self._respond({"ret": -1, "errmsg": "Synthetic fixture rejected request"}, 400)
                except OSError:
                    pass

    return SyntheticHandler


def _redacted_log(path):
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")[-4000:]
    # Even failures must not print a capability, account value or full URL.
    text = re.sub(r"https?://[^\s<>]+", "<local endpoint>", text)
    text = re.sub(r"#t=[A-Za-z0-9_-]+", "#t=<redacted>", text)
    for value in (OWNER, TOKEN, CONTEXT):
        text = text.replace(value, "<synthetic credential>")
    return text


def check(executable=None):
    import cv2
    import imageio_ffmpeg
    import numpy as np

    if executable is not None:
        executable = Path(executable).resolve(strict=True)
    command = [str(executable)] if executable else [sys.executable, str(ROOT / "main.py")]
    lan_address = _assigned_lan_address()
    (ROOT / ".tmp").mkdir(exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    state = _FixtureState()
    report = {}
    with tempfile.TemporaryDirectory(prefix="synthetic-clawbot-preview-", dir=ROOT / ".tmp") as tmp:
        data = Path(tmp)
        sample = data / "sample.mp4"
        ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve(strict=True)
        subprocess.run([str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=30",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart", str(sample)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       timeout=30, **FLAGS)
        assert b"ftyp" in sample.read_bytes()[:32], "Synthetic camera MP4 was not generated"
        service = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
        service.daemon_threads = True
        service.block_on_close = False
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        ports = {service.server_port}
        app_port = _free_port("127.0.0.1", ports)
        ports.add(app_port)
        preview_port = _free_port("0.0.0.0", ports)
        destinations = {"127.0.0.1": {service.server_port, app_port, preview_port},
                        lan_address: {preview_port}}
        base = "http://127.0.0.1:%d" % service.server_port
        app_url = "http://127.0.0.1:%d" % app_port
        preview_url = "http://%s:%d" % (lan_address, preview_port)
        for value in (base, app_url, preview_url):
            _approved_url(value, destinations)
        config = {
            "site": {"name": "Synthetic ClawBot LAN preview verification"},
            "server": {"host": "127.0.0.1", "port": app_port, "token": ""},
            "preview": {"enabled": True, "address": lan_address, "port": preview_port},
            "cameras": [{"id": CAMERA_ID, "name": CAMERA_NAME,
                         "url": str(sample), "enabled": True, "detect": False}],
            "stream": {"auto_pick": False, "retry_sec": 0.2, "width": 320, "fps": 5},
            "record": {"root": str(data / "records"), "keep_days": 7},
            "detect": {"enabled": False},
            "mail": {"enabled": False},
            "clawbot": {"enabled": True, "auto_start": True, "token": TOKEN,
                        "bot_id": "synthetic-preview-bot", "user_id": OWNER,
                        "allowed_users": [], "login_at": time.time(),
                        "base_url": base, "cdn_url": base + "/unused-local-cdn"},
            "system": {"autostart": False, "close_to_tray": False},
        }
        (data / "config.json").write_text(json.dumps(config), encoding="utf-8")
        env = dict(os.environ, NO_PROXY="*", no_proxy="*", NVR_DATA_DIR=str(data))
        # urllib's app requests also cannot inherit any HTTP(S)/ALL proxy.
        for key in tuple(env):
            if key.lower() in ("http_proxy", "https_proxy", "all_proxy"):
                env.pop(key)

        def request(url, payload=None, headers=None, timeout=3, limit=4 * 1024 * 1024):
            target = _approved_url(url, destinations)
            body = None if payload is None else json.dumps(payload).encode("utf-8")
            request_headers = {"Content-Type": "application/json"}
            request_headers.update(headers or {})
            req = urllib.request.Request(target, data=body, headers=request_headers)
            try:
                response = opener.open(req, timeout=timeout)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                raw = response.read(limit + 1)
                assert len(raw) <= limit, "Unexpected oversized local response"
                return response.status, response.headers, raw

        def json_request(url, payload=None, headers=None, timeout=3):
            code, response_headers, raw = request(url, payload, headers, timeout)
            assert response_headers.get_content_type() == "application/json", \
                "Local API did not return JSON"
            return code, json.loads(raw)

        process = None
        log_file = data / "synthetic-application.log"
        try:
            with log_file.open("wb") as logs:
                process = subprocess.Popen(
                    command + ["--data-dir", str(data), "--host", "127.0.0.1",
                               "--port", str(app_port), "--no-tray", "--no-browser"],
                    cwd=ROOT, env=env, stdout=logs, stderr=logs, **FLAGS)
                status = None
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        code, status = json_request(app_url + "/api/status")
                        if code == 200 and status.get("ok"):
                            break
                    except (OSError, urllib.error.URLError):
                        pass
                    time.sleep(0.2)
                assert status and status.get("ok"), "Application HTTP startup failed"
                assert status["version"] == EXPECTED_VERSION, "Unexpected application version"
                report["version"] = status["version"]
                report["http_startup"] = "PASS"
                code, _ = json_request(app_url + "/api/status",
                                       headers={"Host": "%s:%d" % (lan_address, app_port)})
                assert code == 403, "Remote Host must not receive anonymous management data"
                report["management_anonymous_protection"] = "PASS"
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline and process.poll() is None:
                    if state.link_received.wait(0.2):
                        break
                    with state.lock:
                        assert not state.errors, "Fake iLink rejected a request"
                assert state.link_received.is_set(), "Preview command did not return a link"
                with state.lock:
                    assert not state.errors, "Fake iLink rejected a request"
                    assert len(state.links) == 1, "Expected one camera preview link"
                    link = state.links[0]
                    assert len(state.messages) == 1 and state.messages[0]["item_list"] == [
                        {"type": 1, "text_item": {"text": link}}], \
                        "Preview reply must be one complete URL without extra text or markup"
                    assert "getupdates" in state.calls and "sendmessage" in state.calls
                _approved_url(link, {lan_address: {preview_port}}, fragment=True)
                parsed = urllib.parse.urlsplit(link)
                assert parsed.path == "/preview", "Unexpected preview path"
                fragment = urllib.parse.parse_qs(parsed.fragment, strict_parsing=True)
                assert set(fragment) == {"t"} and len(fragment["t"]) == 1
                ticket = fragment["t"][0]
                assert re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket), "Invalid preview capability"
                assert parsed.fragment == "t=" + ticket, "Unexpected capability encoding"
                auth = {"Authorization": "Bearer " + ticket}
                report["synthetic_preview_command"] = "PASS"

                code, page_headers, page = request(preview_url + "/preview")
                assert code == 200 and page_headers.get_content_type() == "text/html" and page
                assert ticket.encode("ascii") not in page, "Capability leaked into static HTML"
                assert page_headers.get("Referrer-Policy") == "no-referrer"
                code, _, script = request(preview_url + "/preview.js")
                assert code == 200 and script, "Preview JavaScript is missing from app assets"
                assert ticket.encode("ascii") not in script, "Capability leaked into static script"
                report["preview_assets"] = "PASS"
                code, info = json_request(preview_url + "/info", headers=auth)
                assert code == 200 and info["ok"], "Preview info authorization failed"
                assert info["camera_id"] == CAMERA_ID and info["name"] == CAMERA_NAME
                assert 0 < info["expires_in"] <= 600, "Preview expiry exceeds ten minutes"
                report["camera_scoped_capability"] = "PASS"
                deadline = time.monotonic() + 45
                frame = b""
                while time.monotonic() < deadline and process.poll() is None:
                    code, frame_headers, frame = request(
                        preview_url + "/frame/" + CAMERA_ID, headers=auth)
                    if code == 200:
                        break
                    assert code == 503, "Authorized preview failed unexpectedly"
                    time.sleep(0.2)
                assert code == 200 and frame_headers.get_content_type() == "image/jpeg", \
                    "Synthetic camera did not produce a live preview frame"
                assert frame.startswith(b"\xff\xd8") and frame.endswith(b"\xff\xd9"), \
                    "Preview response is not a complete JPEG"
                image = cv2.imdecode(np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
                assert image is not None and image.shape[:2] == (180, 320), \
                    "Preview JPEG could not be decoded at the synthetic camera dimensions"
                assert frame_headers.get("Cache-Control", "").startswith("no-store")
                report["lan_live_jpeg"] = "PASS"

                for path in ("/info", "/frame/" + CAMERA_ID):
                    code, body = json_request(preview_url + path)
                    assert code == 401 and body.get("ok") is False, \
                        "Preview media/info accepted a missing capability"
                code, body = json_request(preview_url + "/frame/other", headers=auth)
                assert code == 403 and body.get("code") == "wrong_camera", \
                    "Preview capability was accepted for another camera"
                report["missing_and_wrong_camera_denied"] = "PASS"
                for path in ("/", "/api/status", "/api/settings", "/api/recordings",
                             "/recordings", "/live/" + CAMERA_ID, "/snap/" + CAMERA_ID):
                    code, _, _ = request(preview_url + path, headers=auth)
                    assert code == 404, "A management/recording route exists on preview listener"
                report["preview_routes_isolated"] = "PASS"
                state.stopping.set()
                code, result = json_request(app_url + "/api/system/quit", {}, timeout=10)
                assert code == 200 and result["ok"], "Application quit request failed"
                assert process.wait(timeout=30) == 0, "Application did not exit cleanly"
                with state.lock:
                    assert not state.errors, "Fake iLink rejected a request"
                report["web_quit"] = "PASS"
                for address in ("127.0.0.1", lan_address):
                    try:
                        connection = socket.create_connection((address, preview_port), timeout=1)
                    except OSError:
                        continue
                    connection.close()
                    raise AssertionError("Preview listener survived application exit")
                report["preview_listener_closed"] = "PASS"
        except Exception as exc:
            # Do not include exception repr/URL values: tickets are transient secrets.
            detail = str(exc)
            detail = re.sub(r"https?://[^\s<>]+", "<local endpoint>", detail)
            detail = re.sub(r"#t=[A-Za-z0-9_-]+", "#t=<redacted>", detail)
            raise RuntimeError("Synthetic ClawBot preview check failed: %s\n%s" %
                               (detail, _redacted_log(log_file))) from None
        finally:
            state.stopping.set()
            if process is not None and process.poll() is None:
                try:
                    json_request(app_url + "/api/system/quit", {}, timeout=3)
                    process.wait(timeout=15)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
            service.shutdown()
            service.server_close()
            thread.join(timeout=5)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    if len(sys.argv) > 2:
        raise SystemExit("Usage: python scripts/check_clawbot_preview.py [MiniNVR2.exe]")
    check(sys.argv[1] if len(sys.argv) == 2 else None)
