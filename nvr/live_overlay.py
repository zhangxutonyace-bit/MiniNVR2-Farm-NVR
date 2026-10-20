"""Shared preview pixels, derived only from exact existing model decisions.

Capture and HTTP readers never execute native drawing/tracking. A camera owns
one lazy worker and one replaceable result, independent of its viewer count.
"""
import copy
from dataclasses import dataclass, replace
import threading
import time
import weakref

import cv2
import numpy as np

from .algorithm_catalog import selected_classes
from .event_labels import CategoryLabelRenderer
from .target_tracking import TargetTracker


_OWNER_LOCK = threading.Lock()
_OWNERS = weakref.WeakKeyDictionary()


def jpeg_size(data):
    """Read JPEG SOF dimensions without native decoding or entropy scanning."""
    if not isinstance(data, bytes) or not data.startswith(b"\xff\xd8"):
        return None
    offset, limit = 2, min(len(data), 1024 * 1024)
    while offset < limit:
        if data[offset] != 255:
            return None
        while offset < limit and data[offset] == 255:
            offset += 1
        if offset >= limit:
            return None
        marker, offset = data[offset], offset + 1
        if marker in (0, 0xda, 0xd9):
            return None
        if marker == 0xd8 or 0xd0 <= marker <= 0xd7 or marker == 1:
            continue
        if offset + 2 > limit:
            return None
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2 or offset + length > limit:
            return None
        if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7,
                      0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
            if length < 8:
                return None
            height = int.from_bytes(data[offset + 3:offset + 5], "big")
            width = int.from_bytes(data[offset + 5:offset + 7], "big")
            return (width, height) if width and height else None
        offset += length
    return None


@dataclass
class _Frame:
    packet: object
    image: object
    semantic: object = None
    after: object = None


