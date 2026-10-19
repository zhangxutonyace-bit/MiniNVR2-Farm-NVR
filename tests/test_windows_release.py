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
        for version in ("2.2.3", "2.3.0"):
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
                             "LAN_PREVIEW_QUICK_START.txt", "REMOTE_PREVIEW.md"):
                    (repo / "docs" / name).write_text("合成操作说明\n第二行\n", encoding="utf-8")
                identity = {"version": version, "exe_sha256": digest, "exe_bytes": len(executable)}
                records = {"build": {**identity, "mobile_and_status_assets_match_source": True,
                                     "preview_module_included": True},
                           "startup": {**identity, **{key: "PASS" for key in release.STARTUP_CHECKS}},
                           "event": {**identity, **{key: "PASS" for key in release.EVENT_CHECKS}},
                           "preview": {**identity, **{key: "PASS" for key in release.PREVIEW_CHECKS}}}
                if version == "2.3.0":
                    records["startup"]["remote_preview_default_off"] = "PASS"
                    phone = {**self.remote_receipt(), **identity}
                else:
                    phone = {**identity, "phone_on_final_exe_verified": True,
                             "verified_date": "2026-10-19", "verified_command": "预览1号"}
                records["phone"] = phone
                argv = ["package_windows_release.py", "--version", version,
                        "--output-dir", str(repo / "output")]
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
                    if version == "2.3.0":
                        self.assertIn("微信远程预览-操作步骤.txt", manifest["user_guides"])
                        self.assertEqual(len(manifest["user_guides"]), 4)
                        self.assertNotIn("real_phone_lan_preview", verification)
                        self.assertTrue(verification["real_phone_remote_preview"]["synthetic_only"])
                        self.assertEqual(verification["existing_local_exe_startup"]["remote_preview_default_off"], "PASS")
                    else:
                        self.assertNotIn("微信远程预览-操作步骤.txt", manifest["user_guides"])
                        self.assertEqual(len(manifest["user_guides"]), 3)
                        self.assertIn("real_phone_lan_preview", verification)
                        self.assertNotIn("real_phone_remote_preview", verification)
                self.assertEqual((repo / "dist" / "MiniNVR2.exe").read_bytes(), bytes(executable))

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


if __name__ == "__main__":
    unittest.main()
