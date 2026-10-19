"""Verify the actual desktop entry point using loopback HTTP and disposable data."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def check(executable=None):
    (ROOT / ".tmp").mkdir(exist_ok=True)
    command = [str(executable)] if executable else [sys.executable, str(ROOT / "main.py")]
    flags = {"creationflags": 0x08000000} if os.name == "nt" else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    report = {}
    with tempfile.TemporaryDirectory(prefix="startup-", dir=ROOT / ".tmp") as tmp:
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        args = command + ["--data-dir", tmp, "--host", "127.0.0.1", "--port", str(port),
                          "--no-tray", "--no-browser"]
        process = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, **flags)
        try:
            url = "http://127.0.0.1:%d" % port
            status = None
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    with opener.open(url + "/api/status", timeout=1) as response:
                        status = json.load(response)
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.2)
            assert status and status["ok"] and status["cams"] == [], "Server did not become ready"
            report["version"] = status["version"]
            with opener.open(url, timeout=5) as response:
                assert response.status == 200 and b"<html" in response.read().lower()
            report["http_startup"] = "PASS"
            if tuple(int(part) for part in status["version"].split(".")) >= (2, 3, 0):
                with opener.open(url + "/api/remote-preview", timeout=5) as response:
                    remote = json.load(response)
                assert remote["ok"] and remote["state"] == "stopped"
                assert not remote["enabled"] and not remote["running"] and not remote["url"]
                report["remote_preview_default_off"] = "PASS"
            # A competing instance must fail, never claim to be ready or stay alive.
            conflict = subprocess.run(args, cwd=ROOT, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, timeout=40, **flags)
            assert conflict.returncode != 0, "Port conflict must fail"
            report["port_conflict"] = "PASS"
            # Feed a synthetic local clip through the running application's
            # FFmpeg path (including the embedded binary in a frozen EXE).
            import imageio_ffmpeg
            sample = Path(tmp) / "synthetic.mp4"
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner",
                            "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc2=size=320x180:rate=10:duration=15",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(sample)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **flags)
            def post(path, data):
                req = urllib.request.Request(url + path, data=json.dumps(data).encode(),
                                             headers={"Content-Type": "application/json"}, method="POST")
                with opener.open(req, timeout=10) as response:
                    return json.load(response)
            assert post("/api/settings", {"stream": {"auto_pick": False},
                        "detect": {"enabled": False}})["ok"]
            assert post("/api/cameras", {"id": "synthetic", "name": "Synthetic test",
                        "url": str(sample), "enabled": True, "detect": False})["ok"]
            deadline = time.monotonic() + 20
            online = False
            while time.monotonic() < deadline:
                with opener.open(url + "/api/status", timeout=3) as response:
                    current = json.load(response)
                if current["cams"] and current["cams"][0]["state"] == "online":
                    online = True
                    break
                time.sleep(0.2)
            assert online, "Application FFmpeg did not produce a live frame"
            with opener.open(url + "/snap/synthetic", timeout=5) as response:
                picture = response.read()
                assert picture.startswith(b"\xff\xd8") and len(picture) > 1000
            report["ffmpeg_preview"] = "PASS"
            request = urllib.request.Request(url + "/api/system/quit", data=b"{}",
                                             headers={"Content-Type": "application/json"}, method="POST")
            with opener.open(request, timeout=10) as response:
                assert json.load(response)["ok"]
            assert process.wait(timeout=25) == 0, "Web quit must terminate process cleanly"
            report["web_quit"] = "PASS"
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        invalid = subprocess.run(command + ["--data-dir", tmp, "--host", "0.0.0.0",
                                 "--no-tray", "--no-browser"], cwd=ROOT,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 timeout=40, **flags)
        assert invalid.returncode != 0, "LAN without token must fail"
        report["lan_requires_token"] = "PASS"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    check(Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None)
