"""Windows transient sharing failures preserve atomic settings and bounded work."""
import json
from unittest import mock

from nvr import config
from tests.support import OfflineTestCase


def sharing_error(code=5):
    error = PermissionError(13, "controlled sharing conflict")
    error.winerror = code
    return error


class ConfigPersistenceTests(OfflineTestCase):
    def test_successful_atomic_replace_does_not_wait(self):
        with mock.patch.object(config.os, "replace") as replace, mock.patch.object(config.time, "sleep") as sleep:
            config._replace_config_file("source", "target")
        replace.assert_called_once_with("source", "target")
        sleep.assert_not_called()

    def test_only_windows_sharing_conflicts_receive_bounded_retry(self):
        for code in (5, 32):
            with self.subTest(code=code), mock.patch.object(config.os, "name", "nt"), \
                    mock.patch.object(config.os, "replace", side_effect=[sharing_error(code), sharing_error(code), None]) as replace, \
                    mock.patch.object(config.time, "sleep") as sleep:
                config._replace_config_file("source", "target")
                self.assertEqual(replace.call_count, 3)
                self.assertEqual(sleep.call_args_list, [mock.call(.02), mock.call(.04)])

    def test_persistent_failure_is_bounded_and_keeps_original_config(self):
        path = self.root / "config.json"
        before = path.read_bytes()
        self.cfg.set("site", "name", "not yet committed", save=False)
        with mock.patch.object(config.os, "name", "nt"), \
                mock.patch.object(config.os, "replace", side_effect=sharing_error()) as replace, \
                mock.patch.object(config.time, "sleep") as sleep:
            with self.assertRaises(PermissionError):
                self.cfg.save()
        self.assertEqual(replace.call_count, 4)
        self.assertEqual(sum(call.args[0] for call in sleep.call_args_list), .14)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(json.loads((self.root / "config.json.tmp").read_text(encoding="utf-8"))["site"]["name"],
                         "not yet committed")

    def test_transient_conflict_commits_whole_new_document_after_original_stays_readable(self):
        path = self.root / "config.json"
        before = path.read_bytes()
        native_replace = config.os.replace
        attempts = []

        def conflict_then_commit(source, target):
            attempts.append(1)
            if len(attempts) == 1:
                raise sharing_error()
            native_replace(source, target)

        with mock.patch.object(config.os, "name", "nt"), \
                mock.patch.object(config.os, "replace", side_effect=conflict_then_commit), \
                mock.patch.object(config.time, "sleep", side_effect=lambda delay: self.assertEqual(path.read_bytes(), before)):
            self.cfg.set("site", "name", "committed complete document")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["site"]["name"], "committed complete document")

    def test_other_errors_and_non_windows_fail_without_retry(self):
        for system, error in (("posix", sharing_error()), ("nt", sharing_error(13)),
                              ("nt", FileNotFoundError("controlled missing temp"))):
            with self.subTest(system=system, error=type(error).__name__), \
                    mock.patch.object(config.os, "name", system), \
                    mock.patch.object(config.os, "replace", side_effect=error) as replace, \
                    mock.patch.object(config.time, "sleep") as sleep:
                with self.assertRaises(type(error)):
                    config._replace_config_file("source", "target")
                replace.assert_called_once()
                sleep.assert_not_called()