class PairedLiveOverlay:
    """Bounded rewind/replay: a delayed decision belongs to its original frame.

Tracker checkpoints are before the oldest retained frame and after each frame.
Replaying from those checkpoints preserves the tracker's local smoothing rather
than resetting on every semantic result. Optical flow cannot renew semantic age.
    """
    MAX_FRAMES = 32  # 30 fps * 0.75 s, plus bounded late-anchor insertion.
    MAX_AGE = TargetTracker.MAX_AGE
    DISPLAY_DELAY = .30

    def __init__(self, renderer=None):
        self.renderer = renderer or CategoryLabelRenderer()
        self.reset()

    def reset(self):
        self.frames = []
        self.tracker = None
        self.base = None
        self.revision = None
        self.identity = None
        self.last_result_key = None
        self.last_detections = ()
        self.render_count = 0
        self.awaiting_semantic = False
        self.last_semantic_at = None
        self.semantic_state = "unavailable"

    @staticmethod
    def _decode(packet):
        image = cv2.imdecode(np.frombuffer(packet.jpeg, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("预览 JPEG 无法解码")
        return image

    def _configure(self, packet, image, revision):
        identity = (packet.source, image.shape[:2], revision)
        if self.identity != identity:
            self.reset()
            self.identity, self.revision = identity, revision
            self.tracker = TargetTracker(revision[3], object_boxes=True)
            self.base = copy.deepcopy(self.tracker)

    def _trim(self, stamp):
        while self.frames and (len(self.frames) > self.MAX_FRAMES
                               or stamp - self.frames[0].packet.timestamp > self.MAX_AGE):
            retired = self.frames.pop(0)
            # Never retain semantic support beyond the same bounded history.
            self.base = retired.after

    def _replay(self, start):
        self.tracker = copy.deepcopy(self.base if start == 0 else self.frames[start - 1].after)
        for frame in self.frames[start:]:
            self.tracker.update(frame.image, frame.semantic or (), frame.packet.timestamp,
                                frame.packet.seq, frame.packet.source)
            frame.after = copy.deepcopy(self.tracker)

    def _short_semantic_gap(self, packet, negative):
        """A due model result may arrive just after this capture.

        Never paint expired coordinates on it. If pristine current pixels still
        support every previously visible target, a viewer may finish showing the
        preceding *paired* cache within the existing 300 ms display budget.
        Departure/flow failure cannot start this bridge. An explicit negative
        never starts it either; normal short-miss tracker semantics are intact.
        """
        if negative or self.last_detections:
            return False
        for frame in reversed(self.frames[:-1]):
            dt = packet.timestamp - frame.packet.timestamp
            if dt > self.DISPLAY_DELAY:
                break
            prior = frame.after
            if prior is None or not prior.tracks:
                continue
            if not all(packet.timestamp - track.confirmed_at > self.MAX_AGE for track in prior.tracks):
                return False
            image = self.frames[-1].image
            gray, scale = prior._gray(image)
            return all(prior._flow(track, gray, scale) is not None for track in prior.tracks)
        return False

    def process(self, packet, latest_pair, revision):
        if not packet.jpeg:
            self.reset()
            return packet.jpeg
        image = None
        if self.frames:
            previous = self.frames[-1].packet
            if (packet.source != previous.source or revision != self.revision
                    or packet.seq < previous.seq or packet.timestamp < previous.timestamp
                    or packet.timestamp - previous.timestamp > self.MAX_AGE
                    or (packet.seq == previous.seq and packet.jpeg != previous.jpeg)):
                self.reset()
        # Repeat requests may deliver a semantic result for the already seen
        # capture. Reuse its pristine decoded pixels, never annotated pixels.
        same = bool(self.frames and packet.seq == self.frames[-1].packet.seq
                    and packet.timestamp == self.frames[-1].packet.timestamp)
        if not same:
            image = self._decode(packet)
            self._configure(packet, image, revision)
            self.frames.append(_Frame(packet, image))
            start = len(self.frames) - 1
        else:
            start = len(self.frames)
        negative = False
        if latest_pair is not None:
            batch, anchor = latest_pair
            paired = (anchor.source == packet.source and anchor.seq <= packet.seq
                      and batch.matches(anchor, revision))
            negative = paired and not batch.detections
            valid = (batch.key != self.last_result_key and paired
                     and 0 <= packet.timestamp - anchor.timestamp <= self.MAX_AGE
                     )
            if valid:
                # A skipped capture can be inserted as an exact semantic
                # anchor, but cannot rewind beyond the retained checkpoint.
                index = next((i for i, f in enumerate(self.frames)
                              if f.packet.seq >= anchor.seq), len(self.frames))
                retained = (index < len(self.frames)
                            and self.frames[index].packet.seq == anchor.seq)
                if retained:
                    frame = self.frames[index]
                    valid = (frame.packet == anchor and batch.size ==
                             (frame.image.shape[1], frame.image.shape[0]))
                else:
                    valid = (self.base.seq is None or anchor.seq > self.base.seq)
                    if valid:
                        anchor_image = self._decode(anchor)
                        valid = (anchor_image.shape[:2] == self.frames[-1].image.shape[:2]
                                 and batch.size == (anchor_image.shape[1], anchor_image.shape[0]))
                        if valid:
                            frame = _Frame(anchor, anchor_image)
                            self.frames.insert(index, frame)
                if valid:
                    allowed = selected_classes({"model_id": revision[0],
                                                "targets": list(revision[2]),
                                                "confidence": revision[3]})
                    frame.semantic = tuple(d for d in batch.detections if d.class_id in allowed)
                    self.last_result_key = batch.key
                    self.last_semantic_at = (anchor.timestamp if self.last_semantic_at is None
                                             else max(anchor.timestamp, self.last_semantic_at))
                    start = min(start, index)
        if start < len(self.frames):
            self._replay(start)
        self._trim(packet.timestamp)
        self.last_detections = self.tracker.last_detections
        age = None if self.last_semantic_at is None else packet.timestamp - self.last_semantic_at
        self.semantic_state = "unavailable" if age is None else ("fresh" if 0 <= age <= self.MAX_AGE else "stale")
        self.awaiting_semantic = self._short_semantic_gap(packet, negative)
        current = self.frames[-1]
        self.render_count += 1
        if not self.last_detections:
            return packet.jpeg
        painted = self.renderer.render(current.image, self.last_detections)
        ok, encoded = cv2.imencode(".jpg", painted, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            raise ValueError("预览目标框 JPEG 无法编码")
        return encoded.tobytes()


class LivePreviewWorker:
    """One native worker, latest-only cache, short demand lease; no model calls."""
    CACHE_MAX_AGE = PairedLiveOverlay.DISPLAY_DELAY
    DEMAND_LEASE = 1.8
    INTERVAL = .02
    FAILURE_BACKOFF = .20
    START_COOLDOWN = .50

    def __init__(self, sample, latest, context, warning=None, processor=None, slot=None):
        self.sample, self.latest, self.context = sample, latest, context
        self.warning = warning or (lambda message: None)
        self.processor = processor
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.thread = None
        self.cached = None
        self.demand_until = 0.
        self.started_at = 0.
        self.generation = 0
        self.waiting_semantic = False
        self.output_source = None
        self.output_seq = -1
        self.output_identity = None
        self.output_revision = None
        self.last_output = None
        self.slot = slot
        self.start_failed = False
        self.start_retry_at = 0.
        self.degraded = False

    def demanded(self):
        """Read an existing painted-view lease without starting any work."""
        with self.lock:
            return (not self.stop_event.is_set() and self.thread is not None
                    and self.thread.is_alive()
                    and time.monotonic() < self.demand_until)

    def invalidate(self):
        # The owner thread alone touches native tracker state. A running call
        # cannot publish its old result after this generation is invalidated.
        with self.lock:
            self.cached = None
            self.waiting_semantic = False
            self.output_source, self.output_seq = None, -1
            self.output_identity, self.output_revision, self.last_output = None, None, None
            self.degraded = False
            self.generation += 1
        self.wake.set()

    def _warn(self, message):
        try:
            self.warning(message)
        except Exception:
            pass

    def _claim_slot(self):
        if self.slot is None:
            return True
        cfg, cid = self.slot
        with _OWNER_LOCK:
            owners = _OWNERS.setdefault(cfg, {})
            owner = owners.get(cid)
            if owner is not None and owner is not self:
                return False
            owners[cid] = self
            return True

    def _release_slot(self):
        if self.slot is None:
            return
        cfg, cid = self.slot
        with _OWNER_LOCK:
            owners = _OWNERS.get(cfg)
            if owners is not None and owners.get(cid) is self:
                owners.pop(cid, None)
                if not owners:
                    _OWNERS.pop(cfg, None)

    def stop(self):
        self.stop_event.set()
        self.invalidate()
        self.wake.set()
        thread = self.thread
        if (thread is not None and thread is not threading.current_thread()
                and thread.is_alive()):
            thread.join(timeout=.3)

    def read(self, raw, context):
        if self.stop_event.is_set() or not raw.jpeg or context is None:
            return raw
        now = time.monotonic()
        with self.lock:
            if self.stop_event.is_set():
                return raw
            self.demand_until = now + self.DEMAND_LEASE
            identity = (raw.source, jpeg_size(raw.jpeg))
            if identity != self.output_identity or context != self.output_revision:
                self.cached, self.last_output = None, None
                self.waiting_semantic = False
                self.degraded = False
                self.generation += 1
                if identity != self.output_identity:
                    self.output_source, self.output_seq = raw.source, -1
                self.output_identity, self.output_revision = identity, context
            if (self.thread is None or self.start_failed) and now >= self.start_retry_at:
                if not self._claim_slot():
                    self.start_retry_at = now + .1
                    self._warn("上一预览任务仍在结束，暂时显示原画面。")
                else:
                    self.start_failed = False
                    try:
                        self.thread = threading.Thread(target=self._run, name="nvr-live-overlay", daemon=True)
                        self.thread.start()
                    except Exception:
                        if self.thread is None or not self.thread.is_alive():
                            self.start_failed = True
                            self.start_retry_at = now + self.START_COOLDOWN
                            self._release_slot()
                        self._warn("实时目标框暂时无法启动，稍后自动重试；当前显示原画面。")
            cached = self.cached
            waiting = self.waiting_semantic
            slow = self.started_at and now - self.started_at > self.CACHE_MAX_AGE
            if slow:
                self.degraded = True
            selected = raw
            if cached is not None:
                packet, revision = cached
                age = time.time() - packet.timestamp
                # A render may finish after the caller sampled raw. Its exact
                # newer capture is safe; only regressions behind a previously
                # delivered source/epoch sequence are rejected.
                if (revision == context and packet.source == raw.source
                        and jpeg_size(packet.jpeg) == identity[1]
                        and packet.seq >= self.output_seq
                        and 0 <= age <= self.CACHE_MAX_AGE):
                    selected = packet
            processing = (self.started_at > 0 and not slow and not self.degraded and self.thread is not None
                          and self.thread.is_alive())
            if selected is raw and self.last_output is not None and processing:
                held = self.last_output
                if (held.source == raw.source and held.seq >= self.output_seq
                        and jpeg_size(held.jpeg) == identity[1]
                        and 0 <= time.time() - held.timestamp <= self.CACHE_MAX_AGE):
                    # Hold the last delivered *whole* frame briefly instead of
                    # advancing the raw watermark on every 30 fps capture. A
                    # 100 ms renderer can then catch up without ever going back.
                    selected = held
            self.output_seq = max(self.output_seq, selected.seq)
            self.last_output = selected
        self.wake.set()
        if slow:
            self._warn("实时目标框处理较慢，已保留原画面；可换用轻量模型或减少摄像头路数。")
        if selected is not raw:
            return selected
        if waiting:
            self._warn("算法的新识别结果尚未就绪，短时画框缓存已结束，正在显示当前原画面。")
        return raw

    def _run(self):
        try:
            self._work()
        finally:
            # A stopped Cam can be replaced before native OpenCV returns. Its
            # logical camera slot remains owned until this exact thread exits.
            self._release_slot()

    def _work(self):
        last_key, active_generation = None, -1
        retry_at = 0.
        while not self.stop_event.is_set():
            with self.lock:
                active = time.monotonic() <= self.demand_until
                generation = self.generation
            if not active:
                if self.processor is not None:
                    self.processor.reset()
                with self.lock:
                    self.cached = None
                    self.last_output = None
                    self.waiting_semantic = False
                last_key = None
                self.wake.wait(.25)
                self.wake.clear()
                continue
            if time.monotonic() < retry_at:
                self.stop_event.wait(self.INTERVAL)
                continue
            key = None
            try:
                context, packet = self.context(), self.sample()
                if context is None or not packet.jpeg:
                    if self.processor is not None:
                        self.processor.reset()
                    with self.lock:
                        self.cached = None
                        self.waiting_semantic = False
                    last_key = None
                else:
                    latest = self.latest()
                    key = (packet.source, packet.seq, packet.timestamp, context,
                           latest[0].key if latest is not None else None)
                    if key != last_key or generation != active_generation:
                        if self.processor is None:
                            self.processor = PairedLiveOverlay()
                        if generation != active_generation:
                            self.processor.reset()
                        with self.lock:
                            self.started_at = time.monotonic()
                        jpeg = self.processor.process(packet, latest, context)
                        result = replace(packet, jpeg=jpeg)
                        current = self.sample()
                        current_context = self.context()
                        published = False
                        with self.lock:
                            if (generation == self.generation
                                    and (time.monotonic() - self.started_at > self.CACHE_MAX_AGE
                                         or time.time() - packet.timestamp > self.CACHE_MAX_AGE)):
                                self.degraded = True
                            if (not self.stop_event.is_set() and generation == self.generation
                                    and current_context == context and current.source == packet.source
                                    and jpeg_size(current.jpeg) == jpeg_size(packet.jpeg)
                                    and 0 <= time.time() - packet.timestamp <= self.CACHE_MAX_AGE):
                                # Keep the previous coherent frame for only its
                                # existing display budget while semantic support
                                # is renewed. Do not replace it with an expired-
                                # semantic raw frame then flash back moments later.
                                pending = getattr(self.processor, "awaiting_semantic", False)
                                self.waiting_semantic = pending
                                previous = self.cached
                                keep = (pending and previous is not None
                                        and previous[1] == context
                                        and previous[0].source == packet.source
                                        and jpeg_size(previous[0].jpeg) == jpeg_size(packet.jpeg)
                                        and 0 <= time.time() - previous[0].timestamp <= self.CACHE_MAX_AGE)
                                if not keep:
                                    self.cached = (result, context)
                                self.degraded = False
                                published = True
                        if published:
                            state = getattr(self.processor, "semantic_state", "fresh")
                            if state == "unavailable":
                                self._warn("尚未收到当前模型可用的识别结果，正在显示原画面。")
                            elif state == "stale" and not keep:
                                self._warn("当前模型的识别结果尚未续上，正在显示原画面。")
                            else:
                                self._warn("")
                        elif not self.stop_event.is_set() and current_context == context:
                            self._warn("实时目标框处理较慢，已保留原画面；可换用轻量模型或减少摄像头路数。")
                        last_key, active_generation = key, generation
            except Exception:
                if self.processor is not None:
                    self.processor.reset()
                with self.lock:
                    self.cached = None
                    self.last_output = None
                # New captures may retry, but a failing fixed frame cannot
                # spawn 50 native attempts/s and changing frames remain bounded.
                last_key = key
                active_generation = generation
                retry_at = time.monotonic() + self.FAILURE_BACKOFF
                self._warn("实时目标框处理失败，已保留原画面；请检查算法状态。")
            finally:
                with self.lock:
                    self.started_at = 0.
            self.stop_event.wait(self.INTERVAL)
