import threading
import time
from types import SimpleNamespace
from unittest import mock

from nvr.camera import Cam
from nvr.hub import Hub
from nvr.event_overlay import FramePacket
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
                                 last_event_end=0, frame_sample=mock.Mock(return_value=(object(), FramePacket(b"", 9, 0, ()))),
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

    def test_preview_cannot_restart_while_shutdown_waits_for_camera(self):
        hub = self.new_idle_hub()
        entered = threading.Event()
        release = threading.Event()
        service = mock.Mock()
        service.running = True
        service.stop.side_effect = lambda: setattr(service, "running", False)
        service.make_link.side_effect = lambda cid: setattr(service, "running", True)
        service.sync.side_effect = lambda: setattr(service, "running", True)
        hub.preview_service = service

        def stop_camera():
            entered.set()
            release.wait(3)

        hub.cams["sample"] = SimpleNamespace(stop=stop_camera)
        shutdown = threading.Thread(target=hub.stop_all)
        shutdown.start()
        try:
            self.assertTrue(entered.wait(2), "shutdown did not reach the camera barrier")
            self.assertTrue(hub._stop.is_set())
            with self.assertRaises(ValueError):
                hub.preview_link("sample")
            hub.sync_preview()
            service.make_link.assert_not_called()
            service.sync.assert_not_called()
        finally:
            release.set()
            shutdown.join(3)
        self.assertFalse(shutdown.is_alive())
        self.assertFalse(service.running)

    def test_shutdown_serializes_inflight_preview_start_and_clears_listener(self):
        for operation in ("make_link", "sync"):
            with self.subTest(operation=operation):
                hub = self.new_idle_hub()
                entered = threading.Event()
                release = threading.Event()
                service = mock.Mock()
                service.running = False
                service.stop.side_effect = lambda: setattr(service, "running", False)
                hub.preview_service = service
                failures = []

                def start_listener(*args):
                    entered.set()
                    release.wait(3)
                    service.running = True
                    return "fixture-preview-link"

                getattr(service, operation).side_effect = start_listener

                def request_preview():
                    try:
                        if operation == "make_link":
                            hub.preview_link("sample")
                        else:
                            hub.sync_preview()
                    except Exception as exc:
                        failures.append(exc)

                request = threading.Thread(target=request_preview)
                shutdown = threading.Thread(target=hub.stop_all)
                request.start()
                try:
                    self.assertTrue(entered.wait(2), "preview did not reach the start barrier")
                    shutdown.start()
                    self.assertTrue(hub._stop.wait(2), "shutdown did not publish its stop state")
                finally:
                    release.set()
                    request.join(3)
                    if shutdown.ident is not None:
                        shutdown.join(3)
                self.assertFalse(request.is_alive())
                self.assertFalse(shutdown.is_alive())
                self.assertFalse(service.running)
                if operation == "make_link":
                    self.assertEqual(len(failures), 1)
                    self.assertIsInstance(failures[0], ValueError)
                else:
                    self.assertEqual(failures, [])
                calls = getattr(service, operation).call_count
                with self.assertRaises(ValueError):
                    hub.preview_link("sample")
                hub.sync_preview()
                self.assertEqual(getattr(service, operation).call_count, calls)
