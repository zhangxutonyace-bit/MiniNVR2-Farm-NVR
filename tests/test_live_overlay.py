"""Offline temporal/worker assertions; synthetic pixels are not camera acceptance."""
import threading
import time
import unittest
from dataclasses import replace
from unittest import mock

import cv2
import numpy as np

from nvr.algorithm_catalog import revision_for
from nvr.algorithm_types import DetectionBatch, ObjectDetection, frame_key
from nvr.camera import Cam
from nvr.config import Config
from nvr.event_overlay import FramePacket
from nvr.live_overlay import LivePreviewWorker, PairedLiveOverlay, jpeg_size
from tests.support import OfflineTestCase
from tests.test_target_tracking import Scene, center


OPTIONS = {"model_id": "nanodet-coco", "targets": ["cup", "person", "cat", "dog"], "confidence": .35}
REVISION = revision_for(OPTIONS)


def packet(image, seq, stamp=None, source=("scene", 1)):
    ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 94])
    assert ok
    return FramePacket(jpeg.tobytes(), seq, seq / 8 if stamp is None else stamp, source)


def decision(sample, boxes, class_ids=None, revision=REVISION):
    image = cv2.imdecode(np.frombuffer(sample.jpeg, np.uint8), cv2.IMREAD_COLOR)
    ids = class_ids if class_ids is not None else [41] * len(boxes)
    found = tuple(ObjectDetection(tuple(box), .9, cid) for box, cid in zip(boxes, ids))
    return DetectionBatch(frame_key(sample, revision), (image.shape[1], image.shape[0]), found), sample


