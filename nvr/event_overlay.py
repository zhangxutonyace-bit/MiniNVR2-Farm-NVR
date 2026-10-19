"""Per-event annotation of immutable, frame-matched shared capture packets.

The Hub still owns event decisions. Person annotation owns a private offline
network and image tracker; motion annotation retains a separate MOG2 instance.
Capture, preview and timelapse pixels are never painted.
"""
from dataclasses import dataclass, replace
import threading
import time

import cv2
import numpy as np

from .detect import Detector
from .person_localizer import PersonLocalizer
from .safety import redact_urls
from .target_tracking import TargetTracker


def _raw(sample):
    return sample.jpeg if isinstance(sample, FramePacket) else sample[0]


class AnnotationRunner:
    """One native annotation call per camera, including retiring events.

    Python cannot cancel a blocked OpenCV call. A timed-out job retains this
    slot until it returns; later frames/events use raw bytes without queuing.
    Only the caller waiting for that exact job can receive its result.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.job = None

    def render(self, transform, sample, stop_event, failure, timeout=0.5):
        if stop_event.is_set():
            return _raw(sample)

        def report(error):
            try:
                failure(error)
            except Exception:
                pass

        with self.lock:
            if stop_event.is_set():
                return _raw(sample)
            if self.job is not None and not self.job["done"].is_set():
                report(RuntimeError("previous annotation worker is still retiring"))
                return _raw(sample)
            job = {"done": threading.Event(), "abandoned": False,
                   "result": None, "error": None}
            self.job = job

            def execute():
                try:
                    result = _raw(sample) if stop_event.is_set() else transform(sample)
                    error = None
                except Exception as exc:
                    result, error = None, exc
                with self.lock:
                    if not job["abandoned"]:
                        job["result"], job["error"] = result, error
                    job["done"].set()

            job["thread"] = threading.Thread(target=execute, name="nvr-event-annotation", daemon=True)
            try:
                job["thread"].start()
            except Exception as exc:
                job["done"].set()
                report(exc)
                return _raw(sample)
        deadline = time.monotonic() + timeout
        while True:
            if stop_event.is_set() or time.monotonic() >= deadline:
                with self.lock:
                    job["abandoned"] = True
                    job["result"] = None
                if not stop_event.is_set():
                    report(TimeoutError("annotation processing exceeded %.2f seconds" % timeout))
                return _raw(sample)
            if job["done"].wait(min(0.02, max(0.001, deadline - time.monotonic()))):
                with self.lock:
                    result, error = job["result"], job["error"]
                    job["result"], job["error"] = None, None
                if error is not None:
                    report(error)
                    return _raw(sample)
                return result if result is not None else _raw(sample)


@dataclass(frozen=True)
class Detection:
    boxes: tuple = ()
    provenance: str = ""
    size: tuple = ()


@dataclass(frozen=True)
class FramePacket:
    jpeg: bytes
    seq: int
    timestamp: float
    source: tuple
    detection: object = None

    def detected(self, detector, img, positive):
        metadata = Detection(tuple(detector.confirmed_boxes) if positive else (),
                             detector.provenance if positive else "",
                             (img.shape[1], img.shape[0]))
        return replace(self, detection=metadata)


class EventOverlay:
    def __init__(self, color, detect_options, warning=None, log=None):
        self.color = tuple(int(color[i:i + 2], 16) for i in (5, 3, 1))
        self.options = dict(detect_options)
        self.warning = warning or (lambda message: None)
        self.log = log or (lambda message: None)
        self.detector = None
        self.localizer = None
        self.tracker = None
        self.identity = None
        self.cached_packet = None
        self.cached_jpeg = b""
        self.warned = False

    def failed(self, error):
        if self.warned:
            return
        self.warned = True
        try:
            detail = redact_urls(str(error)).replace("\r", " ").replace("\n", " ")[:160]
        except Exception:
            detail = "diagnostic unavailable"
        try:
            self.log("[目标框] %s: %s" % (type(error).__name__[:60], detail))
        except Exception:
            pass
        try:
            self.warning("事件目标框处理失败，已保留原画面；本次录像可能缺少目标框，请查看运行日志。")
        except Exception:
            pass

    def reset(self):
        self.detector = None
        self.localizer = None
        self.tracker = None
        self.identity = None
        self.cached_packet = None
        self.cached_jpeg = b""

    def render(self, packet):
        if not packet or not packet.jpeg:
            self.reset()
            return b""
        if (self.cached_packet is not None and packet.source == self.cached_packet.source
                and packet.seq == self.cached_packet.seq
                and packet.jpeg == self.cached_packet.jpeg):
            return self.cached_jpeg
        try:
            image = cv2.imdecode(np.frombuffer(packet.jpeg, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("JPEG decode failed")
            height, width = image.shape[:2]
            identity = (packet.source, width, height)
            if (self.cached_packet is not None and
                    (packet.seq <= self.cached_packet.seq or
                     packet.timestamp <= self.cached_packet.timestamp or
                     packet.timestamp - self.cached_packet.timestamp > TargetTracker.MAX_AGE)):
                self.reset()
            if self.identity != identity:
                self.reset()
                if (self.options.get("mode") or "auto").lower() == "motion":
                    self.detector = Detector(self.options, independent=True)
                else:
                    self.localizer = PersonLocalizer()
                    self.tracker = TargetTracker()
                self.identity = identity
            if self.detector is not None:
                positive = self.detector.process(image)
                boxes = self.detector.confirmed_boxes if positive else ()
                # Only motion mode may reuse specifically paired Hub regions.
                if packet.detection is not None and packet.detection.size == (width, height):
                    boxes = packet.detection.boxes
            else:
                # The Hub's auto/Haar contours are event metadata, not semantic
                # people. Localization always sees pristine decoded pixels.
                boxes = self.tracker.update(image, self.localizer.locate(image),
                                            packet.timestamp, packet.seq, packet.source)
            if boxes:
                annotated = image.copy()
                for x, y, w, h in boxes:
                    x0, y0 = max(0, int(x)), max(0, int(y))
                    x1, y1 = min(width - 1, int(x + w - 1)), min(height - 1, int(y + h - 1))
                    if x1 > x0 and y1 > y0:
                        cv2.rectangle(annotated, (x0, y0), (x1, y1), self.color, 2)
                ok, encoded = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 90])
                if not ok:
                    raise ValueError("JPEG encode failed")
                result = encoded.tobytes()
            else:
                result = packet.jpeg
            self.cached_packet, self.cached_jpeg = packet, result
            return result
        except Exception as exc:
            # Keep raw event content whenever annotation alone fails; never
            # cache coordinates or a partly drawn image after an exception.
            self.reset()
            self.failed(exc)
            return packet.jpeg
