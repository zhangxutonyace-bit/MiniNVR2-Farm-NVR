"""Short-lived person boxes supported by current image evidence.

LK displacement is applied in full. Only the detector's local correction and
size residual are damped; a distant proposal starts a new box immediately.
Optical flow never updates the last semantic person confirmation time.
"""
from dataclasses import dataclass
import math

import cv2
import numpy as np

from .person_localizer import clipped_box
from .algorithm_types import ObjectDetection, object_box


def _iou(a, b):
    x, y = max(a[0], b[0]), max(a[1], b[1])
    w = max(0., min(a[0] + a[2], b[0] + b[2]) - x)
    h = max(0., min(a[1] + a[3], b[1] + b[3]) - y)
    overlap = w * h
    return overlap / max(1., a[2] * a[3] + b[2] * b[3] - overlap)


def _same_geometry(a, b):
    """Local association only; a near-concentric partial/full box may agree.

    IoU alone rejects a half-width, half-height box inside the full person.
    Require almost complete containment *and* close centers for that case, so
    merely overlapping or offset neighboring targets retain separate support.
    """
    if _iou(a, b) >= .3:
        return True
    size_a, size_b = np.asarray(a[2:]), np.asarray(b[2:])
    start = np.maximum(a[:2], b[:2])
    end = np.minimum(np.asarray(a[:2]) + size_a, np.asarray(b[:2]) + size_b)
    overlap = float(np.prod(np.maximum(0., end - start)))
    if overlap / max(1., min(float(np.prod(size_a)), float(np.prod(size_b)))) < .9:
        return False
    delta = np.abs((np.asarray(a[:2]) + size_a / 2) - (np.asarray(b[:2]) + size_b / 2))
    return bool(np.all(delta <= np.maximum(6., .2 * np.minimum(size_a, size_b))))


@dataclass
class _Track:
    box: tuple
    confirmed_at: float
    points: object
    class_id: int = 0
    confidence: float = .35


