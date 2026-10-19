"""Synthetic multi-class geometry, shared native work and semantic lifecycle."""
import base64
import threading
import time
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np

from nvr.algorithm_catalog import model_spec, revision_for, validate_options
from nvr.algorithm_runtime import AlgorithmRunner
from nvr.algorithm_types import DetectionBatch, ObjectDetection, frame_key
from nvr.camera import Cam
from nvr.clawbot import ClawBot
from nvr.event_overlay import EventOverlay, FramePacket
from nvr.hub import Hub
from nvr.local_camera import SharedJpegRecorder
from nvr.model_store import ModelStore
from nvr.object_models import YoloXObjects, class_nms
from nvr.target_tracking import TargetTracker
from tests.support import OfflineTestCase


def packet(seq=1, source=("fixture", 1), stamp=None):
    image = np.zeros((270, 480, 3), np.uint8)
    cv2.rectangle(image, (60 + seq, 50), (125 + seq, 220), (60, 180, 90), -1)
    ok, jpeg = cv2.imencode(".jpg", image)
    return FramePacket(jpeg.tobytes(), seq, stamp or time.time(), source)


class AdapterTests(OfflineTestCase):
    def adapter(self, model_id="yolox-nano"):
        native = mock.Mock()
        native.getUnconnectedOutLayersNames.return_value = ("output",)
        with mock.patch("cv2.dnn.readNetFromONNX", return_value=native):
            return YoloXObjects("unused", model_spec(model_id)), native

    def test_yolox_raw_profiles_bgr_rgb_padding_and_geometry(self):
        for mid, rgb, rows in (("yolox-nano", False, 3549), ("yolox-s", True, 8400)):
            adapter, native = self.adapter(mid)
            output = np.zeros((1, rows, 85), np.float32)
            index = 100
            output[0, index, :4] = (0, 0, 2, 2)
            output[0, index, 4] = .8
            output[0, index, 5 + 16] = .9
            native.forward.return_value = (output,)
            image = np.empty((100, 200, 3), np.uint8)
            image[:] = (10, 20, 30)
            result = adapter.locate(image)
            self.assertEqual([d.class_id for d in result], [16])
            self.assertAlmostEqual(result[0].confidence, .72, places=5)
            blob = native.setInput.call_args.args[0]
            self.assertEqual(tuple(blob.shape), (1, 3, adapter.spec.input_size, adapter.spec.input_size))
            self.assertEqual(tuple(blob[0, :, 0, 0]), (30, 20, 10) if rgb else (10, 20, 30))
            self.assertEqual(tuple(blob[0, :, -1, -1]), (114, 114, 114))
            self.assertTrue(all(np.isfinite(result[0].box)))
            native.forward.assert_called_once()

    def test_all_class_winner_before_filter_and_selected_before_global_cap(self):
        adapter, _ = self.adapter()
        adapter.selected_classes = {16}
        raw = np.zeros((1, 3549, 85), np.float32)
        for index in range(60, 100):
            raw[0, index, :4] = (0, 0, 2, 2)
            raw[0, index, 4] = 1
            raw[0, index, 5] = .99
        raw[0, 120, :4] = (0, 0, 2, 2)
        raw[0, 120, 4] = 1
        raw[0, 120, 5 + 16] = .5
        raw[0, 121, :4] = (0, 0, 2, 2)
        raw[0, 121, 4] = 1
        raw[0, 121, 5 + 15] = .9
        raw[0, 121, 5 + 16] = .8
        result = adapter._decode((raw,), 416, 416, 1, 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].class_id, 16)
        self.assertAlmostEqual(result[0].confidence, .5)

    def test_class_nms_preserves_person_phone_and_total_cap_twelve(self):
        same = (80., 60., 50., 100.)
        result = class_nms([ObjectDetection(same, .9, 0), ObjectDetection(same, .8, 67),
                            ObjectDetection(same, .7, 0)], .35)
        self.assertEqual([d.class_id for d in result], [0, 67])
        many = [ObjectDetection((i * 15., 10., 8., 20.), .99 - i * .01, i % 80) for i in range(20)]
        self.assertEqual(len(class_nms(many, .1)), 12)
        tracker = TargetTracker(threshold=.1)
        tracker.update(np.zeros((270, 480, 3), np.uint8), many, 1, 1, ())
        self.assertEqual(len(tracker.tracks), 12)

    def test_tracker_never_associates_different_classes(self):
        tracker = TargetTracker()
        image = np.random.default_rng(2).integers(0, 256, (270, 480, 3), np.uint8)
        box = (80., 50., 60., 140.)
        tracker.update(image, (ObjectDetection(box, .9, 0), ObjectDetection(box, .8, 67)), 1, 1, ())
        self.assertEqual({t.class_id for t in tracker.tracks}, {0, 67})
        with mock.patch.object(tracker, "_flow", return_value=box):
            tracker.update(image, (ObjectDetection(box, .8, 67),), 1.1, 2, ())
        person = next(t for t in tracker.tracks if t.class_id == 0)
        self.assertEqual(person.confirmed_at, 1)
        self.assertEqual(next(t for t in tracker.tracks if t.class_id == 67).confirmed_at, 1.1)

    def test_malformed_or_nonfinite_native_output_rejected(self):
        adapter, _ = self.adapter()
        for output in (np.zeros((1, 20, 85)), np.full((1, 3549, 85), np.nan)):
            with self.assertRaises(RuntimeError):
                adapter._decode((output,), 416, 416, 1, 1)

    def test_small_horizontal_phone_and_low_car_survive_adapter_and_market_tracker(self):
        for cid in (67, 2):
            adapter, _ = self.adapter()
            raw = np.zeros((1, 3549, 85), np.float32)
            index, center, size = 1000, np.array((200., 100.)), np.array((40., 6.))
            raw[0, index, :2] = center / 8 - adapter.grid[index]
            raw[0, index, 2:4] = np.log(size / 8)
            raw[0, index, 4] = 1
            raw[0, index, 5 + cid] = .9
            result = adapter._decode((raw,), 480, 270, 1, 1)
            self.assertEqual(len(result), 1)
            self.assertAlmostEqual(result[0].box[3], 6, places=4)
            tracker = TargetTracker(object_boxes=True)
            tracker.update(np.zeros((270, 480, 3), np.uint8), result, 1, 1, ())
            self.assertEqual(len(tracker.tracks), 1)


