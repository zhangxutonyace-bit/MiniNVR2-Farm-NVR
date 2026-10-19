"""Local management and Hub policy tests; never use an account or real camera."""
import threading
from unittest import mock

from nvr.hub import Hub
from nvr.server import create_app
from tests.support import OfflineTestCase
from tests.test_api import FakeHub


class RemoteApiTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.hub = FakeHub()
        status = {"state": "stopped", "running": False, "installed": False,
                  "url": "", "error": "", "enabled": False}
        self.hub.remote_preview_status = mock.Mock(return_value=status)
        self.hub.start_remote_preview = mock.Mock(return_value=dict(status, state="starting"))
        self.hub.stop_remote_preview = mock.Mock(return_value=status)
        self.app = create_app(self.cfg, self.hub)
        self.app.testing = True
        self.client = self.app.test_client()

    def test_remote_start_requires_explicit_relay_confirmation(self):
        for value in ({}, {"confirm_relay": False}, {"confirm_relay": "true"}):
            response = self.client.post("/api/remote-preview/start", json=value)
            self.assertEqual(response.status_code, 400)
        self.hub.start_remote_preview.assert_not_called()
        response = self.client.post("/api/remote-preview/start", json={"confirm_relay": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["state"], "starting")
        self.hub.start_remote_preview.assert_called_once()

    def test_non_object_json_is_rejected_by_central_management_guard(self):
        for body in ('null', '[]', '"text"', '1'):
            response = self.client.post("/api/remote-preview/start", data=body,
                                        content_type="application/json")
            self.assertEqual(response.status_code, 400)
        self.hub.start_remote_preview.assert_not_called()

    def test_remote_controls_keep_management_auth_and_origin_guards(self):
        self.cfg.set("server", "token", "fixture-management-password", save=False)
        self.assertEqual(self.client.get("/api/remote-preview").status_code, 401)
        self.assertEqual(self.client.get("/api/remote-preview", headers={
            "Authorization": "Bearer fixture-viewer-ticket"}).status_code, 401)
        for path in ("start", "stop"):
            url = "/api/remote-preview/" + path
            self.assertEqual(self.client.post(url, json={"confirm_relay": True}).status_code, 401)
            self.assertEqual(self.client.post(url, json={"confirm_relay": True}, headers={
                "X-Token": "fixture-management-password", "Origin": "https://other.example"}).status_code, 403)
        self.hub.start_remote_preview.assert_not_called()
        self.hub.stop_remote_preview.assert_not_called()

    def test_invalid_remote_preferences_do_not_partially_write_settings(self):
        previous = self.cfg.get("site", "name")
        for value in (None, [], {}, {"enabled": 1}, {"enabled": "true"},
                      {"enabled": True, "port": 8802}):
            response = self.client.post("/api/settings", json={
                "site": {"name": "must-not-be-written"}, "remote_preview": value})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.cfg.get("site", "name"), previous)
            self.assertFalse(self.cfg.get("remote_preview", "enabled"))
        response = self.client.post("/api/settings", json={"remote_preview": {"enabled": True}})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.cfg.get("remote_preview", "enabled"))
        self.hub.start_remote_preview.assert_not_called()

    def test_remote_disabled_without_management_password_remains_loopback_only(self):
        response = self.client.get("/api/remote-preview", base_url="http://192.168.1.10:8802",
                                   environ_overrides={"REMOTE_ADDR": "192.168.1.20"})
        self.assertEqual(response.status_code, 403)
        self.hub.remote_preview_status.assert_not_called()


class RemoteHubTests(OfflineTestCase):
    def new_hub(self):
        with mock.patch.object(Hub, "_start_threads"), mock.patch.object(Hub, "_boot_clawbot"):
            hub = Hub(self.cfg, log=lambda *args: None)
        hub._threads = []
        self.addCleanup(hub.stop_all)
        return hub

    def test_remote_selection_never_falls_back_to_lan_when_not_ready(self):
        hub = self.new_hub()
        self.cfg.set("remote_preview", "enabled", True, save=False)
        hub.remote_preview = mock.Mock()
        hub.remote_preview.make_link.side_effect = ValueError("远程连接未就绪")
        hub.preview_service = mock.Mock()
        with self.assertRaisesRegex(ValueError, "远程"):
            hub.preview_link("sample")
        hub.preview_service.make_link.assert_not_called()
        hub.remote_preview.make_link.assert_called_once_with("sample")

    def test_remote_setting_and_clawbot_disable_stop_even_without_lan_service(self):
        hub = self.new_hub()
        hub.remote_preview = mock.Mock()
        self.cfg.set("remote_preview", "enabled", False, save=False)
        hub.sync_preview()
        hub.remote_preview.stop.assert_called_once()
        hub.remote_preview.reset_mock()
        self.cfg.set("remote_preview", "enabled", True, save=False)
        self.cfg.set("clawbot", "enabled", False, save=False)
        hub.sync_preview()
        hub.remote_preview.stop.assert_called_once()

    def test_saved_preference_does_not_autostart_on_application_boot(self):
        self.cfg.set("remote_preview", "enabled", True, save=False)
        hub = self.new_hub()
        self.assertIsNone(hub.remote_preview)
        hub.remote_preview = mock.Mock()
        hub.remote_preview.status.return_value = {"state": "stopped", "running": False}
        hub.remote_preview_status()
        hub.remote_preview.start.assert_not_called()

    def test_identity_revocation_and_quit_affect_both_listeners(self):
        hub = self.new_hub()
        hub.remote_preview, hub.preview_service = mock.Mock(), mock.Mock()
        hub.revoke_preview()
        hub.remote_preview.revoke.assert_called_once()
        hub.preview_service.revoke.assert_called_once()
        hub.stop_all()
        hub.remote_preview.stop.assert_called_once()
        hub.preview_service.stop.assert_called_once()
        with self.assertRaises(ValueError):
            hub.start_remote_preview()
        hub.remote_preview.start.assert_not_called()

    def test_quit_serializes_an_inflight_remote_start(self):
        hub = self.new_hub()
        self.cfg.set("remote_preview", "enabled", True, save=False)
        self.cfg.set("clawbot", "enabled", True, save=False)
        entered, release = threading.Event(), threading.Event()
        service = mock.Mock()
        service.start.side_effect = lambda: (entered.set(), release.wait(3))
        hub.remote_preview = service
        errors = []

        def start():
            try:
                hub.start_remote_preview()
            except ValueError as exc:
                errors.append(str(exc))

        starter = threading.Thread(target=start)
        quitter = threading.Thread(target=hub.stop_all)
        starter.start()
        try:
            self.assertTrue(entered.wait(2))
            quitter.start()
            self.assertTrue(hub._stop.wait(2))
        finally:
            release.set()
            starter.join(3)
            if quitter.ident is not None:
                quitter.join(3)
        self.assertFalse(starter.is_alive())
        self.assertFalse(quitter.is_alive())
        self.assertTrue(errors)
        self.assertGreaterEqual(service.stop.call_count, 1)
