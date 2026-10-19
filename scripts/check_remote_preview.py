"""Local synthetic smoke check; never starts NetBird or contacts its service.

Usage: python scripts/check_remote_preview.py

Uses an isolated config, generated JPEGs, the real loopback camera viewer, and
an owned Python child that only prints a reserved example HTTPS origin. Client
discovery, signature verification and launch are replaced in this check's
process only. No product test hook, account, camera, WeChat send, DNS lookup,
proxy or external HTTP request is involved. This is not external-phone proof.
"""
from contextlib import ExitStack
import hashlib
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
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ORIGIN = "https://mininvr-fixture.example"
CAMERA_ID = "synthetic-camera"
CAMERA_URL = "synthetic://generated-jpeg-no-real-camera"
FLAGS = {"creationflags": 0x08000000} if os.name == "nt" else {}


def _fake_relay():
    # Deliberately does not import MiniNVR2, bind a port or access any network.
    print("Service exposed successfully!", flush=True)
    print("  URL:      " + FIXTURE_ORIGIN, flush=True)
    time.sleep(90)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise AssertionError("Local fixture must not follow redirects")


def _wait(predicate, message, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.04)
    raise AssertionError(message)


def _closed(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return False
    except OSError:
        return True


class _Camera:
    def __init__(self, cv2, np):
        self.id, self.url, self.enabled = CAMERA_ID, CAMERA_URL, True
        self.lock = threading.RLock()
        self.state, self.last_ts, self.last_jpeg = "online", 0, b""
        self.sequence = 0
        self._cv2, self._np = cv2, np
        self._stop = threading.Event()
        self._paused = threading.Event()
        self.update()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def update(self):
        with self.lock:
            if self._paused.is_set():
                return
            self.sequence += 1
            image = self._np.zeros((90, 160, 3), dtype=self._np.uint8)
            image[:, :, 1] = (self.sequence * 31) % 255
            self._cv2.putText(image, str(self.sequence), (12, 55),
                              self._cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            success, encoded = self._cv2.imencode(".jpg", image)
            if not success:
                raise RuntimeError("Could not generate synthetic JPEG")
            self.last_jpeg = encoded.tobytes()
            self.last_ts = time.time()

    def _run(self):
        while not self._stop.wait(0.15):
            self.update()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)


def check():
    sys.path.insert(0, str(ROOT))
    import cv2
    import numpy as np
    from nvr import __version__
    from nvr.config import Config
    import nvr.preview as preview
    import nvr.remote_preview as remote
    import nvr.remote_bridge as bridge

    report = {
        "kind": "remote-preview-synthetic-local-smoke",
        "version": __version__,
        "tested_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "scope": "real loopback viewer and camera-only bridge bound to loopback fixture; owned fake relay process; generated JPEGs only",
        "real_netbird": "NOT_TESTED",
        "real_wechat": "NOT_TESTED",
        "external_phone": "NOT_TESTED",
        "real_camera": "NOT_USED",
        "checks": {},
        "result": "FAIL",
    }
    checks = report["checks"]
    processes, started_ports = [], []
    controller = camera = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    stage = "setup"
    try:
        with tempfile.TemporaryDirectory(prefix="mininvr-remote-synthetic-") as scratch:
            cfg = Config(str(Path(scratch) / "config.json"))
            cfg.set("remote_preview", "enabled", True)
            cfg.set("clawbot", "enabled", True)
            cfg.set("clawbot", "token", "synthetic-no-real-wechat-token")
            cfg.set("clawbot", "user_id", "synthetic-no-real-wechat-owner")
            cfg.upsert_camera({"id": CAMERA_ID, "name": "Synthetic remote preview camera",
                               "url": CAMERA_URL, "enabled": True})
            camera = _Camera(cv2, np)
            controller = remote.RemotePreviewController(
                cfg, lambda cid: camera if cid == CAMERA_ID else None)

            def launch(path, port):
                assert path == sys.executable, "Fixture attempted a different executable"
                assert isinstance(port, int) and 1024 <= port <= 65535
                assert controller._bridge.server.server_address == ("127.0.0.1", port)
                origin_port = controller.viewer.status()["port"]
                assert controller.viewer._server.server_address == ("127.0.0.1", origin_port)
                started_ports.extend((port, origin_port))
                process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "--fake-relay"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                    errors="replace", bufsize=1, shell=False, close_fds=True,
                    cwd=scratch, **FLAGS)
                processes.append(process)
                return process

            def request(path, token=None):
                # Construct destinations from the viewer's assigned port only.
                # Never fetch the public origin printed by the fake process.
                port = controller._bridge.server.server_port
                assert port in started_ports and port != 0, "Unknown local destination"
                assert path.startswith("/") and not path.startswith("//")
                parsed = urllib.parse.urlsplit("http://127.0.0.1:%d%s" % (port, path))
                assert (parsed.scheme, parsed.hostname, parsed.port) == ("http", "127.0.0.1", port)
                assert parsed.netloc == "127.0.0.1:%d" % port
                assert not parsed.query and not parsed.fragment
                headers = {"Authorization": "Bearer " + token} if token else {}
                req = urllib.request.Request(parsed.geturl(), headers=headers)
                try:
                    response = opener.open(req, timeout=3)
                except urllib.error.HTTPError as exc:
                    response = exc
                with response:
                    return response.status, response.headers, response.read()

            def token_from_link():
                parsed = urllib.parse.urlsplit(controller.make_link(CAMERA_ID))
                assert (parsed.scheme + "://" + parsed.netloc) == FIXTURE_ORIGIN
                assert parsed.path == "/preview" and not parsed.query
                assert re.fullmatch(r"t=[A-Za-z0-9_-]{43}", parsed.fragment)
                return parsed.fragment[2:]

            with ExitStack() as fixtures:
                fixtures.enter_context(mock.patch.object(remote, "_find_client", return_value=sys.executable))
                fixtures.enter_context(mock.patch.object(remote, "_verify_client", return_value=True))
                fixtures.enter_context(mock.patch.object(remote, "_query_address", return_value="127.0.0.1"))
                # The host has no real NetBird adapter. Change only fixture
                # binding/source checks; the real four-route bridge still runs.
                fixtures.enter_context(mock.patch.object(bridge, "validate_adapter", return_value="127.0.0.1"))
                fixtures.enter_context(mock.patch.object(bridge, "is_overlay_address", side_effect=lambda address: address == "127.0.0.1"))
                fixtures.enter_context(mock.patch.object(remote, "_launch_client", side_effect=launch))
                stage = "ready"
                controller.start()
                _wait(lambda: controller.status()["state"] == "ready", "Fake relay did not become ready")
                assert controller.status()["url"] == FIXTURE_ORIGIN
                first_port = controller.viewer.status()["port"]
                assert controller.viewer.status()["address"] == "127.0.0.1"
                checks["real_loopback_origin_and_owned_fake_process"] = "PASS"

                stage = "assets"
                for path, content_type in (("/preview", "text/html"), ("/preview.js", "text/javascript")):
                    code, headers, body = request(path)
                    assert code == 200 and body
                    assert headers.get_content_type() in (content_type, "application/javascript")
                    assert headers.get("Referrer-Policy") == "no-referrer"
                checks["viewer_assets"] = "PASS"

                stage = "capability"
                token = token_from_link()
                code, _, body = request("/info", token)
                info = json.loads(body)
                assert code == 200 and info["ok"] and info["camera_id"] == CAMERA_ID
                assert 0 < info["expires_in"] <= 600
                checks["https_camera_scoped_link"] = "PASS"

                stage = "fresh_jpeg"
                code, headers, frame = request("/frame/" + CAMERA_ID, token)
                assert code == 200 and headers.get_content_type() == "image/jpeg"
                assert headers.get("Cache-Control", "").startswith("no-store")
                image = cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_COLOR)
                assert image is not None and image.shape[:2] == (90, 160)
                before = hashlib.sha256(frame).hexdigest()
                time.sleep(0.25)
                code, _, frame = request("/frame/" + CAMERA_ID, token)
                assert code == 200 and hashlib.sha256(frame).hexdigest() != before
                with camera.lock:
                    camera._paused.set()
                    camera.last_ts = time.time() - preview.FRAME_MAX_AGE - 5
                code, _, stale = request("/frame/" + CAMERA_ID, token)
                assert code == 503 and json.loads(stale)["code"] == "unavailable"
                with camera.lock:
                    camera.last_ts = time.time()
                    camera._paused.clear()
                checks["fresh_updating_decodable_jpeg_and_stale_denial"] = "PASS"

                stage = "isolation"
                for path in ("/info", "/frame/" + CAMERA_ID):
                    code, _, _ = request(path)
                    assert code == 401
                code, _, body = request("/frame/other-camera", token)
                assert code == 403 and json.loads(body)["code"] == "wrong_camera"
                for path in ("/", "/api/status", "/api/settings", "/api/recordings",
                             "/recordings", "/snap/" + CAMERA_ID):
                    code, _, _ = request(path, token)
                    assert code == 404
                checks["missing_token_wrong_camera_and_admin_routes_denied"] = "PASS"

                stage = "revocation"
                controller.revoke()
                code, _, _ = request("/info", token)
                assert code == 401
                with mock.patch.object(preview, "TOKEN_SECONDS", 0):
                    expired = token_from_link()
                code, _, body = request("/info", expired)
                assert code == 410 and json.loads(body)["code"] == "expired"
                checks["revoked_and_expired_tokens_denied"] = "PASS"

                stage = "child_exit"
                processes[-1].terminate()
                processes[-1].wait(timeout=3)
                _wait(lambda: controller.status()["state"] == "error", "Child exit was not observed")
                _wait(lambda: _closed(first_port), "Origin survived fake relay exit")
                _wait(lambda: all(_closed(port) for port in started_ports), "Bridge survived fake relay exit")
                assert not controller.viewer.status()["running"]
                try:
                    controller.make_link(CAMERA_ID)
                except ValueError:
                    pass
                else:
                    raise AssertionError("A failed session issued a link")
                checks["child_exit_closes_origin_and_blocks_new_link"] = "PASS"

                stage = "explicit_restart_stop"
                controller.start()
                _wait(lambda: controller.status()["state"] == "ready", "Explicit restart failed")
                token_from_link()
                second_port = controller.viewer.status()["port"]
                controller.stop()
                assert controller.status()["state"] == "stopped"
                assert not controller.viewer.status()["running"]
                assert processes[-1].poll() is not None
                _wait(lambda: _closed(second_port), "Origin survived explicit stop")
                _wait(lambda: all(_closed(port) for port in started_ports), "Bridge survived explicit stop")
                checks["explicit_restart_and_stop_close_owned_resources"] = "PASS"
                assert len(processes) == 2
                report["result"] = "PASS"
    except Exception as exc:
        # No URLs, transient tokens, private config or raw client output in reports.
        report["failure_stage"] = stage
        report["error_type"] = type(exc).__name__
        raise RuntimeError("Synthetic remote-preview check failed at " + stage) from None
    finally:
        if controller is not None:
            controller.stop()
        if camera is not None:
            camera.close()
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
        destination = ROOT / "validation-local" / "remote-preview-synthetic.json"
        destination.parent.mkdir(exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    if sys.argv[1:] == ["--fake-relay"]:
        _fake_relay()
    elif sys.argv[1:]:
        raise SystemExit("Usage: python scripts/check_remote_preview.py")
    else:
        check()
