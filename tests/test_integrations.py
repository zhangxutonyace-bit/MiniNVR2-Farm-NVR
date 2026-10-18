import base64
import datetime
from unittest import mock

from nvr import mailer
from nvr.clawbot import ClawBot, _aes_ecb_encrypt, _media_aes_key, _pkcs7_size
from tests.support import OfflineTestCase


class OfflineIntegrationTests(OfflineTestCase):
    def test_aes_padding_size_and_round_trip(self):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        key = bytes(range(16))
        for size in (0, 1, 15, 16, 17, 31, 32, 47, 255):
            with self.subTest(size=size):
                original = b"x" * size
                cipher = _aes_ecb_encrypt(original, key)
                self.assertEqual(_pkcs7_size(size), len(cipher))
                decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
                plain = decryptor.update(cipher) + decryptor.finalize()
                self.assertEqual(plain[:-plain[-1]], original)
        self.assertEqual(base64.b64decode(_media_aes_key(key.hex())).decode("ascii"), key.hex())

    def test_clawbot_video_protocol_uses_mock_transport_only(self):
        bot = ClawBot(self.cfg)
        fixture = self.root / "synthetic-video.mp4"
        fixture.write_bytes(b"synthetic-content" * 63)
        seen = {}

        def post(endpoint, body, **kwargs):
            seen[endpoint] = body
            if endpoint == "getuploadurl":
                return {"upload_param": "FAKE-UPLOAD"}
            return {"ret": 0}

        response = mock.MagicMock()
        response.__enter__.return_value.headers = {"x-encrypted-param": "FAKE-RECEIPT"}
        with mock.patch.object(bot, "_post", side_effect=post), \
             mock.patch("urllib.request.urlopen", return_value=response) as upload:
            self.assertTrue(bot.send_video("fixture-peer", "fixture-context", str(fixture)))
        request = upload.call_args.args[0]
        self.assertEqual(len(request.data), seen["getuploadurl"]["filesize"])
        message = seen["sendmessage"]["msg"]
        self.assertEqual(message["context_token"], "fixture-context")
        media = message["item_list"][0]["video_item"]["media"]
        self.assertEqual(media["encrypt_query_param"], "FAKE-RECEIPT")
        self.assertEqual(base64.b64decode(media["aes_key"]).decode("ascii"),
                         seen["getuploadurl"]["aeskey"])

    def test_clawbot_date_camera_selection_is_local(self):
        self.cfg.upsert_camera({"id": "sample", "name": "Farm", "url": "fixture.mp4"})
        bot = ClawBot(self.cfg)
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        self.assertEqual(bot._parse_day("昨天"), yesterday)
        self.assertEqual(bot._parse_day("2020-01-02"), "2020-01-02")
        self.assertEqual(bot._parse_cam("昨天 Farm")["id"], "sample")
        self.assertEqual(bot._parse_cam("1 号")["id"], "sample")

    def test_clawbot_ignores_unknown_peer_disabled_and_missing_context(self):
        self.cfg.set("clawbot", "enabled", True, save=False)
        self.cfg.set("clawbot", "user_id", "fixture-owner", save=False)
        bot = ClawBot(self.cfg)
        base = {"message_type": 1, "from_user_id": "fixture-owner",
                "context_token": "fixture-context",
                "item_list": [{"type": 1, "text_item": {"text": "昨天"}}]}
        for changes, enabled in (({"from_user_id": "stranger"}, True),
                                  ({"context_token": ""}, True), ({}, False)):
            with self.subTest(changes=changes, enabled=enabled):
                self.cfg.set("clawbot", "enabled", enabled, save=False)
                with mock.patch.object(bot, "_typing") as typing, \
                     mock.patch.object(bot, "_answer") as answer:
                    bot.on_message(dict(base, **changes))
                typing.assert_not_called()
                answer.assert_not_called()

    def test_clawbot_owner_request_reaches_answer_only_with_context(self):
        self.cfg.set("clawbot", "enabled", True, save=False)
        self.cfg.set("clawbot", "user_id", "fixture-owner", save=False)
        bot = ClawBot(self.cfg)
        with mock.patch.object(bot, "_typing"), mock.patch.object(bot, "_answer") as answer:
            bot.on_message({"message_type": 1, "from_user_id": "fixture-owner",
                            "context_token": "fixture-context",
                            "item_list": [{"type": 1, "text_item": {"text": "昨天"}}]})
        answer.assert_called_once_with("fixture-owner", "fixture-context", "昨天")

    def test_clawbot_unknown_text_does_not_send_recording(self):
        self.cfg.set("clawbot", "enabled", True, save=False)
        self.cfg.set("clawbot", "user_id", "fixture-owner", save=False)
        self.cfg.upsert_camera({"id": "sample", "name": "Farm", "url": "fixture.mp4"})
        bot = ClawBot(self.cfg)
        with mock.patch.object(bot, "_typing"), mock.patch.object(bot, "send_text"), \
             mock.patch.object(bot, "_timelapse") as timelapse, \
             mock.patch.object(bot, "send_video") as video:
            bot.on_message({"message_type": 1, "from_user_id": "fixture-owner",
                            "context_token": "fixture-context",
                            "item_list": [{"type": 1, "text_item": {"text": "随便聊聊天"}}]})
        timelapse.assert_not_called()
        video.assert_not_called()

    def test_smtp_starttls_failure_never_sends_credentials(self):
        for key, value in {"smtp_host": "smtp.example.invalid", "smtp_port": 587,
                           "ssl": False, "user": "sender@example.invalid", "pass": "fake",
                           "to": "recipient@example.invalid"}.items():
            self.cfg.set("mail", key, value, save=False)
        smtp = mock.MagicMock()
        smtp.starttls.side_effect = RuntimeError("TLS unavailable")
        with mock.patch("smtplib.SMTP", return_value=smtp):
            ok, message = mailer.send_mail(self.cfg, "Offline test", "No real mail", [])
        self.assertFalse(ok)
        smtp.login.assert_not_called()
        smtp.sendmail.assert_not_called()

    def test_unconfigured_mail_does_not_connect(self):
        with mock.patch("smtplib.SMTP") as smtp, mock.patch("smtplib.SMTP_SSL") as smtp_ssl:
            ok, _ = mailer.send_mail(self.cfg, "Offline test", "No real mail", [])
        self.assertFalse(ok)
        smtp.assert_not_called()
        smtp_ssl.assert_not_called()