class PairedOverlayTests(unittest.TestCase):
    def test_late_decisions_replay_to_current_motion_without_refreshing_semantic_time(self):
        overlay, scene, delayed, errors = PairedLiveOverlay(), Scene(), {}, []
        for i in range(24):
            image, truth = scene.frame([(20 + 16 * i, 70)])
            sample = packet(image, i)
            if i % 3 == 0:
                delayed[i + 2] = decision(sample, truth)
            result = overlay.process(sample, delayed.get(i), REVISION)
            self.assertEqual(sample.jpeg, packet(image, i).jpeg, "raw input must remain pristine")
            if i >= 2:
                self.assertEqual(len(overlay.last_detections), 1)
                found = overlay.last_detections[0]
                errors.append(float(np.linalg.norm(center(found.box) - center(truth[0]))))
                self.assertEqual(found.class_id, 41)
                self.assertNotEqual(result, sample.jpeg)
                self.assertLessEqual(overlay.tracker.tracks[0].confirmed_at, (i - 2) / 8)
        self.assertGreaterEqual(sum(e <= 6 for e in errors) / len(errors), .95)

    def test_delayed_jitter_uses_preceding_checkpoints_instead_of_resetting_tracker(self):
        overlay, scene, delayed, raw, filtered = PairedLiveOverlay(), Scene(), {}, [], []
        image, truth = scene.frame([(170, 70)])
        for i in range(28):
            sample = packet(image, i)
            shift = 6 if i % 2 else -6
            observed = (170 + shift, 70, 52, 108)
            delayed[i + 2] = decision(sample, [observed])
            overlay.process(sample, delayed.get(i), REVISION)
            if i >= 6:
                raw.append(center(observed)[0])
                filtered.append(center(overlay.last_detections[0].box)[0])
        self.assertLess(np.std(filtered), np.std(raw) * .75)

    def test_semantic_expiry_departure_and_unrelated_jump_clear_old_boxes(self):
        for change in ("expiry", "departure", "jump"):
            with self.subTest(change=change):
                overlay, scene = PairedLiveOverlay(), Scene()
                first, boxes = scene.frame([(30, 70)])
                initial = packet(first, 0)
                overlay.process(initial, decision(initial, boxes), REVISION)
                self.assertEqual(len(overlay.last_detections), 1)
                for i in range(1, 9):
                    image, _ = scene.frame([(30 + 8 * i, 70)])
                    if change == "departure": image = scene.background
                    if change == "jump": image, _ = scene.frame([(360, 70)])
                    sample = packet(image, i)
                    result = overlay.process(sample, None, REVISION)
                    if change != "expiry" or i == 7:
                        self.assertEqual(overlay.last_detections, ())
                        self.assertEqual(result, sample.jpeg)

    def test_multiple_classes_never_exchange_identity_and_pristine_image_survives(self):
        overlay, scene = PairedLiveOverlay(), Scene()
        for i in range(12):
            image, boxes = scene.frame([(20 + 16 * i, 40), (370 - 16 * i, 145)])
            before = image.copy()
            sample = packet(image, i)
            overlay.process(sample, decision(sample, boxes, [15, 16]), REVISION)
            self.assertEqual({d.class_id for d in overlay.last_detections}, {15, 16})
            for cid, box in zip((15, 16), boxes):
                found = next(d for d in overlay.last_detections if d.class_id == cid)
                self.assertLess(np.linalg.norm(center(found.box) - center(box)), 6)
            np.testing.assert_array_equal(image, before)

    def test_result_must_match_exact_original_bytes_revision_source_and_size(self):
        scene = Scene()
        image, boxes = scene.frame([(30, 70)])
        first = packet(image, 0)
        batch, anchor = decision(first, boxes)
        for kind in ("digest", "revision", "source", "size", "future"):
            with self.subTest(kind=kind):
                overlay = PairedLiveOverlay()
                candidate, old = batch, anchor
                if kind == "digest": old = replace(anchor, jpeg=anchor.jpeg + b"wrong")
                if kind == "source": old = replace(anchor, source=("other", 1))
                if kind == "size": candidate = replace(batch, size=(960, 540))
                if kind == "future": old = replace(anchor, timestamp=1.)
                if kind == "revision": candidate = replace(batch, key=frame_key(anchor, ("wrong",)))
                self.assertEqual(overlay.process(first, (candidate, old), REVISION), first.jpeg)
                self.assertEqual(overlay.last_detections, ())

    def test_generation_epoch_resolution_reorder_and_long_gap_invalidate(self):
        for kind in ("revision", "epoch", "size", "reorder", "gap"):
            with self.subTest(kind=kind):
                overlay, scene = PairedLiveOverlay(), Scene()
                image, boxes = scene.frame([(30, 70)])
                first = packet(image, 10, stamp=1.)
                latest = decision(first, boxes)
                overlay.process(first, latest, REVISION)
                sample, revision = packet(image, 11, stamp=1.125), REVISION
                if kind == "revision": revision = revision_for(dict(OPTIONS, targets=["dog"]))
                if kind == "epoch": sample = replace(sample, source=("scene", 2))
                if kind == "size": sample = packet(cv2.resize(image, (960, 540)), 11, stamp=1.125)
                if kind == "reorder": sample = packet(image, 9, stamp=.875)
                if kind == "gap": sample = packet(image, 11, stamp=2.)
                result = overlay.process(sample, latest, revision)
                self.assertEqual(result, sample.jpeg)
                self.assertEqual(overlay.last_detections, ())

    def test_missing_capture_can_be_inserted_at_its_real_semantic_timestamp(self):
        overlay, scene = PairedLiveOverlay(), Scene()
        samples, truths = [], []
        for i in range(4):
            image, boxes = scene.frame([(30 + 12 * i, 70)])
            samples.append(packet(image, i))
            truths.append(boxes)
        overlay.process(samples[0], None, REVISION)
        overlay.process(samples[2], decision(samples[1], truths[1]), REVISION)
        self.assertEqual([f.packet.seq for f in overlay.frames], [0, 1, 2])
        self.assertEqual(overlay.tracker.tracks[0].confirmed_at, .125)
        self.assertLess(np.linalg.norm(center(overlay.last_detections[0].box) - center(truths[2][0])), 6)
        overlay.process(samples[3], None, REVISION)
        self.assertEqual(overlay.tracker.tracks[0].confirmed_at, .125)

    def test_history_is_bounded_and_semantic_older_than_checkpoint_is_not_reintroduced(self):
        overlay, scene = PairedLiveOverlay(), Scene()
        image, boxes = scene.frame([(30, 70)])
        original = packet(image, 0, stamp=0.)
        for i in range(60):
            sample = packet(image, i, stamp=i / 50)
            overlay.process(sample, None, REVISION)
            self.assertLessEqual(len(overlay.frames), overlay.MAX_FRAMES)
            self.assertLessEqual(sample.timestamp - overlay.frames[0].packet.timestamp, overlay.MAX_AGE)
        self.assertEqual(overlay.process(sample, decision(original, boxes), REVISION), sample.jpeg)
        self.assertEqual(overlay.last_detections, ())

    def test_thirty_fps_late_anchor_inside_semantic_lifetime_remains_available(self):
        overlay, scene, samples, truths = PairedLiveOverlay(), Scene(), [], []
        for i in range(20):
            image, boxes = scene.frame([(30 + 4 * i, 70)])
            samples.append(packet(image, i, stamp=i / 30))
            truths.append(boxes)
            latest = decision(samples[0], truths[0]) if i == 12 else None
            overlay.process(samples[-1], latest, REVISION)
            if i >= 12:
                self.assertEqual(len(overlay.last_detections), 1)
                self.assertLess(np.linalg.norm(center(overlay.last_detections[0].box) - center(boxes[0])), 6)
                self.assertEqual(overlay.tracker.tracks[0].confirmed_at, 0.)
        self.assertLessEqual(len(overlay.frames), overlay.MAX_FRAMES)
        self.assertEqual(overlay.MAX_AGE, .75)

    def test_semantic_status_distinguishes_unavailable_negative_and_stale(self):
        overlay, scene = PairedLiveOverlay(), Scene()
        image, boxes = scene.frame([(30, 70)])
        first = packet(image, 0)
        self.assertEqual(overlay.process(first, None, REVISION), first.jpeg)
        self.assertEqual(overlay.semantic_state, "unavailable")
        overlay.process(first, decision(first, []), REVISION)
        self.assertEqual(overlay.semantic_state, "fresh")
        for i in range(1, 8):
            overlay.process(packet(image, i), None, REVISION)
        self.assertEqual(overlay.semantic_state, "stale")

    def test_repeated_capture_can_accept_late_result_without_drawing_into_raw(self):
        overlay, scene = PairedLiveOverlay(), Scene()
        image, boxes = scene.frame([(30, 70)])
        sample = packet(image, 0)
        self.assertEqual(overlay.process(sample, None, REVISION), sample.jpeg)
        painted = overlay.process(sample, decision(sample, boxes), REVISION)
        self.assertNotEqual(painted, sample.jpeg)
        self.assertEqual(len(overlay.frames), 1)
        self.assertEqual(len(overlay.last_detections), 1)

    def test_expired_semantics_never_paint_new_frame_but_supported_gap_allows_short_paired_display(self):
        for state in ("supported", "departed", "negative"):
            with self.subTest(state=state):
                overlay, scene = PairedLiveOverlay(), Scene()
                for i in range(8):
                    image, boxes = scene.frame([(30 + 8 * i, 70)])
                    if i == 7 and state == "departed": image = scene.background
                    sample = packet(image, i)
                    latest = decision(sample, boxes) if i == 0 else None
                    if i == 7 and state == "negative": latest = decision(sample, [])
                    result = overlay.process(sample, latest, REVISION)
                self.assertEqual(result, sample.jpeg, "expired coordinates cannot paint current pixels")
                self.assertEqual(overlay.last_detections, ())
                self.assertEqual(overlay.awaiting_semantic, state == "supported")
                self.assertEqual(overlay.MAX_AGE, .75)

    def test_deselected_classes_are_filtered_before_tracking(self):
        overlay, scene = PairedLiveOverlay(), Scene()
        image, boxes = scene.frame([(30, 70)])
        sample = packet(image, 0)
        only_dog = revision_for(dict(OPTIONS, targets=["dog"]))
        batch = decision(sample, boxes, [41], revision=only_dog)
        self.assertEqual(overlay.process(sample, batch, only_dog), sample.jpeg)
        self.assertEqual(overlay.last_detections, ())


