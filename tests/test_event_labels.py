"""Offline Chinese labels, class colors and raw fallback; no new inference."""
from dataclasses import replace

import cv2
import numpy as np

from nvr.algorithm_catalog import (CLASS_STYLES, category_styles, public_capabilities,
                                   revision_for, validate_options)
from nvr.algorithm_types import DetectionBatch, ObjectDetection, frame_key
from nvr.event_labels import CategoryLabelRenderer, FONT_BYTES, FONT_SHA256, foreground, verify_font
from nvr.event_overlay import EventOverlay, FramePacket
from tests.support import OfflineTestCase
from unittest import mock


class ChineseLabelTests(OfflineTestCase):
    def test_styles_cover_thirteen_classes_and_pet_uses_actual_cat_dog(self):
        self.assertEqual(len(CLASS_STYLES), 13)
        self.assertEqual(set(color for _, color in CLASS_STYLES.values()),
                         {"#F51616", "#E86D08", "#0931C0", "#91C20D"})
        self.assertEqual(CLASS_STYLES[0], ("行人", "#F51616"))
        self.assertEqual(CLASS_STYLES[56], ("座椅", "#E86D08"))
        self.assertEqual(CLASS_STYLES[15], ("猫", "#0931C0"))
        self.assertEqual(CLASS_STYLES[16], ("狗", "#91C20D"))
        pet = next(c for c in public_capabilities() if c["id"] == "pets")
        self.assertEqual(pet["styles"], category_styles((15, 16)))
        self.assertEqual([style["label"] for style in pet["styles"]], ["猫", "狗"])

    def test_font_is_pinned_offline_covers_labels_and_rejects_damage(self):
        path = verify_font()
        self.assertEqual(path.stat().st_size, FONT_BYTES)
        self.assertEqual(len(FONT_SHA256), 64)
        renderer = CategoryLabelRenderer()
        font = renderer.font(18)
        for label, _ in CLASS_STYLES.values():
            self.assertIsNotNone(font.getmask(label).getbbox())
        self.assertNotEqual(bytes(font.getmask("猫")), bytes(font.getmask("狗")))
        altered = self.root / "font.otf"
        damaged = bytearray(path.read_bytes())
        damaged[0] ^= 255
        altered.write_bytes(damaged)
        with self.assertRaises(RuntimeError):
            verify_font(altered)

    def test_exact_palette_outlines_clear_interiors_and_readable_foreground(self):
        renderer = CategoryLabelRenderer()
        original = np.full((270, 480, 3), (75, 85, 95), np.uint8)
        untouched = original.copy()
        for cid in (0, 56, 15, 16):
            output = renderer.render(original, [ObjectDetection((100., 60., 80., 150.), .9, cid)])
            color = CLASS_STYLES[cid][1]
            bgr = tuple(int(color[index:index + 2], 16) for index in (5, 3, 1))
            self.assertEqual(tuple(output[130, 100]), bgr)
            self.assertEqual(tuple(output[130, 140]), tuple(original[130, 140]))
            self.assertTrue(np.array_equal(original, untouched))
        self.assertEqual(foreground("#0931C0"), "#FFFFFF")
        for color in ("#F51616", "#E86D08", "#91C20D"):
            self.assertEqual(foreground(color), "#000000")

    def test_full_chinese_labels_fit_all_frame_edges_and_long_name(self):
        renderer = CategoryLabelRenderer()
        image = np.full((270, 480, 3), 90, np.uint8)
        targets = ((0., 0., 50., 90.), (450., 0., 50., 90.),
                   (0., 220., 50., 90.), (450., 220., 50., 90.), (200., 120., 40., 6.))
        for target in targets:
            renderer.render(image, [ObjectDetection(target, .9, 63)])
            cid, label, color, placement = renderer.last_layout[0]
            self.assertEqual(label, "笔记本电脑")
            left, top, right, bottom = placement.rectangle
            self.assertGreaterEqual(left, 0)
            self.assertGreaterEqual(top, 0)
            self.assertLessEqual(right, image.shape[1])
            self.assertLessEqual(bottom, image.shape[0])
            bounds = renderer.font(placement.font_size).getbbox(label)
            text_x, text_y = placement.text_origin
            self.assertGreaterEqual(text_x + bounds[0], left)
            self.assertGreaterEqual(text_y + bounds[1], top)
            self.assertLessEqual(text_x + bounds[2], right)
            self.assertLessEqual(text_y + bounds[3], bottom)

    def test_small_top_edge_target_keeps_label_outside_its_interior(self):
        renderer = CategoryLabelRenderer()
        image = np.full((270, 480, 3), 80, np.uint8)
        output = renderer.render(image, [ObjectDetection((0., 0., 40., 6.), .9, 67)])
        self.assertGreaterEqual(renderer.last_layout[0][3].rectangle[1], 6)
        self.assertEqual(tuple(output[2, 20]), tuple(image[2, 20]))

    def test_font_failure_preserves_exact_raw_packet_and_one_warning(self):
        options = validate_options({"targets": ["dog"]})
        ok, jpeg = cv2.imencode(".jpg", np.full((270, 480, 3), 90, np.uint8))
        sample = FramePacket(jpeg.tobytes(), 1, 1., ("test", 1))
        def result(p):
            return DetectionBatch(frame_key(p, revision_for(options)), (480, 270),
                                  (ObjectDetection((50., 40., 100., 180.), .9, 16),))
        warnings = []
        overlay = EventOverlay("#F51616", {"mode": "market"}, runtime=result,
                               algorithm_options=options, warning=warnings.append)
        with mock.patch("nvr.event_labels.verify_font", side_effect=RuntimeError("font fixture failure")):
            self.assertEqual(overlay.render(sample), sample.jpeg)
            self.assertEqual(overlay.render(replace(sample, seq=2, timestamp=1.1)), sample.jpeg)
        self.assertEqual(len(warnings), 1)

    def test_market_colors_ignore_legacy_global_selection_without_extra_forward(self):
        options = validate_options({"targets": ["dog"]})
        ok, jpeg = cv2.imencode(".jpg", np.full((270, 480, 3), 90, np.uint8))
        sample = FramePacket(jpeg.tobytes(), 1, 1., ("test", 1))
        native = mock.Mock(return_value=DetectionBatch(frame_key(sample, revision_for(options)), (480, 270),
                               (ObjectDetection((100., 60., 80., 150.), .9, 16),)))
        overlay = EventOverlay("#0931C0", {"mode": "market"}, runtime=native, algorithm_options=options)
        image = cv2.imdecode(np.frombuffer(overlay.render(sample), np.uint8), cv2.IMREAD_COLOR)
        expected = np.array((13, 194, 145))
        self.assertLess(np.max(np.abs(image[130, 100].astype(float) - expected)), 55)
        native.assert_called_once()
        self.assertEqual(overlay.label_renderer.last_layout[0][1], "狗")
