import threading
import time
from types import SimpleNamespace
from unittest import mock

from nvr.camera import Cam
from nvr.hub import Hub
from tests.support import OfflineTestCase


class LifecycleTests(OfflineTestCase):
    def test_connecting_without_first_frame_is_stopped_by_watchdog(self):
        self.cfg.set("stream", "timeout_sec", 0.1, save=False)
        camera = Cam({"id": "sample", "url": "unused-local-fixture.mp4"}, self.cfg)
        camera.state = "connecting"
        camera.proc = object()
        camera._connect_t0 = time.time() - 1
        with mock.patch.object(camera, "_close_proc", side_effect=camera._stop_event.set) as close:
            camera._watchdog()
        close.assert_called_once()
        self.assertTrue(camera.last_error)

    def new_idle_hub(self):
        with mock.patch.object(Hub, "_start_threads"), mock.patch.object(Hub, "_boot_clawbot"):
            hub = Hub(self.cfg, log=lambda *args: None)
        hub._threads = []
        return hub

    def test_disabling_detection_stops_current_recording_immediately(self):
        self.cfg.set("detect", "enabled", False, save=False)
        self.cfg.set("detect", "interval", 0.7, save=False)
        hub = self.new_idle_hub()
        camera = SimpleNamespace(person=True, hit=4, last_event_end=0,
                                 recording=mock.Mock(return_value=True), stop_record=mock.Mock())
        hub.cams = {"sample": camera}
        hub._stop = mock.Mock()
        hub._stop.is_set.return_value = False
        hub._stop.wait.return_value = True
        hub._detect_loop()
        self.assertFalse(camera.person)
        self.assertEqual(camera.hit, 0)
        camera.stop_record.assert_called_once()
        hub._stop.wait.assert_called_once_with(0.7)

    def test_stale_frame_does_not_extend_person_recording_forever(self):
        hub = self.new_idle_hub()
        camera = SimpleNamespace(person=True, hit=4, detect_on=True, state="online", seq=9,
                                 _last_detection_seq=9, last_person=time.time() - 100,
                                 last_event_end=0, frame_bgr=mock.Mock(return_value=object()),
                                 ensure_detector=mock.Mock(), stop_record=mock.Mock())
        hub.cams = {"sample": camera}
        hub._stop = mock.Mock()
        hub._stop.is_set.return_value = False
        hub._stop.wait.return_value = True
        hub._detect_loop()
        self.assertFalse(camera.person)
        self.assertEqual(camera.hit, 0)
        camera.stop_record.assert_called_once()
        camera.ensure_detector.assert_not_called()

    def test_scan_cancel_finishes_before_new_scan_and_can_restart(self):
        hub = self.new_idle_hub()
        entered = threading.Event()
        release = threading.Event()

        def simulated_scan(*args, **kwargs):
            entered.set()
            release.wait(2)
            self.assertTrue(kwargs["cancel"]())
            return []

        try:
            with mock.patch("nvr.discovery.scan", side_effect=simulated_scan) as scanner:
                self.assertTrue(hub.start_scan("192.0.2.1", users="fixture", pwds="fixture"))
                self.assertTrue(entered.wait(2))
                hub.stop_scan()
                self.assertTrue(hub.scan_state["running"])
                self.assertFalse(hub.start_scan("192.0.2.1", users="fixture", pwds="fixture"))
                release.set()
                hub._scan_thread.join(3)
                self.assertFalse(hub._scan_thread.is_alive())
                self.assertFalse(hub.scan_state["running"])
                scanner.assert_called_once()
            with mock.patch("nvr.discovery.scan", return_value=[]) as scanner:
                self.assertTrue(hub.start_scan("192.0.2.1", users="fixture", pwds="fixture"))
                hub._scan_thread.join(3)
                self.assertFalse(hub._scan_thread.is_alive())
                self.assertFalse(hub.scan_state["running"])
                scanner.assert_called_once()
        finally:
            release.set()
            hub.stop_all()
