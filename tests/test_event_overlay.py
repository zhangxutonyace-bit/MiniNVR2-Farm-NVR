"""Offline frame correspondence, unchanged decisions, and burned-in MP4 checks."""
import json
import re
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from nvr import ffmpeg, local_camera
from nvr.camera import Cam
from nvr.config import Config, TARGET_BOX_COLORS
from nvr.detect import Detector
from nvr.event_overlay import AnnotationRunner, Detection, EventOverlay, FramePacket
from nvr.person_localizer import PersonDetection
from nvr.server import create_app
from tests.support import OfflineTestCase
from tests.test_api import FakeHub
from tests.test_local_camera import spec


def image_packet(seq=1, source=("fixture",), value=40, detection=None, size=(320, 180)):
    image = np.full((size[1], size[0], 3), value, np.uint8)
    jpeg = cv2.imencode(".jpg", image)[1].tobytes()
    return FramePacket(jpeg, seq, time.time(), source, detection)


def colored_pixels(image, color):
    bgr = np.array([int(color[i:i + 2], 16) for i in (5, 3, 1)])
    return np.max(np.abs(image.astype(np.int16) - bgr), axis=2) < 65


class FixedLocalizer:
    """Explicit semantic fixture, unrelated to the Hub's contour metadata."""
    def locate(self, image):
        return (PersonDetection((70, 20, 80, 130), .9),)


class DetectorMetadataTests(OfflineTestCase):
    def controlled(self, mode="auto"):
        detector = Detector({"mode": mode, "min_area": 0.001})
        detector._warmed = True
        detector.mog = mock.Mock()
        detector.mog.apply.return_value = np.zeros((180, 320), np.uint8)
        # First region is wide/unconfirmed; the second is tall enough for the
        # historical heuristic. Haar confirmation is relative to either ROI.
        contours = [np.array([[[20, 30]], [[80, 30]], [[80, 60]], [[20, 60]]]),
                    np.array([[[150, 20]], [[180, 20]], [[180, 160]], [[150, 160]]])]
        return detector, contours

    def test_haar_reports_real_roi_offsets_and_excludes_unconfirmed_contour(self):
        detector, contours = self.controlled("haar")
        detector.cls = mock.Mock()
        detector.cls.detectMultiScale.side_effect = [(), [(3, 7, 20, 90)]]
        with mock.patch("nvr.detect.cv2.findContours", return_value=(contours, None)):
            self.assertTrue(detector.process(np.zeros((180, 320, 3), np.uint8)))
        self.assertEqual(detector.confirmed_boxes, [(153, 27, 20, 90)])
        self.assertEqual(len(detector.boxes), 2)
        self.assertEqual(detector.provenance, "haar")

    def test_modes_positive_negative_and_warmup_metadata_never_inherit(self):
        for mode in ("auto", "haar", "motion"):
            detector, contours = self.controlled(mode)
            detector.cls = None
            with self.subTest(mode=mode), mock.patch("nvr.detect.cv2.findContours", return_value=(contours, None)):
                positive = detector.process(np.zeros((180, 320, 3), np.uint8))
                self.assertEqual(positive, mode != "haar")
                if mode == "auto":
                    self.assertEqual(detector.confirmed_boxes, [(150, 20, 31, 141)])
                    self.assertEqual(detector.provenance, "heuristic")
                elif mode == "motion":
                    self.assertEqual(len(detector.confirmed_boxes), 2)
                    self.assertEqual(detector.provenance, "motion")
            with mock.patch("nvr.detect.cv2.findContours", return_value=([], None)):
                self.assertFalse(detector.process(np.zeros((180, 320, 3), np.uint8)))
            self.assertEqual(detector.confirmed_boxes, [])
            detector._warmed = False
            self.assertFalse(detector.process(np.ones((180, 320, 3), np.uint8)))
            self.assertEqual(detector.confirmed_boxes, [])

    def test_annotation_native_models_are_independent_of_hub_and_each_other(self):
        hub = Detector({"mode": "auto"})
        first = Detector({"mode": "auto"}, independent=True)
        second = Detector({"mode": "auto"}, independent=True)
        for attribute in ("mog", "cls"):
            self.assertIsNot(getattr(first, attribute), getattr(hub, attribute))
            self.assertIsNot(getattr(first, attribute), getattr(second, attribute))