class _CountingProcessor:
    def __init__(self, gate=None):
        self.calls = 0
        self.resets = 0
        self.entered = threading.Event()
        self.gate = gate

    def reset(self):
        self.resets += 1

    def process(self, sample, latest, context):
        self.calls += 1
        self.entered.set()
        if self.gate is not None: self.gate.wait(3.)
        return sample.jpeg + b"painted"


class PreviewWorkerTests(OfflineTestCase):
    def make_worker(self, processor=None):
        self.current = FramePacket(b"raw", 4, time.time(), ("fixture", 1))
        self.context = REVISION
        self.processor = processor or _CountingProcessor()
        self.warn = mock.Mock()
        worker = LivePreviewWorker(lambda: self.current, lambda: None,
                                   lambda: self.context, self.warn, self.processor)
        self.addCleanup(worker.stop)
        return worker

    def test_three_viewers_share_one_render_and_return_paired_cache_identity(self):
        worker = self.make_worker()
        for _ in range(3): self.assertEqual(worker.read(self.current, self.context), self.current)
        self.wait_until(lambda: worker.cached is not None)
        first = self.current
        # A cache from a slightly older capture is valid and keeps its own
        # timestamp/sequence; it must not alternate raw only because seq grew.
        self.current = replace(first, jpeg=b"new-raw", seq=5, timestamp=first.timestamp + .01)
        for _ in range(3):
            result = worker.read(self.current, self.context)
            self.assertEqual(result.seq, 4)
            self.assertEqual(result.timestamp, first.timestamp)
            self.assertEqual(result.jpeg, b"rawpainted")
        self.assertIsNotNone(worker.thread)
        self.assertLessEqual(self.processor.calls, 2)

    def test_slow_native_work_never_blocks_readers_queues_or_replaces_worker(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        worker = self.make_worker(_CountingProcessor(gate))
        worker.read(self.current, self.context)
        self.assertTrue(self.processor.entered.wait(1.))
        thread = worker.thread
        started = time.monotonic()
        for i in range(80):
            self.current = replace(self.current, seq=i + 5, timestamp=time.time())
            observed = worker.read(self.current, self.context)
            self.assertEqual(observed.jpeg, self.current.jpeg)
            self.assertLessEqual(observed.seq, self.current.seq)
            self.assertIs(worker.thread, thread)
        self.assertLess(time.monotonic() - started, .2)
        self.assertEqual(self.processor.calls, 1)
        self.assertIsNone(worker.cached)
        gate.set()
        self.wait_until(lambda: self.processor.calls == 2)
        self.assertLessEqual(self.processor.calls, 2, "only latest sample survives slow processing")

    def test_hung_render_over_budget_returns_every_new_raw_frame_without_holding(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        worker = self.make_worker(_CountingProcessor(gate))
        worker.read(self.current, self.context)
        self.assertTrue(self.processor.entered.wait(1.))
        time.sleep(worker.CACHE_MAX_AGE + .04)
        for i in range(20):
            self.current = FramePacket(("raw-%d" % i).encode(), 10 + i, time.time(), ("fixture", 1))
            self.assertEqual(worker.read(self.current, self.context), self.current)
        self.assertEqual(self.processor.calls, 1)
        gate.set()

    def test_old_epoch_context_and_expired_cache_are_never_returned(self):
        worker = self.make_worker()
        worker.read(self.current, self.context)
        self.wait_until(lambda: worker.cached is not None)
        with worker.lock:
            cached = worker.cached
            worker.cached = (replace(cached[0], timestamp=time.time() - .4), cached[1])
        self.assertEqual(worker.read(self.current, self.context), self.current)
        with worker.lock: worker.cached = cached
        new = replace(self.current, source=("fixture", 2))
        self.assertEqual(worker.read(new, self.context), new)
        new_context = revision_for(dict(OPTIONS, targets=["dog"]))
        self.assertEqual(worker.read(self.current, new_context), self.current)

    def test_semantic_gap_keeps_only_fresh_paired_cache_then_new_semantics_replace_it(self):
        processor = _CountingProcessor()
        processor.awaiting_semantic = False
        original = processor.process
        def staged(sample, latest, context):
            result = original(sample, latest, context)
            processor.awaiting_semantic = sample.seq == 5
            return sample.jpeg if processor.awaiting_semantic else result
        processor.process = staged
        worker = self.make_worker(processor)
        self.current = replace(self.current, timestamp=time.time() - .15)
        worker.read(self.current, self.context)
        self.wait_until(lambda: worker.cached is not None)
        first = self.current
        self.current = replace(first, jpeg=b"next-raw", seq=5, timestamp=first.timestamp + .125)
        self.wait_until(lambda: processor.calls >= 2)
        held = worker.read(self.current, self.context)
        self.assertEqual(held.jpeg, b"rawpainted")
        self.assertEqual((held.seq, held.timestamp), (first.seq, first.timestamp))
        self.assertEqual(self.current.jpeg, b"next-raw")
        self.current = replace(self.current, jpeg=b"renewed", seq=6, timestamp=time.time())
        self.wait_until(lambda: worker.cached is not None and worker.cached[0].seq == 6)
        self.assertEqual(worker.read(self.current, self.context).jpeg, b"renewedpainted")

    def test_semantic_display_bridge_expires_without_changing_capture_time_and_warns(self):
        worker = self.make_worker()
        worker.read(self.current, self.context)
        self.wait_until(lambda: worker.cached is not None)
        with worker.lock:
            previous = worker.cached[0]
            worker.cached = (replace(previous, timestamp=time.time() - .31), self.context)
            worker.waiting_semantic = True
        result = worker.read(self.current, self.context)
        self.assertEqual(result, self.current)
        self.assertEqual(result.jpeg, b"raw")
        self.assertTrue(any("短时画框缓存已结束" in call.args[0] for call in self.warn.call_args_list))
        self.assertEqual(worker.CACHE_MAX_AGE, .30)

    def test_raw_fallback_advances_watermark_and_cache_cannot_regress_afterward(self):
        worker = self.make_worker()
        worker.read(self.current, self.context)
        self.wait_until(lambda: worker.cached is not None)
        old = worker.cached[0]
        # A newer raw frame has already been delivered because its old cache
        # briefly expired. Making that old cache fresh again cannot go back.
        self.current = replace(self.current, seq=7, timestamp=time.time())
        with worker.lock:
            worker.cached = (replace(old, timestamp=time.time() - .4), self.context)
            worker.last_output = None
        self.assertEqual(worker.read(self.current, self.context).seq, 7)
        with worker.lock:
            worker.cached = (replace(old, timestamp=time.time()), self.context)
        self.assertEqual(worker.read(self.current, self.context).seq, 7)
        # A render which overtook the caller's raw snapshot keeps its own newer
        # identity. A new source epoch resets the monotonic watermark.
        with worker.lock:
            worker.cached = (replace(old, seq=8, timestamp=time.time()), self.context)
        self.assertEqual(worker.read(self.current, self.context).seq, 8)
        fresh_source = replace(self.current, seq=1, source=("fixture", 2))
        self.assertEqual(worker.read(fresh_source, self.context), fresh_source)

    def test_invalidate_and_stop_discard_retiring_native_result(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        worker = self.make_worker(_CountingProcessor(gate))
        worker.read(self.current, self.context)
        self.assertTrue(self.processor.entered.wait(1.))
        worker.invalidate()
        worker.stop()
        gate.set()
        worker.thread.join(1.)
        self.assertFalse(worker.thread.is_alive())
        self.assertIsNone(worker.cached)
        self.assertEqual(worker.read(self.current, self.context), self.current)

    def test_worker_error_falls_back_without_poisoning_camera_bytes(self):
        processor = _CountingProcessor()
        processor.process = mock.Mock(side_effect=RuntimeError("controlled native failure"))
        worker = self.make_worker(processor)
        worker.read(self.current, self.context)
        self.wait_until(lambda: self.warn.called)
        self.assertIsNone(worker.cached)
        self.assertEqual(self.current.jpeg, b"raw")

    def test_thread_start_failure_retains_one_slot_returns_raw_and_stops_safely(self):
        worker = self.make_worker()
        with mock.patch("nvr.live_overlay.threading.Thread.start", side_effect=RuntimeError("controlled start failure")):
            self.assertEqual(worker.read(self.current, self.context), self.current)
        retired = worker.thread
        self.assertIsNotNone(retired)
        self.assertEqual(worker.read(self.current, self.context), self.current)
        self.assertIs(worker.thread, retired)
        worker.stop()
        self.assertFalse(retired.is_alive())
        self.assertEqual(self.processor.calls, 0)

    def test_start_failure_recovers_after_bounded_cooldown_without_parallel_workers(self):
        worker = self.make_worker()
        starts, real_start = [], threading.Thread.start
        def controlled_start(thread):
            starts.append(thread)
            if len(starts) == 1:
                raise RuntimeError("controlled transient thread limit")
            return real_start(thread)
        with mock.patch("nvr.live_overlay.threading.Thread.start", new=controlled_start):
            worker.read(self.current, self.context)
            for _ in range(10):
                worker.read(self.current, self.context)
            self.assertEqual(len(starts), 1)
            deadline = time.monotonic() + 1.5
            while len(starts) == 1 and time.monotonic() < deadline:
                self.current = replace(self.current, timestamp=time.time())
                worker.read(self.current, self.context)
                time.sleep(.025)
            self.assertEqual(len(starts), 2)
            self.assertFalse(starts[0].is_alive())
            self.wait_until(lambda: worker.cached is not None)
            self.assertIs(worker.thread, starts[1])

    def test_continuous_thirty_fps_reads_recover_with_a_hundred_ms_renderer(self):
        processor = _CountingProcessor()
        original = processor.process
        def slow_process(sample, latest, context):
            time.sleep(.10)
            return original(sample, latest, context)
        processor.process = slow_process
        worker = self.make_worker(processor)
        started, observed = time.monotonic(), []
        previous = -1
        while time.monotonic() - started < 1.:
            seq = int((time.monotonic() - started) * 30)
            if seq != previous:
                self.current = FramePacket(("raw-%d" % seq).encode(), seq, time.time(), ("fixture", 1))
                previous = seq
            result = worker.read(self.current, self.context)
            observed.append((result.seq, result.jpeg.endswith(b"painted"), time.time() - result.timestamp))
            time.sleep(.006)
        self.assertTrue(all(a[0] <= b[0] for a, b in zip(observed, observed[1:])))
        self.assertGreaterEqual(sum(marked for _, marked, _ in observed), len(observed) * .5)
        self.assertGreaterEqual(len({seq for seq, marked, _ in observed if marked}), 4)
        self.assertLessEqual(max(age for _, _, age in observed), .31)
        self.assertGreaterEqual(processor.calls, 5)

    def test_repeated_over_budget_jobs_stream_raw_freely_then_timely_rendering_recovers(self):
        processor = _CountingProcessor()
        original = processor.process
        starts, slow_done = [], []
        def varying(sample, latest, context):
            index = len(starts)
            starts.append(index)
            time.sleep(.32 if index < 2 else .10)
            result = original(sample, latest, context)
            if index < 2:
                slow_done.append(index)
            return result
        processor.process = varying
        worker = self.make_worker(processor)
        started, previous, observations, checked_raw = time.monotonic(), -1, [], 0
        while time.monotonic() - started < 1.4:
            elapsed = time.monotonic() - started
            seq = int(elapsed * 30)
            if seq != previous:
                self.current = FramePacket(("raw-%d" % seq).encode(), seq, time.time(), ("fixture", 1))
                previous = seq
            result = worker.read(self.current, self.context)
            if elapsed > .35 and len(slow_done) < 2:
                self.assertEqual(result, self.current, "a new slow job must not re-enable holding raw")
                checked_raw += 1
            observations.append((elapsed, result.seq, result.jpeg.endswith(b"painted")))
            time.sleep(.006)
        self.assertGreater(checked_raw, 15)
        self.assertTrue(all(a[1] <= b[1] for a, b in zip(observations, observations[1:])))
        recovered = [marked for elapsed, _, marked in observations if elapsed > 1.1]
        self.assertGreater(sum(recovered), len(recovered) * .5)
        self.assertFalse(worker.degraded)

    def test_jpeg_dimensions_need_no_native_decode_and_malformed_headers_are_bounded(self):
        for progressive in (0, 1):
            image = np.zeros((27, 43, 3), np.uint8)
            ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_PROGRESSIVE, progressive])
            self.assertTrue(ok)
            with mock.patch("cv2.imdecode", side_effect=AssertionError("HTTP must not decode")):
                self.assertEqual(jpeg_size(data.tobytes()), (43, 27))
        for data in (b"", b"raw", b"\xff\xd8", b"\xff\xd8\xff\xc0\x00\x01",
                     b"\xff\xd8\xff\xe0\xff\xffx", b"\xff\xd8\xff\xda\x00\x02"):
            self.assertIsNone(jpeg_size(data))

    def test_first_new_size_read_never_reuses_old_size_cache_in_same_epoch(self):
        worker = self.make_worker()
        self.current = packet(np.zeros((20, 30, 3), np.uint8), 1, stamp=time.time(), source=("fixture", 1))
        worker.read(self.current, self.context)
        self.wait_until(lambda: worker.cached is not None)
        old_source = self.current.source
        self.current = packet(np.zeros((40, 50, 3), np.uint8), 2, stamp=time.time(), source=old_source)
        result = worker.read(self.current, self.context)
        self.assertEqual(jpeg_size(result.jpeg), (50, 40))
        self.assertEqual(result.source, old_source, "dimension checking must not mutate capture epoch")

    def test_size_changed_during_processing_cannot_publish_old_size_result(self):
        first, second, second_entered = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(first.set)
        self.addCleanup(second.set)
        processor = _CountingProcessor()
        original = processor.process
        def gated(sample, latest, context):
            if jpeg_size(sample.jpeg) == (30, 20):
                processor.entered.set()
                first.wait(2.)
            else:
                second_entered.set()
                second.wait(2.)
            return original(sample, latest, context)
        processor.process = gated
        worker = self.make_worker(processor)
        self.current = packet(np.zeros((20, 30, 3), np.uint8), 1, stamp=time.time(), source=("fixture", 1))
        worker.read(self.current, self.context)
        self.assertTrue(processor.entered.wait(1.))
        self.current = packet(np.zeros((40, 50, 3), np.uint8), 2, stamp=time.time(), source=("fixture", 1))
        # No second read is needed for the publication-time size guard.
        first.set()
        self.assertTrue(second_entered.wait(1.))
        self.assertIsNone(worker.cached)
        self.current = replace(self.current, timestamp=time.time())
        second.set()
        worker.read(self.current, self.context)
        self.wait_until(lambda: worker.cached is not None)
        self.assertEqual(jpeg_size(worker.cached[0].jpeg), (50, 40))

    def test_persistent_failures_retry_only_new_keys_with_small_bounded_backoff(self):
        processor = _CountingProcessor()
        processor.process = mock.Mock(side_effect=RuntimeError("persistent controlled failure"))
        worker = self.make_worker(processor)
        worker.read(self.current, self.context)
        self.wait_until(lambda: processor.process.called)
        time.sleep(.25)
        self.assertEqual(processor.process.call_count, 1, "fixed failing frames must not spin")
        started = time.monotonic()
        while time.monotonic() - started < .55:
            self.current = replace(self.current, seq=self.current.seq + 1, timestamp=time.time())
            worker.read(self.current, self.context)
            time.sleep(.01)
        self.assertLessEqual(processor.process.call_count, 4)
        self.assertIsNone(worker.cached)

    def test_missing_semantics_warns_but_exact_fresh_negative_is_healthy(self):
        worker = self.make_worker(PairedLiveOverlay())
        self.current = packet(np.zeros((80, 120, 3), np.uint8), 1, stamp=time.time(), source=("fixture", 1))
        latest = [None]
        worker.latest = lambda: latest[0]
        worker.read(self.current, self.context)
        self.wait_until(lambda: any("尚未收到" in str(c.args[0]) for c in self.warn.call_args_list))
        self.assertEqual(worker.processor.semantic_state, "unavailable")
        latest[0] = decision(self.current, [])
        self.wait_until(lambda: worker.processor.semantic_state == "fresh")
        self.wait_until(lambda: self.warn.call_args_list[-1].args == ("",))
        self.assertEqual(worker.cached[0].jpeg, self.current.jpeg)

    def test_camera_raw_path_never_starts_worker_or_infers_and_model_mode_is_required(self):
        cam = Cam({"id": "fixture", "url": "offline.mp4"}, self.cfg)
        self.addCleanup(cam.stop)
        cam.state, cam.last_jpeg, cam.last_ts, cam.seq = "online", b"raw", time.time(), 1
        with mock.patch.object(cam, "market_infer", side_effect=AssertionError("no additional inference")):
            self.assertEqual(cam.preview_snapshot(), (b"raw", 1))
            self.assertIsNone(cam._live_preview)
            self.cfg.set("detect", "mode", "market", save=False)
            self.assertEqual(cam.preview_snapshot(boxes=False), (b"raw", 1))
            self.assertIsNone(cam._live_preview)
            self.cfg.set("detect", "enabled", False, save=False)
            self.assertEqual(cam.preview_snapshot(), (b"raw", 1))
            self.assertIsNone(cam._live_preview)
        self.assertEqual(cam.snapshot(), (b"raw", 1))

    def test_camera_stop_wins_race_before_lazy_worker_admission(self):
        cam = Cam({"id": "fixture", "url": "offline.mp4"}, self.cfg)
        self.addCleanup(cam.stop)
        cam.state, cam.last_jpeg, cam.last_ts, cam.seq = "online", b"raw", time.time(), 1
        context_ready, continue_read = threading.Event(), threading.Event()
        self.addCleanup(continue_read.set)

        def delayed_context():
            context_ready.set()
            self.assertTrue(continue_read.wait(2.))
            return REVISION

        result = []
        with mock.patch.object(cam, "_preview_context", side_effect=delayed_context), \
                mock.patch("nvr.live_overlay.LivePreviewWorker") as factory:
            reader = threading.Thread(target=lambda: result.append(cam.preview_sample()))
            reader.start()
            self.assertTrue(context_ready.wait(1.))
            # This completes while the reader still holds its stale context.
            cam.stop()
            continue_read.set()
            reader.join(2.)
            self.assertFalse(reader.is_alive())
            factory.assert_not_called()
        self.assertIsNone(cam._live_preview)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].jpeg, b"")

    def test_camera_stop_after_raw_sampling_cannot_return_early_none_context_raw(self):
        self.cfg.set("detect", "mode", "market", save=False)
        cam = Cam({"id": "fixture", "url": "offline.mp4"}, self.cfg)
        self.addCleanup(cam.stop)
        cam.state, cam.last_jpeg, cam.last_ts, cam.seq = "online", b"old-raw", time.time(), 1
        ready, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        original = cam._preview_context
        def delayed_real_context():
            ready.set()
            self.assertTrue(resume.wait(2.))
            return original()  # The real method returns None after stop().
        results = []
        with mock.patch.object(cam, "_preview_context", side_effect=delayed_real_context):
            reader = threading.Thread(target=lambda: results.append(cam.preview_sample()))
            reader.start()
            self.assertTrue(ready.wait(1.))
            cam.stop()
            resume.set()
            reader.join(2.)
            self.assertFalse(reader.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].jpeg, b"")
        self.assertIsNone(cam._live_preview)

    def test_real_camera_cache_chain_recovers_at_thirty_fps_with_slow_renderer(self):
        self.cfg.set("detect", "mode", "market", save=False)
        self.cfg.upsert_camera({"id": "fixture", "url": "offline.mp4"})
        cam = Cam({"id": "fixture", "url": "offline.mp4"}, self.cfg)
        self.addCleanup(cam.stop)
        cam.state = "online"
        raw = packet(np.zeros((40, 60, 3), np.uint8), 0).jpeg
        processor = _CountingProcessor()
        original = processor.process
        def slow(sample, latest, context):
            time.sleep(.10)
            return original(sample, latest, context)
        processor.process = slow
        cam._live_preview = LivePreviewWorker(cam.record_sample, cam.market_latest, cam._preview_context,
                                               cam._preview_failed, processor, slot=(self.cfg, cam.id))
        started, results = time.monotonic(), []
        while time.monotonic() - started < .85:
            with cam.lock:
                seq = int((time.monotonic() - started) * 30)
                if seq != cam.seq or not cam.last_jpeg:
                    cam.seq, cam.last_ts, cam.last_jpeg = seq, time.time(), raw
            result = cam.preview_sample()
            results.append((result.seq, result.jpeg.endswith(b"painted")))
            time.sleep(.008)
        self.assertGreaterEqual(sum(marked for _, marked in results), len(results) * .5)
        self.assertTrue(all(a[0] <= b[0] for a, b in zip(results, results[1:])))
        self.assertEqual(cam.last_jpeg, raw)
        self.assertFalse(cam.snapshot()[0].endswith(b"painted"))

    def test_saved_detection_disable_while_renderer_is_running_rejects_its_publication(self):
        self.cfg.set("detect", "mode", "market", save=False)
        self.cfg.upsert_camera({"id": "fixture", "url": "offline.mp4"})
        cam = Cam({"id": "fixture", "url": "offline.mp4"}, self.cfg)
        gate = threading.Event()
        self.addCleanup(cam.stop)
        self.addCleanup(gate.set)
        cam.state, cam.last_jpeg, cam.last_ts, cam.seq = "online", b"raw", time.time(), 1
        processor = _CountingProcessor(gate)
        cam._live_preview = LivePreviewWorker(cam.record_sample, cam.market_latest,
            cam._preview_context, cam._preview_failed, processor, slot=(self.cfg, cam.id))
        cam.preview_sample()
        self.assertTrue(processor.entered.wait(1.))
        self.cfg.get_camera(cam.id)["detect"] = False
        self.assertTrue(cam.detect_on, "asynchronous Hub sync has not run")
        gate.set()
        self.wait_until(lambda: cam._live_preview.started_at == 0.)
        with cam._live_preview.lock:
            self.assertIsNone(cam._live_preview.cached)
        self.assertIsNone(cam._preview_context())
        self.assertEqual(cam.preview_sample().jpeg, b"raw")

    def test_replaced_cameras_share_retirement_slot_until_old_native_worker_exits(self):
        self.cfg.set("detect", "mode", "market", save=False)
        gate = threading.Event()
        self.addCleanup(gate.set)
        old = Cam({"id": "same", "url": "old.mp4"}, self.cfg)
        new = Cam({"id": "same", "url": "new.mp4"}, self.cfg)
        self.addCleanup(old.stop)
        self.addCleanup(new.stop)
        for cam in (old, new):
            cam.state, cam.last_jpeg, cam.last_ts, cam.seq = "online", b"raw", time.time(), 1
        old_processor, new_processor = _CountingProcessor(gate), _CountingProcessor()
        old._live_preview = LivePreviewWorker(old.record_sample, old.market_latest, old._preview_context,
                                               old._preview_failed, old_processor, slot=(self.cfg, old.id))
        new._live_preview = LivePreviewWorker(new.record_sample, new.market_latest, new._preview_context,
                                               new._preview_failed, new_processor, slot=(self.cfg, new.id))
        self.cfg.upsert_camera({"id": "same", "url": "old.mp4"})
        old.preview_sample()
        self.assertTrue(old_processor.entered.wait(1.))
        old.stop()  # Returns after 0.3 s, but the native operation is still owned.
        self.assertTrue(old._live_preview.thread.is_alive())
        self.cfg.upsert_camera({"id": "same", "url": "new.mp4"})
        for index in range(8):
            new.seq, new.last_ts, new.last_jpeg = index + 2, time.time(), ("raw-%d" % index).encode()
            result = new.preview_sample()
            self.assertEqual((result.seq, result.jpeg), (new.seq, new.last_jpeg),
                             "a blocked retirement slot must stream current raw, not hold it")
        self.assertIsNone(new._live_preview.thread)
        self.assertEqual(new_processor.calls, 0)
        # Another independent configuration with the same camera id is isolated.
        other_cfg = Config(str(self.root / "independent-config.json"))
        other_cfg.set("detect", "mode", "market", save=False)
        other_cfg.upsert_camera({"id": "same", "url": "other.mp4"})
        other = Cam({"id": "same", "url": "other.mp4"}, other_cfg)
        self.addCleanup(other.stop)
        other.state, other.last_jpeg, other.last_ts, other.seq = "online", b"other", time.time(), 1
        other_processor = _CountingProcessor()
        other._live_preview = LivePreviewWorker(other.record_sample, other.market_latest, other._preview_context,
                                                 other._preview_failed, other_processor, slot=(other_cfg, other.id))
        other.preview_sample()
        self.assertTrue(other_processor.entered.wait(1.))
        gate.set()
        old._live_preview.thread.join(1.)
        self.assertFalse(old._live_preview.thread.is_alive())
        deadline = time.monotonic() + 1.
        while new_processor.calls == 0 and time.monotonic() < deadline:
            new.last_ts = time.time()
            new.preview_sample()
            time.sleep(.025)
        self.assertGreater(new_processor.calls, 0)
        self.assertIsNotNone(new._live_preview.thread)


if __name__ == "__main__":
    unittest.main()
