"""Check event delivery through the actual app, using only synthetic local data.

Usage: python scripts/check_clawbot_events.py [dist/MiniNVR2.exe]

The disposable configuration, camera file, iLink service and CDN are fixtures.
No real camera, WeChat account, login, recipient or production configuration is
used. This verifies the local request/encryption/upload/reply path, not receipt
by the actual WeChat service or a real phone.
"""
import base64
import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
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
OWNER = "synthetic-owner-no-real-account"
TOKEN = "synthetic-token-no-real-account"
CONTEXT = "synthetic-context-no-real-conversation"
RECEIPT = "synthetic-upload-receipt"
UPLOAD_PARAM = "synthetic-upload-param"
FLAGS = {"creationflags": 0x08000000} if os.name == "nt" else {}


def _local_url(url, allowed_ports):
    """Reject DNS names, redirects, credentials and every non-loopback target."""
    parsed = urllib.parse.urlsplit(url)
    assert parsed.scheme == "http" and parsed.hostname == "127.0.0.1", url
    assert parsed.port in allowed_ports, "Unexpected local HTTP port"
    assert not parsed.username and not parsed.password and not parsed.fragment
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise AssertionError("The synthetic fixture must never redirect HTTP")


class _FixtureState:
    def __init__(self):
        self.lock = threading.Lock()
        self.message_issued = False
        self.calls = []
        self.messages = []
        self.upload_requests = []
        self.uploads = []
        self.errors = []
        self.video_received = threading.Event()
        self.stopping = threading.Event()


