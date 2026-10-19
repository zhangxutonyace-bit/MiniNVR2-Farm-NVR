"""Windows video-device discovery and recording from an existing JPEG stream.

Enumeration never opens a camera. A recorder consumes at most the latest shared
frame; the capture reader never waits for an encoder or queues image history.
"""
import hashlib
import os
import re
import subprocess
import threading
import time
from .event_overlay import AnnotationRunner

from . import ffmpeg


def source_identity(spec):
    """Stable source identity, shared by configuration, workers and capabilities."""
    if spec.get("source_type", "rtsp") == "local":
        return ("local", spec.get("device_id") or "", spec.get("device") or "")
    return ("rtsp", spec.get("url") or "")


def worker_identity(camera):
    value = getattr(camera, "source_identity", None)
    return value if value is not None and value[0] == "local" else ("rtsp", getattr(camera, "url", None))


def parse_devices(output):
    """Pair an explicit video row with its DirectShow alternative name only."""
    devices, pending, seen = [], None, set()
    for line in str(output).splitlines():
        row = re.search(r'\][ \t]+"([^"\r\n]+)" \((video|audio|none)\)\s*$', line)
        if row:
            pending = row.group(1) if row.group(2) == "video" else None
            continue
        alt = re.search(r'\][ \t]+Alternative name "([^"\r\n]+)"\s*$', line)
        if alt and pending:
            alias = alt.group(1)
            if alias.startswith("@device_") and len(alias) <= 2048 and alias not in seen:
                devices.append({"device_id": hashlib.sha256(alias.encode("utf-8")).hexdigest(),
                                "name": pending[:160], "device": alias})
                seen.add(alias)
            pending = None
        elif "Alternative name" in line or re.search(r'\][ \t]+"', line):
            pending = None
    return devices


def enumerate_devices(timeout=6):
    if os.name != "nt":
        raise ValueError("电脑摄像头接入目前支持 Windows 内置和 USB 摄像头。")
    try:
        result = ffmpeg.run([ffmpeg.ffmpeg_exe(), "-hide_banner", "-list_devices", "true",
                             "-f", "dshow", "-i", "dummy"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise ValueError("读取电脑摄像头列表超时，请稍后刷新。") from None
    except OSError:
        raise ValueError("无法读取摄像头列表，请检查内置 FFmpeg 和摄像头驱动。") from None
    # FFmpeg intentionally exits nonzero after listing devices for dummy input.
    return parse_devices((result.stderr or b"").decode("utf-8", "replace"))


class SharedJpegRecorder:
    """One bounded writer thread. EOF, never interactive 'q', closes MJPEG input."""
    def __init__(self, path, snapshot, fps=8, log=None, transform=None, initial=None,
                 write_timeout=2.0, annotation_runner=None, annotation_failure=None):
        self.snapshot = snapshot
        self.transform = transform
        self.annotation_runner = annotation_runner or AnnotationRunner()
        self.annotation_failure = annotation_failure or (lambda error: None)
        self._annotation_retired = False
        self.initial = initial
        self.write_timeout = max(0.1, float(write_timeout))
        self._write_started = 0.0
        self.fps = max(1, min(60, int(fps)))
        self.log = log or (lambda message: None)
        self.stop_event = threading.Event()
        self.failed = False
        self.frames = 0
        self.proc = ffmpeg.popen(ffmpeg.cmd_jpeg_record(path, self.fps),
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, bufsize=0)
        self.thread = threading.Thread(target=self._write, name="nvr-webcam-event", daemon=True)
        self.watch = threading.Thread(target=self._watch_write, name="nvr-event-pipe-watch", daemon=True)
        try:
            self.thread.start()
            self.watch.start()
        except Exception:
            self.proc.terminate()
            self.proc.wait(timeout=2)
            self.proc.stdin.close()
            raise

    def _write(self):
        period = 1.0 / self.fps
        deadline = time.monotonic()
        try:
            while not self.stop_event.is_set():
                sample, self.initial = self.initial, None
                if sample is None:
                    sample = self.snapshot()
                if self.transform and not self._annotation_retired:
                    jpeg = self.annotation_runner.render(self.transform, sample, self.stop_event,
                                                         self._annotation_failed)
                else:
                    jpeg = sample.jpeg if hasattr(sample, "jpeg") else sample[0]
                if jpeg:
                    # Unbuffered OS pipes can accept a partial frame.
                    pending = memoryview(jpeg)
                    self._write_started = time.monotonic()
                    while pending and not self.stop_event.is_set():
                        count = self.proc.stdin.write(pending)
                        if not count:
                            raise OSError("encoder input closed")
                        pending = pending[count:]
                    if not pending:
                        self.frames += 1
                    self._write_started = 0.0
                deadline += period
                delay = deadline - time.monotonic()
                if delay < -period:
                    # Keep only the current frame, never queue missed samples.
                    # FFmpeg timestamps actual arrival, so skipped intervals
                    # remain on the video timeline instead of speeding it up.
                    deadline = time.monotonic()
                elif delay > 0 and self.stop_event.wait(delay):
                    break
        except Exception:
            self.failed = self.failed or not self.stop_event.is_set()
        finally:
            self._write_started = 0.0
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass

    def _annotation_failed(self, error):
        if isinstance(error, TimeoutError):
            # This event's detector may still be mutating in a native call.
            # Retire it permanently; a later event gets a fresh detector.
            self._annotation_retired = True
        self.annotation_failure(error)

    def _watch_write(self):
        while not self.stop_event.wait(0.1):
            if not self.thread.is_alive():
                return
            started = self._write_started
            if started and time.monotonic() - started > self.write_timeout:
                self.failed = True
                self.stop_event.set()
                self._terminate()
                return

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=1.5)
        if self.thread.is_alive():
            # A full encoder pipe must not hold the capture loop or shutdown.
            self.failed = True
            self._terminate()
            self.thread.join(timeout=2)
        self.watch.join(timeout=2)
        try:
            self.proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            self.failed = True
            self._terminate()
        return not self.failed and self.proc.returncode == 0 and self.frames > 0

    def _terminate(self):
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
            self.proc.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if self.proc.poll() is None:
                    self.proc.kill()
                self.proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
