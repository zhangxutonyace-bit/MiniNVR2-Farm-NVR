"""Labeled textured rectangles exercise tracking, not real-person accuracy."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from nvr.person_localizer import PersonDetection, PersonLocalizer, clipped_box, verify_model
from nvr.target_tracking import TargetTracker


class Scene:
    def __init__(self):
        rng = np.random.default_rng(9821)
        self.background = rng.integers(20, 40, (270, 480, 3), dtype=np.uint8)
        self.targets = [rng.integers(65, 220, (108, 52, 3), dtype=np.uint8) for _ in range(2)]

    def frame(self, positions):
        image = self.background.copy()
        boxes = []
        for index, (x, y) in enumerate(positions):
            patch = self.targets[index]
            image[y:y + 108, x:x + 52] = patch
            boxes.append((x, y, 52, 108))
        return image, boxes


def proposals(boxes):
    return tuple(PersonDetection(tuple(box), .9) for box in boxes)


def center(box):
    return np.asarray(box[:2]) + np.asarray(box[2:]) / 2


class TrackingTests(unittest.TestCase):
    def test_immediate_motion_two_misses_no_accumulated_lag(self):
        for speed in (8, 20):
            with self.subTest(speed=speed):
                tracker, scene, errors = TargetTracker(), Scene(), []
                for i in range(17):
                    frame, truth = scene.frame([(15 + speed * i, 70)])
                    jitter = (-6, 6, -3, 3)[i % 4]
                    observed = [(truth[0][0] + jitter, 70, 52, 108)]
                    detections = () if i in (4, 5, 11, 12) else proposals(observed)
                    output = tracker.update(frame, detections, i / 8, i, ("scene",))
                    self.assertEqual(len(output), 1)
                    errors.append(float(np.linalg.norm(center(output[0]) - center(truth[0]))))
                self.assertGreaterEqual(sum(error <= 6.01 for error in errors) / len(errors), .95)
                self.assertLess(errors[-1], 6.01)

    def test_stationary_center_and_size_jitter_reduced_by_quarter(self):
        tracker, scene, raw, filtered, sizes, raw_sizes = TargetTracker(), Scene(), [], [], [], []
        frame, truth = scene.frame([(170, 70)])
        for i in range(40):
            jitter = 6 if i % 2 else -6
            box = (170 + jitter, 70 + jitter, 52 + jitter, 108 - jitter)
            output = tracker.update(frame, proposals([box]), i / 8, i)
            raw.append(center(box))
            filtered.append(center(output[0]))
            raw_sizes.append(box[2:])
            sizes.append(output[0][2:])
        self.assertLess(np.std(filtered[5:], axis=0).max(), np.std(raw[5:], axis=0).max() * .75)
        self.assertLess(np.std(sizes[5:], axis=0).max(), np.std(raw_sizes[5:], axis=0).max() * .75)

    def test_departure_flow_failure_clears_immediately(self):
        tracker, scene = TargetTracker(), Scene()
        image, boxes = scene.frame([(40, 70)])
        tracker.update(image, proposals(boxes), 0.)
        self.assertEqual(tracker.update(scene.background, (), .125), ())

    def test_flow_never_extends_semantic_expiry(self):
        tracker, scene = TargetTracker(), Scene()
        for i in range(9):
            image, boxes = scene.frame([(40 + i * 8, 70)])
            output = tracker.update(image, proposals(boxes) if i == 0 else (), i / 8, i)
            self.assertEqual(len(output), 1 if i <= 6 else 0)

    def test_fast_jump_and_unrelated_detection_do_not_interpolate(self):
        tracker, scene = TargetTracker(), Scene()
        first, boxes = scene.frame([(20, 70)])
        tracker.update(first, proposals(boxes), 0., 1)
        jumped, truth = scene.frame([(360, 70)])
        output = tracker.update(jumped, proposals(truth), .125, 2)
        self.assertEqual(len(output), 1)
        self.assertLess(np.linalg.norm(center(output[0]) - center(truth[0])), .01)
        # A separately confirmed person cannot drag the image-supported first.
        pair, truth = scene.frame([(360, 70), (40, 70)])
        output = tracker.update(pair, proposals([truth[1]]), .25, 3)
        self.assertEqual(len(output), 2)
        self.assertTrue(all(min(np.linalg.norm(center(box) - center(t)) for t in truth) < 1 for box in output))

    def test_textured_background_cannot_carry_departed_padded_box(self):
        scene = Scene()
        scene.background = np.random.default_rng(12).integers(0, 255, (270, 480, 3), dtype=np.uint8)
        for jumping in (False, True):
            tracker = TargetTracker()
            first, _ = scene.frame([(40, 70)])
            padded = (20, 48, 92, 152)
            tracker.update(first, proposals([padded]), 0., 1)
            if jumping:
                following, truth = scene.frame([(350, 70)])
            else:
                following, truth = scene.background.copy(), []
            output = tracker.update(following, proposals(truth), .125, 2)
            self.assertEqual(len(output), 1 if jumping else 0)
            if jumping:
                self.assertLess(np.linalg.norm(center(output[0]) - center(truth[0])), .01)

    def test_two_targets_one_to_one_and_bounded_duplicate_proposals(self):
        tracker, scene = TargetTracker(), Scene()
        for i in range(12):
            image, truth = scene.frame([(20 + i * 8, 30), (370 - i * 8, 150)])
            found = proposals(list(reversed(truth)) + truth * 20)
            output = tracker.update(image, found, i / 8, i)
            self.assertEqual(len(output), 2)
            for box in truth:
                self.assertEqual(sum(np.linalg.norm(center(item) - center(box)) < 6 for item in output), 1)

    def test_current_confirmation_wins_capacity_over_twelve_missed_tracks(self):
        tracker = TargetTracker()
        image = np.random.default_rng(8).integers(0, 255, (270, 480, 3), dtype=np.uint8)
        old_boxes = [(15 + column * 90, 10 + row * 82, 35, 60)
                     for row in range(3) for column in range(4)]
        tracker.update(image, proposals(old_boxes), 0., 1)
        self.assertEqual(len(tracker.tracks), 12)
        # All 12 old tracks have valid current flow, so the capacity decision
        # cannot be hidden by a texture failure or departure.
        original_flow = tracker._flow
        with mock.patch.object(tracker, "_flow", wraps=original_flow) as flow:
            new_box = (420, 90, 35, 60)
            output = tracker.update(image, proposals([new_box]), .125, 2)
        self.assertEqual(flow.call_count, 12)
        self.assertEqual(len(output), 12)
        self.assertIn(tuple(map(float, new_box)), output)
        self.assertEqual(sum(np.linalg.norm(center(box) - center(new_box)) < 1 for box in output), 1)

    def test_concentric_large_size_change_uses_current_geometry_immediately(self):
        image = np.random.default_rng(9).integers(0, 255, (270, 480, 3), dtype=np.uint8)
        for old_box, new_box in (((100, 40, 100, 200), (120, 80, 60, 120)),
                                 ((120, 80, 60, 120), (100, 40, 100, 200))):
            with self.subTest(old_box=old_box, new_box=new_box):
                tracker = TargetTracker()
                tracker.update(image, proposals([old_box]), 0., 1)
                output = tracker.update(image, proposals([new_box]), .125, 2)
                self.assertEqual(len(output), 1)
                np.testing.assert_allclose(output[0], new_box, atol=.01)

    def test_nested_person_geometry_has_one_box_and_keeps_independent_miss(self):
        scene = Scene()
        image, truth = scene.frame([(170, 70), (210, 70)])
        full, small = truth[0], (183, 97, 26, 54)
        for old_box, new_box in ((small, full), (full, small)):
            with self.subTest(old_box=old_box, new_box=new_box):
                tracker = TargetTracker()
                tracker.update(image, proposals([old_box, truth[1]]), 0., 1)
                output = tracker.update(image, proposals([new_box]), .125, 2)
                self.assertEqual(len(output), 2)
                self.assertEqual(sum(np.linalg.norm(center(box) - center(new_box)) < 1 for box in output), 1)
                self.assertTrue(any(np.allclose(box, new_box, atol=.01) for box in output))
                self.assertTrue(any(np.allclose(box, truth[1], atol=.01) for box in output),
                                "A separate image-supported target may briefly miss confirmation")

    def test_already_matched_detection_suppresses_old_nested_duplicate(self):
        scene, tracker = Scene(), TargetTracker()
        image, truth = scene.frame([(170, 70), (210, 70)])
        full, small = truth[0], (183, 97, 26, 54)
        tracker.update(image, proposals([small, full, truth[1]]), 0., 1)
        # Initial duplicate semantic proposals are possible at partial/full
        # scale. A current full proposal gets one association, and must not
        # leave the other image-supported old box alongside it.
        output = tracker.update(image, proposals([full]), .125, 2)
        self.assertEqual(len(output), 2)
        self.assertEqual(sum(np.linalg.norm(center(box) - center(full)) < 1 for box in output), 1)
        self.assertTrue(any(np.allclose(box, truth[1], atol=.01) for box in output))

    def test_offset_containment_does_not_merge_independent_supported_region(self):
        tracker = TargetTracker()
        image = np.random.default_rng(19).integers(0, 255, (270, 480, 3), dtype=np.uint8)
        large, offset = (170, 50, 100, 180), (175, 60, 20, 40)
        tracker.update(image, proposals([large]), 0., 1)
        output = tracker.update(image, proposals([offset]), .125, 2)
        self.assertEqual(len(output), 2)
        self.assertTrue(any(np.allclose(box, large, atol=.01) for box in output))
        self.assertTrue(any(np.allclose(box, offset, atol=.01) for box in output))

    def test_low_confidence_invalid_and_background_do_not_create_people(self):
        tracker, scene = TargetTracker(), Scene()
        bad = (PersonDetection((10, 10, 40, 100), .2), PersonDetection((np.nan, 0, 4, 8), .9),
               PersonDetection((10, 10, -20, 100), .9))
        self.assertEqual(tracker.update(scene.background, bad, 0.), ())
        self.assertEqual(tracker.update(scene.background, (), .125), ())

    def test_source_size_reorder_gap_empty_reset_and_input_unchanged(self):
        for change in ("source", "size", "reorder", "gap", "empty"):
            with self.subTest(change=change):
                tracker, scene = TargetTracker(), Scene()
                image, boxes = scene.frame([(40, 70)])
                before = image.copy()
                tracker.update(image, proposals(boxes), 1., 10, "first")
                sample, timestamp, seq, source = image, 1.125, 11, "first"
                if change == "source": source = "second"
                if change == "size": sample = cv2.resize(image, (960, 540))
                if change == "reorder": seq = 9
                if change == "gap": timestamp = 2.
                if change == "empty": sample = None
                self.assertEqual(tracker.update(sample, (), timestamp, seq, source), ())
                np.testing.assert_array_equal(image, before)

    def test_size_change_resets_even_with_equal_internal_gray_size(self):
        tracker, scene = TargetTracker(), Scene()
        image, _ = scene.frame([(40, 70)])
        first = cv2.resize(image, (800, 450))
        second = cv2.resize(image, (1280, 720))
        box = (40 * 800 / 480, 70 * 450 / 270, 52 * 800 / 480, 108 * 450 / 270)
        tracker.update(first, proposals([box]), 0., 1)
        self.assertEqual(tracker.previous.shape, tracker._gray(second)[0].shape)
        self.assertEqual(tracker.update(second, (), .125, 2), ())

    def test_decoded_moving_mp4_stays_with_labels(self):
        # A real codec round trip independently perturbs texture and pixels.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "labeled-motion.mp4"
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 8, (480, 270))
            self.assertTrue(writer.isOpened())
            scene, truth = Scene(), []
            for i in range(20):
                image, boxes = scene.frame([(20 + i * 16, 70)])
                truth.append(boxes[0])
                writer.write(image)
            writer.release()
            capture, tracker, errors = cv2.VideoCapture(str(path)), TargetTracker(), []
            try:
                for i, box in enumerate(truth):
                    ok, frame = capture.read()
                    self.assertTrue(ok)
                    detected = () if i in (5, 6, 12, 13) else proposals([box])
                    output = tracker.update(frame, detected, i / 8, i)
                    self.assertEqual(len(output), 1)
                    errors.append(np.linalg.norm(center(output[0]) - center(box)))
            finally:
                capture.release()
            self.assertGreaterEqual(sum(error <= 6 for error in errors) / len(errors), .95)


class LocalizerTests(unittest.TestCase):
    def test_model_integrity_offline_forward_and_raw_pixels(self):
        self.assertTrue(verify_model().is_file())
        model = PersonLocalizer()
        image = np.full((270, 480, 3), 30, np.uint8)
        before = image.copy()
        self.assertEqual(model.locate(image), ())
        np.testing.assert_array_equal(image, before)

    def test_networks_are_private_and_malformed_native_output_fails(self):
        first, second = mock.Mock(), mock.Mock()
        with mock.patch("nvr.person_localizer.cv2.dnn.readNet", side_effect=[first, second]):
            one, two = PersonLocalizer(), PersonLocalizer()
        self.assertIsNot(one.net, two.net)
        first.setPreferableTarget.assert_called_once_with(cv2.dnn.DNN_TARGET_CPU)
        second.setPreferableTarget.assert_called_once_with(cv2.dnn.DNN_TARGET_CPU)
        first.forward.return_value = ()
        with self.assertRaises(RuntimeError):
            one.locate(np.zeros((270, 480, 3), np.uint8))

    def test_decode_person_class_nms_letterbox_and_nonfinite_failure(self):
        model = PersonLocalizer()
        outputs = []
        for anchors in model.anchors:
            outputs.extend([np.zeros((1, len(anchors), 80), np.float32),
                            np.zeros((1, len(anchors), 32), np.float32)])
        # A single 52px input-space-wide box on level 8, with all distances
        # concentrated at bin 3. Map its center through the 91px black border.
        index = 25 * 52 + 25
        outputs[0][0, index, 0] = .9
        outputs[1][0, index, :] = -100
        outputs[1][0, index, [3, 11, 19, 27]] = 100
        # Different-class winning output is rejected even if person is .8.
        outputs[0][0, index + 3, 0] = .8
        outputs[0][0, index + 3, 2] = .95
        found = model._decode(outputs, 480, 270, 416 / 480, 234 / 270, 0, 91)
        self.assertEqual(len(found), 1)
        expected = ((203.5 - 24) * 480 / 416, (203.5 - 24 - 91) * 270 / 234,
                    48 * 480 / 416, 48 * 270 / 234)
        np.testing.assert_allclose(found[0].box, expected, atol=.001)
        outputs[0][0, 0, 0] = np.nan
        with self.assertRaises(RuntimeError):
            model._decode(outputs, 480, 270, 416 / 480, 234 / 270, 0, 91)

    def test_model_missing_or_modified_is_explicit_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.onnx"
            with self.assertRaises(RuntimeError): verify_model(path)
            path.write_bytes(b"fake model")
            with self.assertRaises(RuntimeError): verify_model(path)

    def test_letterbox_rgb_mapping_finite_clipped_person_only(self):
        model = PersonLocalizer.__new__(PersonLocalizer)
        model.threshold, model.net = .35, mock.Mock()
        model.names = ("fixture",)
        image = np.full((270, 480, 3), (10, 20, 30), np.uint8)
        with mock.patch.object(model, "_decode", return_value=()) as decode:
            model.locate(image)
        blob = model.net.setInput.call_args.args[0]
        np.testing.assert_allclose(blob[0, :, 208, 208],
                                   (np.array([30, 20, 10]) - [103.53, 116.28, 123.675]) /
                                   [57.375, 57.12, 58.395], atol=1e-6)
        self.assertEqual(decode.call_args.args[1:], (480, 270, 416 / 480, 234 / 270, 0, 91))
        self.assertIsNone(clipped_box((np.inf, 0, 20, 40), 480, 270))
        self.assertEqual(clipped_box((-10, -10, 40, 80), 480, 270), (0., 0., 30., 70.))


if __name__ == "__main__":
    unittest.main()
