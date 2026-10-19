"""Offline ClawBot and LAN preview isolation tests; no live camera or WeChat."""
import time
import urllib.parse
from types import SimpleNamespace
from unittest import mock

from nvr.clawbot import ClawBot
from tests.support import OfflineTestCase


class ClawBotPreviewTests(OfflineTestCase):
    owner = "preview-fixture-owner"
    context = "preview-fixture-context"
    link = "http://192.168.1.10:8803/preview#t=fixture-only-ticket"

    def setUp(self):
        super().setUp()
        self.cfg.set("clawbot", "enabled", True, save=False)
        self.cfg.set("clawbot", "user_id", self.owner, save=False)
        self.cfg.set("clawbot", "allowed_users", ["preview-fixture-helper"], save=False)
        for cid, name, enabled in (("farm", "农场", True), ("coop", "鸡舍", True),
                                    ("disabled", "停用", False)):
            self.cfg.upsert_camera({"id": cid, "name": name, "url": "fixture.mp4",
                                    "enabled": enabled})
        self.hub = SimpleNamespace(preview_link=mock.Mock(return_value=self.link))
        self.bot = ClawBot(self.cfg, hub=self.hub)
        self.texts = mock.Mock(return_value={"ret": 0})
        self.bot.send_text = self.texts
        self.bot._typing = mock.Mock()
        self.bot.send_video = mock.Mock()
        self.bot._timelapse = mock.Mock()

    def request(self, text, peer=None, ctx=None, **changes):
        msg = {"message_type": 1, "from_user_id": peer or self.owner,
               "context_token": self.context if ctx is None else ctx,
               "item_list": [{"type": 1, "text_item": {"text": text}}]}
        msg.update(changes)
        self.bot.on_message(msg)

    def text(self):
        return "\n".join(str(call.args[2]) for call in self.texts.call_args_list)

    def reset(self):
        self.hub.preview_link.reset_mock()
        self.texts.reset_mock()
        self.bot.send_video.reset_mock()
        self.bot._timelapse.reset_mock()

    def test_preview_commands_select_one_camera_and_send_standalone_link(self):
        cases = (("预览1号", "farm"), ("预览 农场", "farm"),
                 ("预览 摄像头1", "farm"), ("实时预览 鸡舍", "coop"),
                 ("查看预览2号", "coop"), ("预览 2", "coop"))
        for command, camera_id in cases:
            with self.subTest(command=command):
                self.reset()
                self.request(command)
                self.hub.preview_link.assert_called_once_with(camera_id)
                self.texts.assert_called_once_with(self.owner, self.context,
                                                    "[打开实时预览](%s)" % self.link)
                self.bot.send_video.assert_not_called()
                self.bot._timelapse.assert_not_called()

    def test_unspecified_preview_lists_multiple_cameras_without_link(self):
        self.request("预览")
        self.hub.preview_link.assert_not_called()
        self.assertIn("农场", self.text())
        self.assertIn("鸡舍", self.text())
        self.assertNotIn("停用", self.text())

    def test_unspecified_preview_uses_only_enabled_camera(self):
        self.cfg.remove_camera("coop")
        self.request("预览")
        self.hub.preview_link.assert_called_once_with("farm")
        self.assertIn(self.link, self.text())

    def test_preview_without_enabled_camera_sends_explanation(self):
        self.cfg.remove_camera("farm")
        self.cfg.remove_camera("coop")
        self.request("预览")
        self.hub.preview_link.assert_not_called()
        self.assertTrue(self.text())

    def test_invalid_preview_selector_never_falls_back_to_other_camera(self):
        for command in ("预览99号", "预览0号", "预览 不存在", "预览 停用",
                        "预览 农场 鸡舍", "预览1号 2号", "预览 农场 删除",
                        "预览 今天", "预览第一号", "预览-1号"):
            with self.subTest(command=command):
                self.reset()
                self.request(command)
                self.hub.preview_link.assert_not_called()
                self.bot.send_video.assert_not_called()
                self.bot._timelapse.assert_not_called()
                self.assertTrue(self.text())

    def test_preview_authorization_requires_enabled_owner_or_allowlist_and_context(self):
        for changes, enabled in (({"peer": "stranger"}, True), ({"ctx": ""}, True),
                                  ({}, False)):
            with self.subTest(changes=changes, enabled=enabled):
                self.reset()
                self.cfg.set("clawbot", "enabled", enabled, save=False)
                self.request("预览1号", **changes)
                self.hub.preview_link.assert_not_called()
                self.texts.assert_not_called()
        self.cfg.set("clawbot", "enabled", True, save=False)
        self.request("预览1号", peer="preview-fixture-helper")
        self.hub.preview_link.assert_called_once_with("farm")

    def test_preview_unavailable_returns_explanation_without_url_or_video(self):
        self.hub.preview_link.side_effect = ValueError("请在设置中启用局域网预览")
        self.request("预览1号")
        self.assertIn("启用局域网预览", self.text())
        self.assertNotIn(self.link, self.text())
        self.bot.send_video.assert_not_called()

    def test_remote_reply_has_one_explicit_link_with_unmodified_https_target(self):
        self.cfg.set("remote_preview", "enabled", True, save=False)
        link = "https://fixture.example/preview#t=fixture-only-ticket"
        self.hub.preview_link.return_value = link
        self.request("预览1号")
        self.hub.preview_link.assert_called_once_with("farm")
        self.texts.assert_called_once_with(self.owner, self.context, "[打开实时预览](%s)" % link)

    def test_explicit_link_preserves_url_safe_ticket_punctuation(self):
        self.cfg.set("remote_preview", "enabled", True, save=False)
        for ticket in ("_" + "a" * 41 + "_", "__" + "-" * 40 + "_", "a" * 42 + "-"):
            with self.subTest(ticket=ticket):
                self.reset()
                link = "https://fixture.example/preview#t=" + ticket
                self.hub.preview_link.return_value = link
                self.request("预览1号")
                self.texts.assert_called_once_with(self.owner, self.context,
                                                    "[打开实时预览](%s)" % link)

    def test_remote_failure_explains_recovery_without_returning_a_lan_link(self):
        self.cfg.set("remote_preview", "enabled", True, save=False)
        self.hub.preview_link.side_effect = ValueError("远程预览尚未就绪，请在电脑设置中点击启动")
        self.request("预览1号")
        self.assertIn("远程预览尚未就绪", self.text())
        self.assertNotIn("http://", self.text())
        self.bot.send_video.assert_not_called()


