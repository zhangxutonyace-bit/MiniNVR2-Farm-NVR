"""Offline, annotation-only NanoDet person proposals; never event decisions.

The network belongs to one EventOverlay, and is only touched by its bounded
annotation worker. No global network, runtime download, contour or Haar fallback.
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sys

import cv2
import numpy as np


MODEL_NAME = "object_detection_nanodet_2022nov.onnx"
MODEL_BYTES = 3800954
MODEL_SHA256 = "4b82da9944b88577175ee23a459dce2e26e6e4be573def65b1055dc2d9720186"


@dataclass(frozen=True)
class PersonDetection:
    box: tuple
    confidence: float


def model_path():
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return root / "models" / MODEL_NAME


def verify_model(path=None):
    path = Path(path) if path is not None else model_path()
    if not path.is_file() or path.stat().st_size != MODEL_BYTES:
        raise RuntimeError("Bundled person model is missing or has an invalid size")
    if hashlib.sha256(path.read_bytes()).hexdigest() != MODEL_SHA256:
        raise RuntimeError("Bundled person model checksum mismatch")
    return path


def clipped_box(box, width, height):
    values = np.asarray(box, dtype=np.float64)
    if values.shape != (4,) or not np.isfinite(values).all():
        return None
    x, y, w, h = values
    x0, y0 = max(0., x), max(0., y)
    x1, y1 = min(float(width), x + w), min(float(height), y + h)
    if x1 - x0 < 4 or y1 - y0 < 8:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


class PersonLocalizer:
    INPUT = 416
    MAX_PROPOSALS = 12

    def __init__(self, path=None, threshold=0.35):
        self.threshold = float(threshold)
        self.net = cv2.dnn.readNet(str(verify_model(path)))
        self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        self.names = self.net.getUnconnectedOutLayersNames()
        self.anchors = []
        # The pinned 2022nov graph has exactly three cls/regression levels.
        for stride in (8, 16, 32):
            side = self.INPUT // stride
            xx, yy = np.meshgrid(np.arange(side), np.arange(side))
            self.anchors.append(np.column_stack((xx.ravel(), yy.ravel())) * stride + .5 * (stride - 1))

    def locate(self, image):
        if image is None or not image.size:
            return ()
        height, width = image.shape[:2]
        scale = min(self.INPUT / width, self.INPUT / height)
        rw, rh = max(1, int(width * scale)), max(1, int(height * scale))
        left, top = (self.INPUT - rw) // 2, (self.INPUT - rh) // 2
        canvas = np.zeros((self.INPUT, self.INPUT, 3), np.uint8)
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        canvas[top:top + rh, left:left + rw] = cv2.resize(rgb, (rw, rh), interpolation=cv2.INTER_AREA)
        normalized = ((canvas.astype(np.float32) - (103.53, 116.28, 123.675)) /
                      (57.375, 57.12, 58.395)).astype(np.float32)
        self.net.setInput(cv2.dnn.blobFromImage(normalized))  # RGB already, no second swap
        return self._decode(self.net.forward(self.names), width, height, rw / width, rh / height, left, top)

    def _decode(self, outputs, width, height, sx, sy, left, top):
        if len(outputs) != 6:
            raise RuntimeError("Unexpected person model output count")
        boxes, scores = [], []
        for stride, anchors, raw_score, raw_distance in zip(
                (8, 16, 32), self.anchors, outputs[::2], outputs[1::2]):
            score = np.asarray(raw_score).reshape(-1, 80)
            distance = np.asarray(raw_distance).reshape(-1, 4, 8)
            if len(score) != len(anchors) or len(distance) != len(anchors):
                raise RuntimeError("Unexpected person model output geometry")
            if not np.isfinite(score).all() or not np.isfinite(distance).all():
                raise RuntimeError("Non-finite person model output")
            # COCO class 0 is person. A stronger different class is not a person.
            chosen = np.flatnonzero((score[:, 0] >= self.threshold) & (score.argmax(axis=1) == 0))
            chosen = chosen[np.argsort(score[chosen, 0])[::-1][:200]]
            logits = distance[chosen]
            weights = np.exp(logits - logits.max(axis=2, keepdims=True))
            dist = (weights * np.arange(8)).sum(axis=2) / weights.sum(axis=2) * stride
            for anchor, offsets, confidence in zip(anchors[chosen], dist, score[chosen, 0]):
                x0, y0 = (anchor - offsets[:2] - (left, top)) / (sx, sy)
                x1, y1 = (anchor + offsets[2:] - (left, top)) / (sx, sy)
                box = clipped_box((x0, y0, x1 - x0, y1 - y0), width, height)
                if box:
                    boxes.append(box)
                    scores.append(float(confidence))
        if not boxes:
            return ()
        keep = cv2.dnn.NMSBoxes(boxes, scores, self.threshold, 0.5)
        return tuple(PersonDetection(boxes[int(i)], scores[int(i)])
                     for i in np.asarray(keep).reshape(-1)[:self.MAX_PROPOSALS])