class RuntimeTests(OfflineTestCase):
    def runner(self, locate):
        model = SimpleNamespace(locate=mock.Mock(side_effect=locate))
        factory = mock.Mock(return_value=model)
        warnings = []
        return AlgorithmRunner(ModelStore(self.root / "models"), warnings.append, factory), model, warnings

    def test_decision_and_annotation_share_one_forward_exact_packet_only(self):
        runner, model, _ = self.runner(lambda image: (ObjectDetection((60., 50., 60., 150.), .9, 0),))
        options = validate_options({"targets": ["person", "phone"]})
        sample = packet()
        result = runner.infer(sample, options)
        overlay = EventOverlay("#F51616", {"mode": "market"}, runtime=lambda p: runner.infer(p, options),
                               algorithm_options=options)
        raw = sample.jpeg
        annotated = overlay.render(sample)
        self.assertNotEqual(raw, annotated)
        self.assertEqual(sample.jpeg, raw)
        model.locate.assert_called_once()
        self.assertTrue(result.matches(sample, revision_for(options)))
        changed = FramePacket(packet(4).jpeg, sample.seq, sample.timestamp, sample.source)
        self.assertFalse(result.matches(changed, revision_for(options)))
        self.assertIsNone(runner.infer(changed, options))

    def test_blocked_job_retains_slot_across_revision_and_discards_late_result(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def blocked(image):
            entered.set()
            release.wait(3)
            return (ObjectDetection((50., 40., 40., 80.), .9, 0),)
        runner, model, warnings = self.runner(blocked)
        first, next_options = validate_options({}), validate_options({"targets": ["dog"]})
        self.assertIsNone(runner.infer(packet(), first, timeout=.02))
        self.assertTrue(entered.is_set())
        for seq in range(2, 10):
            self.assertIsNone(runner.infer(packet(seq), next_options, timeout=.01))
        self.assertEqual(model.locate.call_count, 1)
        release.set()
        self.wait_until(lambda: runner.job["done"].is_set())
        self.assertIsNone(runner.cached)
        self.assertTrue(warnings)

    def test_concurrent_same_frame_joins_one_native_call(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def blocked(image):
            entered.set()
            release.wait(1)
            return ()
        runner, model, _ = self.runner(blocked)
        sample, options, results = packet(), validate_options({}), []
        thread = threading.Thread(target=lambda: results.append(runner.infer(sample, options)))
        thread.start()
        self.assertTrue(entered.wait(1))
        release.set()
        second = runner.infer(sample, options)
        thread.join(1)
        self.assertEqual(results, [second])
        model.locate.assert_called_once()

    def test_reset_waits_for_atomic_job_admission_and_never_leaks_global_slot(self):
        from nvr.algorithm_runtime import _NATIVE_SLOT
        entered, release, reset_done = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        runner, model, _ = self.runner(lambda image: ())
        original = runner.configure
        errors = []
        def configure(options):
            result = original(options)
            entered.set()
            release.wait(2)
            return result
        runner.configure = configure
        def run():
            try:
                runner.infer(packet(), validate_options({}))
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(entered.wait(1))
        resetting = threading.Thread(target=lambda: (runner.reset(), reset_done.set()))
        resetting.start()
        self.assertFalse(reset_done.wait(.03))
        release.set()
        worker.join(2)
        resetting.join(2)
        self.assertFalse(errors)
        self.assertTrue(reset_done.is_set())
        self.wait_until(lambda: runner.job is None or runner.job["done"].is_set())
        acquired = _NATIVE_SLOT.acquire(blocking=False)
        self.assertTrue(acquired)
        if acquired:
            _NATIVE_SLOT.release()

    def test_thread_start_failure_returns_native_slot(self):
        from nvr.algorithm_runtime import _NATIVE_SLOT
        runner, _, _ = self.runner(lambda image: ())
        with mock.patch("nvr.algorithm_runtime.threading.Thread.start", side_effect=RuntimeError("fixture")):
            self.assertIsNone(runner.infer(packet(), validate_options({})))
        self.assertTrue(runner.job["done"].is_set())
        acquired = _NATIVE_SLOT.acquire(blocking=False)
        self.assertTrue(acquired)
        if acquired:
            _NATIVE_SLOT.release()

    def test_stale_or_wrong_revision_overlay_is_raw_and_resets_tracking(self):
        options, sample = validate_options({}), packet()
        wrong = DetectionBatch(frame_key(packet(2), revision_for(options)), (480, 270),
                               (ObjectDetection((40., 40., 50., 120.), .9, 0),))
        overlay = EventOverlay("#F51616", {"mode": "market"}, runtime=lambda p: wrong,
                               algorithm_options=options)
        self.assertEqual(overlay.render(sample), sample.jpeg)
        self.assertEqual(overlay.tracker.tracks, [])

    def test_only_fresh_semantic_decisions_control_confirmation_and_departure(self):
        self.cfg.set("detect", "mode", "market", save=False)
        self.cfg.set("detect", "confirm", 2, save=False)
        self.cfg.set("detect", "gone_sec", 1, save=False)
        self.cfg.set("detect", "cooldown", 0, save=False)
        camera = Cam({"id": "sample", "url": "unused"}, self.cfg)
        camera.state = "online"
        active = [False]
        camera.recording = lambda: active[0]
        camera.start_record = mock.Mock(side_effect=lambda *a: active.__setitem__(0, True))
        camera.stop_record = mock.Mock(side_effect=lambda: active.__setitem__(0, False))
        with mock.patch.object(Hub, "_start_threads"), mock.patch.object(Hub, "_boot_clawbot"):
            hub = Hub(self.cfg, log=lambda *a: None)
        options = validate_options({})
        for seq, now, positive in ((1, 100., True), (2, 100.5, True), (3, 102., None)):
            sample = packet(seq, stamp=now)
            camera.frame_sample = lambda: (np.zeros((270, 480, 3), np.uint8), sample)
            result = (DetectionBatch(frame_key(sample, revision_for(options)), (480, 270),
                                     (ObjectDetection((50., 30., 40., 90.), .9, 0),)) if positive else None)
            camera.market_infer = lambda p: result
            with mock.patch("nvr.hub.time.time", return_value=now):
                hub._detect_camera(camera, self.cfg.get("detect"))
        camera.start_record.assert_called_once()
        camera.stop_record.assert_called_once()
        self.assertFalse(active[0])
        self.assertEqual(camera.last_person, 100.5)

    def test_no_native_decision_still_respects_maximum_event_duration(self):
        camera = Cam({"id": "sample", "url": "unused"}, self.cfg)
        camera.state, camera.person, camera.last_person = "online", True, 99.9
        sample = packet(stamp=100.)
        camera.frame_sample = lambda: (np.zeros((270, 480, 3), np.uint8), sample)
        camera.market_infer = lambda p: None
        camera.rec_too_long = lambda: True
        camera.stop_record = mock.Mock()
        with mock.patch.object(Hub, "_start_threads"), mock.patch.object(Hub, "_boot_clawbot"):
            hub = Hub(self.cfg, log=lambda *a: None)
        with mock.patch("nvr.hub.time.time", return_value=100.):
            hub._detect_camera(camera, {"mode": "market", "gone_sec": 10000})
        camera.stop_record.assert_called_once()
        self.assertFalse(camera.person)

    def test_latest_paired_semantics_can_sustain_event_but_never_count_twice(self):
        camera = Cam({"id": "sample", "url": "unused"}, self.cfg)
        camera.state = "online"
        camera.recording = lambda: False
        camera.rec_too_long = lambda: False
        camera.start_record = mock.Mock()
        decision, current = packet(1, stamp=100.), packet(2, stamp=100.1)
        result = DetectionBatch(frame_key(decision, revision_for(validate_options({}))), (480, 270),
                                (ObjectDetection((50., 30., 40., 90.), .9, 0),))
        camera.frame_sample = lambda: (np.zeros((270, 480, 3), np.uint8), current)
        camera.market_infer = lambda p: None
        camera.market_latest = lambda: (result, decision)
        with mock.patch.object(Hub, "_start_threads"), mock.patch.object(Hub, "_boot_clawbot"):
            hub = Hub(self.cfg, log=lambda *a: None)
        with mock.patch("nvr.hub.time.time", return_value=100.1):
            hub._detect_camera(camera, {"mode": "market", "confirm": 2})
            current = packet(3, stamp=100.1)
            hub._detect_camera(camera, {"mode": "market", "confirm": 2})
        self.assertEqual(camera.hit, 1)
        self.assertEqual(camera.last_person, 100.)
        camera.start_record.assert_not_called()

    def test_bad_persisted_config_never_silently_falls_back_to_person(self):
        camera = Cam({"id": "sample", "url": "unused"}, self.cfg)
        self.cfg.data["algorithms"] = "bad config"
        self.assertIsNone(camera.market_infer(packet()))
        self.assertIsNone(camera._algorithm_runner)
        self.assertTrue(camera.algorithm_warning)

    def market_camera(self):
        camera = Cam({"id": "sample", "url": "unused"}, self.cfg)
        camera.state = "online"
        camera.recording = mock.Mock(return_value=False)
        camera.start_record, camera.stop_record = mock.Mock(), mock.Mock()
        camera.rec_too_long = lambda: False
        camera.market_latest = lambda: None
        with mock.patch.object(Hub, "_start_threads"), mock.patch.object(Hub, "_boot_clawbot"):
            hub = Hub(self.cfg, log=lambda *a: None)
        return hub, camera

    def market_tick(self, hub, camera, sample, now, positive=True, image=True):
        camera.frame_sample = lambda: (np.zeros((270, 480, 3), np.uint8) if image else None, sample)
        result = None if positive is None else DetectionBatch(
            frame_key(sample, revision_for(validate_options({}))), (480, 270),
            (ObjectDetection((50., 30., 40., 90.), .9, 0),) if positive else ())
        camera.market_infer = mock.Mock(return_value=result)
        with mock.patch("nvr.hub.time.time", return_value=now):
            hub._detect_camera(camera, {"mode": "market", "confirm": 2, "gone_sec": 1, "cooldown": 0})

    def test_new_source_same_seq_resets_confirmation_before_dedup_and_closes_old_event(self):
        hub, camera = self.market_camera()
        self.market_tick(hub, camera, packet(1, ("fixture", 1), 100.), 100.)
        self.assertEqual(camera.hit, 1)
        self.market_tick(hub, camera, packet(1, ("fixture", 2), 100.1), 100.1)
        self.assertEqual(camera.hit, 1)
        camera.start_record.assert_not_called()
        self.market_tick(hub, camera, packet(2, ("fixture", 2), 100.2), 100.2)
        camera.start_record.assert_called_once()
        camera.recording.return_value = True
        self.market_tick(hub, camera, packet(2, ("fixture", 3), 100.3), 100.3)
        camera.stop_record.assert_called_once()
        self.assertFalse(camera.person)
        self.assertEqual(camera.hit, 1)

    def test_partial_confirmations_expire_for_unknown_missing_and_duplicate_frames(self):
        for kind in ("unknown", "missing", "duplicate"):
            with self.subTest(kind=kind):
                hub, camera = self.market_camera()
                first = packet(1, stamp=100.)
                self.market_tick(hub, camera, first, 100.)
                self.market_tick(hub, camera, packet(2, stamp=100.3), 100.3, None)
                self.assertEqual(camera.hit, 1)  # short unknown is not a negative
                sample = first if kind == "duplicate" else packet(3, stamp=103.)
                self.market_tick(hub, camera, sample, 103., None, image=kind != "missing")
                self.assertEqual(camera.hit, 0)
                self.market_tick(hub, camera, packet(4, stamp=103.1), 103.1)
                self.assertEqual(camera.hit, 1)
                camera.start_record.assert_not_called()

    def test_busy_semantic_frame_is_retried_and_accepted_only_once(self):
        hub, camera = self.market_camera()
        sample = packet(1, stamp=100.)
        self.market_tick(hub, camera, sample, 100., None)
        self.market_tick(hub, camera, sample, 100.1)
        self.assertEqual(camera.hit, 1)
        self.market_tick(hub, camera, sample, 100.2)
        camera.market_infer.assert_not_called()
        self.assertEqual(camera.hit, 1)

    def test_failed_native_job_can_retry_same_frame_without_recounting_success(self):
        calls = [RuntimeError("transient fixture failure"), ()]
        def locate(image):
            value = calls.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        runner, model, _ = self.runner(locate)
        sample, options = packet(), validate_options({})
        self.assertIsNone(runner.infer(sample, options))
        result = runner.infer(sample, options)
        self.assertIsNotNone(result)
        self.assertIs(runner.infer(sample, options), result)
        self.assertEqual(model.locate.call_count, 2)

    def test_actual_classed_event_mp4_is_playable_and_fake_wechat_upload_identical(self):
        options = validate_options({"targets": ["pets", "cat", "dog"]})
        current = [packet()]
        def infer(sample):
            return DetectionBatch(frame_key(sample, revision_for(options)), (480, 270),
                                  (ObjectDetection((60. + sample.seq, 50., 65., 170.), .9, 15),
                                   ObjectDetection((160. + sample.seq, 90., 70., 100.), .85, 16)))
        overlay = EventOverlay("#F51616", {"mode": "market"}, runtime=infer, algorithm_options=options)
        path = self.root / "classed-event.mp4"
        recorder = SharedJpegRecorder(str(path), lambda: current[0], fps=8, transform=overlay.render)
        try:
            for seq in range(1, 9):
                current[0] = packet(seq)
                time.sleep(.125)
        finally:
            recorder.stop()
        capture = cv2.VideoCapture(str(path))
        colored_frames = 0
        while True:
            ok, image = capture.read()
            if not ok:
                break
            blue = np.max(np.abs(image.astype(float) - (192, 49, 9)), axis=2) < 35
            green = np.max(np.abs(image.astype(float) - (13, 194, 145)), axis=2) < 35
            colored_frames += int(np.count_nonzero(blue) > 250 and np.count_nonzero(green) > 250)
        capture.release()
        self.assertGreaterEqual(colored_frames, 4)
        self.assertEqual([entry[1] for entry in overlay.label_renderer.last_layout], ["猫", "狗"])
        bot, seen = ClawBot(self.cfg), {}
        def post(endpoint, body, **kwargs):
            seen[endpoint] = body
            return {"upload_param": "FAKE"} if endpoint == "getuploadurl" else {"ret": 0}
        response = mock.MagicMock()
        response.__enter__.return_value.headers = {"x-encrypted-param": "FAKE-RECEIPT"}
        with mock.patch.object(bot, "_post", side_effect=post), \
             mock.patch("urllib.request.urlopen", return_value=response) as upload:
            self.assertTrue(bot.send_video("fake-peer", "fake-context", str(path)))
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        key = bytes.fromhex(seen["getuploadurl"]["aeskey"])
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        plain = decryptor.update(upload.call_args.args[0].data) + decryptor.finalize()
        self.assertEqual(plain[:-plain[-1]], path.read_bytes())
