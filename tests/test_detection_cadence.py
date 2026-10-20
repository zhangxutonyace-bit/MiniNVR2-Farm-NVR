"""Exercise the real Hub loop with synthetic work and an interruptible clock.

No model or camera is opened. Each work callback advances a deterministic
clock, while the existing per-camera confirmation/recording decisions run.
"""
import copy
from types import SimpleNamespace
from unittest import mock

from nvr.algorithm_types import DetectionBatch, ObjectDetection, frame_key
from nvr.camera import Cam
from nvr.event_overlay import FramePacket
from nvr.hub import Hub
from tests.support import OfflineTestCase


class _Clock:
    def __init__(self):
        self.now = 100.

    def advance(self, seconds):
        self.now += seconds


class _LoopStop:
    """A finite, interruptible stand-in for Event.wait; never actually sleeps."""
    def __init__(self, clock, rounds, on_wait=None):
        self.clock, self.rounds, self.on_wait = clock, rounds, on_wait
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(float(seconds))
        if self.on_wait is not None:
            self.on_wait(self, len(self.waits))
        if self.stopped or len(self.waits) >= self.rounds:
            self.stopped = True
            return True
        self.clock.advance(seconds)
        return False


class DetectionCadenceTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        for key, value in (("enabled", True), ("mode", "market"), ("interval", .5),
                           ("confirm", 2), ("gone_sec", 8), ("cooldown", 0)):
            self.cfg.set("detect", key, value, save=False)
        self.clock = _Clock()
        self.order = []
        self.image = SimpleNamespace(shape=(270, 480, 3))
        # Instantiating Hub is safe with all background/service entry points
        # patched; the real _detect_loop and _detect_camera remain in use.
        with mock.patch.object(Hub, "_start_threads"), \
                mock.patch.object(Hub, "_boot_clawbot"), \
                mock.patch.object(Hub, "sync_preview"):
            self.hub = Hub(self.cfg, log=mock.Mock())
        self.addCleanup(self.hub.model_store.close)
        for target in ("nvr.algorithm_runtime.AlgorithmRunner.infer", "nvr.ffmpeg.popen",
                       "cv2.VideoCapture", "cv2.dnn.readNetFromONNX"):
            patcher = mock.patch(target, side_effect=AssertionError(
                "Cadence regression must not execute model/capture work"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def camera(self, cid="farm", cost=.2, positive=True, after_work=None):
        camera = Cam({"id": cid, "url": cid + "-fixture.mp4"}, self.cfg)
        camera.state = "online"
        active = [False]
        camera.recording = mock.Mock(side_effect=lambda: active[0])
        camera.start_record = mock.Mock(side_effect=lambda *args: active.__setitem__(0, True))
        camera.stop_record = mock.Mock(side_effect=lambda: active.__setitem__(0, False))
        camera.rec_too_long = mock.Mock(return_value=False)
        camera.market_latest = mock.Mock(return_value=None)
        seq = [0]

        def sample():
            seq[0] += 1
            self.order.append((cid, self.clock.now, self.cfg.get("detect", "mode")))
            packet = FramePacket((cid + "-%d" % seq[0]).encode(), seq[0], self.clock.now,
                                 (camera.source_identity, 1))
            return self.image, packet

        def work():
            self.clock.advance(cost)
            if after_work is not None:
                after_work()

        def infer(packet):
            work()
            objects = (ObjectDetection((50., 30., 40., 90.), .9, 0),) if positive else ()
            return DetectionBatch(frame_key(packet, self.hub.algorithm_revision()),
                                  (480, 270), objects)

        def legacy(image):
            self.assertIs(image, self.image)
            work()
            return positive

        detector = SimpleNamespace(process=mock.Mock(side_effect=legacy),
                                   confirmed_boxes=(), provenance="offline-fixture")
        camera.frame_sample = mock.Mock(side_effect=sample)
        camera.market_infer = mock.Mock(side_effect=infer)
        camera.ensure_detector = mock.Mock(return_value=detector)
        camera.fixture_detector = detector
        self.hub.cams[cid] = camera
        return camera

    def run_loop(self, rounds=3, on_wait=None):
        self.hub._stop = _LoopStop(self.clock, rounds, on_wait)
        with mock.patch("nvr.hub.time.monotonic", side_effect=lambda: self.clock.now), \
                mock.patch("nvr.hub.time.time", side_effect=lambda: self.clock.now):
            self.hub._detect_loop()
        return self.hub._stop.waits

    def assert_times(self, actual, expected):
        self.assertEqual(len(actual), len(expected))
        for observed, wanted in zip(actual, expected):
            self.assertAlmostEqual(observed, wanted, places=7)

    def test_market_fast_work_preserves_start_cadence_and_existing_confirmation(self):
        camera = self.camera(cost=.2)
        before_detect = copy.deepcopy(self.cfg.get("detect"))
        before_algorithm = copy.deepcopy(self.cfg.get("algorithms"))
        waits = self.run_loop(rounds=3)
        self.assert_times([stamp for _, stamp, _ in self.order], [100., 100.5, 101.])
        self.assert_times(waits, [.3, .3, .3])
        self.assertEqual(camera.market_infer.call_count, 3)
        camera.ensure_detector.assert_not_called()
        # Real Hub event logic still waits for the configured second hit.
        camera.start_record.assert_called_once()
        trigger = camera.start_record.call_args.args[0]
        self.assertEqual(trigger.seq, 2)
        self.assertAlmostEqual(trigger.timestamp, 100.5)
        camera.stop_record.assert_not_called()
        self.assertEqual(camera.hit, 3)
        self.assertEqual(self.cfg.get("detect"), before_detect)
        self.assertEqual(self.cfg.get("algorithms"), before_algorithm)

    def test_overbudget_market_work_keeps_small_interruptible_wait_without_stale_trigger(self):
        camera = self.camera(cost=.8)
        waits = self.run_loop(rounds=3)
        self.assert_times([stamp for _, stamp, _ in self.order], [100., 100.82, 101.64])
        self.assert_times(waits, [.02, .02, .02])
        self.assertEqual(camera.market_infer.call_count, 3)
        camera.ensure_detector.assert_not_called()
        # Cadence changes do not relax the existing 0.75 s semantic age rule.
        self.assertEqual(camera.hit, 0)
        self.assertFalse(camera.person)
        camera.start_record.assert_not_called()
        camera.stop_record.assert_not_called()

    def test_multiple_cameras_are_serial_and_share_one_round_interval(self):
        farm = self.camera("farm", .12)
        coop = self.camera("coop", .18)
        waits = self.run_loop(rounds=2)
        self.assertEqual([cid for cid, _, _ in self.order], ["farm", "coop", "farm", "coop"])
        self.assert_times([stamp for _, stamp, _ in self.order], [100., 100.12, 100.5, 100.62])
        self.assert_times(waits, [.2, .2])
        for camera in (farm, coop):
            self.assertEqual(camera.market_infer.call_count, 2)
            camera.start_record.assert_called_once()
            self.assertEqual(camera.start_record.call_args.args[0].seq, 2)
            camera.stop_record.assert_not_called()

    def test_legacy_modes_keep_full_interval_after_work(self):
        for mode in ("auto", "haar", "motion"):
            with self.subTest(mode=mode):
                self.cfg.set("detect", "mode", mode, save=False)
                self.clock.now = 100.
                self.order.clear()
                self.hub.cams.clear()
                camera = self.camera(cost=.2, positive=False)
                waits = self.run_loop(rounds=2)
                self.assert_times([stamp for _, stamp, _ in self.order], [100., 100.7])
                self.assert_times(waits, [.5, .5])
                camera.market_infer.assert_not_called()
                self.assertEqual(camera.fixture_detector.process.call_count, 2)
                camera.start_record.assert_not_called()

    def test_switch_from_market_to_legacy_uses_fresh_options_next_round(self):
        camera = self.camera(cost=.2, positive=False)
        before_algorithm = copy.deepcopy(self.cfg.get("algorithms"))

        def switch(stop, completed):
            if completed == 1:
                self.cfg.set("detect", "mode", "auto", save=False)

        waits = self.run_loop(rounds=3, on_wait=switch)
        self.assertEqual([mode for _, _, mode in self.order], ["market", "auto", "auto"])
        self.assert_times([stamp for _, stamp, _ in self.order], [100., 100.5, 101.2])
        self.assert_times(waits, [.3, .5, .5])
        camera.market_infer.assert_called_once()
        self.assertEqual(camera.fixture_detector.process.call_count, 2)
        self.assertEqual(self.cfg.get("algorithms"), before_algorithm)
        camera.start_record.assert_not_called()

    def test_stop_during_wait_interrupts_without_starting_another_round(self):
        camera = self.camera(cost=.2, positive=False)
        waits = self.run_loop(rounds=5, on_wait=lambda stop, count: stop.set())
        self.assert_times(waits, [.3])
        self.assertAlmostEqual(self.clock.now, 100.2)
        camera.market_infer.assert_called_once()
        self.assertEqual(camera.frame_sample.call_count, 1)

    def test_stop_after_first_camera_does_not_schedule_second_camera(self):
        farm = self.camera("farm", .2, positive=False,
                           after_work=lambda: self.hub._stop.set())
        coop = self.camera("coop", .2, positive=False)
        waits = self.run_loop(rounds=5)
        self.assertEqual(waits, [])
        self.assertAlmostEqual(self.clock.now, 100.2)
        farm.market_infer.assert_called_once()
        coop.market_infer.assert_not_called()
        coop.frame_sample.assert_not_called()
