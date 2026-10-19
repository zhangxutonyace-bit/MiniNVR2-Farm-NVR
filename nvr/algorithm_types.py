"""Immutable, frame-matched results shared by decisions and event annotation."""
from dataclasses import dataclass
import hashlib
import math


@dataclass(frozen=True)
class ObjectDetection:
    box: tuple
    confidence: float
    class_id: int


def object_box(box, width, height):
    """General object geometry: no person-only minimum height/aspect rule."""
    if len(box) != 4 or not all(math.isfinite(float(v)) for v in box):
        return None
    x, y, w, h = (float(v) for v in box)
    x0, y0 = max(0., x), max(0., y)
    x1, y1 = min(float(width), x + w), min(float(height), y + h)
    return (x0, y0, x1 - x0, y1 - y0) if x1 > x0 and y1 > y0 else None


def frame_key(packet, revision):
    return (packet.source, packet.seq, packet.timestamp, revision,
            hashlib.sha256(packet.jpeg).digest())


@dataclass(frozen=True)
class DetectionBatch:
    key: tuple
    size: tuple
    detections: tuple

    def matches(self, packet, revision):
        return self.key == frame_key(packet, revision)