class OverlayTests(OfflineTestCase):
    def test_exception_diagnostic_is_sanitized_bounded_and_logger_failure_keeps_raw(self):
        packet = image_packet()
        logs, warnings = [], []
        error = RuntimeError("failed rtsp://username:secret@fixture/live?token=private\n" + "x" * 400)
        overlay = EventOverlay("#F51616", {}, warnings.append, logs.append)
        with mock.patch("nvr.event_overlay.PersonLocalizer", side_effect=error):
            self.assertEqual(overlay.render(packet), packet.jpeg)
        self.assertEqual(len(logs), 1)
        self.assertIn("RuntimeError", logs[0])
        self.assertNotIn("secret", logs[0])
        self.assertNotIn("private", logs[0])
        self.assertNotIn("\n", logs[0])
        self.assertLess(len(logs[0]), 230)
        broken_logger = mock.Mock(side_effect=OSError("log unavailable"))
        overlay = EventOverlay("#F51616", {}, warnings.append, broken_logger)
        with mock.patch("nvr.event_overlay.PersonLocalizer", side_effect=error):
            self.assertEqual(overlay.render(packet), packet.jpeg)
        self.assertEqual(len(warnings), 2)

    def test_all_exact_colors_clip_outline_and_preserve_input(self):
        for color in TARGET_BOX_COLORS:
            with self.subTest(color=color):
                packet = image_packet(detection=Detection(((-5, -4, 90, 130), (500, 500, 5, 5)), "haar", (320, 180)))
                before = packet.jpeg
                overlay = EventOverlay(color, {"mode": "motion"})
                rendered = overlay.render(packet)
                decoded = cv2.imdecode(np.frombuffer(rendered, np.uint8), cv2.IMREAD_COLOR)
                mask = colored_pixels(decoded, color)
                self.assertTrue(mask[0:3, 10:70].any())
                self.assertFalse(mask[20:100, 20:60].any(), "Outline must not fill the interior")
                self.assertEqual(packet.jpeg, before)
                self.assertIs(overlay.render(packet), rendered)

    def test_new_negative_or_changed_frame_never_inherits_trigger_coordinates(self):
        overlay = EventOverlay("#F51616", {"mode": "motion"})
        packet = image_packet(detection=Detection(((60, 20, 50, 120),), "motion", (320, 180)))
        self.assertNotEqual(overlay.render(packet), packet.jpeg)
        for next_packet in (image_packet(seq=2), image_packet(seq=2, value=41),
                            image_packet(seq=2, source=("other",)),
                            image_packet(seq=3, size=(160, 90))):
            with self.subTest(seq=next_packet.seq, source=next_packet.source):
                with mock.patch.object(overlay.detector, "process", return_value=False):
                    output = overlay.render(next_packet)
                self.assertEqual(output, next_packet.jpeg)
        overlay.render(FramePacket(b"", 4, 0, ()))
        self.assertIsNone(overlay.detector)
        self.assertIsNone(overlay.cached_packet)

    def test_failure_preserves_raw_bytes_and_emits_one_visible_warning(self):
        warnings = []
        packet = image_packet()
        overlay = EventOverlay("#0931C0", {"mode": "auto"}, warnings.append)
        with mock.patch("nvr.event_overlay.PersonLocalizer", side_effect=RuntimeError("fixture")):
            self.assertEqual(overlay.render(packet), packet.jpeg)
            self.assertEqual(overlay.render(image_packet(seq=2)), packet.jpeg)
        self.assertEqual(len(warnings), 1)
        self.assertIn("已保留原画面", warnings[0])
        self.assertIsNone(overlay.cached_packet)

    def test_person_annotation_never_seeds_from_paired_event_contours(self):
        for mode in ("auto", "haar"):
            overlay = EventOverlay("#F51616", {"mode": mode})
            packet = image_packet(detection=Detection(((60, 20, 50, 120),), "heuristic", (320, 180)))
            localizer = mock.Mock()
            localizer.locate.return_value = ()
            with mock.patch("nvr.event_overlay.PersonLocalizer", return_value=localizer), \
                    mock.patch("nvr.event_overlay.Detector", side_effect=AssertionError("No motion person fallback")):
                self.assertEqual(overlay.render(packet), packet.jpeg)
                self.assertEqual(overlay.render(packet), packet.jpeg)
            self.assertEqual(localizer.locate.call_count, 1, "Duplicate cache must be idempotent")
            self.assertEqual(overlay.tracker.last_boxes, ())

    def test_frame_and_sequence_remain_paired_during_concurrent_capture(self):
        camera = Cam(spec(), self.cfg)
        first, second = image_packet(seq=11), image_packet(seq=12, value=90)
        camera.last_jpeg, camera.seq, camera.last_ts = first.jpeg, first.seq, first.timestamp
        original_decode = cv2.imdecode
        def capture_while_decoding(*args):
            with camera.lock:
                camera.last_jpeg, camera.seq = second.jpeg, second.seq
            return original_decode(*args)
        with mock.patch("nvr.camera.cv2.imdecode", side_effect=capture_while_decoding):
            image, packet = camera.frame_sample()
        self.assertEqual(packet.seq, 11)
        self.assertEqual(packet.jpeg, first.jpeg)
        self.assertAlmostEqual(float(image.mean()), 40, delta=1)
        self.assertEqual(camera.snapshot(), (second.jpeg, 12))

    def test_stale_offline_and_restart_clear_annotation_input(self):
        camera = Cam(spec(), self.cfg)
        packet = image_packet()
        camera.state, camera.last_jpeg, camera.seq = "online", packet.jpeg, packet.seq
        camera.last_ts = time.time() - 100
        self.assertFalse(camera.record_sample().jpeg)
        camera.last_ts = time.time()
        current = camera.record_sample()
        camera.state = "offline"
        self.assertFalse(camera.record_sample().jpeg)
        camera.state = "online"
        camera._capture_epoch += 1
        self.assertNotEqual(current.source, camera.record_sample().source)

    def test_stale_or_previous_source_trigger_is_never_forwarded_to_encoder(self):
        camera = Cam(spec(), self.cfg)
        packet = image_packet()
        camera.state, camera.last_jpeg, camera.seq, camera.last_ts = "online", packet.jpeg, 1, packet.timestamp
        current = camera.record_sample()
        for source, timestamp in ((current.source, time.time() - 100), (("previous source",), time.time())):
            trigger = FramePacket(current.jpeg, 1, timestamp, source,
                                  Detection(((10, 10, 80, 120),), "haar", (320, 180)))
            recorder = mock.Mock()
            recorder.proc.poll.return_value = None
            with self.subTest(source=source), mock.patch("nvr.camera.SharedJpegRecorder", return_value=recorder) as encoder:
                camera.start_record(trigger)
                self.assertIsNone(encoder.call_args.kwargs["initial"])
            camera.rec_proc, camera._jpeg_recorder, camera.rec_path = None, None, ""