def _handler(state):
    class SyntheticHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _respond(self, data, status=200, extra_headers=None):
            payload = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            try:
                assert self.client_address[0] == "127.0.0.1"
                netloc = "127.0.0.1:%d" % self.server.server_port
                assert self.headers.get("Host") == netloc
                assert self.path.startswith("/")
                _local_url("http://" + netloc + self.path,
                           {self.server.server_port})
                parsed = urllib.parse.urlsplit(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                assert 0 < length <= 4 * 1024 * 1024
                raw = self.rfile.read(length)
                assert len(raw) == length
                with state.lock:
                    state.calls.append(parsed.path)
                if parsed.path == "/synthetic-cdn/upload":
                    query = urllib.parse.parse_qs(parsed.query)
                    with state.lock:
                        assert len(state.upload_requests) == 1
                        upload = state.upload_requests[0]
                        assert query == {"encrypted_query_param": [UPLOAD_PARAM],
                                         "filekey": [upload["filekey"]]}
                        assert len(raw) == upload["filesize"]
                        state.uploads.append(raw)
                    assert self.headers.get_content_type() == "application/octet-stream"
                    self._respond({}, extra_headers={"x-encrypted-param": RECEIPT})
                    return

                assert not parsed.query
                assert self.headers.get("Authorization") == "Bearer " + TOKEN
                assert self.headers.get("AuthorizationType") == "ilink_bot_token"
                assert self.headers.get_content_type() == "application/json"
                body = json.loads(raw)
                assert isinstance(body, dict)
                endpoint = parsed.path.removeprefix("/ilink/bot/")
                assert parsed.path == "/ilink/bot/" + endpoint
                if endpoint == "getupdates":
                    with state.lock:
                        issue = not state.message_issued
                        state.message_issued = True
                    if issue:
                        messages = [{"message_type": 1, "from_user_id": OWNER,
                                     "context_token": CONTEXT,
                                     "item_list": [{"type": 1, "text_item": {
                                         "text": "最新事件"}}]}]
                    else:
                        state.stopping.wait(0.15)
                        messages = []
                    self._respond({"ret": 0, "get_updates_buf": "synthetic-cursor",
                                   "msgs": messages})
                elif endpoint == "getconfig":
                    assert body["ilink_user_id"] == OWNER
                    self._respond({"ret": 0, "typing_ticket": "synthetic-ticket"})
                elif endpoint == "sendtyping":
                    assert body["ilink_user_id"] == OWNER
                    assert body["typing_ticket"] == "synthetic-ticket"
                    assert body["status"] in (1, 2)
                    self._respond({"ret": 0})
                elif endpoint == "getuploadurl":
                    assert body["to_user_id"] == OWNER
                    assert body["media_type"] == 2 and body["no_need_thumb"] is True
                    with state.lock:
                        state.upload_requests.append(body)
                    # Exercise the app's configured local CDN URL as well.
                    self._respond({"ret": 0, "upload_param": UPLOAD_PARAM})
                elif endpoint == "sendmessage":
                    message = body["msg"]
                    assert message["to_user_id"] == OWNER
                    assert message["context_token"] == CONTEXT
                    assert message["message_type"] == 2
                    assert message["message_state"] == 2
                    with state.lock:
                        state.messages.append(message)
                    self._respond({"ret": 0})
                    if any(item.get("type") == 5 for item in message["item_list"]):
                        state.video_received.set()
                else:
                    raise AssertionError("Unexpected synthetic endpoint: " + parsed.path)
            except Exception as exc:
                with state.lock:
                    state.errors.append(str(exc) or type(exc).__name__)
                try:
                    self._respond({"ret": -1, "errmsg": "Synthetic fixture rejected request"},
                                  status=400)
                except OSError:
                    pass

    return SyntheticHandler


def check(executable=None):
    import imageio_ffmpeg
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if executable is not None:
        executable = Path(executable).resolve(strict=True)
    command = [str(executable)] if executable else [sys.executable, str(ROOT / "main.py")]
    (ROOT / ".tmp").mkdir(exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    state = _FixtureState()
    report = {}
    with tempfile.TemporaryDirectory(prefix="synthetic-clawbot-event-", dir=ROOT / ".tmp") as tmp:
        data = Path(tmp)
        day = datetime.date.today().isoformat()
        event = data / "records" / "sample" / day / "event" / "120000.mp4"
        event.parent.mkdir(parents=True)
        # Only the installed local imageio FFmpeg helper generates this media.
        ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve(strict=True)
        subprocess.run([str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=3",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart", str(event)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       timeout=30, **FLAGS)
        original = event.read_bytes()
        assert b"ftyp" in original[:32] and len(original) > 512
        expected_md5 = hashlib.md5(original).hexdigest()
        service = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
        service.daemon_threads = True
        service.block_on_close = False
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            app_port = port_socket.getsockname()[1]
        ports = {service.server_port, app_port}
        base = _local_url("http://127.0.0.1:%d" % service.server_port, ports)
        cdn = _local_url(base + "/synthetic-cdn", ports)
        app_url = _local_url("http://127.0.0.1:%d" % app_port, ports)
        config = {
            "site": {"name": "Synthetic ClawBot event verification"},
            "server": {"host": "127.0.0.1", "port": app_port, "token": ""},
            "cameras": [{"id": "sample", "name": "Synthetic local camera",
                         "url": str(event), "enabled": True, "detect": False}],
            "stream": {"auto_pick": False},
            "record": {"root": str(data / "records"), "keep_days": 7},
            "detect": {"enabled": False},
            "mail": {"enabled": False},
            "clawbot": {"enabled": True, "auto_start": True, "token": TOKEN,
                        "bot_id": "synthetic-bot", "user_id": OWNER,
                        "allowed_users": [], "login_at": time.time(),
                        "base_url": base, "cdn_url": cdn, "max_video_mb": 1},
            "system": {"autostart": False, "close_to_tray": False},
        }
        (data / "config.json").write_text(json.dumps(config), encoding="utf-8")
        # The child reads only this new configuration and local synthetic clip.
        assert Path(config["cameras"][0]["url"]).is_file()
        for key in ("base_url", "cdn_url"):
            _local_url(config["clawbot"][key], {service.server_port})
        env = dict(os.environ, NO_PROXY="*", no_proxy="*")
        env["NVR_DATA_DIR"] = str(data)

        def request(path, payload=None, timeout=3):
            target = _local_url(app_url + path, {app_port})
            body = None if payload is None else json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(target, data=body,
                                         headers={"Content-Type": "application/json"})
            with opener.open(req, timeout=timeout) as response:
                return json.load(response)

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
                        status = request("/api/status")
                        break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.2)
                assert status and status["ok"], "Application HTTP startup failed"
                assert status["version"] == EXPECTED_VERSION, status["version"]
                report["version"] = status["version"]
                report["http_startup"] = "PASS"
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline and process.poll() is None:
                    if state.video_received.wait(0.2):
                        break
                    with state.lock:
                        assert not state.errors, state.errors
                assert state.video_received.is_set(), "Synthetic event video was not returned"
                with state.lock:
                    assert not state.errors, state.errors
                    assert len(state.upload_requests) == len(state.uploads) == 1
                    upload = state.upload_requests[0]
                    ciphertext = state.uploads[0]
                    videos = [item["video_item"] for message in state.messages
                              for item in message["item_list"] if item.get("type") == 5]
                    assert len(videos) == 1
                    required = {"/ilink/bot/" + name for name in (
                        "getupdates", "getconfig", "sendtyping", "getuploadurl", "sendmessage")}
                    assert required.issubset(state.calls)
                    assert "/synthetic-cdn/upload" in state.calls
                report["synthetic_loopback_delivery"] = "PASS"
                assert upload["rawsize"] == len(original)
                assert upload["rawfilemd5"] == expected_md5
                assert upload["filesize"] == (len(original) // 16 + 1) * 16
                key = bytes.fromhex(upload["aeskey"])
                assert len(key) == 16
                decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
                padded = decryptor.update(ciphertext) + decryptor.finalize()
                unpadder = padding.PKCS7(128).unpadder()
                plaintext = unpadder.update(padded) + unpadder.finalize()
                assert plaintext == original, "Returned MP4 differs from synthetic event"
                video = videos[0]
                assert video["video_size"] == len(original)
                assert video["video_md5"] == expected_md5
                assert video["media"]["encrypt_type"] == 1
                assert video["media"]["encrypt_query_param"] == RECEIPT
                assert base64.b64decode(video["media"]["aes_key"]).decode("ascii") == key.hex()
                report["encrypted_event_integrity"] = "PASS"
                assert request("/api/system/quit", {}, timeout=10)["ok"]
                assert process.wait(timeout=30) == 0, "Application did not exit cleanly"
                with state.lock:
                    assert not state.errors, state.errors
                report["web_quit"] = "PASS"
        except Exception as exc:
            details = log_file.read_text(encoding="utf-8", errors="replace")[-4000:]
            raise RuntimeError("Synthetic ClawBot event check failed: %s\n%s" %
                               (exc, details)) from exc
        finally:
            if process is not None and process.poll() is None:
                try:
                    request("/api/system/quit", {}, timeout=3)
                    process.wait(timeout=15)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
            state.stopping.set()
            service.shutdown()
            service.server_close()
            thread.join(timeout=5)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    if len(sys.argv) > 2:
        raise SystemExit("Usage: python scripts/check_clawbot_events.py [MiniNVR2.exe]")
    check(sys.argv[1] if len(sys.argv) == 2 else None)
