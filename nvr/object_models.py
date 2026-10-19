"""Pinned COCO model adapters using the existing OpenCV CPU runtime only."""
import cv2
import numpy as np

from .algorithm_types import ObjectDetection, object_box
from .person_localizer import PersonLocalizer


MAX_DETECTIONS = 12


def class_nms(detections, threshold, overlap=.5):
    """Retain overlapping different classes; bound the total result count."""
    retained = []
    for cid in sorted({d.class_id for d in detections}):
        candidates = [d for d in detections if d.class_id == cid]
        keep = cv2.dnn.NMSBoxes([d.box for d in candidates], [d.confidence for d in candidates],
                                threshold, overlap)
        retained.extend(candidates[int(i)] for i in np.asarray(keep).reshape(-1))
    return tuple(sorted(retained, key=lambda d: d.confidence, reverse=True)[:MAX_DETECTIONS])


class NanoDetObjects(PersonLocalizer):
    def __init__(self, path, threshold=.35):
        super().__init__(path, threshold)
        self.selected_classes = None

    def locate(self, image):
        # Reuse the pinned RGB letterbox/normalization; dispatch to our decoder.
        return super().locate(image)

    def _decode(self, outputs, width, height, sx, sy, left, top):
        if len(outputs) != 6:
            raise RuntimeError("NanoDet 输出数量不匹配")
        result = []
        for stride, anchors, score, distance in zip((8, 16, 32), self.anchors, outputs[::2], outputs[1::2]):
            score = np.asarray(score)
            distance = np.asarray(distance)
            if (score.size != len(anchors) * 80 or distance.size != len(anchors) * 32
                    or not np.isfinite(score).all() or not np.isfinite(distance).all()
                    or np.any(score < 0) or np.any(score > 1)):
                raise RuntimeError("NanoDet 输出形状或数值无效")
            score, distance = score.reshape(-1, 80), distance.reshape(-1, 4, 8)
            # Winner among all 80 classes, BEFORE filtering selected classes.
            classes = score.argmax(axis=1)
            confidence = score[np.arange(len(score)), classes]
            chosen = np.flatnonzero(confidence >= self.threshold)
            if self.selected_classes is not None:
                chosen = chosen[np.isin(classes[chosen], tuple(self.selected_classes))]
            chosen = chosen[np.argsort(confidence[chosen])[::-1][:200]]
            logits = distance[chosen]
            weights = np.exp(logits - logits.max(axis=2, keepdims=True))
            offsets = (weights * np.arange(8)).sum(axis=2) / weights.sum(axis=2) * stride
            for index, dist in zip(chosen, offsets):
                x0, y0 = (anchors[index] - dist[:2] - (left, top)) / (sx, sy)
                x1, y1 = (anchors[index] + dist[2:] - (left, top)) / (sx, sy)
                box = object_box((x0, y0, x1 - x0, y1 - y0), width, height)
                if box:
                    result.append(ObjectDetection(box, float(confidence[index]), int(classes[index])))
        return class_nms(result, self.threshold)


class YoloXObjects:
    def __init__(self, path, spec, threshold=.35):
        self.spec, self.threshold = spec, float(threshold)
        self.selected_classes = None
        self.net = cv2.dnn.readNetFromONNX(str(path))
        self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        self.names = self.net.getUnconnectedOutLayersNames()
        grids, strides = [], []
        for stride in (8, 16, 32):
            side = spec.input_size // stride
            x, y = np.meshgrid(np.arange(side), np.arange(side))
            grids.append(np.column_stack((x.ravel(), y.ravel())))
            strides.extend([stride] * (side * side))
        self.grid = np.concatenate(grids).astype(np.float32)
        self.strides = np.asarray(strides, np.float32)[:, None]

    def locate(self, image):
        if image is None or not image.size:
            return ()
        height, width = image.shape[:2]
        size = self.spec.input_size
        scale = min(size / width, size / height)
        rw, rh = max(1, int(width * scale)), max(1, int(height * scale))
        canvas = np.full((size, size, 3), 114, np.uint8)
        canvas[:rh, :rw] = cv2.resize(image, (rw, rh), interpolation=cv2.INTER_LINEAR)
        blob = cv2.dnn.blobFromImage(canvas, scalefactor=1.0,
                                    swapRB=self.spec.profile == "yolox-rgb640", crop=False)
        self.net.setInput(blob)
        outputs = self.net.forward(self.names)
        return self._decode(outputs, width, height, rw / width, rh / height)

    def _decode(self, outputs, width, height, sx, sy):
        if (len(outputs) != 1 or np.asarray(outputs[0]).shape != (1, len(self.grid), 85)
                or not np.isfinite(outputs[0]).all()):
            raise RuntimeError("YOLOX 输出形状或数值无效")
        raw = np.asarray(outputs[0])[0]
        if np.any(raw[:, 4:] < 0) or np.any(raw[:, 4:] > 1):
            raise RuntimeError("YOLOX 概率输出超出支持范围")
        classes = raw[:, 5:].argmax(axis=1)
        confidence = raw[:, 4] * raw[np.arange(len(raw)), classes + 5]
        chosen = np.flatnonzero(confidence >= self.threshold)
        if self.selected_classes is not None:
            chosen = chosen[np.isin(classes[chosen], tuple(self.selected_classes))]
        chosen = chosen[np.argsort(confidence[chosen])[::-1][:600]]
        result = []
        for index in chosen:
            row = raw[index]
            # This catalog contains undecoded graphs only. Reject overflow.
            if np.any(np.abs(row[2:4]) > 20):
                raise RuntimeError("YOLOX 框尺寸超出支持范围")
            center = (row[:2] + self.grid[index]) * self.strides[index]
            size = np.exp(row[2:4]) * self.strides[index]
            start = (center - size / 2) / (sx, sy)
            end = (center + size / 2) / (sx, sy)
            box = object_box((*start, *(end - start)), width, height)
            if box:
                result.append(ObjectDetection(box, float(confidence[index]), int(classes[index])))
        return class_nms(result, self.threshold, .45)


def create_model(path, spec, threshold=.35):
    if spec.profile == "nanodet-rgb416":
        return NanoDetObjects(path, threshold)
    if spec.profile in ("yolox-bgr416", "yolox-rgb640"):
        return YoloXObjects(path, spec, threshold)
    raise ValueError("不支持的模型适配配置")