class AnnotationCancellationTests(unittest.TestCase):
    def test_already_cancelled_event_never_starts_native_work(self):
        runner, stop = AnnotationRunner(), threading.Event()
        stop.set()
        sample = image_packet()
        transform, failure = mock.Mock(), mock.Mock()
        with mock.patch("nvr.event_overlay.threading.Thread") as thread:
            self.assertEqual(runner.render(transform, sample, stop, failure), sample.jpeg)
        thread.assert_not_called()
        transform.assert_not_called()
        failure.assert_not_called()
        self.assertIsNone(runner.job)

    def test_cancellation_during_thread_start_skips_native_transform(self):
        runner, stop = AnnotationRunner(), threading.Event()
        sample = image_packet()
        transform, failure = mock.Mock(), mock.Mock()
        original_thread = threading.Thread

        class StopBeforeExecute(original_thread):
            def start(self):
                stop.set()
                super().start()

        surface = SimpleNamespace(Thread=StopBeforeExecute, Event=threading.Event)
        with mock.patch("nvr.event_overlay.threading", surface):
            self.assertEqual(runner.render(transform, sample, stop, failure), sample.jpeg)
        runner.job["thread"].join(timeout=1)
        self.assertFalse(runner.job["thread"].is_alive())
        transform.assert_not_called()
        failure.assert_not_called()

    def test_cancellation_while_waiting_for_slot_does_not_create_job(self):
        runner, stop = AnnotationRunner(), threading.Event()
        sample = image_packet()
        transform, failure = mock.Mock(), mock.Mock()
        entered, output = threading.Event(), []

        def call():
            entered.set()
            output.append(runner.render(transform, sample, stop, failure))

        runner.lock.acquire()
        caller = threading.Thread(target=call, daemon=True)
        try:
            caller.start()
            self.assertTrue(entered.wait(1))
            stop.set()
        finally:
            runner.lock.release()
        caller.join(timeout=1)
        self.assertFalse(caller.is_alive())
        self.assertEqual(output, [sample.jpeg])
        transform.assert_not_called()
        failure.assert_not_called()
        self.assertIsNone(runner.job)


