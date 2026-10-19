# -*- coding: utf-8 -*-
"""人形检测（轻量 CPU）。

两级：MOG2 前景门控（超便宜，画面没变化直接返回）→ Haar / 轮廓比例确认。
检测由全局调度器串行调用（同一时刻只算 1 路），可在小图上跑，开销极低。
砍掉了 DNN/YOLO：需求只是「有没有人」，不值得为它背上重型依赖和算力。
"""
import os

import cv2

_HAAR_NAMES = ("haarcascade_fullbody.xml", "haarcascade_upperbody.xml")
_CLS = None


def _haar_path():
    base = getattr(cv2, "data", None)
    base = getattr(base, "haarcascades", "") if base else ""
    if not base:
        base = os.path.join(os.path.dirname(cv2.__file__), "data")
    for n in _HAAR_NAMES:
        p = os.path.join(base, n)
        if os.path.exists(p):
            return p
    return ""


def cascade():
    global _CLS
    if _CLS is None:
        p = _haar_path()
        if p:
            c = cv2.CascadeClassifier(p)
            _CLS = c if not c.empty() else False
        else:
            _CLS = False
    return _CLS or None


def private_cascade():
    """Encoding threads must own their native classifier, just like MOG2."""
    path = _haar_path()
    if not path:
        return None
    classifier = cv2.CascadeClassifier(path)
    return classifier if not classifier.empty() else None


class Detector(object):
    """per-camera 实例（MOG2 背景模型需要各路独立），但被串行调用。"""

    def __init__(self, opts=None, independent=False):
        o = opts or {}
        self.mode = (o.get("mode") or "auto").lower()
        self.min_area = float(o.get("min_area") or 0.02)
        self.mog = cv2.createBackgroundSubtractorMOG2(
            history=200, varThreshold=36, detectShadows=True)
        self.cls = ((private_cascade() if independent else cascade())
                    if self.mode in ("auto", "haar") else None)
        self.boxes = []
        self.confirmed_boxes = []
        self.provenance = ""
        self._kern = None
        self._warmed = False

    def process(self, img):
        """img: BGR 小图（如 480x270）。返回 True/False（有人/没动静）。"""
        self.boxes = []
        self.confirmed_boxes = []
        self.provenance = ""
        if img is None or img.size == 0:
            return False

        h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)

        fg = self.mog.apply(gray, learningRate=0.006)
        # MOG2 marks the entire initial frame as foreground. It is a baseline,
        # not a detection. Its 127-valued shadow mask is not motion either.
        if not self._warmed:
            self._warmed = True
            return False
        _, fg = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, None, iterations=1)
        fg = cv2.dilate(fg, None, iterations=2)

        cnts = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
        total = float(w * h)
        thr = total * self.min_area
        boxes = []
        for c in cnts:
            a = cv2.contourArea(c)
            if a < thr:
                continue
            x, y, cw, ch = cv2.boundingRect(c)
            if cw < 8 or ch < 8:
                continue
            boxes.append((x, y, cw, ch))

        if not boxes:
            return False

        if self.mode == "motion":
            self.boxes = boxes
            self.confirmed_boxes = list(boxes)
            self.provenance = "motion"
            return True

        # Haar 确认（只在候选框区域内跑，比全图快得多）
        if self.cls is not None:
            min_h = max(12, int(h * 0.35))
            for (x, y, cw, ch) in boxes:
                roi = gray[max(0, y):y + ch, max(0, x):x + cw]
                if roi.size == 0:
                    continue
                found = self.cls.detectMultiScale(
                    roi, scaleFactor=1.1, minNeighbors=2,
                    minSize=(max(10, int(min_h * 0.35)), min_h))
                if len(found):
                    self.boxes = boxes
                    # Haar returns coordinates inside this candidate ROI.
                    # Legacy boxes/boolean and short-circuit order stay intact.
                    self.confirmed_boxes = [(x + int(rx), y + int(ry), int(rw), int(rh))
                                            for rx, ry, rw, rh in found]
                    self.provenance = "haar"
                    return True

        if self.mode == "haar":
            # A missing cascade or a negative Haar result must not silently
            # become a positive person result based only on contour shape.
            return False

        # 没配 Haar 或 Haar 未确认：用「高瘦 + 够大」的轮廓兜底
        for (x, y, cw, ch) in boxes:
            if ch >= cw * 1.15 and ch >= h * 0.35:
                self.boxes = boxes
                self.confirmed_boxes = [(x, y, cw, ch)]
                self.provenance = "heuristic"
                return True

        self.boxes = boxes
        return False
