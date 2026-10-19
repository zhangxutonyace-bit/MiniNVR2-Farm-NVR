import io
import os
import subprocess
import struct
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np

from nvr import ffmpeg, mailer
from nvr.camera import Cam, _is_fragmented, remuxing_paths
from nvr.detect import Detector, _haar_path
from tests.support import OfflineTestCase


class MediaTests(OfflineTestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_tmp = tempfile.TemporaryDirectory(prefix="mininvr-media-")
        cls.addClassCleanup(cls.media_tmp.cleanup)
        cls.source = str(Path(cls.media_tmp.name) / "synthetic.mp4")
        cls.ff = ffmpeg.ffmpeg_exe()
        result = subprocess.run([
            cls.ff, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10",
            "-t", "12", "-c:v", "libx264", "-preset", "ultrafast",
            "-g", "10", "-pix_fmt", "yuv420p", cls.source,
        ], capture_output=True, timeout=20)
        if result.returncode:
            raise RuntimeError("FFmpeg synthetic source failed: " +
                               result.stderr.decode("utf-8", "replace"))

    def new_camera(self):
        return Cam({"id": "sample", "name": "Synthetic camera", "url": self.source,
                    "enabled": True, "detect": False}, self.cfg)

    def test_bounded_validation_decodes_both_ends_of_long_synthetic_video(self):
        self.assertTrue(ffmpeg.validate_video(self.source))

    def test_validation_rejects_actual_late_truncated_tail(self):
        staged = self.root / "tail-check.part.mp4"
        result = ffmpeg.run([self.ff, "-hide_banner", "-loglevel", "error", "-y",
                             "-i", self.source, "-c", "copy", "-movflags", "+faststart", str(staged)],
                            capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(ffmpeg.validate_video(str(staged)))
        raw = staged.read_bytes()
        staged.write_bytes(raw[:-256])
        self.assertFalse(ffmpeg.validate_video(str(staged)))

    def test_validation_decodes_final_coded_samples_despite_intact_mp4_tables(self):
        """Corrupt late NAL lengths only; file length/container tables survive."""
        blob = Path(self.source).read_bytes()
        def atoms(start, end):
            while start + 8 <= end:
                size, name = struct.unpack_from(">I4s", blob, start)
                header = 8
                if size == 1:
                    size, header = struct.unpack_from(">Q", blob, start + 8)[0], 16
                elif size == 0:
                    size = end - start
                self.assertGreaterEqual(size, header)
                self.assertLessEqual(start + size, end)
                yield name, start + header, start + size
                start += size
        def child(parent, name):
            return next((start, end) for kind, start, end in atoms(*parent) if kind == name)
        samples = []
        moov = child((0, len(blob)), b"moov")
        for kind, start, end in atoms(*moov):
            if kind != b"trak":
                continue
            mdia = child((start, end), b"mdia")
            if blob[child(mdia, b"hdlr")[0] + 8:child(mdia, b"hdlr")[0] + 12] != b"vide":
                continue
            stbl = child(child(mdia, b"minf"), b"stbl")
            stsz, stco, stsc = (child(stbl, name) for name in (b"stsz", b"stco", b"stsc"))
            fixed, count = struct.unpack_from(">II", blob, stsz[0] + 4)
            sizes = [fixed] * count if fixed else list(struct.unpack_from(">%dI" % count, blob, stsz[0] + 12))
            chunks = struct.unpack_from(">%dI" % struct.unpack_from(">I", blob, stco[0] + 4)[0], blob, stco[0] + 8)
            rows = [struct.unpack_from(">III", blob, pos) for pos in range(stsc[0] + 8, stsc[1], 12)]
            index = 0
            for chunk, offset in enumerate(chunks, 1):
                for _ in range(next(row[1] for row in reversed(rows) if row[0] <= chunk)):
                    samples.append((offset, sizes[index]))
                    offset, index = offset + sizes[index], index + 1
        self.assertGreater(len(samples), 100)
        self.assertTrue(ffmpeg.validate_video(self.source))
        for back in (1, 2):
            with self.subTest(back=back):
                offset, size = samples[-back]
                altered = bytearray(blob)
                struct.pack_into(">I", altered, offset, size + 4096)
                output = self.root / ("late-sample-%d.part.mp4" % back)
                output.write_bytes(altered)
                self.assertEqual(len(altered), len(blob))
                reasons = []
                self.assertFalse(ffmpeg.validate_video(str(output), diagnostic=reasons.append))
                self.assertEqual(reasons, ["tail_decode_failed"])

    def test_preview_jpeg_timelapse_and_clean_shutdown(self):
        self.cfg.set("record", "timelapse_sec", 0.25, save=False)
        camera = self.new_camera()

        def cleanup():
            camera.stop()
            camera.join(timeout=6)

        self.addCleanup(cleanup)
        camera.start()
        self.wait_until(lambda: camera.state == "online" and camera.seq >= 3,
                        description="synthetic preview frames")
        jpeg, seq = camera.snapshot()
        self.assertGreater(seq, 0)
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))
        self.assertTrue(jpeg.endswith(b"\xff\xd9"))
        frame = camera.frame_bgr()
        self.assertIsNotNone(frame)
        self.assertEqual(frame.shape[1], 480)
        self.wait_until(lambda: len(list((self.root / "records").rglob("*.jpg"))) >= 2,
                        description="periodic timelapse files")
        process = camera.proc
        camera.stop()
        camera.join(timeout=6)
        self.assertFalse(camera.is_alive())
        self.assertFalse(camera.running)
        self.assertIsNotNone(process.poll())

    def test_event_recording_finalizes_playable_faststart_mp4(self):
        self.cfg.set("record", "target_boxes", False, save=False)
        camera = self.new_camera()
        self.addCleanup(camera.stop)
        camera.start_record()
        self.assertTrue(camera.recording())
        path = Path(camera.rec_path)
        self.wait_until(lambda: path.exists() and path.stat().st_size > 30000,
                        timeout=10, description="event video content")
        camera.stop_record()
        self.wait_until(lambda: str(path) not in remuxing_paths(),
                        description="faststart remux completion")
        self.assertFalse(camera.recording())
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 20 * 1024)
        self.assertFalse(_is_fragmented(str(path)))
        self.assertIn(b"moov", path.read_bytes()[:65536])
        self.assertEqual(ffmpeg.probe(str(path))[:2], (640, 360))
        self.assertEqual(list(path.parent.glob("*.fs.mp4")), [])

    def test_daily_merge_is_valid_idempotent_and_cleans_partial(self):
        day = "2020-01-02"
        target = self.root / "records" / "sample" / day / "tl"
        target.mkdir(parents=True)
        for index, color in enumerate((30, 100, 220), 1):
            frame = np.full((180, 320, 3), color, np.uint8)
            cv2.putText(frame, str(index), (40, 100), cv2.FONT_HERSHEY_SIMPLEX,
                        2, (0, 255, 0), 3)
            ok, image = cv2.imencode(".jpg", frame)
            self.assertTrue(ok)
            (target / ("%06d.jpg" % index)).write_bytes(image.tobytes())
        output = mailer.merge_day(self.cfg, "sample", day)
        self.assertTrue(output)
        self.assertEqual(ffmpeg.probe(output)[:2], (320, 180))
        self.assertFalse(_is_fragmented(output))
        before = os.stat(output).st_mtime_ns
        self.assertEqual(mailer.merge_day(self.cfg, "sample", day), output)
        self.assertEqual(os.stat(output).st_mtime_ns, before)
        self.assertEqual(list(self.root.rglob("*.part.mp4")), [])
        self.assertEqual(mailer.merge_day(self.cfg, "missing", day), "")


