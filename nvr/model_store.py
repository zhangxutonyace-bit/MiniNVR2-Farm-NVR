"""Explicit, catalog-only downloads into persistent storage; never execute code."""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import threading
import time
import urllib.parse
import urllib.request
import uuid

import numpy as np

from .algorithm_catalog import MODELS, model_spec
from .config import data_dir
from .object_models import create_model
from .person_localizer import model_path
from .model_transport import DownloadGuard, guarded_https_handler


APPROVED_HOSTS = frozenset(("github.com", "release-assets.githubusercontent.com",
                            "media.githubusercontent.com", "raw.githubusercontent.com"))


def validate_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in APPROVED_HOSTS
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443) or parsed.fragment):
        raise ValueError("下载地址不在已批准的 HTTPS 官方来源中")
    return url


class _Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_official(url, timeout=10, guard=None):
    validate_url(url)
    handlers = [_Redirects()]
    if guard is not None:
        handlers.append(guarded_https_handler(guard))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"User-Agent": "MiniNVR2-ModelStore/3.0",
                                                  "Accept": "application/octet-stream"})
    return opener.open(request, timeout=timeout)


def verify_file(path, spec):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size != spec.bytes:
        raise ValueError("算法文件缺失或大小不正确，请重新下载")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != spec.sha256:
        raise ValueError("算法文件校验失败，请重新下载")
    return path


