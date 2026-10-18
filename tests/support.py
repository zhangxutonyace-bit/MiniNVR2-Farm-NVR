import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from nvr.config import Config


class OfflineTestCase(unittest.TestCase):
    """Never load the user's config or allow Python to open a connection."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mininvr-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = Config(str(self.root / "config.json"))
        for section, key, value in (
            ("server", "host", "127.0.0.1"),
            ("server", "token", ""),
            ("record", "root", str(self.root / "records")),
            ("mail", "enabled", False),
            ("clawbot", "enabled", False),
            ("clawbot", "auto_start", False),
            ("clawbot", "token", ""),
            ("stream", "auto_pick", False),
        ):
            self.cfg.set(section, key, value, save=False)
        self.cfg.save()
        guard = mock.patch("socket.socket.connect",
                           side_effect=AssertionError("Network is forbidden in offline tests"))
        guard.start()
        self.addCleanup(guard.stop)

    def wait_until(self, predicate, timeout=8, description="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("Timed out waiting for " + description)
