"""Camera-only relay-origin checks without external accounts or connections."""
import hashlib
import time
import urllib.parse
from types import SimpleNamespace
from unittest import mock

from nvr.preview import PreviewService, RemotePreviewService, validate_public_origin
from tests.support import OfflineTestCase


class PublicOriginTests(OfflineTestCase):
    def test_clean_https_origins_are_normalized(self):
        for value, expected in (
            ("https://viewer.fixture.example", "https://viewer.fixture.example"),
            ("https://Viewer.Fixture.Example/", "https://viewer.fixture.example"),
            ("https://viewer.fixture.example:443", "https://viewer.fixture.example"),
            ("https://viewer.fixture.example:8443/", "https://viewer.fixture.example:8443"),
        ):
            with self.subTest(value=value):
                self.assertEqual(validate_public_origin(value), expected)

    def test_unsafe_or_non_origin_values_are_rejected_without_echoing_input(self):
        values = (None, {}, 443, "", "http://viewer.fixture.example",
            "https://user:private-fixture@viewer.fixture.example",
            "https://viewer.fixture.example/path", "https://viewer.fixture.example//",
            "https://viewer.fixture.example?", "https://viewer.fixture.example?secret=fixture",
            "https://viewer.fixture.example#", "https://viewer.fixture.example#t=fixture",
            "https://127.0.0.1", "https://192.168.1.1", "https://8.8.8.8",
            "https://[::1]", "https://localhost", "https://camera.localhost",
            "https://camera.local", "https://127.1", "https://2130706433",
            "https://0x7f.0x0.0x0.0x1", "https://viewer.fixture.example:",
            "https://viewer.fixture.example:0", "https://viewer.fixture.example:65536",
            "https://viewer.fixture.example:bad", "https://-viewer.fixture.example",
            "https://viewer..example", "https://viewer.fixture.example.",
            "https://viewer.fixture.example\\path", "https://viewer.fixture.example\n",
            " https://viewer.fixture.example", "https://viewer.fixture.例子",
            "https://" + ("a" * 64) + ".example")
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as raised:
                    validate_public_origin(value)
                self.assertNotIn("private-fixture", str(raised.exception))
                self.assertNotIn("viewer.fixture", str(raised.exception))