class ClawBotTextReceiptTests(OfflineTestCase):
    """Exercise the real text sender without contacting WeChat."""

    secret = "fixturesecret"

    def setUp(self):
        super().setUp()
        self.owner = self.secret + "-owner"
        self.context = self.secret + "-context"
        self.cfg.set("clawbot", "enabled", True, save=False)
        self.cfg.set("clawbot", "user_id", self.owner, save=False)
        self.cfg.set("clawbot", "token", self.secret + "-bot-token", save=False)
        self.cfg.upsert_camera({"id": "farm", "name": self.secret + "-camera",
                                "url": "fixture.mp4", "enabled": True})
        self.link = "http://192.168.1.10:8803/preview#t=" + self.secret
        self.logs = []
        self.bot = ClawBot(self.cfg,
                           hub=SimpleNamespace(preview_link=mock.Mock(return_value=self.link)),
                           log=self.logs.append)
        self.bot._typing = mock.Mock()

    def request(self, command="预览1号"):
        self.bot.on_message({"message_type": 1, "from_user_id": self.owner,
                             "context_token": self.context,
                             "item_list": [{"type": 1, "text_item": {"text": command}}]})

    def assert_private_values_not_logged(self):
        output = "\n".join(self.logs)
        self.assertNotIn(self.secret, output)
        self.assertNotIn(self.link, output)
        self.assertNotIn(self.owner, output)
        self.assertNotIn(self.context, output)

    def test_preview_accepted_and_omitted_zero_receipts_are_logged(self):
        for result in ({}, {"ret": 0}, {"errcode": "0"},
                       {"ret": "0", "errcode": 0, "errmsg": self.secret}):
            with self.subTest(result=result):
                self.logs.clear()
                with mock.patch.object(self.bot, "_post", return_value=result) as post:
                    self.request()
                post.assert_called_once()
                self.assertEqual(post.call_args.args[0], "sendmessage")
                sent = post.call_args.args[1]["msg"]
                self.assertEqual(sent["context_token"], self.context)
                self.assertEqual(sent["item_list"], [{"type": 1, "text_item": {
                    "text": "[打开实时预览](%s)" % self.link}}])
                self.assertIn("微信接口已接受文字回复（不代表手机已送达）", self.logs[-1])
                self.assert_private_values_not_logged()

    def test_preview_rejected_and_malformed_receipts_are_logged_without_secrets(self):
        cases = (({"ret": 7, "errmsg": self.secret}, "ret=7，errcode=invalid"),
                 ({"ret": 0, "errcode": "-14", "errmsg": self.link}, "ret=0，errcode=-14"),
                 ({"ret": None}, "ret=invalid，errcode=invalid"),
                 ({"ret": False}, "ret=invalid，errcode=invalid"),
                 ({"ret": self.secret}, "ret=invalid，errcode=invalid"),
                 ({"ret": "7\n" + self.secret}, "ret=invalid，errcode=invalid"),
                 ({"errcode": "１２"}, "ret=invalid，errcode=invalid"),
                 ({"ret": 2147483648}, "ret=invalid，errcode=invalid"),
                 ({"errcode": "1" * 100}, "ret=invalid，errcode=invalid"),
                 ([], "ret=invalid，errcode=invalid"),
                 (None, "ret=invalid，errcode=invalid"),
                 (self.secret, "ret=invalid，errcode=invalid"))
        for result, codes in cases:
            with self.subTest(result=result):
                self.logs.clear()
                with mock.patch.object(self.bot, "_post", return_value=result) as post:
                    self.request()
                post.assert_called_once()
                self.assertIn("微信接口未接受文字回复（" + codes + "）", self.logs[-1])
                self.assertNotIn("微信接口已接受", "\n".join(self.logs))
                self.assertEqual(self.bot.state, "idle")
                self.assertIn("微信文字回复发送失败", self.bot.error)
                self.assert_private_values_not_logged()

    def test_text_sender_preserves_receipt_object_for_event_pagination(self):
        for result in ({"ret": 0}, {"ret": 7}, {"errcode": None}, []):
            with self.subTest(result=result):
                with mock.patch.object(self.bot, "_post", return_value=result):
                    self.assertIs(self.bot.send_text(self.owner, self.context, self.secret), result)
                self.assert_private_values_not_logged()

    def test_all_text_commands_share_receipt_logging(self):
        for command in ("帮助", "列表", "预览", "预览99号", "预览1号"):
            with self.subTest(command=command):
                self.logs.clear()
                with mock.patch.object(self.bot, "_post", return_value={"errcode": 9}) as post:
                    self.request(command)
                post.assert_called_once()
                self.assertIn("微信接口未接受文字回复", self.logs[-1])
                self.assert_private_values_not_logged()

    def test_non_json_http_response_is_a_visible_text_rejection(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = ("not JSON " + self.secret).encode()
        with mock.patch("urllib.request.urlopen", return_value=response) as transport:
            self.request()
        transport.assert_called_once()
        self.assertIn("微信接口未接受文字回复（ret=-1，errcode=invalid）", self.logs[-1])
        self.assert_private_values_not_logged()

    def test_text_transport_exception_logs_type_and_propagates(self):
        failure = OSError(self.link + " " + self.context + " " + self.secret)
        with mock.patch.object(self.bot, "_post", side_effect=failure) as post:
            with self.assertRaises(OSError) as raised:
                self.request()
        self.assertIs(raised.exception, failure)
        post.assert_called_once()
        self.assertIn("文字回复发送异常（OSError）", self.logs[-1])
        self.assertNotIn("微信接口已接受", "\n".join(self.logs))
        self.assertIn("微信文字回复发送失败", self.bot.error)
        self.assert_private_values_not_logged()

    def test_success_clears_only_prior_text_reply_error(self):
        with mock.patch.object(self.bot, "_post", return_value={"ret": 7}):
            self.request()
        self.assertTrue(self.bot.error)
        with mock.patch.object(self.bot, "_post", return_value={"ret": 0}):
            self.request()
        self.assertEqual(self.bot.error, "")
        self.bot.error = "unrelated fixture error"
        with mock.patch.object(self.bot, "_post", return_value={}):
            self.request()
        self.assertEqual(self.bot.error, "unrelated fixture error")

    def test_polling_handler_does_not_log_exception_message(self):
        received = []

        def post(endpoint, body, **kwargs):
            if endpoint == "getupdates":
                if received:
                    self.bot.stop()
                    return {"ret": 0}
                received.append(True)
                return {"ret": 0, "msgs": [{"message_type": 1,
                    "from_user_id": self.owner, "context_token": self.context,
                    "item_list": [{"type": 1, "text_item": {"text": "预览1号"}}]}]}
            raise OSError(self.link + " " + self.context + " " + self.secret)

        with mock.patch.object(self.bot, "_post", side_effect=post) as transport:
            self.bot.run()
        self.assertEqual(transport.call_count, 3)
        self.assertIn("处理消息异常（OSError）", "\n".join(self.logs))
        self.assert_private_values_not_logged()


class PreviewServiceTests(OfflineTestCase):
    """Use the real Flask routes with a mocked listener and isolated config."""

    def setUp(self):
        super().setUp()
        from nvr.preview import PreviewService
        from nvr.server import placeholder

        for section, key, value in (("preview", "enabled", True),
                                    ("preview", "address", "192.168.1.10"),
                                    ("preview", "port", 8803),
                                    ("clawbot", "enabled", True),
                                    ("clawbot", "user_id", "fixture-owner"),
                                    ("clawbot", "token", "fixture-bot-token")):
            self.cfg.set(section, key, value, save=False)
        self.jpeg = placeholder("fixture-live-frame")
        self.cameras = {}
        for cid, name in (("farm", "农场"), ("coop", "鸡舍")):
            self.cfg.upsert_camera({"id": cid, "name": name, "url": "fixture.mp4",
                                    "enabled": True})
            self.cameras[cid] = SimpleNamespace(id=cid, name=name, url="fixture.mp4", enabled=True,
                state="online", last_ts=time.time(), last_error="",
                snapshot=lambda: (self.jpeg, 1))
        clock_patch = mock.patch("nvr.preview.time.monotonic", return_value=1000.0)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)
        bind_patch = mock.patch("nvr.preview._bindable_address", return_value=True)
        self.bindable = bind_patch.start()
        self.addCleanup(bind_patch.stop)
        self.service = PreviewService(self.cfg, self.cameras.get)
        self.listener = mock.Mock()
        self.thread = mock.Mock()
        self.thread.is_alive.return_value = True
        listener_patch = mock.patch.object(self.service, "_start_listener",
                return_value=(self.listener, self.thread))
        self.start_listener = listener_patch.start()
        self.addCleanup(listener_patch.stop)
        self.addCleanup(self.service.stop)
        self.service.app.testing = True
        self.client = self.service.app.test_client()

    def link(self, cid="farm"):
        url = self.service.make_link(cid)
        parsed = urllib.parse.urlsplit(url)
        ticket = urllib.parse.parse_qs(parsed.fragment)["t"][0]
        return url, ticket

    def get(self, path, ticket=None, address="192.168.1.20", **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        if ticket is not None:
            headers["Authorization"] = "Bearer " + ticket
        response = self.client.get(path, headers=headers,
            environ_overrides={"REMOTE_ADDR": address},
            base_url="http://192.168.1.10:8803", **kwargs)
        self.addCleanup(response.close)
        return response

    def assert_denied(self, response):
        self.assertIn(response.status_code, (401, 403, 410))
        self.assertNotEqual(response.mimetype, "image/jpeg")
        self.assertNotIn(self.jpeg, response.data)

    def test_link_has_only_private_address_and_fragment_ticket(self):
        url, ticket = self.link()
        parsed = urllib.parse.urlsplit(url)
        self.assertEqual(parsed.scheme, "http")
        self.assertEqual(parsed.netloc, "192.168.1.10:8803")
        self.assertEqual(parsed.path, "/preview")
        self.assertEqual(parsed.query, "")
        self.assertGreaterEqual(len(ticket), 32)
        self.assertNotIn("fixture-bot-token", url)
        self.assertNotIn("fixture.mp4", url)

    def test_ticket_reads_only_selected_camera_info_and_fresh_jpeg(self):
        _, ticket = self.link()
        response = self.get("/info", ticket)
        self.assertEqual(response.status_code, 200)
        info = response.get_json()
        self.assertEqual(info["camera_id"], "farm")
        self.assertEqual(info["name"], "农场")
        self.assertEqual(info["state"], "online")
        self.assertGreater(info["expires_in"], 0)
        self.assertLessEqual(info["expires_in"], 600)
        response = self.get("/frame/farm", ticket)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertEqual(response.data, self.jpeg)
        self.assert_denied(self.get("/frame/coop", ticket))

    def test_missing_tampered_and_query_string_tickets_are_rejected(self):
        _, ticket = self.link()
        for path in ("/info", "/frame/farm"):
            with self.subTest(path=path):
                self.assert_denied(self.get(path))
                self.assert_denied(self.get(path, "invalid-" + ticket))
                self.assert_denied(self.get(path + "?t=" + urllib.parse.quote(ticket)))

    def test_ticket_expires_at_boundary_for_info_and_frame(self):
        _, ticket = self.link()
        self.clock.return_value = 1599.0
        self.assertEqual(self.get("/frame/farm", ticket).status_code, 200)
        self.clock.return_value = 1600.0
        self.assert_denied(self.get("/info", ticket))
        self.assert_denied(self.get("/frame/farm", ticket))

    def test_revocation_immediately_stops_existing_page_frame_requests(self):
        _, ticket = self.link()
        self.service.revoke()
        self.assert_denied(self.get("/info", ticket))
        self.assert_denied(self.get("/frame/farm", ticket))

    def test_disable_or_owner_change_invalidates_existing_ticket_without_sync(self):
        for section, key, value in (("preview", "enabled", False),
                                    ("preview", "address", "192.168.1.11"),
                                    ("preview", "port", 8804),
                                    ("clawbot", "enabled", False),
                                    ("clawbot", "user_id", "different-owner"),
                                    ("clawbot", "token", "different-session")):
            old = self.cfg.get(section, key)
            with self.subTest(section=section, key=key):
                _, ticket = self.link()
                self.cfg.set(section, key, value, save=False)
                self.assert_denied(self.get("/frame/farm", ticket))
            self.cfg.set(section, key, old, save=False)

    def test_deleted_or_disabled_camera_cannot_keep_streaming(self):
        _, ticket = self.link()
        spec = self.cfg.get_camera("farm")
        self.cfg.upsert_camera(dict(spec, enabled=False))
        self.assert_denied(self.get("/frame/farm", ticket))
        self.cfg.upsert_camera(dict(spec, enabled=True))
        _, ticket = self.link()
        self.cfg.remove_camera("farm")
        self.assert_denied(self.get("/frame/farm", ticket))

    def test_offline_connecting_stale_and_missing_frames_are_not_real_time(self):
        _, ticket = self.link()
        camera = self.cameras["farm"]
        for state, age, jpeg in (("offline", 0, self.jpeg),
                                 ("connecting", 0, self.jpeg),
                                 ("online", 3600, self.jpeg),
                                 ("online", 0, b"")):
            with self.subTest(state=state, age=age, has_frame=bool(jpeg)):
                camera.state = state
                camera.last_ts = time.time() - age
                camera.snapshot = lambda data=jpeg: (data, 1)
                response = self.get("/frame/farm", ticket)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.mimetype, "application/json")
                self.assertNotIn(self.jpeg, response.data)

    def test_new_source_ticket_waits_for_matching_camera_worker(self):
        _, previous_ticket = self.link()
        previous = dict(self.cfg.get_camera("farm"))
        self.cfg.upsert_camera(dict(previous, url="replacement-fixture.mp4"))
        _, ticket = self.link()
        self.assert_denied(self.get("/frame/farm", previous_ticket))
        # Hub still holds the old, online worker until its async sync completes.
        response = self.get("/frame/farm", ticket)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.mimetype, "application/json")
        self.assertNotIn(self.jpeg, response.data)
        self.assertEqual(self.get("/info", ticket).get_json()["state"], "connecting")
        self.cameras["farm"].url = "replacement-fixture.mp4"
        self.cameras["farm"].last_ts = time.time()
        # The new ticket remains usable when the replacement worker is ready.
        self.assertEqual(self.get("/frame/farm", ticket).data, self.jpeg)

    def test_camera_worker_without_matching_identity_is_not_trusted(self):
        _, ticket = self.link()
        camera = self.cameras["farm"]
        missing = object()
        for field, value in (("id", "coop"), ("url", "another-source.mp4"),
                              ("id", missing), ("url", missing)):
            original = getattr(camera, field)
            with self.subTest(field=field, missing=value is missing):
                if value is missing:
                    delattr(camera, field)
                else:
                    setattr(camera, field, value)
                response = self.get("/frame/farm", ticket)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(self.jpeg, response.data)
                self.assertEqual(self.get("/info", ticket).get_json()["state"], "connecting")
            setattr(camera, field, original)
        self.assertEqual(self.get("/frame/farm", ticket).data, self.jpeg)

    def test_public_peer_and_spoofed_forwarding_headers_are_rejected(self):
        _, ticket = self.link()
        for address in ("8.8.8.8", "100.64.0.1", "169.254.1.2", "192.0.2.1", "::ffff:8.8.8.8"):
            with self.subTest(address=address):
                response = self.get("/frame/farm", ticket, address=address,
                    headers={"X-Forwarded-For": "192.168.1.20", "Forwarded": "for=127.0.0.1"})
                self.assert_denied(response)
        for address in ("10.0.0.2", "172.16.0.2", "192.168.2.2", "127.0.0.1"):
            with self.subTest(address=address):
                self.assertEqual(self.get("/frame/farm", ticket, address=address).status_code, 200)

    def test_preview_listener_exposes_no_management_or_recording_routes(self):
        _, ticket = self.link()
        for path in ("/", "/api/settings", "/api/status", "/api/recordings", "/api/auth",
                     "/api/cameras", "/live/farm", "/snap/farm", "/api/system/quit"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, ticket).status_code, 404)

    def test_sensitive_preview_responses_disable_cache_and_referrers(self):
        _, ticket = self.link()
        for path, supplied in (("/preview", None), ("/preview.js", None),
                                ("/info", ticket), ("/frame/farm", ticket),
                                ("/frame/farm", "invalid")):
            with self.subTest(path=path, authorized=supplied == ticket):
                response = self.get(path, supplied)
                self.assertIn("no-store", response.headers.get("Cache-Control", ""))
                self.assertEqual(response.headers.get("Referrer-Policy"), "no-referrer")
                self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
                self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")

    def test_ticket_cache_has_fixed_upper_bound_and_evicts_oldest(self):
        from nvr.preview import MAX_TICKETS
        _, oldest = self.link()
        latest = oldest
        for _ in range(MAX_TICKETS):
            _, latest = self.link()
        self.assertLessEqual(len(self.service._tickets), MAX_TICKETS)
        self.assert_denied(self.get("/frame/farm", oldest))
        self.assertEqual(self.get("/frame/farm", latest).status_code, 200)

    def test_stop_revokes_tickets_and_stops_listener(self):
        _, ticket = self.link()
        self.service.stop()
        self.assert_denied(self.get("/frame/farm", ticket))
        self.listener.shutdown.assert_called()
        self.listener.server_close.assert_called()

    def test_normal_stop_and_settings_toggle_can_start_preview_again(self):
        _, first_ticket = self.link()
        self.service.stop()
        _, second_ticket = self.link()
        self.assert_denied(self.get("/frame/farm", first_ticket))
        self.assertEqual(self.get("/frame/farm", second_ticket).status_code, 200)
        self.cfg.set("preview", "enabled", False, save=False)
        self.service.sync()
        self.assert_denied(self.get("/frame/farm", second_ticket))
        self.cfg.set("preview", "enabled", True, save=False)
        _, third_ticket = self.link()
        self.assertEqual(self.get("/frame/farm", third_ticket).status_code, 200)
        self.assertEqual(self.start_listener.call_count, 3)

    def test_public_or_nonlocal_address_never_creates_link(self):
        for address in ("8.8.8.8", "example.invalid", "127.0.0.1", "169.254.1.2", "192.0.2.1"):
            with self.subTest(address=address):
                self.cfg.set("preview", "address", address, save=False)
                with self.assertRaises(ValueError):
                    self.service.make_link("farm")
        self.cfg.set("preview", "address", "192.168.1.10", save=False)
        self.bindable.return_value = False
        with self.assertRaises(ValueError):
            self.service.make_link("farm")

    def test_disabled_feature_unknown_camera_and_bad_port_do_not_issue_ticket(self):
        self.cfg.set("preview", "enabled", False, save=False)
        with self.assertRaises(ValueError):
            self.service.make_link("farm")
        self.cfg.set("preview", "enabled", True, save=False)
        with self.assertRaises(ValueError):
            self.service.make_link("missing-camera")
        for port in (0, -1, 65536, "not-a-port", True):
            with self.subTest(port=port):
                self.cfg.set("preview", "port", port, save=False)
                with self.assertRaises(ValueError):
                    self.service.make_link("farm")

    def test_preview_routes_are_read_only(self):
        _, ticket = self.link()
        for path in ("/info", "/frame/farm", "/preview"):
            with self.subTest(path=path):
                response = self.client.post(path, json={},
                    headers={"Authorization": "Bearer " + ticket},
                    environ_overrides={"REMOTE_ADDR": "192.168.1.20"},
                    base_url="http://192.168.1.10:8803")
                self.addCleanup(response.close)
                self.assertEqual(response.status_code, 405)