class ModelStore:
    """One download and one compatibility job, also when native code retires."""
    def __init__(self, root=None, opener=None, validator=None):
        self._root = Path(root) if root is not None else None
        self.opener = opener or open_official
        self._official_opener = opener is None
        self.validator = validator or self._native_probe
        self.lock = threading.RLock()
        self._native_lock = threading.Lock()
        self._opening_lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread = None
        self._closed = False
        self._verified_signatures = {}
        self.job = {"state": "idle", "model_id": "", "received": 0, "total": 0, "error": ""}

    @property
    def root(self):
        return self._root if self._root is not None else Path(data_dir()) / "models"

    def path_for(self, model_id):
        spec = model_spec(model_id)
        return model_path() if spec.bundled else self.root / (spec.id + "-" + spec.sha256 + ".onnx")

    @staticmethod
    def _native_probe(path, spec):
        model = create_model(path, spec)
        model.locate(np.zeros((spec.input_size, spec.input_size, 3), np.uint8))

    def check_native(self, path, spec, timeout=5):
        # Python cannot kill an OpenCV call; retain its only slot until it ends.
        if not self._native_lock.acquire(blocking=False):
            raise ValueError("上次模型检查尚未结束，请稍后重试")
        result, done = {}, threading.Event()

        def work():
            try:
                self.validator(path, spec)
            except Exception as exc:
                result["error"] = exc
            finally:
                self._native_lock.release()
                done.set()
        thread = threading.Thread(target=work, name="nvr-model-check", daemon=True)
        try:
            thread.start()
        except Exception:
            self._native_lock.release()
            raise
        if not done.wait(timeout):
            raise ValueError("模型兼容性检查超时，原算法仍保留；请稍后重试")
        if "error" in result:
            raise ValueError("模型与当前 CPU 推理环境不兼容，请尝试内置模型") from result["error"]

    def prepare(self, model_id):
        spec = model_spec(model_id)
        path = verify_file(self.path_for(model_id), spec)
        self.check_native(path, spec)
        return path

    def status(self):
        installed = {}
        for spec in MODELS:
            try:
                path = self.path_for(spec.id)
                if path.is_symlink():
                    raise ValueError("算法安装位置不是普通文件")
                stat = path.stat()
                signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
                with self.lock:
                    known = self._verified_signatures.get(spec.id)
                if known != signature:
                    verify_file(path, spec)
                    with self.lock:
                        self._verified_signatures[spec.id] = signature
                installed[spec.id] = {"installed": True, "error": ""}
            except (OSError, ValueError):
                installed[spec.id] = {"installed": False,
                                      "error": "文件不可用，请重新下载" if self.path_for(spec.id).exists() else ""}
        with self.lock:
            return {"installed": installed, "download": dict(self.job)}

    def start(self, model_id):
        spec = model_spec(model_id)
        if spec.bundled:
            raise ValueError("内置算法无需下载")
        with self.lock:
            if self._closed:
                raise ValueError("程序正在退出")
            if self._opening_lock.locked() or (self._thread is not None and self._thread.is_alive()):
                raise ValueError("已有下载正在进行，请先取消或等待完成")
            self._cancel = threading.Event()
            cancel = self._cancel
            self.job = {"state": "downloading", "model_id": spec.id,
                        "received": 0, "total": spec.bytes, "error": ""}
            self._thread = threading.Thread(target=self._download, args=(spec, cancel),
                                            name="nvr-model-download", daemon=True)
            self._thread.start()

    def cancel(self):
        with self.lock:
            self._cancel.set()
        return True

    def close(self):
        with self.lock:
            self._closed = True
            self._cancel.set()
        # Socket reads and validation are bounded; shutdown never joins native work.

    def _open_response(self, spec, guard):
        # OS DNS/connect may not be interruptible. Bound the caller while
        # retaining this single admission slot until the actual open returns.
        if not self._opening_lock.acquire(blocking=False):
            raise ValueError("上次网络连接尚未结束，请稍后重试")
        result, done, delivery = {}, threading.Event(), threading.Lock()
        abandoned = False

        def work():
            try:
                kwargs = {"timeout": min(10, guard.check())}
                if self._official_opener:
                    kwargs["guard"] = guard
                response = self.opener(spec.url, **kwargs)
                with delivery:
                    if abandoned:
                        response.close()
                    else:
                        result["response"] = response
            except Exception as exc:
                result["error"] = exc
            finally:
                self._opening_lock.release()
                done.set()
        worker = threading.Thread(target=work, name="nvr-model-open", daemon=True)
        try:
            worker.start()
        except Exception:
            self._opening_lock.release()
            raise
        try:
            while not done.wait(.05):
                guard.check()
            guard.check()
            if "error" in result:
                raise result["error"]
            return result["response"]
        except BaseException:
            with delivery:
                abandoned = True
                response = result.pop("response", None)
                if response is not None:
                    response.close()
            raise

    def _download(self, spec, cancel):
        temporary = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = self.root / ("." + spec.id + "-" + uuid.uuid4().hex + ".part")
            digest, received, deadline = hashlib.sha256(), 0, time.monotonic() + 600
            guard = DownloadGuard(cancel, deadline)
            validate_url(spec.url)
            with closing(self._open_response(spec, guard)) as response, temporary.open("xb") as output:
                validate_url(response.geturl())
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        valid_length = int(length) == spec.bytes
                    except (ValueError, TypeError):
                        valid_length = False
                    if not valid_length:
                        raise ValueError("下载文件大小与算法目录不一致")
                while True:
                    if cancel.is_set():
                        raise InterruptedError()
                    if time.monotonic() > deadline:
                        raise ValueError("下载超时，请检查网络后重试")
                    # HTTPResponse.read(n) may keep filling n bytes forever
                    # under slow trickle traffic. read1 makes one buffered/raw
                    # read, allowing deadline/cancel checks between arrivals.
                    read_chunk = getattr(response, "read1", response.read)
                    chunk = read_chunk(min(256 * 1024, spec.bytes - received + 1))
                    if cancel.is_set():
                        raise InterruptedError()
                    if time.monotonic() > deadline:
                        raise ValueError("下载超时，请检查网络后重试")
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > spec.bytes:
                        raise ValueError("下载文件超过批准大小")
                    output.write(chunk)
                    digest.update(chunk)
                    with self.lock:
                        self.job["received"] = received
                output.flush()
                os.fsync(output.fileno())
            if received != spec.bytes or digest.hexdigest() != spec.sha256:
                raise ValueError("下载不完整或 SHA-256 校验失败，请重试")
            if cancel.is_set():
                raise InterruptedError()
            with self.lock:
                self.job["state"] = "validating"
            self.check_native(temporary, spec)
            with self.lock:
                if cancel.is_set() or self._closed:
                    raise InterruptedError()
                final = self.path_for(spec.id)
                if final.is_symlink():
                    raise ValueError("算法安装位置不是普通文件")
                os.replace(temporary, final)
                self.job["state"] = "complete"
        except InterruptedError:
            with self.lock:
                self.job.update(state="cancelled", error="已取消，原算法未改变")
        except Exception as exc:
            # No URLs, redirected signed queries or filesystem paths in UI errors.
            message = str(exc) if isinstance(exc, ValueError) else "下载失败，请检查网络或目录权限后重试"
            with self.lock:
                self.job.update(state="error", error=message[:160])
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
