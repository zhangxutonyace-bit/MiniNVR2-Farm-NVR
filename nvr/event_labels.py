"""Compact Chinese category tags; no inference, identities or system fonts."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .algorithm_catalog import CLASS_STYLES
from .algorithm_types import object_box


# Fixed licensed subset supplied with the executable (not downloaded at runtime).
FONT_NAME = "mininvr-labels-sc-regular.otf"
FONT_BYTES = 14440
FONT_SHA256 = "6586b77fc663de9718099b498cdcbc7aa301d33f3551445243535fc14e61ea00"


def font_path():
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return root / "fonts" / FONT_NAME


def verify_font(path=None):
    path = Path(path) if path is not None else font_path()
    if (not path.is_file() or path.stat().st_size != FONT_BYTES
            or hashlib.sha256(path.read_bytes()).hexdigest() != FONT_SHA256):
        raise RuntimeError("离线中文标签字体缺失或校验失败")
    return path


def foreground(color):
    values = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in values]
    luminance = sum(weight * value for weight, value in zip((.2126, .7152, .0722), linear))
    return "#000000" if luminance > .179 else "#FFFFFF"


@dataclass(frozen=True)
class LabelPlacement:
    rectangle: tuple
    text_origin: tuple
    font_size: int


def overlap_area(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


class CategoryLabelRenderer:
    def __init__(self, path=None):
        self.path = verify_font(path)
        self.fonts = {}
        self.last_layout = ()

    def font(self, size):
        if size not in self.fonts:
            self.fonts[size] = ImageFont.truetype(str(self.path), size=size)
        return self.fonts[size]

    def layout(self, text, box, image_size, occupied=()):
        width, height = image_size
        nominal = max(10, min(18, round(min(width / 480, height / 270) * 16)))
        # Size only shrinks for truly narrow frames, never truncate a category.
        for size in range(nominal, 0, -1):
            bounds = self.font(size).getbbox(text)
            pad = max(1, round(size * .2))
            label_w = bounds[2] - bounds[0] + pad * 2
            label_h = bounds[3] - bounds[1] + pad * 2
            if label_w <= width and label_h <= height:
                break
        else:
            raise ValueError("画面过小，无法完整绘制中文类别标签")
        x0, y0, x1, y1 = box
        left = min(max(0, x0), width - label_w)
        candidates = []
        for top in (y0 - label_h - 2, y1 + 2, y0 + 2):
            if 0 <= top <= height - label_h:
                candidates.append((left, top, left + label_w, top + label_h))
        top = min(max(0, y0), height - label_h)
        for beside in (x1 + 2, x0 - label_w - 2):
            if 0 <= beside <= width - label_w:
                candidates.append((beside, top, beside + label_w, top + label_h))
        if not candidates:
            top = min(max(0, y0), height - label_h)
            candidates.append((left, top, left + label_w, top + label_h))
        # Prefer an outside, unoccupied tag. When crowded, choose the least
        # overlap while keeping every glyph and background within the frame.
        rectangle = min(candidates, key=lambda r: (overlap_area(r, box) +
                                                   sum(overlap_area(r, other) for other in occupied)))
        return LabelPlacement(rectangle, (rectangle[0] + pad - bounds[0],
                                           rectangle[1] + pad - bounds[1]), size)

    def render(self, image, detections):
        self.last_layout = ()
        height, width = image.shape[:2]
        canvas = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
        draw, occupied, layouts = ImageDraw.Draw(canvas), [], []
        for detection in detections:
            if detection.class_id not in CLASS_STYLES:
                raise ValueError("未定义的类别标签")
            box = object_box(detection.box, width, height)
            if box is None:
                continue
            x, y, bw, bh = box
            target = (max(0, int(x)), max(0, int(y)), min(width - 1, int(x + bw - 1)),
                      min(height - 1, int(y + bh - 1)))
            if target[2] <= target[0] or target[3] <= target[1]:
                continue
            label, color = CLASS_STYLES[detection.class_id]
            draw.rectangle(target, outline=color, width=2)
            placement = self.layout(label, target, (width, height), occupied)
            # Only this small tag is filled; the target's interior stays clear.
            left, top, right, bottom = placement.rectangle
            draw.rectangle((left, top, right - 1, bottom - 1), fill=color)
            draw.text(placement.text_origin, label, font=self.font(placement.font_size), fill=foreground(color))
            occupied.append(placement.rectangle)
            layouts.append((detection.class_id, label, color, placement))
        self.last_layout = tuple(layouts)
        return cv2.cvtColor(np.asarray(canvas), cv2.COLOR_RGB2BGR)