class StreamAndDetectionTests(unittest.TestCase):
    def test_event_validation_rejects_timeout_or_missing_end_sample(self):
        with tempfile.NamedTemporaryFile(suffix=".part.mp4") as output:
            valid = SimpleNamespace(returncode=0, stdout=b"0, 0, 0, 1, 100, hash\n")
            empty = SimpleNamespace(returncode=0, stdout=b"# no decoded frames\n")
            invalid = SimpleNamespace(returncode=1, stdout=b"0, 0, 0, 1, 100, hash\n")
            for results in ((empty,), (invalid,), (valid, empty), (valid, invalid)):
                with mock.patch("nvr.ffmpeg.run", side_effect=results) as runner:
                    self.assertFalse(ffmpeg.validate_video(output.name))
                    self.assertLessEqual(runner.call_args.kwargs["timeout"], 3)
                    self.assertGreater(runner.call_args.kwargs["timeout"], 0)
            with mock.patch("nvr.ffmpeg.run", side_effect=subprocess.TimeoutExpired("fixture", 3)):
                self.assertFalse(ffmpeg.validate_video(output.name))

    def test_head_tail_validation_share_deadline_and_tail_has_no_frame_limit(self):
        with tempfile.NamedTemporaryFile(suffix=".part.mp4") as output:
            valid = SimpleNamespace(returncode=0, stdout=b"0, 0, 0, 1, 100, hash\n")
            with mock.patch("nvr.ffmpeg.run", return_value=valid) as runner, \
                 mock.patch("nvr.ffmpeg.time.monotonic", side_effect=(0., .25, 1.5)):
                self.assertTrue(ffmpeg.validate_video(output.name))
            self.assertEqual([call.kwargs["timeout"] for call in runner.call_args_list], [2.75, 1.5])
            self.assertIn("-frames:v", runner.call_args_list[0].args[0])
            tail = runner.call_args_list[1].args[0]
            self.assertIn("-sseof", tail)
            self.assertNotIn("-frames:v", tail)
            self.assertNotIn("-t", tail)
            reasons = []
            with mock.patch("nvr.ffmpeg.run", return_value=valid) as runner, \
                 mock.patch("nvr.ffmpeg.time.monotonic", side_effect=(0., .25, 3.1)):
                self.assertFalse(ffmpeg.validate_video(output.name, diagnostic=reasons.append))
            runner.assert_called_once()
            self.assertEqual(reasons, ["tail_budget_exhausted"])

    def test_real_haar_model_and_classifier_are_available_and_executable(self):
        """Verify the installed OpenCV API/assets, not detection accuracy."""
        model_path = _haar_path()
        self.assertTrue(model_path, "OpenCV must include a Haar body model")
        self.assertTrue(Path(model_path).is_file())
        self.assertTrue(hasattr(cv2, "CascadeClassifier"),
                        "OpenCV must provide the CascadeClassifier API")
        classifier = cv2.CascadeClassifier(model_path)
        self.assertFalse(classifier.empty(), "The actual Haar XML must load")
        detector = Detector({"mode": "haar"})
        self.assertIsNotNone(detector.cls)
        self.assertFalse(detector.cls.empty())
        background = np.zeros((180, 320, 3), np.uint8)
        for _ in range(20):
            self.assertIsInstance(detector.process(background), bool)
        changed = background.copy()
        changed[15:170, 100:180] = 255
        self.assertIsInstance(detector.process(changed), bool)
        # Exercise the installed native method directly as well: no mock or
        # contour fallback can make this compatibility check pass.
        result = classifier.detectMultiScale(cv2.cvtColor(changed, cv2.COLOR_BGR2GRAY))
        self.assertIsNotNone(result)

    def test_mjpeg_parser_handles_split_markers_and_multiple_frames(self):
        first = b"\xff\xd8" + b"a" * 300 + b"\xff\xd9"
        second = b"\xff\xd8" + b"b" * 350 + b"\xff\xd9"
        payload = b"junk" + first + second
        self.assertEqual(list(ffmpeg.iter_jpegs(io.BytesIO(payload), bufsize=5)),
                         [first, second])

    def test_motion_detects_broad_movement_after_background_warmup(self):
        detector = Detector({"mode": "motion", "min_area": 0.02})
        background = np.zeros((180, 320, 3), np.uint8)
        for _ in range(20):
            detector.process(background)
        changed = background.copy()
        changed[80:130, 40:260] = 255
        self.assertTrue(detector.process(changed))

    def test_haar_without_cascade_does_not_claim_human(self):
        with mock.patch("nvr.detect.cascade", return_value=None):
            detector = Detector({"mode": "haar"})
        background = np.zeros((180, 320, 3), np.uint8)
        for _ in range(20):
            detector.process(background)
        changed = background.copy()
        changed[15:170, 120:160] = 255
        self.assertFalse(detector.process(changed))
