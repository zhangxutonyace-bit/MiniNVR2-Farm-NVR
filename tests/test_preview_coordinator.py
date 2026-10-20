"""Demand refresh shares inference without entering event decisions or queues."""
import threading
from unittest import mock

from nvr.camera import Cam
from nvr.hub import Hub
from tests.support import OfflineTestCase
from tests.test_detection_cadence import _Clock, _LoopStop


class PreviewCoordinatorTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.cfg.set("detect", "enabled", True, save=False)
        self.cfg.set("detect", "mode", "market", save=False)
        self.cfg.set("detect", "interval", .5, save=False)
        with mock.patch.object(Hub, "_start_threads"), \
                mock.patch.object(Hub, "_boot_clawbot"), \
                mock.patch.object(Hub, "sync_preview"):
            self.hub = Hub(self.cfg, log=mock.Mock())
        self.addCleanup(self.hub.model_store.close)
        for name in ("_detect_camera", "_detect_loop"):
            patcher = mock.patch.object(self.hub, name, side_effect=AssertionError(
                "Preview refresh must never enter an event-decision path"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def camera(self, cid="farm"):
        spec = {"id": cid, "url": cid + "-fixture.mp4", "enabled": True, "detect": True}
        self.cfg.upsert_camera(spec)
        cam = Cam(self.cfg.get_camera(cid), self.cfg)
        cam.refresh_preview_semantics = mock.Mock(return_value=True)
        cam.start_record = mock.Mock()
        cam.stop_record = mock.Mock()
        self.hub.cams[cid] = cam
        return cam

    def test_one_coordinator_registered_with_existing_shutdown_threads(self):
        with mock.patch("nvr.hub.threading.Thread") as factory:
            Hub._start_threads(self.hub)
        targets = [call.kwargs["target"] for call in factory.call_args_list]
        self.assertEqual(targets.count(self.hub._preview_semantic_loop), 1)
        self.assertEqual(len(targets), 4)
        self.assertEqual(len(self.hub._threads), 4)

    def test_current_camera_refresh_does_not_change_event_state_or_configured_interval(self):
        cam = self.camera()
        cam.hit, cam.person, cam.last_person, cam.last_event_end = 7, True, 91., 81.
        self.assertTrue(self.hub._refresh_preview_semantics(cam))
        cam.refresh_preview_semantics.assert_called_once_with()
        self.assertEqual((cam.hit, cam.person, cam.last_person, cam.last_event_end), (7, True, 91., 81.))
        self.assertEqual(self.cfg.get("detect", "interval"), .5)
        cam.start_record.assert_not_called()
        cam.stop_record.assert_not_called()

    def test_algorithm_lock_is_nonblocking_and_busy_work_is_not_queued(self):
        cam = self.camera()
        busy = mock.Mock()
        busy.acquire.return_value = False
        self.hub.algorithm_lock = busy
        self.assertFalse(self.hub._refresh_preview_semantics(cam))
        busy.acquire.assert_called_once_with(blocking=False)
        busy.release.assert_not_called()
        cam.refresh_preview_semantics.assert_not_called()

    def test_lock_is_released_after_native_failure(self):
        cam = self.camera()
        cam.refresh_preview_semantics.side_effect = ValueError("offline failure")
        lock = threading.Lock()
        self.hub.algorithm_lock = lock
        with self.assertRaises(ValueError):
            self.hub._refresh_preview_semantics(cam)
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_removed_replaced_disabled_or_changed_source_never_refreshes_old_cam(self):
        for state in ("removed", "replaced", "disabled", "detect_off", "source", "global_off", "legacy", "stop"):
            with self.subTest(state=state):
                self.hub._stop = threading.Event()
                self.cfg.set("detect", "enabled", True, save=False)
                self.cfg.set("detect", "mode", "market", save=False)
                cam = self.camera()
                spec = dict(self.cfg.get_camera(cam.id))
                if state == "removed":self.hub.cams.pop(cam.id)
                elif state == "replaced":self.hub.cams[cam.id] = object()
                elif state == "disabled":spec["enabled"] = False
                elif state == "detect_off":spec["detect"] = False
                elif state == "source":spec["url"] = "new-source.mp4"
                elif state == "global_off":self.cfg.set("detect", "enabled", False, save=False)
                elif state == "legacy":self.cfg.set("detect", "mode", "auto", save=False)
                else:self.hub._stop.set()
                self.cfg.upsert_camera(spec)
                self.assertFalse(self.hub._refresh_preview_semantics(cam))
                cam.refresh_preview_semantics.assert_not_called()

    def run_loop(self, cams, cost=.08, rounds=3, failure=False, stop_first=False):
        clock, starts = _Clock(), []
        self.hub._stop = _LoopStop(clock, rounds)
        def refresh(cam):
            starts.append((cam.id, clock.now))
            clock.advance(cost)
            if stop_first:self.hub._stop.set()
            if failure:raise ValueError("must not escape coordinator")
            return True
        with mock.patch.object(self.hub, "_refresh_preview_semantics", side_effect=refresh), \
                mock.patch("nvr.hub.time.monotonic", side_effect=lambda: clock.now):
            self.hub._preview_semantic_loop()
        return starts, self.hub._stop.waits

    def test_refresh_start_interval_subtracts_work_without_extra_event_decisions(self):
        cam = self.camera()
        starts, waits = self.run_loop([cam])
        self.assertEqual([round(at, 6) for _, at in starts], [100., 100.25, 100.5])
        self.assertEqual([round(wait, 6) for wait in waits], [.17, .17, .17])
        cam.start_record.assert_not_called()
        cam.stop_record.assert_not_called()

    def test_slow_multiple_cameras_are_serial_and_keep_interruptible_overrun_wait(self):
        self.camera("a")
        self.camera("b")
        starts, waits = self.run_loop([], cost=.3, rounds=2)
        self.assertEqual([cid for cid, _ in starts], ["a", "b", "a", "b"])
        self.assertEqual([round(at, 6) for _, at in starts], [100., 100.3, 100.62, 100.92])
        self.assertEqual([round(wait, 6) for wait in waits], [.02, .02])

    def test_failure_does_not_create_retry_backlog_or_escape_shutdown(self):
        cam = self.camera()
        starts, waits = self.run_loop([cam], failure=True, rounds=2)
        self.assertEqual(len(starts), 2)
        self.assertEqual([round(wait, 6) for wait in waits], [.17, .17])
        self.assertEqual(self.hub.log.call_count, 2)

    def test_stop_after_first_camera_does_not_schedule_second(self):
        self.camera("a")
        self.camera("b")
        starts, waits = self.run_loop([], stop_first=True)
        self.assertEqual([cid for cid, _ in starts], ["a"])
        self.assertEqual(waits, [])
