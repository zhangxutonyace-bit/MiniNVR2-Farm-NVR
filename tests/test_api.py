import copy
import datetime
import json
from types import SimpleNamespace

from nvr.server import create_app
from tests.support import OfflineTestCase


class FakeHub:
    def __init__(self):
        self.scan_state = {"running": False, "results": []}
        self.active_paths = set()
        self.sync_requests = 0
        self.preview_sync_requests = 0
        self.preview_status_requests = 0
        self.log = lambda *args: None
        self.bot = SimpleNamespace(state="idle", qr_url="", error="", _running=False,
                                   HELP="Offline fixture", logged_in=lambda: False,
                                   remain_sec=lambda: 0)

    def get(self, cid):
        return None

    def info(self):
        return []

    def recording_paths(self):
        return self.active_paths

    def request_sync(self):
        self.sync_requests += 1

    def get_clawbot(self, restart=False):
        return self.bot

    def sync_preview(self):
        self.preview_sync_requests += 1

    def preview_status(self):
        self.preview_status_requests += 1
        return {"enabled": False, "running": False, "address": "",
                "port": 8803, "error": ""}


class ApiTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.cfg.upsert_camera({"id": "sample", "name": "Synthetic", "url": "fixture.mp4"})
        self.hub = FakeHub()
        self.app = create_app(self.cfg, self.hub)
        self.app.testing = True
        self.client = self.app.test_client()
        self.day = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        event_dir = self.root / "records" / "sample" / self.day / "event"
        event_dir.mkdir(parents=True)
        self.payload = bytes(range(256)) * 16
        self.event = event_dir / "123456.mp4"
        self.event.write_bytes(self.payload)
        self.timelapse = event_dir.parents[1] / ("timelapse_" + self.day + ".mp4")
        self.timelapse.write_bytes(self.payload)
        self.url = "/api/rec/sample/%s/event/123456.mp4" % self.day

    def test_static_pages_status_and_snapshot(self):
        for path in ("/", "/app.js", "/api/status", "/api/settings"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.data)
                response.close()
        response = self.client.get("/snap/sample")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertTrue(response.data.startswith(b"\xff\xd8"))

    def test_live_endpoint_emits_mjpeg_and_can_be_closed(self):
        response = self.client.get("/live/sample", buffered=False)
        try:
            first = next(iter(response.response))
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Content-Type: image/jpeg", first)
            self.assertIn(b"\xff\xd8", first)
        finally:
            response.close()

    def test_clawbot_status_and_qr_render_without_network(self):
        response = self.client.get("/api/clawbot")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertFalse(response.get_json()["logged"])
        self.assertEqual(self.client.get("/api/clawbot/qr.png").status_code, 404)
        self.hub.bot.qr_url = "https://example.invalid/offline-qr-fixture"
        response = self.client.get("/api/clawbot/qr.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/png")
        self.assertTrue(response.data.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_recordings_filter_active_files_and_today_timelapse(self):
        today = datetime.date.today().isoformat()
        (self.timelapse.parent / ("timelapse_" + today + ".mp4")).write_bytes(self.payload)
        result = self.client.get("/api/recordings").get_json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 2)
        self.assertEqual({item["type"] for item in result["items"]}, {"event", "timelapse"})
        self.hub.active_paths.add(str(self.event))
        result = self.client.get("/api/recordings").get_json()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["type"], "timelapse")
        result = self.client.get("/api/recordings?type=event").get_json()
        self.assertEqual(result["count"], 0)

    def test_full_and_partial_playback_bytes(self):
        full = self.client.get(self.url)
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.data, self.payload)
        self.assertEqual(full.headers["Accept-Ranges"], "bytes")
        for requested, start, end in (("bytes=0-99", 0, 99),
                                      ("bytes=100-", 100, len(self.payload) - 1),
                                      ("bytes=-100", len(self.payload) - 100, len(self.payload) - 1)):
            with self.subTest(range=requested):
                response = self.client.get(self.url, headers={"Range": requested})
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.data, self.payload[start:end + 1])
                self.assertEqual(response.headers["Content-Range"],
                                 "bytes %d-%d/%d" % (start, end, len(self.payload)))
        response = self.client.get(self.url, headers={"Range": "bytes=999999-"})
        self.assertEqual(response.status_code, 416)

    def test_playback_rejects_traversal(self):
        for path in ("/api/rec/sample/%s/event/..%%2f..%%2fconfig.json" % self.day,
                     "/api/rec/sample/%s/event/..\\config.mp4" % self.day):
            self.assertIn(self.client.get(path).status_code, (400, 404))

    def test_token_protects_api_live_and_snapshot(self):
        self.cfg.set("server", "token", "test-token")
        for path in ("/api/status", "/api/settings", "/api/recordings",
                     self.url, "/live/sample", "/snap/sample"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)
                response.close()
        self.assertEqual(self.client.get("/api/status", headers={"X-Token": "test-token"}).status_code, 200)
        self.assertEqual(self.client.get("/api/status?token=test-token").status_code, 401)

    def test_cookie_login_logout_and_rotation(self):
        self.cfg.set("server", "token", "old-token")
        response = self.client.post("/api/auth", json={"token": "old-token"})
        self.assertEqual(response.status_code, 200)
        cookies = "; ".join(response.headers.getlist("Set-Cookie"))
        self.assertIn("HttpOnly", cookies)
        self.assertIn("SameSite=Strict", cookies)
        self.assertEqual(self.client.get("/api/status").status_code, 200)
        self.cfg.set("server", "token", "new-token")
        self.assertEqual(self.client.get("/api/status").status_code, 401)
        self.assertEqual(self.client.post("/api/auth", json={"token": "new-token"}).status_code, 200)
        self.assertEqual(self.client.delete("/api/auth", json={}).status_code, 200)
        self.assertEqual(self.client.get("/api/status").status_code, 401)

    def test_settings_redact_secrets_and_blank_save_preserves_them(self):
        secrets = {"server": ("token", "test-token"), "mail": ("pass", "mail-secret"),
                   "scan": ("pwds", "camera-secret"), "clawbot": ("token", "bot-secret")}
        for section, (key, value) in secrets.items():
            self.cfg.set(section, key, value)
        self.cfg.upsert_camera({"id": "sample", "name": "Synthetic",
                                "url": "rtsp://fixture:camera-secret@example.invalid/stream"})
        headers = {"X-Token": "test-token"}
        response = self.client.get("/api/settings", headers=headers)
        body = response.get_json()
        for section, (key, value) in secrets.items():
            self.assertEqual(body[section][key], "")
            self.assertNotIn(value, response.get_data(as_text=True))
        response = self.client.post("/api/settings", headers=headers,
                                    json={section: {key: ""} for section, (key, _) in secrets.items()})
        self.assertEqual(response.status_code, 200)
        for section, (key, value) in secrets.items():
            self.assertEqual(self.cfg.get(section, key), value)

    def test_cameras_can_be_added_without_opening_connections(self):
        response = self.client.post("/api/cameras", json={"id": "sample2", "name": "Fixture 2",
                                                         "url": "fixture2.mp4"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(self.hub.sync_requests, 1)
        self.assertIn("sample2", {camera["id"] for camera in self.client.get("/api/settings").get_json()["cameras"]})
        response = self.client.post("/api/cameras", json={"id": "../outside", "url": "fixture.mp4"})
        self.assertIn(response.status_code, (400, 422))

    def test_remote_unauthenticated_and_cross_origin_writes_rejected(self):
        response = self.client.get("/api/status", environ_overrides={"REMOTE_ADDR": "192.0.2.4"})
        self.assertIn(response.status_code, (401, 403))
        response = self.client.post("/api/settings", json={"site": {"name": "Bad origin"}},
                                    headers={"Origin": "https://example.invalid"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/api/settings", data="{}", content_type="text/plain")
        self.assertIn(response.status_code, (400, 415))

    def test_preview_status_keeps_management_token_and_cookie_authentication(self):
        self.cfg.set("server", "token", "management-only-token")
        for path, headers in (("/api/preview", {}),
                              ("/api/preview?token=management-only-token", {}),
                              ("/api/preview", {"X-Token": "wrong-token"}),
                              ("/api/preview", {"Authorization": "Bearer fixture-preview-ticket"})):
            with self.subTest(path=path, headers=headers):
                response = self.client.get(path, headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertTrue(response.get_json()["auth_required"])
        self.assertEqual(self.hub.preview_status_requests, 0)
        response = self.client.get("/api/preview", headers={"X-Token": "management-only-token"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertFalse(response.get_json()["running"])
        self.assertEqual(self.hub.preview_status_requests, 1)
        self.assertEqual(self.client.post("/api/auth", json={"token": "management-only-token"}).status_code, 200)
        self.assertEqual(self.client.get("/api/preview").status_code, 200)
        self.assertEqual(self.hub.preview_status_requests, 2)

    def test_invalid_preview_settings_reject_entire_save_without_partial_mutation(self):
        original = copy.deepcopy(self.cfg.data)
        stored = (self.root / "config.json").read_bytes()
        invalid = ({"enabled": "true"}, {"enabled": 1},
                   {"address": "8.8.8.8"}, {"address": "127.0.0.1"},
                   {"address": "169.254.1.2"}, {"address": "example.invalid"},
                   {"address": 123}, {"port": True}, {"port": 8803.5},
                   {"port": 1023}, {"port": 65536}, {"port": "8803"},
                   [], "invalid", None)
        for preview in invalid:
            with self.subTest(preview=preview):
                # Place unrelated updates first, so validation must precede all writes.
                response = self.client.post("/api/settings", json={
                    "site": {"name": "Must not be saved"},
                    "server": {"token": "must-not-be-installed"},
                    "preview": preview})
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()["ok"])
                self.assertEqual(self.cfg.data, original)
                self.assertEqual((self.root / "config.json").read_bytes(), stored)
                self.assertEqual(self.hub.sync_requests, 0)
                self.assertEqual(self.hub.preview_sync_requests, 0)

    def test_valid_preview_settings_persist_and_synchronize_listener(self):
        response = self.client.post("/api/settings", json={
            "site": {"name": "Preview fixture"},
            "preview": {"enabled": True, "address": " 192.168.1.10 ", "port": 8810}})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        expected = {"enabled": True, "address": "192.168.1.10", "port": 8810}
        self.assertEqual(self.cfg.get("preview"), expected)
        self.assertEqual(self.cfg.get("site", "name"), "Preview fixture")
        stored = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["preview"], expected)
        self.assertEqual(self.hub.sync_requests, 1)
        self.assertEqual(self.hub.preview_sync_requests, 1)
        # A partial change retains the configured address and port and applies disabling.
        response = self.client.post("/api/settings", json={"preview": {"enabled": False}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.cfg.get("preview"), dict(expected, enabled=False))
        self.assertEqual(self.hub.preview_sync_requests, 2)
