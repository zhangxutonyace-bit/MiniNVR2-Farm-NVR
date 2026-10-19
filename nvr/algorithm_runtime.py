"""One bounded frame/model job per camera, shared by Hub and the event writer.

There is no image queue. Timed-out jobs retain both their camera slot and the
global native slot until OpenCV returns. Another revision cannot bypass it.
"""
import threading
import time

import cv2
import numpy as np

from .algorithm_catalog import model_spec, revision_for, selected_classes, validate_options
from .algorithm_types import DetectionBatch, frame_key
from .model_store import verify_file
from .object_models import create_model


_NATIVE_SLOT = threading.BoundedSemaphore(1)


class AlgorithmRunner:
    def __init__(self, store, warning=None, factory=None):
        self.store = store
        self.warning = warning or (lambda message: None)
        self.factory = factory or create_model
        self.lock = threading.RLock()
        self.job = None
        self.cached = None
        self.cached_packet = None
        self.model = None
        self.model_revision = None
        self.revision = None
        self.options = None
        self.generation = 0

    def configure(self, options):
        options = validate_options(options)
        revision = revision_for(options)
        with self.lock:
            if revision != self.revision:
                self.generation += 1
                self.revision, self.options = revision, options
                self.cached = None
                self.cached_packet = None
            return revision

    def reset(self):
        with self.lock:
            self.cached = None
            self.cached_packet = None
            self.revision = None
            self.options = None
            self.generation += 1
            # A running job owns its model until done; never modify native state.

    def _warn(self):
        try:
            self.warning("商城算法未及时返回，已保留原画面；不会用旧框触发新事件，请检查算法状态或换用轻量模型。")
        except Exception:
            pass

    def latest(self):
        """One paired decision, never coordinates relabelled as another frame."""
        with self.lock:
            return (self.cached, self.cached_packet) if self.cached is not None else None

    def infer(self, packet, options, stop_event=None, timeout=.5):
        opts = validate_options(options)
        revision = revision_for(opts)
        if not packet.jpeg or (stop_event is not None and stop_event.is_set()):
            return None
        key = frame_key(packet, revision)
        with self.lock:
            # Configure, snapshot and admission are one critical section. reset()
            # cannot erase options between taking the native slot and startup.
            self.configure(opts)
            generation = self.generation
            if self.cached is not None and self.cached.key == key:
                return self.cached
            if self.cached is not None and self.cached.key[0] == packet.source and packet.seq <= self.cached.key[1]:
                return None
            job = self.job
            if (job is not None and job["key"] == key and job["done"].is_set()
                    and job["generation"] == generation and job["result"] is not None
                    and not job["abandoned"]):
                return job["result"]
            if job is not None and not job["done"].is_set():
                if job["key"] != key or job["generation"] != generation:
                    self._warn()
                    return None
            else:
                job = {"key": key, "done": threading.Event(), "result": None,
                       "abandoned": False, "generation": generation}

                def execute():
                    try:
                        image = cv2.imdecode(np.frombuffer(packet.jpeg, np.uint8), cv2.IMREAD_COLOR)
                        if image is None:
                            raise ValueError("算法输入 JPEG 无效")
                        if self.model_revision != revision:
                            spec = model_spec(opts["model_id"])
                            path = verify_file(self.store.path_for(spec.id), spec)
                            self.model = self.factory(path, spec, opts["confidence"])
                            self.model.selected_classes = selected_classes(opts)
                            self.model_revision = revision
                        detections = tuple(d for d in self.model.locate(image)
                                           if d.class_id in selected_classes(opts))
                        result = DetectionBatch(key, (image.shape[1], image.shape[0]), detections)
                        with self.lock:
                            if self.revision == revision and self.generation == generation and not job["abandoned"]:
                                job["result"] = result
                                self.cached = result
                                self.cached_packet = packet
                    except Exception:
                        self._warn()
                    finally:
                        _NATIVE_SLOT.release()
                        job["done"].set()

                thread = threading.Thread(target=execute, name="nvr-object-inference", daemon=True)
                if not _NATIVE_SLOT.acquire(blocking=False):
                    self._warn()
                    return None
                self.job = job
                try:
                    thread.start()
                except Exception:
                    _NATIVE_SLOT.release()
                    job["done"].set()
                    self._warn()
                    return None
        deadline = time.monotonic() + timeout
        while not job["done"].wait(.01):
            if time.monotonic() >= deadline or (stop_event is not None and stop_event.is_set()):
                with self.lock:
                    job["abandoned"] = True
                self._warn()
                return None
        with self.lock:
            if (self.revision != revision or self.generation != generation
                    or (stop_event is not None and stop_event.is_set())):
                return None
            return job["result"]