class OverlaySettingsTests(OfflineTestCase):
    def test_falsey_non_object_json_rejects_without_any_save_or_sync(self):
        hub = FakeHub()
        client = create_app(self.cfg, hub).test_client()
        before_memory = json.dumps(self.cfg.data, sort_keys=True)
        before_disk = Path(self.cfg.path).read_bytes()
        for body in ("null", "false", "0", "[]"):
            with self.subTest(body=body), mock.patch.object(self.cfg, "save") as save:
                response = client.post("/api/settings", data=body, content_type="application/json")
                self.assertEqual(response.status_code, 400)
                save.assert_not_called()
                self.assertEqual(json.dumps(self.cfg.data, sort_keys=True), before_memory)
                self.assertEqual(Path(self.cfg.path).read_bytes(), before_disk)
        self.assertEqual(hub.sync_requests, 0)

    def test_missing_fields_migrate_to_enabled_and_red(self):
        path = self.root / "old.json"
        path.write_text(json.dumps({"record": {"keep_days": 3}}), encoding="utf-8")
        cfg = Config(str(path))
        self.assertIs(cfg.get("record", "target_boxes"), True)
        self.assertEqual(cfg.get("record", "target_box_color"), "#F51616")

    def test_invalid_settings_reject_entire_request_before_memory_or_disk_change(self):
        hub = FakeHub()
        client = create_app(self.cfg, hub).test_client()
        before_memory = json.dumps(self.cfg.data, sort_keys=True)
        before_disk = Path(self.cfg.path).read_bytes()
        for invalid in ({"target_boxes": "false"}, {"target_boxes": 0}, {"target_boxes": None},
                        {"target_box_color": "#f51616"}, {"target_box_color": "#FF0000"},
                        {"target_box_color": ["#F51616"]}, None, []):
            with self.subTest(invalid=invalid):
                result = client.post("/api/settings", json={"site": {"name": "must not save"}, "record": invalid})
                self.assertEqual(result.status_code, 400)
                self.assertEqual(json.dumps(self.cfg.data, sort_keys=True), before_memory)
                self.assertEqual(Path(self.cfg.path).read_bytes(), before_disk)
        self.assertEqual(hub.sync_requests, 0)


