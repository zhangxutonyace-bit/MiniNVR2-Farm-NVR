"""Demanded preview refresh uses the existing paired model, never event logic."""
import copy
import threading
import time
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np

from nvr.algorithm_catalog import DEFAULT_OPTIONS, revision_for
from nvr.algorithm_runtime import AlgorithmRunner
from nvr.algorithm_types import DetectionBatch, ObjectDetection, frame_key
from nvr.camera import Cam
from nvr.event_overlay import FramePacket
from nvr.live_overlay import LivePreviewWorker
from tests.support import OfflineTestCase


class PreviewSemanticsTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.now = 100.
        self.cfg.set("detect", "mode", "market", save=False)
        self.cfg.set("detect", "enabled", True, save=False)
        self.cfg.data["algorithms"] = copy.deepcopy(DEFAULT_OPTIONS)
        spec = {"id": "fixture", "url": "fixture.mp4", "enabled": True, "detect": True}
        self.cfg.data["cameras"] = [dict(spec)]
        self.cam = Cam(spec, self.cfg)
        self.cam.state = "online"
        self.cam.last_jpeg = cv2.imencode(".jpg", np.zeros((48, 64, 3), np.uint8))[1].tobytes()
        self.cam.seq, self.cam.last_ts = 10, self.now
        self.cam._live_preview = SimpleNamespace(demanded=mock.Mock(return_value=True))
        self.cam.market_infer = mock.Mock(return_value=None)
        self.cam.market_latest = mock.Mock(return_value=None)
        self.cam.start_record = mock.Mock()
        self.cam.stop_record = mock.Mock()
        patcher = mock.patch("nvr.camera.time.time", side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def pair(self, age=.1, positive=True, revision=None, source=None, size=(64, 48)):
        packet = self.cam.record_sample()
        anchor = FramePacket(packet.jpeg, packet.seq - 1, self.now - age,
                             packet.source if source is None else source)
        revision = revision or revision_for(self.cfg.get("algorithms"))
        detections = (ObjectDetection((2., 2., 20., 30.), .9, 0),) if positive else ()
        return DetectionBatch(frame_key(anchor, revision), size, detections), anchor

    def test_active_demand_uses_shared_model_without_event_mutation(self):
        self.cam.person, self.cam.hit, self.cam.last_person = True, 7, 84.
        self.cam._last_market_decision = ("event-only",)
        before = copy.deepcopy(self.cfg.data)
        self.assertTrue(self.cam.refresh_preview_semantics())
        self.cam.market_infer.assert_called_once()
        packet, options = self.cam.market_infer.call_args.args
        self.assertEqual(packet, self.cam.record_sample())
        self.assertEqual(options, self.cfg.get("algorithms"))
        self.assertEqual((self.cam.person, self.cam.hit, self.cam.last_person), (True, 7, 84.))
        self.assertEqual(self.cam._last_market_decision, ("event-only",))
        self.cam.start_record.assert_not_called()
        self.cam.stop_record.assert_not_called()
        self.assertEqual(self.cfg.data, before)

    def test_no_worker_or_expired_lease_does_not_create_or_run_model(self):
        for preview in (None, SimpleNamespace(demanded=mock.Mock(return_value=False))):
            with self.subTest(preview=preview):
                self.cam._live_preview = preview
                self.assertFalse(self.cam.refresh_preview_semantics())
                self.assertIsNone(self.cam._algorithm_runner)
                self.cam.market_infer.assert_not_called()

    def test_raw_preview_does_not_create_demand(self):
        self.cam._live_preview = None
        self.assertEqual(self.cam.preview_sample(boxes=False), self.cam.record_sample())
        self.assertIsNone(self.cam._live_preview)
        self.assertFalse(self.cam.refresh_preview_semantics())
        self.cam.market_infer.assert_not_called()

    def test_saved_detection_disable_immediately_removes_preview_eligibility_before_sync(self):
        painted = FramePacket(b"painted", 10, self.now, self.cam.record_sample().source)
        self.cam._live_preview.read = mock.Mock(return_value=painted)
        self.assertEqual(self.cam.preview_sample(), painted)
        self.cfg.get_camera(self.cam.id)["detect"] = False
        self.assertTrue(self.cam.detect_on, "Hub has deliberately not synchronized yet")
        self.assertIsNone(self.cam._preview_context())
        self.assertEqual(self.cam.preview_sample(), self.cam.record_sample())
        self.cam._live_preview.read.assert_called_once()
        self.cam.market_infer.assert_not_called()

    def test_disabled_legacy_offline_or_stopped_never_runs_model(self):
        cases = (lambda: self.cfg.set("detect", "enabled", False, save=False),
                 lambda: self.cfg.set("detect", "mode", "auto", save=False),
                 lambda: setattr(self.cam, "detect_on", False),
                 lambda: setattr(self.cam, "enabled", False),
                 lambda: setattr(self.cam, "state", "offline"),
                 lambda: self.cam._stop_event.set())
        original = copy.deepcopy(self.cfg.data)
        for change in cases:
            with self.subTest(change=change):
                self.cfg.data = copy.deepcopy(original)
                self.cam.enabled = self.cam.detect_on = True
                self.cam.state = "online"
                self.cam._stop_event.clear()
                change()
                self.assertFalse(self.cam.refresh_preview_semantics())
                self.cam.market_infer.assert_not_called()

    def test_persisted_camera_must_still_be_enabled_same_source_and_detecting(self):
        for replacement in ([], [{"id": "fixture", "url": "other.mp4"}],
                            [{"id": "fixture", "url": "fixture.mp4", "enabled": False}],
                            [{"id": "fixture", "url": "fixture.mp4", "detect": False}]):
            with self.subTest(replacement=replacement):
                self.cfg.data["cameras"] = replacement
                self.assertFalse(self.cam.refresh_preview_semantics())
                self.cam.market_infer.assert_not_called()

    def test_bad_or_old_capture_never_starts_work(self):
        for jpeg, stamp in ((b"bad", self.now), (self.cam.last_jpeg, self.now - .31),
                            (self.cam.last_jpeg, self.now + .01)):
            with self.subTest(stamp=stamp, valid=jpeg[:2]):
                self.cam.last_jpeg, self.cam.last_ts = jpeg, stamp
                self.assertFalse(self.cam.refresh_preview_semantics())
                self.cam.market_infer.assert_not_called()

    def test_recent_exact_positive_and_negative_results_are_both_reused(self):
        for positive in (True, False):
            with self.subTest(positive=positive):
                self.cam.market_latest.return_value = self.pair(age=.24, positive=positive)
                self.assertFalse(self.cam.refresh_preview_semantics())
                self.cam.market_infer.assert_not_called()

    def test_old_future_wrong_revision_source_or_size_result_does_not_block_refresh(self):
        for kwargs in ({"age": .251}, {"age": -.01}, {"revision": "old-revision"},
                       {"source": (("rtsp", "previous.mp4"), 0)}, {"size": (80, 48)}):
            with self.subTest(kwargs=kwargs):
                self.cam.market_latest.return_value = self.pair(**kwargs)
                self.assertTrue(self.cam.refresh_preview_semantics())
                self.cam.market_infer.reset_mock()

    def test_frame_digest_mismatch_does_not_count_as_fresh_semantics(self):
        batch, packet = self.pair()
        self.cam.market_latest.return_value = batch, FramePacket(
            packet.jpeg + b"changed", packet.seq, packet.timestamp, packet.source)
        self.assertTrue(self.cam.refresh_preview_semantics())

    def test_reset_during_admission_does_not_resurrect_old_options(self):
        old_context = self.cam._preview_context()
        with mock.patch.object(self.cam, "_preview_context", side_effect=(old_context, None)):
            self.assertFalse(self.cam.refresh_preview_semantics())
        self.cam.market_infer.assert_not_called()

    def test_lease_expiry_during_admission_stops_request(self):
        self.cam._live_preview.demanded.side_effect = (True, False)
        self.assertFalse(self.cam.refresh_preview_semantics())
        self.cam.market_infer.assert_not_called()

    def test_same_runner_busy_job_is_skipped_without_busy_warning_or_second_model(self):
        entered, release = threading.Event(), threading.Event()
        model = SimpleNamespace(locate=lambda image: (entered.set(), release.wait(2), ())[2])
        warnings, factory = [], mock.Mock(return_value=model)
        runner = AlgorithmRunner(mock.Mock(), warnings.append, factory=factory)
        self.cam._algorithm_runner = runner
        packet = self.cam.record_sample()
        options = self.cfg.get("algorithms")
        with mock.patch("nvr.algorithm_runtime.verify_file", side_effect=lambda path, spec: path):
            thread = threading.Thread(target=runner.infer, args=(packet, options), daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(1), "controlled native job did not enter")
                for _ in range(3):
                    self.assertFalse(self.cam.refresh_preview_semantics())
                self.cam.market_infer.assert_not_called()
                self.assertEqual(factory.call_count, 1)
                self.assertEqual(warnings, [])
            finally:
                release.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())


class PreviewLeaseTests(OfflineTestCase):
    def test_accessor_is_read_only_and_requires_actual_alive_worker(self):
        worker = LivePreviewWorker(mock.Mock(), mock.Mock(), mock.Mock())
        before = dict(worker.__dict__)
        with mock.patch("nvr.live_overlay.time.monotonic", return_value=10.):
            self.assertFalse(worker.demanded())
            self.assertEqual(worker.__dict__, before)
            worker.thread = SimpleNamespace(is_alive=lambda: True)
            worker.demand_until = 11.
            self.assertTrue(worker.demanded())
            worker.demand_until = 10.
            self.assertFalse(worker.demanded())
            worker.demand_until = 11.
            worker.stop_event.set()
            self.assertFalse(worker.demanded())

    def test_dead_worker_never_requests_semantic_work_even_with_live_lease(self):
        worker = LivePreviewWorker(mock.Mock(), mock.Mock(), mock.Mock())
        worker.thread = SimpleNamespace(is_alive=lambda: False)
        worker.demand_until = time.monotonic() + 10
        self.assertFalse(worker.demanded())
