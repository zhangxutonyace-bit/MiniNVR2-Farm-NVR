"""Offline release-gate checks; fixtures are synthetic, never acceptance receipts."""
import hashlib
import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from scripts import package_windows_release as release


class WindowsReleaseTests(unittest.TestCase):
    digest = "a" * 64
    size = 1234

    def remote_receipt(self):
        return {"version": "2.3.0", "exe_sha256": self.digest, "exe_bytes": self.size,
                "verified_date": "2026-10-19", "mode": "installed-windows-exe",
                "verified_command": "预览 外网测试画面",
                **{key: True for key in release.REMOTE_PHONE_CHECKS}}

    def event_receipt(self):
        return {"artifact_type": "Windows executable", "version": "2.4.0",
                "exe_sha256": self.digest, "exe_bytes": self.size,
                "implementation_commit": "c" * 40,
                "recorded_at_utc": "2026-10-19T12:05:35.346380+00:00",
                "camera_source_type": "local-camera", "scope": release.EVENT_USER_SCOPE,
                **{key: True for key in release.EVENT_USER_CHECKS}}

    def object_receipt(self):
        return {**self.event_receipt(), "version": "3.0.0", "model_id": "yolox-s",
                "confidence": .35, "targets": ["bottle", "cup"], "object_class": "cup",
                **{key: True for key in release.OBJECT_USER_CHECKS}}

    def live_preview_receipt(self):
        return {**self.event_receipt(), "version": "3.1.0", "model_id": "yolox-s",
                "confidence": .35, "targets": ["bottle", "cup"], "object_class": "cup",
                "scope": release.LIVE_PREVIEW_USER_SCOPE,
                **{key: True for key in release.LIVE_PREVIEW_USER_CHECKS}}

    def test_live_preview_acceptance_keeps_desktop_scope_without_phone_or_event_claims(self):
        record = self.live_preview_receipt()
        record.update(answer="private conversation", private_account="secret",
                      phone_live_preview="PASS", event_playback="PASS")
        public = release.validate_live_preview_user(record, "3.1.0", self.digest, self.size)
        self.assertEqual(public["status"], "PASS_USER_CONFIRMED")
        self.assertEqual(public["object_class"], "cup")
        self.assertEqual(public["targets"], ["bottle", "cup"])
        self.assertEqual(public["phone_live_preview"], "NOT_RETESTED")
        self.assertEqual(public["event_playback"], "NOT_RETESTED")
        self.assertNotIn("answer", public)
        self.assertNotIn("private_account", public)
        self.assertNotIn("wechat_video_playback_user_confirmed", public)

    def test_live_preview_acceptance_rejects_wrong_exe_scope_and_incomplete_claims(self):
        bad_fields = [("exe_sha256", "b" * 64), ("exe_bytes", self.size + 1),
                      ("exe_bytes", str(self.size)), ("version", "3.0.0"),
                      ("artifact_type", "source"), ("implementation_commit", "abc"),
                      ("camera_source_type", "rtsp"), ("scope", release.EVENT_USER_SCOPE),
                      ("targets", []), ("targets", ["cup", "cup"]),
                      ("object_class", "dog"), ("model_id", "unapproved"),
                      ("confidence", True), ("confidence", float("nan")),
                      ("recorded_at_utc", "2026-10-19T12:00:00+08:00"),
                      ("recorded_at_utc", "2026-02-30T12:00:00Z")]
        bad_fields += [(key, value) for key in release.LIVE_PREVIEW_USER_CHECKS for value in (False, 1, "true")]
        for key, value in bad_fields:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                release.validate_live_preview_user({**self.live_preview_receipt(), key: value},
                                                   "3.1.0", self.digest, self.size)
        receipt = self.live_preview_receipt()
        for key in tuple(receipt):
            if key in release.EVENT_USER_CHECKS:
                continue
            with self.subTest(missing=key), self.assertRaises(ValueError):
                release.validate_live_preview_user({k: v for k, v in receipt.items() if k != key},
                                                   "3.1.0", self.digest, self.size)

    def test_object_acceptance_does_not_claim_person_or_phone_label_confirmation(self):
        record = self.object_receipt()
        for key in release.EVENT_USER_CHECKS:
            if key not in release.OBJECT_USER_CHECKS:
                record.pop(key)
        record.update(private_account="secret", private_path="private", phone_object_label="PASS")
        public = release.validate_object_user(record, "3.0.0", self.digest, self.size)
        self.assertEqual(public["object_class"], "cup")
        self.assertEqual(public["phone_object_label"], "NOT_VERIFIED")
        self.assertNotIn("box_follows_person_user_confirmed", public)
        self.assertNotIn("private_account", public)
        self.assertNotIn("private_path", public)

    def test_object_acceptance_rejects_wrong_exe_model_class_or_incomplete_confirmation(self):
        bad_fields = [("exe_sha256", "b" * 64), ("exe_bytes", str(self.size)),
                      ("version", "2.4.0"), ("artifact_type", "source"),
                      ("implementation_commit", "abc"), ("camera_source_type", "rtsp"),
                      ("scope", "all scenes"), ("model_id", "unapproved"),
                      ("targets", []), ("targets", ["cup", "cup"]),
                      ("targets", ["unknown"]), ("object_class", "person"),
                      ("confidence", True), ("confidence", float("nan")),
                      ("recorded_at_utc", "2026-10-19T12:00:00+08:00")]
        bad_fields += [(key, value) for key in release.OBJECT_USER_CHECKS for value in (False, 1, "true")]
        for key, value in bad_fields:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                release.validate_object_user({**self.object_receipt(), key: value}, "3.0.0", self.digest, self.size)
        for key in self.object_receipt():
            if key in release.EVENT_USER_CHECKS and key not in release.OBJECT_USER_CHECKS:
                continue
            with self.subTest(missing=key), self.assertRaises(ValueError):
                receipt = self.object_receipt()
                receipt.pop(key)
                release.validate_object_user(receipt, "3.0.0", self.digest, self.size)

    def test_local_event_acceptance_is_exact_and_public_scope_is_whitelisted(self):
        record = self.event_receipt()
        record.update(user_response="private chat text", private_account="private account",
                      question_reference="private reference", extra={"secret": "private secret"})
        public = release.validate_event_user(record, "2.4.0", self.digest, self.size)
        self.assertEqual(public["status"], "PASS_USER_CONFIRMED")
        self.assertEqual(public["verified_date"], "2026-10-19")
        self.assertEqual(public["implementation_commit"], "c" * 40)
        self.assertEqual(public["camera_source_type"], "local-camera")
        self.assertIn("one fresh event", public["scope"])
        self.assertIn("no RTSP", public["scope"])
        for key in ("user_response", "private_account", "question_reference", "extra"):
            self.assertNotIn(key, public)
        for key in release.EVENT_USER_CHECKS:
            self.assertIs(public[key], True)

    def test_local_event_receipt_rejects_missing_false_and_nonboolean_claims(self):
        for key in release.EVENT_USER_CHECKS:
            for bad in (None, False, "true", 1):
                with self.subTest(key=key, value=bad):
                    receipt = self.event_receipt()
                    if bad is None:
                        receipt.pop(key)
                    else:
                        receipt[key] = bad
                    with self.assertRaises(ValueError):
                        release.validate_event_user(receipt, "2.4.0", self.digest, self.size)

    def test_local_event_receipt_rejects_wrong_identity_and_scope(self):
        for key, bad in (("artifact_type", "source diagnostic"), ("version", "2.3.0"),
                         ("exe_sha256", "b" * 64), ("exe_bytes", self.size + 1),
                         ("exe_bytes", str(self.size)), ("exe_bytes", True),
                         ("implementation_commit", "C" * 40), ("implementation_commit", "c" * 39),
                         ("implementation_commit", ["c" * 40]),
                         ("camera_source_type", "rtsp"), ("camera_source_type", None),
                         ("scope", "all cameras and all WeChat accounts"), ("scope", None)):
            with self.subTest(key=key, value=bad):
                receipt = self.event_receipt()
                receipt[key] = bad
                with self.assertRaises(ValueError):
                    release.validate_event_user(receipt, "2.4.0", self.digest, self.size)
        for key in self.event_receipt():
            with self.subTest(missing=key):
                receipt = self.event_receipt()
                receipt.pop(key)
                with self.assertRaises(ValueError):
                    release.validate_event_user(receipt, "2.4.0", self.digest, self.size)

    def test_local_event_receipt_requires_valid_utc_timestamp(self):
        for bad in (None, 20261019, "2026-10-19", "2026-10-19T12:05:35",
                    "2026-02-30T12:05:35+00:00", "2026-10-19T25:05:35Z",
                    "2026-10-19T12:05:35+08:00", "2026-10-19T12:05:35Z\n"):
            with self.subTest(timestamp=bad):
                receipt = self.event_receipt()
                receipt["recorded_at_utc"] = bad
                with self.assertRaises(ValueError):
                    release.validate_event_user(receipt, "2.4.0", self.digest, self.size)
        receipt = self.event_receipt()
        receipt["recorded_at_utc"] = "2026-10-19T12:05:35Z"
        self.assertEqual(release.validate_event_user(receipt, "2.4.0", self.digest, self.size)["verified_date"],
                         "2026-10-19")

    def test_release_cli_keeps_legacy_phone_gate_and_requires_current_event_acceptance(self):
        for version, args, message in (
                ("2.3.0", ["--preview-record", "preview.json"], "--phone-record"),
                ("2.4.0", ["--preview-record", "preview.json"], "--event-user-record"),
                ("3.0.0", ["--preview-record", "preview.json"], "--object-user-record"),
                ("3.1.0", ["--preview-record", "preview.json"], "--live-preview-user-record"),
                ("2.4.0", ["--event-user-record", "event.json"], "--preview-record")):
            with self.subTest(version=version, args=args), \
                    mock.patch.object(release.sys, "argv", ["package_windows_release.py", "--version", version, *args]), \
                    mock.patch.object(release.sys, "version_info", (3, 12, 10)):
                with self.assertRaisesRegex(ValueError, message):
                    release.main()

    def test_new_release_optional_phone_receipt_still_requires_exact_remote_acceptance(self):
        record = {**self.remote_receipt(), "version": "2.4.0"}
        self.assertEqual(release.validate_phone(record, "2.4.0", self.digest, self.size)["status"],
                         "PASS_USER_CONFIRMED")
        for key, bad in (("version", "2.3.0"), ("exe_sha256", "b" * 64),
                         ("exe_bytes", self.size + 1), ("phone_direct_click_verified", False)):
            with self.subTest(key=key, value=bad):
                receipt = {**record, key: bad}
                with self.assertRaises(ValueError):
                    release.validate_phone(receipt, "2.4.0", self.digest, self.size)

    def test_final_remote_receipt_has_precise_public_scope(self):
        receipt = self.remote_receipt()
        receipt.update(private_account="not public", capability="not public")
        public = release.validate_phone(receipt, "2.3.0", self.digest, self.size)
        self.assertEqual(public["command"], "预览 外网测试画面")
        self.assertTrue(public["synthetic_only"])
        self.assertIn("mobile data", public["scope"])
        self.assertIn("WeChat's embedded browser", public["scope"])
        self.assertNotIn("same LAN", public["scope"])
        self.assertNotIn("private_account", public)
        self.assertNotIn("capability", public)

    def test_remote_receipt_rejects_missing_false_or_string_true_claims(self):
        for key in release.REMOTE_PHONE_CHECKS:
            for bad in (None, False, "true", 1):
                with self.subTest(key=key, value=bad):
                    receipt = self.remote_receipt()
                    if bad is None:
                        receipt.pop(key)
                    else:
                        receipt[key] = bad
                    with self.assertRaises(ValueError):
                        release.validate_phone(receipt, "2.3.0", self.digest, self.size)

    def test_remote_receipt_rejects_wrong_exe_and_mode(self):
        for key, bad in (("exe_sha256", "b" * 64), ("exe_bytes", self.size + 1),
                         ("exe_bytes", str(self.size)), ("exe_bytes", True),
                         ("mode", "source-diagnostic"), ("version", "2.2.3")):
            with self.subTest(key=key, value=bad):
                receipt = self.remote_receipt()
                receipt[key] = bad
                with self.assertRaises(ValueError):
                    release.validate_phone(receipt, "2.3.0", self.digest, self.size)
        with self.assertRaises(ValueError):
            release.validate_phone(self.remote_receipt(), "2.3.0", self.digest)

    def test_remote_receipt_rejects_invalid_command_and_date(self):
        for key, bad in (("verified_command", "今天事件"), ("verified_command", "预览 "),
                         ("verified_command", "预览 农场\n"), ("verified_command", "预览 农\u200b场"),
                         ("verified_command", "预览 " + "场" * 126),
                         ("verified_date", "2026-02-30"), ("verified_date", "2026-10-19T10:00")):
            with self.subTest(key=key, value=bad):
                receipt = self.remote_receipt()
                receipt[key] = bad
                with self.assertRaises(ValueError):
                    release.validate_phone(receipt, "2.3.0", self.digest, self.size)

    def test_remote_keeps_receipt_date_and_named_camera_command(self):
        receipt = self.remote_receipt()
        receipt.update(verified_date="2026-10-20", verified_command="预览 合成验收摄像头")
        public = release.validate_phone(receipt, "2.3.0", self.digest, self.size)
        self.assertEqual(public["verified_date"], receipt["verified_date"])
        self.assertEqual(public["command"], receipt["verified_command"])

    def test_legacy_lan_phone_receipt_unchanged(self):
        for command in ("预览", "预览1号", "预览12号"):
            with self.subTest(command=command):
                receipt = {"version": "2.2.3", "exe_sha256": self.digest,
                           "verified_date": "2026-10-19", "verified_command": command,
                           "phone_on_final_exe_verified": True}
                public = release.validate_phone(receipt, "2.2.3", self.digest)
                self.assertEqual(public, {"status": "PASS_USER_CONFIRMED",
                                         "verified_date": "2026-10-19", "command": command,
                                         "scope": "One user-confirmed phone on the same LAN; continuously updating preview from this release EXE"})

    def test_remote_requires_default_off_startup_check(self):
        record = {"version": "2.3.0", "exe_sha256": self.digest, "exe_bytes": self.size,
                  **{key: "PASS" for key in release.STARTUP_CHECKS}}
        checks = release.STARTUP_CHECKS + ("remote_preview_default_off",)
        with self.assertRaises(ValueError):
            release.validate_automatic(record, "2.3.0", checks, "startup", self.digest, self.size)
        record["remote_preview_default_off"] = "PASS"
        public = release.validate_automatic(record, "2.3.0", checks, "startup", self.digest, self.size)
        self.assertEqual(public["remote_preview_default_off"], "PASS")

    def test_duplicate_receipt_fields_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_text('{"synthetic_only": false, "synthetic_only": true}', encoding="utf-8")
            with self.assertRaises(ValueError):
                release.read_record(path)

    def test_remote_and_legacy_packages_keep_correct_guides_and_evidence(self):
        for version in ("2.2.3", "2.3.0", "2.4.0", "3.0.0", "3.1.0"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                repo = Path(directory) / "synthetic-repo"
                (repo / "dist").mkdir(parents=True)
                (repo / "docs").mkdir()
                (repo / "validation-local").mkdir()
                executable = bytearray(70)
                executable[:2] = b"MZ"
                struct.pack_into("<I", executable, 0x3C, 64)
                executable[64:] = b"PE\0\0\x64\x86"
                (repo / "dist" / "MiniNVR2.exe").write_bytes(executable)
                digest = hashlib.sha256(executable).hexdigest()
                for name in ("WINDOWS_DOWNLOAD.md", "WECHAT_QUICK_START.txt",
                             "LAN_PREVIEW_QUICK_START.txt", "REMOTE_PREVIEW.md",
                             "LOCAL_CAMERA_QUICK_START.md", "ALGORITHM_STORE_QUICK_START.md",
                             "LIVE_PREVIEW_DETECTION.md"):
                    (repo / "docs" / name).write_text("合成操作说明\n第二行\n", encoding="utf-8")
                identity = {"version": version, "exe_sha256": digest, "exe_bytes": len(executable)}
                records = {"build": {**identity, "mobile_and_status_assets_match_source": True,
                                     "preview_module_included": True,
                                     "live_overlay_module_included": True},
                           "startup": {**identity, **{key: "PASS" for key in release.STARTUP_CHECKS}},
                           "event": {**identity, **{key: "PASS" for key in release.EVENT_CHECKS}},
                           "preview": {**identity, **{key: "PASS" for key in release.PREVIEW_CHECKS}}}
                if version >= "2.3.0":
                    records["startup"]["remote_preview_default_off"] = "PASS"
                    phone = {**self.remote_receipt(), **identity}
                else:
                    phone = {**identity, "phone_on_final_exe_verified": True,
                             "verified_date": "2026-10-19", "verified_command": "预览1号"}
                if version == "3.1.0":
                    records["live-preview-user"] = {**self.live_preview_receipt(), **identity,
                                                    "private_account": "secret"}
                elif version == "3.0.0":
                    records["object-user"] = {**self.object_receipt(), **identity,
                                              "private_account": "secret"}
                elif version == "2.4.0":
                    records["event-user"] = {**self.event_receipt(), **identity,
                                             "user_response": "private chat text"}
                else:
                    records["phone"] = phone
                argv = ["package_windows_release.py", "--version", version,
                        "--output-dir", str(repo / "output")]
                if version in ("3.0.0", "3.1.0"):
                    alternate = repo / "accepted.exe"
                    alternate.write_bytes(executable)
                    (repo / "dist" / "MiniNVR2.exe").write_bytes(b"preserved old build")
                    argv.extend(["--executable", str(alternate)])
                for label, record in records.items():
                    path = repo / "validation-local" / (label + ".json")
                    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
                    argv.extend(["--" + label + "-record", str(path)])

                def synthetic_notices(root, unused_inputs):
                    notices = root / "THIRD_PARTY_NOTICES"
                    notices.mkdir()
                    (notices / "LICENSE.txt").write_text("Synthetic license fixture\n", encoding="utf-8")
                    (root / "THIRD_PARTY_SOURCES.md").write_text("Synthetic source fixture\n", encoding="utf-8")
                    return []

                with mock.patch.object(release, "REPO", repo), \
                        mock.patch.object(release.sys, "argv", argv), \
                        mock.patch.object(release.sys, "version_info", (3, 12, 10)), \
                        mock.patch.object(release, "collect_notices", synthetic_notices), \
                        mock.patch.object(release, "validate_bundled_person_assets", return_value={"synthetic": True}), \
                        mock.patch.object(release, "validate_bundled_algorithm_assets", return_value={"synthetic": True}), \
                        mock.patch.object(release, "validate_bundled_live_preview_assets", return_value={"synthetic": True}), \
                        mock.patch.object(release, "commit_id", return_value="c" * 40), \
                        mock.patch.object(release.metadata, "version", return_value="6.16.0"), \
                        mock.patch.object(release.subprocess, "run", side_effect=AssertionError("No process should run")), \
                        mock.patch("builtins.print"):
                    self.assertEqual(release.main(), 0)
                stem = "MiniNVR2-v" + version + "-windows-x64"
                with zipfile.ZipFile(repo / "output" / (stem + ".zip")) as archive:
                    manifest = json.loads(archive.read(stem + "/RELEASE_MANIFEST.json"))
                    verification = manifest["verification"]
                    self.assertEqual(archive.read(stem + "/MiniNVR2.exe"), bytes(executable))
                    self.assertIn(stem + "/THIRD_PARTY_NOTICES/LICENSE.txt", archive.namelist())
                    self.assertIn(stem + "/THIRD_PARTY_SOURCES.md", archive.namelist())
                    for guide in manifest["user_guides"]:
                        content = archive.read(stem + "/" + guide)
                        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
                        self.assertNotIn(b"\n", content.replace(b"\r\n", b""))
                    if version >= "2.3.0":
                        self.assertIn("微信远程预览-操作步骤.txt", manifest["user_guides"])
                        self.assertNotIn("real_phone_lan_preview", verification)
                        self.assertEqual(verification["existing_local_exe_startup"]["remote_preview_default_off"], "PASS")
                        if version == "3.1.0":
                            self.assertEqual(len(manifest["user_guides"]), 7)
                            self.assertIn("实时预览识别框-操作步骤.txt", manifest["user_guides"])
                            preview = verification["real_desktop_live_preview"]
                            self.assertEqual(preview["status"], "PASS_USER_CONFIRMED")
                            self.assertEqual(preview["exe_sha256"], digest)
                            self.assertEqual(preview["phone_live_preview"], "NOT_RETESTED")
                            self.assertEqual(preview["event_playback"], "NOT_RETESTED")
                            self.assertEqual(verification["real_wechat_event_delivery"]["status"], "NOT_RETESTED")
                            self.assertEqual(verification["real_object_following"]["status"], "NOT_RETESTED")
                            self.assertEqual(verification["real_phone_remote_preview"]["status"], "NOT_RETESTED")
                            self.assertEqual(verification["real_camera"]["status"], "PASS_USER_CONFIRMED")
                            self.assertNotIn("secret", json.dumps(manifest))
                        elif version == "3.0.0":
                            self.assertEqual(len(manifest["user_guides"]), 6)
                            self.assertEqual(verification["real_phone_remote_preview"]["status"], "NOT_RETESTED")
                            self.assertEqual(verification["real_person_following"]["status"], "NOT_VERIFIED")
                            self.assertEqual(verification["real_object_following"]["object_class"], "cup")
                            self.assertEqual(verification["real_object_following"]["phone_object_label"], "NOT_VERIFIED")
                            self.assertEqual(verification["real_wechat_event_delivery"]["status"], "PASS_USER_CONFIRMED")
                            self.assertNotIn("secret", json.dumps(manifest))
                        elif version == "2.4.0":
                            self.assertEqual(len(manifest["user_guides"]), 5)
                            self.assertIn("电脑内置USB摄像头-操作步骤.txt", manifest["user_guides"])
                            self.assertEqual(verification["real_phone_remote_preview"]["status"], "NOT_RETESTED")
                            self.assertNotIn("PASS", json.dumps(verification["real_phone_remote_preview"]))
                            for key in ("real_camera", "real_wechat_event_delivery", "real_person_following"):
                                self.assertEqual(verification[key]["status"], "PASS_USER_CONFIRMED")
                                self.assertEqual(verification[key]["exe_sha256"], digest)
                                self.assertEqual(verification[key]["exe_bytes"], len(executable))
                            self.assertEqual(verification["real_camera"]["source_type"], "local-camera")
                            self.assertIn("RTSP not verified", verification["real_camera"]["scope"])
                            delivery = verification["real_wechat_event_delivery"]
                            self.assertEqual(set(delivery), {"status", "verified_date", "implementation_commit",
                                                             "exe_sha256", "exe_bytes", "scope"})
                            self.assertIn("One current WeChat account", delivery["scope"])
                            self.assertNotIn("phone", delivery["scope"].lower())
                            self.assertNotIn("private chat text", json.dumps(manifest))
                        else:
                            self.assertEqual(len(manifest["user_guides"]), 4)
                            self.assertTrue(verification["real_phone_remote_preview"]["synthetic_only"])
                            self.assertEqual(verification["real_camera"], "NOT_VERIFIED")
                            self.assertEqual(verification["real_wechat_event_delivery"]["status"], "NOT_VERIFIED")
                    else:
                        self.assertNotIn("微信远程预览-操作步骤.txt", manifest["user_guides"])
                        self.assertEqual(len(manifest["user_guides"]), 3)
                        self.assertIn("real_phone_lan_preview", verification)
                        self.assertNotIn("real_phone_remote_preview", verification)
                self.assertEqual((repo / "dist" / "MiniNVR2.exe").read_bytes(),
                                 b"preserved old build" if version in ("3.0.0", "3.1.0") else bytes(executable))

    def test_person_assets_require_exact_model_and_notices_inside_executable(self):
        from nvr.person_localizer import MODEL_NAME, verify_model
        assets = {"models/" + MODEL_NAME: verify_model().read_bytes()}
        for name in ("LICENSE", "SOURCE.json"):
            assets["third_party/NanoDet/" + name] = (release.REPO / "third_party" / "NanoDet" / name).read_bytes()
        archive = mock.Mock()
        archive.toc = dict.fromkeys(assets)
        archive.extract.side_effect = lambda name: assets[name]
        with mock.patch("PyInstaller.archive.readers.CArchiveReader", return_value=archive):
            result = release.validate_bundled_person_assets(Path("synthetic.exe"))
            self.assertTrue(result["notices_in_exe"])
            original = assets["models/" + MODEL_NAME]
            assets["models/" + MODEL_NAME] = b"x" + original[1:]
            with self.assertRaises(ValueError):
                release.validate_bundled_person_assets(Path("synthetic.exe"))
            archive.toc.pop("models/" + MODEL_NAME)
            with self.assertRaises(ValueError):
                release.validate_bundled_person_assets(Path("synthetic.exe"))

    def test_v3_catalog_notices_and_adapter_modules_are_verified_without_launch(self):
        from nvr.algorithm_catalog import verify_catalog_asset
        from nvr.event_labels import FONT_NAME
        verify_catalog_asset()
        paths = ("third_party/AlgorithmCatalog/catalog.json", "third_party/YOLOX/LICENSE",
                 "third_party/YOLOX/SOURCE.json", "fonts/" + FONT_NAME,
                 "third_party/LabelFont/OFL.txt", "third_party/LabelFont/NOTICE.txt",
                 "third_party/LabelFont/SOURCE.json")
        assets = {name: (release.REPO / name).read_bytes() for name in paths}
        archive = mock.Mock()
        archive.toc = dict.fromkeys(assets)
        archive.extract.side_effect = lambda name: assets[name]
        modules = ("nvr.algorithm_catalog", "nvr.algorithm_types", "nvr.object_models",
                   "nvr.model_store", "nvr.algorithm_runtime", "nvr.event_labels")
        archive.open_embedded_archive.return_value.toc = dict.fromkeys(modules)
        with mock.patch("PyInstaller.archive.readers.CArchiveReader", return_value=archive):
            result = release.validate_bundled_algorithm_assets(Path("synthetic.exe"))
            self.assertEqual(len(result["catalog"]["models"]), 3)
            self.assertEqual(len(result["catalog"]["models"][0]["class_map"]), 80)
            archive.open_embedded_archive.return_value.toc.pop("nvr.object_models")
            with self.assertRaises(ValueError):
                release.validate_bundled_algorithm_assets(Path("synthetic.exe"))
            archive.open_embedded_archive.return_value.toc = dict.fromkeys(modules)
            assets[paths[0]] += b"drift"
            with self.assertRaises(ValueError):
                release.validate_bundled_algorithm_assets(Path("synthetic.exe"))

    def test_v3_package_includes_readable_algorithm_guide(self):
        with tempfile.TemporaryDirectory() as directory:
            names = release.copy_user_guides(Path(directory), "3.0.0")
            self.assertEqual(len(names), 6)
            text = (Path(directory) / "算法商城-操作步骤.txt").read_text(encoding="utf-8-sig")
            self.assertIn("结束当前事件并保留片段", text)

    def test_v31_live_preview_assets_require_exact_controls_and_worker(self):
        paths = ("web/index.html", "web/app.js", "web/preview.html", "web/preview.js")
        assets = {name: (release.REPO / name).read_bytes() for name in paths}
        archive = mock.Mock()
        archive.toc = dict.fromkeys(assets)
        archive.extract.side_effect = lambda name: assets[name]
        archive.open_embedded_archive.return_value.toc = {"nvr.live_overlay": None}
        with mock.patch("PyInstaller.archive.readers.CArchiveReader", return_value=archive):
            self.assertTrue(release.validate_bundled_live_preview_assets(Path("synthetic.exe"))["worker_in_exe"])
            archive.open_embedded_archive.return_value.toc = {}
            with self.assertRaises(ValueError):
                release.validate_bundled_live_preview_assets(Path("synthetic.exe"))
            archive.open_embedded_archive.return_value.toc = {"nvr.live_overlay": None}
            assets["web/app.js"] += b"drift"
            with self.assertRaises(ValueError):
                release.validate_bundled_live_preview_assets(Path("synthetic.exe"))

    def test_v31_build_requires_live_preview_worker_evidence(self):
        record = {"version": "3.1.0", "exe_sha256": self.digest, "exe_bytes": self.size,
                  "mobile_and_status_assets_match_source": True, "preview_module_included": True}
        for value in (None, False, 1, "true"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                release.validate_build({**record, "live_overlay_module_included": value},
                                       "3.1.0", self.digest, self.size)
        release.validate_build({**record, "live_overlay_module_included": True},
                               "3.1.0", self.digest, self.size)

    def test_v31_package_includes_readable_live_preview_guide(self):
        with tempfile.TemporaryDirectory() as directory:
            names = release.copy_user_guides(Path(directory), "3.1.0")
            self.assertEqual(len(names), 7)
            path = Path(directory) / "实时预览识别框-操作步骤.txt"
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertIn("实时显示识别框和标签", path.read_text(encoding="utf-8-sig"))

    def test_packaging_refuses_to_overwrite_existing_release_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            archive = output / "MiniNVR2-v3.1.0-windows-x64.zip"
            archive.write_bytes(b"preserved published package")
            argv = ["package_windows_release.py", "--version", "3.1.0", "--output-dir", str(output),
                    "--preview-record", "unused-preview.json", "--live-preview-user-record", "unused-user.json"]
            with mock.patch.object(release.sys, "argv", argv), \
                    mock.patch.object(release.sys, "version_info", (3, 12, 10)), \
                    self.assertRaisesRegex(ValueError, "Refuse to overwrite"):
                release.main()
            self.assertEqual(archive.read_bytes(), b"preserved published package")


if __name__ == "__main__":
    unittest.main()