class BurnedVideoTests(OfflineTestCase):
    def test_moving_person_fixture_centers_in_actual_annotated_mp4(self):
        from tests.test_target_tracking import Scene, center
        for speed, mode in ((8, "auto"), (20, "auto"), (8, "haar"), (20, "haar")):
            with self.subTest(speed=speed, mode=mode), tempfile.TemporaryDirectory() as directory:
                scene, labels, packets, observed = Scene(), [], [], []
                # Neutral texture makes the new red outline measurable after
                # both JPEG and MP4 encoding, without using any old red border.
                scene.background[:] = cv2.cvtColor(cv2.cvtColor(scene.background, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
                scene.targets = [cv2.cvtColor(cv2.cvtColor(item, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
                                 for item in scene.targets]
                for i in range(25):
                    image, boxes = scene.frame([(15 + speed * min(i, 16), 70)])
                    labels.append(boxes[0])
                    jpeg = cv2.imencode(".jpg", image)[1].tobytes()
                    packets.append(FramePacket(jpeg, i, i / 8, ("labeled-codec",)))
                    jitter = (-6, 6, -3, 3)[i % 4]
                    observed.append(() if i in (4, 5, 11, 12) or i > 16 else
                                    (PersonDetection((boxes[0][0] + jitter, 70, 52, 108), .9),))
                localizer = mock.Mock()
                localizer.locate.side_effect = observed
                overlay = EventOverlay("#F51616", {"mode": mode})
                path = Path(directory) / "moving-annotation.mp4"
                writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 8, (480, 270))
                self.assertTrue(writer.isOpened())
                try:
                    with mock.patch("nvr.event_overlay.PersonLocalizer", return_value=localizer):
                        for packet in packets:
                            output = overlay.render(packet)
                            writer.write(cv2.imdecode(np.frombuffer(output, np.uint8), cv2.IMREAD_COLOR))
                finally:
                    writer.release()
                frames = self.decoded_frames(path)
                self.assertEqual(len(frames), len(labels))
                errors = []
                for i, (frame, truth) in enumerate(zip(frames, labels)):
                    yy, xx = np.nonzero(colored_pixels(frame, "#F51616"))
                    if i > 22:  # Last semantic confirmation was frame 16.
                        self.assertEqual(len(xx), 0, "Flow cannot prolong .75 second expiry")
                        continue
                    self.assertGreater(len(xx), 100)
                    measured = np.array(((xx.min() + xx.max()) / 2, (yy.min() + yy.max()) / 2))
                    # xywh covers discrete pixels x..x+w-1, matching the
                    # rectangle rasterizer's inclusive end point.
                    errors.append(float(np.linalg.norm(measured - (center(truth) - .5))))
                self.assertGreaterEqual(sum(error <= 6 for error in errors) / len(errors), .95)

    def decoded_frames(self, path):
        video = cv2.VideoCapture(str(path))
        frames = []
        try:
            while True:
                ok, frame = video.read()
                if not ok:
                    break
                frames.append(frame)
        finally:
            video.release()
        self.assertTrue(frames)
        return frames

    def finished_path(self, camera):
        part = Path(camera.rec_path)
        camera.stop_record()
        result = part.with_name(part.name.replace(".part.mp4", ".mp4"))
        self.assertTrue(result.is_file())
        return result

    def test_blocked_annotation_preserves_raw_video_and_one_native_job_across_events(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        class BlockingLocalizer:
            def __init__(self, *args, **kwargs):
                self.first = not calls
            def locate(self, image):
                calls.append(1)
                if self.first:
                    entered.set()
                    release.wait(10)
                    boxes = ((20, 20, 50, 120),)
                else:
                    boxes = ((210, 20, 50, 120),)
                return tuple(PersonDetection(box, .9) for box in boxes)
        logs = []
        camera = Cam(spec(), self.cfg, log=logs.append)
        packet = image_packet()
        camera.state, camera.last_jpeg, camera.seq, camera.last_ts = "online", packet.jpeg, 1, packet.timestamp
        self.addCleanup(camera.stop)
        self.addCleanup(release.set)
        with mock.patch("nvr.event_overlay.PersonLocalizer", BlockingLocalizer):
            camera.start_record()
            old_token = camera._annotation_event
            self.assertTrue(entered.wait(2))
            self.wait_until(lambda: camera._jpeg_recorder.frames >= 3)
            self.assertIn("保留原画面", camera.annotation_warning)
            worker = camera._annotation_runner.job["thread"]
            started = time.monotonic()
            first = self.finished_path(camera)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertTrue(worker.is_alive())
            self.assertEqual(sum("本次录像可能缺少目标框" in line for line in logs), 1)
            other = Cam(spec(cid="other"), self.cfg)
            self.assertEqual(other._annotation_runner.render(lambda frame: b"independent", packet,
                                                             threading.Event(), lambda error: None), b"independent")
            self.assertTrue(all(not colored_pixels(frame, "#F51616").any()
                                for frame in self.decoded_frames(first)))
            camera.start_record()
            self.wait_until(lambda: camera._jpeg_recorder.frames >= 3)
            second = self.finished_path(camera)
            self.assertEqual(len(calls), 1)
            self.assertEqual(sum("本次录像可能缺少目标框" in line for line in logs), 2)
            self.assertTrue(self.decoded_frames(second))
            self.cfg.set("record", "target_box_color", "#0931C0", save=False)
            camera.start_record()
            self.wait_until(lambda: camera._jpeg_recorder.frames >= 2)
            camera.annotation_warning = "current event warning"
            camera._annotation_failed("late previous event warning", old_token)
            self.assertEqual(camera.annotation_warning, "current event warning")
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.wait_until(lambda: len(calls) >= 2 and camera._jpeg_recorder.frames >= 5)
            third = self.finished_path(camera)
            self.assertEqual(camera.annotation_warning, "current event warning")
        blue = [colored_pixels(frame, "#0931C0") for frame in self.decoded_frames(third)]
        self.assertTrue(any(mask[18:24, 215:250].any() for mask in blue))
        self.assertFalse(any(mask[18:24, 25:65].any() for mask in blue), "Late old coordinates must not appear")

    def test_new_frame_geometry_moves_then_negative_frame_clears_burned_boxes(self):
        class GeometryLocalizer:
            def __init__(self, *args, **kwargs):
                pass
            def locate(self, image):
                level = float(np.median(image))
                boxes = (((20, 20, 50, 120),) if level < 70 else
                                        ((210, 20, 50, 120),) if level < 130 else ())
                return tuple(PersonDetection(box, .9) for box in boxes)
        camera = Cam(spec(), self.cfg)
        self.addCleanup(camera.stop)
        camera.state = "online"
        with mock.patch("nvr.event_overlay.PersonLocalizer", GeometryLocalizer):
            for seq, level in ((1, 40), (2, 100), (3, 160)):
                packet = image_packet(seq=seq, value=level)
                with camera.lock:
                    camera.last_jpeg, camera.seq, camera.last_ts = packet.jpeg, seq, packet.timestamp
                if seq == 1:
                    camera.start_record()
                target = camera._jpeg_recorder.frames + 5
                self.wait_until(lambda: camera._jpeg_recorder.frames >= target)
            completed = self.finished_path(camera)
        phases = {40: [], 100: [], 160: []}
        for frame in self.decoded_frames(completed):
            key = min(phases, key=lambda value: abs(float(np.median(frame)) - value))
            phases[key].append(colored_pixels(frame, "#F51616"))
        self.assertTrue(all(phases.values()))
        self.assertTrue(all(mask[18:24, 25:65].any() and not mask[18:24, 215:250].any()
                            for mask in phases[40]))
        self.assertTrue(all(mask[18:24, 215:250].any() and not mask[18:24, 25:65].any()
                            for mask in phases[100]))
        self.assertTrue(all(not mask.any() for mask in phases[160]))

    def test_slow_annotation_preserves_wall_clock_duration_and_has_no_backlog(self):
        overlay = EventOverlay("#E86D08", {"mode": "motion"})
        arrivals = []
        def snapshot():
            return image_packet(seq=len(arrivals) + 1,
                                detection=Detection(((70, 20, 80, 130),), "haar", (320, 180)))
        def slow_annotation(packet):
            time.sleep(0.25)
            result = overlay.render(packet)
            arrivals.append(time.monotonic())
            return result
        path = str(self.root / "slow-annotation.mp4")
        recorder = local_camera.SharedJpegRecorder(path, snapshot, fps=8, transform=slow_annotation)
        self.addCleanup(lambda: recorder.stop() if recorder.thread.is_alive() else None)
        self.wait_until(lambda: recorder.frames >= 13, timeout=8)
        self.assertTrue(recorder.stop())
        wall_span = arrivals[recorder.frames - 1] - arrivals[0]
        decoded = ffmpeg.run([ffmpeg.ffmpeg_exe(), "-hide_banner", "-i", path, "-f", "null", "-"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(decoded.returncode, 0)
        duration = re.search(r"Duration: (\d+):(\d+):([\d.]+)", decoded.stderr.decode("utf-8", "replace"))
        self.assertIsNotNone(duration)
        seconds = int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])
        self.assertGreaterEqual(wall_span, 3.0)
        self.assertAlmostEqual(seconds, wall_span, delta=0.5)
        self.assertIn(overlay.cached_packet.seq, (recorder.frames, recorder.frames + 1))
        # At most one current annotation is in progress when stop is requested;
        # no missed 8-fps samples are queued for catch-up afterwards.
        self.assertLessEqual(len(arrivals), recorder.frames + 1)

    def annotated_event(self, kind="local", color="#F51616"):
        self.cfg.set("record", "target_boxes", True, save=False)
        self.cfg.set("record", "target_box_color", color, save=False)
        camera = Cam(spec() if kind == "local" else {"id": "sample", "url": "rtsp://fixture.invalid/live"}, self.cfg)
        self.addCleanup(camera.stop)
        camera.state = "online"
        packet = image_packet(detection=Detection(((70, 20, 80, 130),), "haar", (320, 180)))
        camera.last_jpeg, camera.seq, camera.last_ts = packet.jpeg, packet.seq, packet.timestamp
        _, sample = camera.frame_sample()
        sample = FramePacket(sample.jpeg, sample.seq, sample.timestamp, sample.source, packet.detection)
        with mock.patch("nvr.camera.popen") as source_open, \
                mock.patch("nvr.event_overlay.PersonLocalizer", FixedLocalizer):
            camera.start_record(sample)
            self.assertTrue(camera.recording())
            recorder = camera._jpeg_recorder
            part = Path(camera.rec_path)
            self.assertTrue(part.name.endswith(".part.mp4"))
            self.wait_until(lambda: recorder.frames >= 6, description="annotated encoded samples")
            camera.stop_record()
            source_open.assert_not_called()
        completed = part.with_name(part.name.replace(".part.mp4", ".mp4"))
        self.assertFalse(part.exists())
        self.assertTrue(completed.is_file())
        return camera, completed

    def test_both_sources_all_colors_are_burned_into_actual_decoded_mp4(self):
        for kind in ("local", "rtsp"):
            for color in TARGET_BOX_COLORS:
                with self.subTest(kind=kind, color=color):
                    camera, completed = self.annotated_event(kind, color)
                    decoder = cv2.VideoCapture(str(completed))
                    try:
                        ok, frame = decoder.read()
                        self.assertTrue(ok)
                        mask = colored_pixels(frame, color)
                        self.assertTrue(mask[18:24, 80:140].any())
                        self.assertFalse(mask[40:120, 90:130].any())
                    finally:
                        decoder.release()
                    self.assertEqual(camera.snapshot()[0], image_packet().jpeg)

    def test_mid_event_settings_are_frozen_and_next_event_uses_new_options(self):
        camera = Cam(spec(), self.cfg)
        self.addCleanup(camera.stop)
        packet = image_packet(detection=Detection(((70, 20, 80, 130),), "haar", (320, 180)))
        camera.state, camera.last_jpeg, camera.seq, camera.last_ts = "online", packet.jpeg, 1, packet.timestamp
        _, current = camera.frame_sample()
        trigger = FramePacket(current.jpeg, current.seq, current.timestamp, current.source, packet.detection)
        patcher = mock.patch("nvr.event_overlay.PersonLocalizer", FixedLocalizer)
        patcher.start()
        self.addCleanup(patcher.stop)
        camera.start_record(trigger)
        first = camera._jpeg_recorder
        self.cfg.set("record", "target_box_color", "#0931C0", save=False)
        self.assertTrue(camera.record_options["target_boxes"])
        self.assertEqual(camera.record_options["target_box_color"], "#F51616")
        self.wait_until(lambda: first.frames >= 4)
        red = self.finished_path(camera)
        self.assertTrue(all(colored_pixels(frame, "#F51616")[18:24, 80:140].any()
                            for frame in self.decoded_frames(red)))
        camera.start_record(trigger)
        second = camera._jpeg_recorder
        self.assertTrue(camera.record_options["target_boxes"])
        self.wait_until(lambda: second.frames >= 4)
        blue = self.finished_path(camera)
        self.assertTrue(all(colored_pixels(frame, "#0931C0")[18:24, 80:140].any()
                            for frame in self.decoded_frames(blue)))
        self.cfg.set("record", "target_boxes", False, save=False)
        camera.start_record()
        disabled = camera._jpeg_recorder
        self.assertFalse(camera.record_options["target_boxes"])
        self.assertIsNone(disabled.transform)
        self.wait_until(lambda: disabled.frames >= 2)
        camera.stop_record()

    def test_annotation_error_keeps_playable_raw_video_and_exposes_warning(self):
        camera = Cam(spec(), self.cfg)
        self.addCleanup(camera.stop)
        packet = image_packet()
        camera.state, camera.last_jpeg, camera.seq, camera.last_ts = "online", packet.jpeg, 1, packet.timestamp
        with mock.patch("nvr.event_overlay.PersonLocalizer", side_effect=RuntimeError("fixture")):
            camera.start_record()
            self.wait_until(lambda: camera._jpeg_recorder.frames >= 3)
            part = Path(camera.rec_path)
            camera.stop_record()
        completed = part.with_name(part.name.replace(".part.mp4", ".mp4"))
        self.assertTrue(completed.is_file())
        self.assertIn("保留原画面", camera.info()["annotation_warning"])

    def test_validation_failure_never_publishes_or_lists_finished_video(self):
        self.cfg.upsert_camera(spec())
        camera = Cam(spec(), self.cfg)
        self.addCleanup(camera.stop)
        packet = image_packet()
        camera.state, camera.last_jpeg, camera.seq, camera.last_ts = "online", packet.jpeg, 1, packet.timestamp
        camera.start_record()
        self.wait_until(lambda: camera._jpeg_recorder.frames >= 3)
        part = Path(camera.rec_path)
        retained = []
        def timeout_validation(path, **kwargs):
            retained.append(Path(path).read_bytes())
            return ffmpeg.validate_video(path)
        with mock.patch("nvr.camera.validate_video", side_effect=timeout_validation), \
                mock.patch("nvr.ffmpeg.run", side_effect=subprocess.TimeoutExpired("fixture", 3)):
            camera.stop_record()
        self.assertTrue(part.exists())
        self.assertEqual(part.read_bytes(), retained[0])
        self.assertFalse(part.with_name(part.name.replace(".part.mp4", ".mp4")).exists())
        self.assertIn("已保留为待校验", camera.last_error)
        self.assertEqual(create_app(self.cfg, FakeHub()).test_client().get("/api/recordings").get_json()["count"], 0)