class RemoteViewerTests(OfflineTestCase):
    origin = "https://viewer.fixture.example"

    def setUp(self):
        super().setUp()
        for section, key, value in (
            ("preview", "enabled", False), ("preview", "address", "8.8.8.8"),
            ("preview", "port", "invalid-lan-port"), ("remote_preview", "enabled", True),
            ("clawbot", "enabled", True), ("clawbot", "user_id", "fixture-owner"),
            ("clawbot", "token", "fixture-bot-secret"),
        ):
            self.cfg.set(section, key, value, save=False)
        from nvr.server import placeholder
        self.jpeg = placeholder("synthetic-remote-viewer")
        self.cameras = {}
        for cid, name in (("farm", "农场"), ("coop", "鸡舍")):
            self.cfg.upsert_camera({"id": cid, "name": name,
                                    "url": "fixture-only.mp4", "enabled": True})
            self.cameras[cid] = SimpleNamespace(id=cid, name=name, url="fixture-only.mp4",
                enabled=True, state="online", last_ts=time.time(),
                snapshot=lambda: (self.jpeg, 1))
        clock = mock.patch("nvr.preview.time.monotonic", return_value=1000.0)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.logs = []
        self.service = RemotePreviewService(self.cfg, self.cameras.get, log=self.logs.append)
        self.listener = mock.Mock(server_port=49123)
        self.thread = mock.Mock()
        self.thread.is_alive.return_value = True
        listener = mock.patch.object(self.service, "_start_listener",
                                    return_value=(self.listener, self.thread))
        self.start_listener = listener.start()
        self.addCleanup(listener.stop)
        self.addCleanup(self.service.stop)
        self.service.app.testing = True
        self.client = self.service.app.test_client()

    def ready(self):
        self.assertEqual(self.service.start_origin(), 49123)
        self.service.set_public_url(self.origin)

    def link(self, cid="farm"):
        url = self.service.make_link(cid)
        parsed = urllib.parse.urlsplit(url)
        return url, urllib.parse.parse_qs(parsed.fragment)["t"][0]

    def get(self, path, ticket=None, address="127.0.0.1", **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        if ticket is not None:
            headers["Authorization"] = "Bearer " + ticket
        response = self.client.get(path, headers=headers,
            environ_overrides={"REMOTE_ADDR": address}, base_url=self.origin, **kwargs)
        self.addCleanup(response.close)
        return response

    def denied(self, response):
        self.assertIn(response.status_code, (401, 403, 410))
        self.assertNotEqual(response.mimetype, "image/jpeg")
        self.assertNotIn(self.jpeg, response.data)

    def test_origin_requires_explicit_start_and_separate_remote_permission(self):
        self.service.sync()
        self.assertFalse(self.service.status()["running"])
        with self.assertRaises(ValueError):
            self.link()
        self.start_listener.assert_not_called()
        self.cfg.set("remote_preview", "enabled", False, save=False)
        with self.assertRaises(ValueError):
            self.service.start_origin()
        self.start_listener.assert_not_called()
        self.cfg.set("remote_preview", "enabled", True, save=False)
        self.ready()
        self.assertTrue(self.service.status()["ready"])
        self.assertFalse(self.cfg.get("preview", "enabled"))

    def test_listener_is_fixed_loopback_and_requests_an_os_allocated_port(self):
        with mock.patch("nvr.preview._ViewerServer") as server_type, \
                mock.patch("nvr.preview.threading.Thread") as thread_type:
            server, thread = RemotePreviewService._start_listener(self.service, 8802)
        self.assertIs(server, server_type.return_value)
        self.assertIs(thread, thread_type.return_value)
        args = server_type.call_args.args
        self.assertEqual(args[:2], ("127.0.0.1", 0))
        self.assertIs(args[2], self.service.app)
        thread.start.assert_called_once_with()

    def test_start_failure_is_sanitized_and_does_not_leave_an_origin(self):
        self.start_listener.side_effect = OSError("fixture-private-provider-token")
        with self.assertRaises(ValueError) as raised:
            self.service.start_origin()
        self.assertFalse(self.service.status()["running"])
        self.assertNotIn("fixture-private", str(raised.exception))
        self.assertNotIn("fixture-private", "\n".join(self.logs))

    def test_permission_disabled_during_start_closes_new_listener(self):
        def permission_changes(port):
            self.cfg.set("remote_preview", "enabled", False, save=False)
            return self.listener, self.thread
        self.start_listener.side_effect = permission_changes
        with self.assertRaises(ValueError):
            self.service.start_origin()
        self.assertFalse(self.service.status()["running"])
        self.listener.shutdown.assert_called_once_with()
        self.listener.server_close.assert_called_once_with()

    def test_invalid_preference_closes_origin_and_cannot_create_a_link(self):
        self.ready()
        _, ticket = self.link()
        self.cfg.set("remote_preview", "enabled", "not-a-boolean", save=False)
        with self.assertRaises(ValueError):
            self.service.start_origin()
        self.assertFalse(self.service.status()["running"])
        self.denied(self.get("/frame/farm", ticket))
        self.start_listener.assert_called_once_with(0)

    def test_https_url_is_required_before_ticket_is_minted(self):
        self.service.start_origin()
        self.assertTrue(self.service.status()["running"])
        self.assertFalse(self.service.status()["ready"])
        with self.assertRaises(ValueError):
            self.link()
        self.assertEqual(len(self.service._tickets), 0)
        self.service.set_public_url(self.origin + "/")
        url, ticket = self.link()
        self.assertTrue(url.startswith(self.origin + "/preview#t="))
        self.assertEqual(urllib.parse.urlsplit(url).query, "")
        self.assertEqual(len(ticket), 43)
        self.assertNotIn(ticket, self.service._tickets)
        self.assertIn(hashlib.sha256(ticket.encode("ascii")).hexdigest(), self.service._tickets)
        self.assertNotIn("fixture-bot-secret", url)
        self.assertNotIn("fixture-only.mp4", url)

    def test_only_selected_camera_information_and_fresh_frame_are_available(self):
        self.ready()
        _, ticket = self.link()
        response = self.get("/info", ticket)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["camera_id"], "farm")
        self.assertNotIn("fixture-only.mp4", response.get_data(as_text=True))
        response = self.get("/frame/farm", ticket)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, self.jpeg)
        self.denied(self.get("/frame/coop", ticket))

    def test_remote_origin_rejects_every_non_loopback_source_and_spoofed_headers(self):
        self.ready()
        _, ticket = self.link()
        for address in ("192.168.1.20", "10.0.0.1", "172.19.0.1", "8.8.8.8",
                        "100.64.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1", ""):
            with self.subTest(address=address):
                self.denied(self.get("/frame/farm", ticket, address=address, headers={
                    "X-Forwarded-For": "127.0.0.1", "Forwarded": "for=127.0.0.1"}))
        self.assertEqual(self.get("/frame/farm", ticket).status_code, 200)

    def test_expired_missing_tampered_and_query_tickets_fail(self):
        self.ready()
        _, ticket = self.link()
        self.denied(self.get("/info"))
        self.denied(self.get("/info", "invalid-" + ticket))
        self.denied(self.get("/info?t=" + ticket))
        self.clock.return_value = 1599.0
        self.assertEqual(self.get("/frame/farm", ticket).status_code, 200)
        self.clock.return_value = 1600.0
        self.denied(self.get("/frame/farm", ticket))

    def test_url_change_revokes_tickets_but_normalized_same_url_preserves_them(self):
        self.ready()
        _, first = self.link()
        self.service.set_public_url(self.origin + "/")
        self.assertEqual(self.get("/frame/farm", first).status_code, 200)
        self.service.set_public_url("https://replacement.fixture.example")
        self.denied(self.get("/frame/farm", first))
        url, replacement = self.link()
        self.assertTrue(url.startswith("https://replacement.fixture.example/preview#t="))
        self.assertEqual(self.get("/frame/farm", replacement).status_code, 200)

    def test_invalid_url_does_not_replace_a_working_origin_or_echo_secrets(self):
        self.ready()
        _, ticket = self.link()
        with self.assertRaises(ValueError) as raised:
            self.service.set_public_url("https://private-fixture@viewer.fixture.example#t=secret")
        self.assertNotIn("private-fixture", str(raised.exception))
        self.assertEqual(self.service.status()["base_url"], self.origin)
        self.assertEqual(self.get("/frame/farm", ticket).status_code, 200)
        self.assertNotIn(ticket, "\n".join(self.logs))
        self.assertNotIn(self.origin, "\n".join(self.logs))

    def test_identity_or_permission_change_denies_before_controller_sync(self):
        cases = (("remote_preview", "enabled", False), ("clawbot", "enabled", False),
                 ("clawbot", "user_id", "different-owner"),
                 ("clawbot", "token", "different-session"),
                 ("clawbot", "allowed_users", ["new-fixture-user"]))
        for section, key, value in cases:
            with self.subTest(section=section, key=key):
                previous = self.cfg.get(section, key)
                self.ready()
                _, ticket = self.link()
                self.cfg.set(section, key, value, save=False)
                self.denied(self.get("/frame/farm", ticket))
                self.cfg.set(section, key, previous, save=False)
                self.service.stop()

    def test_camera_disable_or_source_change_revokes_access(self):
        self.ready()
        original = dict(self.cfg.get_camera("farm"))
        _, ticket = self.link()
        self.cfg.upsert_camera(dict(original, enabled=False))
        self.denied(self.get("/frame/farm", ticket))
        self.cfg.upsert_camera(original)
        _, ticket = self.link()
        self.cfg.upsert_camera(dict(original, url="replacement.mp4"))
        self.denied(self.get("/frame/farm", ticket))
        _, new_ticket = self.link()
        # Old runtime camera is still online while Hub asynchronously replaces it.
        self.assertEqual(self.get("/frame/farm", new_ticket).status_code, 503)
        self.cameras["farm"].url = "replacement.mp4"
        self.assertEqual(self.get("/frame/farm", new_ticket).data, self.jpeg)

    def test_stale_offline_and_connecting_frames_do_not_appear_live(self):
        self.ready()
        _, ticket = self.link()
        camera = self.cameras["farm"]
        for state, age in (("offline", 0), ("connecting", 0), ("online", 20)):
            with self.subTest(state=state, age=age):
                camera.state, camera.last_ts = state, time.time() - age
                response = self.get("/frame/farm", ticket)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(self.jpeg, response.data)

    def test_management_recording_and_write_routes_are_absent(self):
        self.ready()
        _, ticket = self.link()
        for path in ("/", "/api/settings", "/api/preview", "/api/remote-preview",
                     "/api/cameras", "/api/recordings", "/api/auth", "/api/system/quit",
                     "/live/farm", "/snap/farm"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, ticket).status_code, 404)
        for path in ("/preview", "/info", "/frame/farm"):
            response = self.client.post(path, json={}, base_url=self.origin,
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
                headers={"Authorization": "Bearer " + ticket})
            self.addCleanup(response.close)
            self.assertEqual(response.status_code, 405)

    def test_lan_and_remote_ticket_stores_are_separate(self):
        self.ready()
        _, remote_ticket = self.link()
        for key, value in (("enabled", True), ("address", "192.168.1.10"), ("port", 8803)):
            self.cfg.set("preview", key, value, save=False)
        lan = PreviewService(self.cfg, self.cameras.get)
        self.addCleanup(lan.stop)
        with mock.patch("nvr.preview._bindable_address", return_value=True), \
                mock.patch.object(lan, "_start_listener", return_value=(mock.Mock(), self.thread)):
            lan_link = lan.make_link("farm")
        lan_ticket = urllib.parse.parse_qs(urllib.parse.urlsplit(lan_link).fragment)["t"][0]
        self.denied(self.get("/frame/farm", lan_ticket))
        response = lan.app.test_client().get("/frame/farm",
            headers={"Authorization": "Bearer " + remote_ticket},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        self.addCleanup(response.close)
        self.denied(response)

    def test_stop_revokes_and_never_restarts_implicitly(self):
        self.ready()
        _, ticket = self.link()
        self.service.stop()
        self.denied(self.get("/frame/farm", ticket))
        self.service.sync()
        self.assertFalse(self.service.status()["running"])
        self.assertEqual(self.service.status()["base_url"], "")
        self.assertEqual(self.service.status()["port"], 0)
        with self.assertRaises(ValueError):
            self.link()
        with self.assertRaises(ValueError):
            self.service.set_public_url(self.origin)
        self.start_listener.assert_called_once_with(0)
        self.listener.shutdown.assert_called_once_with()
        self.listener.server_close.assert_called_once_with()
        self.ready()
        _, fresh = self.link()
        self.assertEqual(self.get("/frame/farm", fresh).status_code, 200)
        self.denied(self.get("/frame/farm", ticket))

    def test_dead_origin_or_disabled_preference_closes_and_revokes(self):
        self.ready()
        _, ticket = self.link()
        self.thread.is_alive.return_value = False
        self.assertFalse(self.service.status()["running"])
        self.denied(self.get("/frame/farm", ticket))
        self.assertEqual(self.service.status()["base_url"], "")
        self.start_listener.assert_called_once_with(0)
        self.thread.is_alive.return_value = True
        self.ready()
        _, ticket = self.link()
        self.cfg.set("remote_preview", "enabled", False, save=False)
        self.assertFalse(self.service.status()["running"])
        self.denied(self.get("/frame/farm", ticket))

    def test_remote_responses_retain_existing_sensitive_response_headers(self):
        self.ready()
        _, ticket = self.link()
        for path, supplied in (("/preview", None), ("/preview.js", None),
                               ("/info", ticket), ("/frame/farm", ticket), ("/info", None)):
            with self.subTest(path=path):
                response = self.get(path, supplied)
                self.assertIn("no-store", response.headers["Cache-Control"])
                self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
                self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