class TargetTracker:
    MAX_AGE = 0.75
    MAX_TRACKS = 12
    MAX_FEATURES = 64

    def __init__(self, threshold=.35, object_boxes=False):
        self.threshold = float(threshold)
        self.clip = object_box if object_boxes else clipped_box
        self.reset()

    def reset(self):
        self.tracks = []
        self.previous = None
        self.timestamp = None
        self.seq = None
        self.source = None
        self.frame_size = None
        self.last_boxes = ()
        self.last_detections = ()

    @staticmethod
    def _gray(image):
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        # Work remains bounded even when a caller provides a full-size frame.
        scale = min(1., 640. / max(gray.shape))
        if scale < 1.:
            gray = cv2.resize(gray, None, fx=scale, fy=scale)
        return gray, (gray.shape[1] / image.shape[1], gray.shape[0] / image.shape[0])

    def _features(self, gray, box, scale):
        x, y, w, h = np.asarray(box) * (scale[0], scale[1], scale[0], scale[1])
        mask = np.zeros(gray.shape, np.uint8)
        # Excluding the outer border prevents stationary background/old outlines
        # from becoming the sole support for a departing person.
        margin = max(3, int(min(w, h) * .12))
        x0, y0 = max(0, int(x) + margin), max(0, int(y) + margin)
        x1, y1 = min(gray.shape[1], int(x + w) - margin), min(gray.shape[0], int(y + h) - margin)
        if x1 <= x0 or y1 <= y0:
            return None
        mask[y0:y1, x0:x1] = 255
        return cv2.goodFeaturesToTrack(gray, self.MAX_FEATURES, .02, 4, mask=mask, blockSize=5)

    def _flow(self, track, gray, scale):
        points = track.points
        if points is None or len(points) < 6:
            return None
        lk = dict(winSize=(25, 25), maxLevel=3,
                  criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, .02))
        moved, status, error = cv2.calcOpticalFlowPyrLK(self.previous, gray, points, None, **lk)
        if moved is None:
            return None
        backward, back_status, _ = cv2.calcOpticalFlowPyrLK(gray, self.previous, moved, None, **lk)
        if backward is None:
            return None
        old, new = points.reshape(-1, 2), moved.reshape(-1, 2)
        valid = ((status.ravel() != 0) & (back_status.ravel() != 0) &
                 np.isfinite(new).all(axis=1) & (error.ravel() < 24) &
                 (np.linalg.norm(backward.reshape(-1, 2) - old, axis=1) < 1.5))
        if valid.sum() < max(6, math.ceil(len(points) * .45)):
            return None
        displacements = new[valid] - old[valid]
        shift = np.median(displacements, axis=0)
        inlier = np.linalg.norm(displacements - shift, axis=1) < 2.5
        if inlier.sum() < max(6, math.ceil(len(points) * .4)):
            return None
        # Validate distributed interior support, not six corners of one patch.
        supported = old[valid][inlier]
        w, h = track.box[2] * scale[0], track.box[3] * scale[1]
        spread = np.ptp(supported, axis=0)
        if spread[0] < w * .18 or spread[1] < h * .18:
            return None
        x, y, bw, bh = track.box
        predicted = (x + float(shift[0]) / scale[0], y + float(shift[1]) / scale[1], bw, bh)
        # Good background corners can survive after the person leaves. Check
        # aligned *interior pixels* as well: background flow alone cannot carry
        # the semantic box through a departure or a large jump.
        patch_size = (max(4, int(w * .5)), max(8, int(h * .65)))
        old_center = ((x + bw / 2) * scale[0], (y + bh / 2) * scale[1])
        new_center = (old_center[0] + float(shift[0]), old_center[1] + float(shift[1]))
        old_patch = cv2.getRectSubPix(self.previous, patch_size, old_center).astype(np.float32)
        new_patch = cv2.getRectSubPix(gray, patch_size, new_center).astype(np.float32)
        old_patch -= old_patch.mean()
        new_patch -= new_patch.mean()
        norm = float(np.linalg.norm(old_patch) * np.linalg.norm(new_patch))
        if norm < 1. or float(np.sum(old_patch * new_patch)) / norm < .55:
            return None
        return predicted

    def update(self, image, detections, timestamp, seq=None, source=None):
        if image is None or not image.size or not math.isfinite(float(timestamp)):
            self.reset()
            return ()
        gray, scale = self._gray(image)
        if (self.previous is not None and
                (self.frame_size != image.shape[:2] or source != self.source or
                 timestamp <= self.timestamp or timestamp - self.timestamp > self.MAX_AGE or
                 (seq is not None and self.seq is not None and seq <= self.seq))):
            self.reset()
        height, width = image.shape[:2]
        proposals, classes, scores = [], [], []
        for detection in detections:
            if not math.isfinite(float(detection.confidence)) or detection.confidence < self.threshold:
                continue
            box = self.clip(detection.box, width, height)
            cid = getattr(detection, "class_id", 0)
            if type(cid) is not int or not 0 <= cid < 80:
                continue
            if box and not any(cid == other_cid and _iou(box, other) > .65
                               for other, other_cid in zip(proposals, classes)):
                proposals.append(box)
                classes.append(cid)
                scores.append(float(detection.confidence))
            if len(proposals) >= self.MAX_TRACKS:
                break
        candidates = []
        if self.previous is not None:
            for track in self.tracks:
                if timestamp - track.confirmed_at > self.MAX_AGE:
                    continue
                predicted = self._flow(track, gray, scale)
                if predicted is not None:
                    predicted = self.clip(predicted, width, height)
                    if predicted:
                        candidates.append((track, predicted))
        # Greedy globally sorted associations are one-to-one. Prefer IoU;
        # strict near-concentric containment also joins partial/full geometry.
        pairs = sorted(((-_iou(predicted, box), ti, di)
                        for ti, (track, predicted) in enumerate(candidates)
                        for di, box in enumerate(proposals)
                        if track.class_id == classes[di] and _same_geometry(predicted, box)))
        matched_tracks, matched_detections, next_tracks = set(), set(), []
        for _, ti, di in pairs:
            if ti in matched_tracks or di in matched_detections:
                continue
            track, predicted = candidates[ti]
            proposal = proposals[di]
            pc = np.asarray(predicted[:2]) + np.asarray(predicted[2:]) / 2
            dc = np.asarray(proposal[:2]) + np.asarray(proposal[2:]) / 2
            residual = dc - pc
            # Larger changes are a new location/geometry, never an EMA sweep.
            size_ratio = np.asarray(proposal[2:]) / predicted[2:]
            if (np.linalg.norm(residual) > max(12., min(predicted[2:]) * .35) or
                    np.any(size_ratio < 2 / 3) or np.any(size_ratio > 1.5)):
                box = proposal
            else:
                center = pc + .25 * residual
                size = np.asarray(predicted[2:]) + .25 * (np.asarray(proposal[2:]) - predicted[2:])
                box = tuple(center - size / 2) + tuple(size)
            next_tracks.append(_Track(box, timestamp, None, classes[di], scores[di]))
            matched_tracks.add(ti)
            matched_detections.add(di)
        # Current semantic confirmations own the capacity. Only remaining
        # slots may carry image-supported tracks through a brief missed result.
        for di, proposal in enumerate(proposals):
            if di not in matched_detections:
                next_tracks.append(_Track(proposal, timestamp, None, classes[di], scores[di]))
        for ti, (track, predicted) in enumerate(candidates):
            if ti not in matched_tracks and not any(track.class_id == cid and _same_geometry(predicted, box)
                                                   for box, cid in zip(proposals, classes)):
                next_tracks.append(_Track(predicted, track.confirmed_at, None, track.class_id, track.confidence))
        self.tracks = next_tracks[:self.MAX_TRACKS]
        for track in self.tracks:
            track.points = self._features(gray, track.box, scale)
        self.previous, self.timestamp, self.seq, self.source = gray, timestamp, seq, source
        self.frame_size = image.shape[:2]
        self.last_boxes = tuple(track.box for track in self.tracks)
        self.last_detections = tuple(ObjectDetection(t.box, t.confidence, t.class_id) for t in self.tracks)
        return self.last_boxes
