"""Offline catalog/install/config tests. No real camera/account/network."""
import copy
from dataclasses import replace
import hashlib
import http.client
import io
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import urllib.request
from unittest import mock

from nvr.algorithm_catalog import (CAPABILITIES, DEFAULT_OPTIONS, model_spec, revision_for,
                                   selected_classes, validate_options)
from nvr.config import Config
from nvr.hub import Hub
from nvr.model_store import ModelStore, _Redirects, validate_url
from nvr.model_transport import DownloadGuard, _ResponseSocket, guarded_https_handler
from nvr.server import create_app
from tests.support import OfflineTestCase


class Response(io.BytesIO):
    def __init__(self, payload, url="https://github.com/model.onnx", length=None):
        super().__init__(payload)
        self.url = url
        self.headers = {"Content-Length": str(len(payload) if length is None else length)}

    def geturl(self):
        return self.url


class WireSocket:
    """Offline bytes for the real HTTPResponse parser, one receive at a time."""
    def __init__(self, payload, on_receive=None):
        self.wire, self.owner = io.BytesIO(payload), io.BytesIO()
        self.timeout, self.on_receive = 10, on_receive

    def makefile(self, *args, **kwargs):
        return self.owner

    def gettimeout(self):
        return self.timeout

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv_into(self, target):
        data = self.wire.read(min(1, len(target)))
        target[:len(data)] = data
        if self.on_receive:
            self.on_receive()
        return len(data)


class StoreTests(OfflineTestCase):
    def tiny_spec(self, payload):
        return replace(model_spec("yolox-nano"), bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())

    def wait_job(self, store):
        self.wait_until(lambda: store.job["state"] not in ("downloading", "validating"))
        store._thread.join(timeout=1)

    def test_legacy_config_keeps_mode_and_default_person_only_without_download(self):
        path = self.root / "old.json"
        path.write_text('{"detect":{"mode":"motion"},"clawbot":{"token":"private-fixture"}}', encoding="utf-8")
        config = Config(str(path))
        self.assertEqual(config.get("detect", "mode"), "motion")
        self.assertEqual(config.get("algorithms"), DEFAULT_OPTIONS)
        self.assertEqual(config.get("clawbot", "token"), "private-fixture")
        self.assertEqual(len(CAPABILITIES), 14)
        self.assertEqual(selected_classes(validate_options({"targets": ["pets", "cat", "dog"]})), {15, 16})
        for invalid in ({"targets": []}, {"targets": ["stool"]}, {"model_id": "https://arbitrary"},
                        {"confidence": True}, {"confidence": float("nan")}, {"url": "https://github.com/a"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_options(invalid)

    def test_url_policy_rejects_unsafe_authority_and_redirect(self):
        for url in ("http://github.com/a", "https://evil.test/a", "https://user@github.com/a",
                    "https://github.com:8443/a", "file:///C:/x", "https://github.com/a#fragment"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)
        validate_url("https://release-assets.githubusercontent.com/a?approved=signed")
        with self.assertRaises(ValueError):
            _Redirects().redirect_request(mock.Mock(), None, 302, "", {}, "http://127.0.0.1/private")

    def test_download_atomic_verified_persistent_and_never_activates(self):
        payload = b"synthetic-onxx" * 71
        spec = self.tiny_spec(payload)
        store = ModelStore(self.root / "models", opener=lambda *a, **k: Response(payload), validator=mock.Mock())
        with mock.patch("nvr.model_store.model_spec", return_value=spec):
            store.start(spec.id)
            self.wait_job(store)
            self.assertEqual(store.job["state"], "complete")
            final = store.path_for(spec.id)
            self.assertEqual(final.read_bytes(), payload)
            self.assertFalse(list(store.root.glob("*.part")))
            restarted = ModelStore(store.root, validator=mock.Mock())
            self.assertEqual(restarted.prepare(spec.id), final)
        self.assertEqual(self.cfg.get("detect", "mode"), "auto")

    def test_bad_size_hash_compatibility_and_redirect_keep_previous_install(self):
        good = b"approved synthetic model"
        spec = self.tiny_spec(good)
        cases = [(b"wrong", None, None), (b"x" * len(good), None, None),
                 (good, ValueError("invalid graph"), None),
                 (good, None, "https://127.0.0.1/private")]
        for index, (payload, native_error, redirect) in enumerate(cases):
            store = ModelStore(self.root / ("models%d" % index),
                               opener=lambda *a, **k: Response(payload, redirect or spec.url),
                               validator=mock.Mock(side_effect=native_error))
            with mock.patch("nvr.model_store.model_spec", return_value=spec):
                store.root.mkdir()
                previous = store.path_for(spec.id)
                previous.write_bytes(good)
                store.start(spec.id)
                self.wait_job(store)
                self.assertEqual(store.job["state"], "error")
                self.assertEqual(previous.read_bytes(), good)
                self.assertFalse(list(store.root.glob("*.part")))

    def test_cancel_during_native_check_never_publishes_and_blocks_duplicate_download(self):
        good = b"approved model"
        spec = self.tiny_spec(good)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def validator(*args):
            entered.set()
            release.wait(3)
        store = ModelStore(self.root / "models", opener=lambda *a, **k: Response(good), validator=validator)
        with mock.patch("nvr.model_store.model_spec", return_value=spec):
            store.start(spec.id)
            self.assertTrue(entered.wait(2))
            store.cancel()
            with self.assertRaises(ValueError):
                store.start(spec.id)
            release.set()
            self.wait_job(store)
            self.assertEqual(store.job["state"], "cancelled")
            self.assertFalse(store.path_for(spec.id).exists())

    def test_timed_out_native_validation_retains_single_slot(self):
        release = threading.Event()
        self.addCleanup(release.set)
        calls = mock.Mock(side_effect=lambda *args: release.wait(2))
        store = ModelStore(self.root / "models", validator=calls)
        with self.assertRaises(ValueError):
            store.check_native("unused", model_spec("yolox-nano"), timeout=.01)
        with self.assertRaises(ValueError):
            store.check_native("unused", model_spec("yolox-nano"), timeout=.01)
        self.assertEqual(calls.call_count, 1)
        release.set()
        self.wait_until(lambda: not store._native_lock.locked())

    def test_download_uses_read1_and_checks_deadline_after_each_arrival(self):
        good = b"approved model"
        spec = self.tiny_spec(good)
        class Trickle(Response):
            def read(self, size=-1):
                raise AssertionError("Filling read(n) must not be used")
            def read1(self, size=-1):
                now[0] = 601.
                return super().read1(1)
        now = [0.]
        store = ModelStore(self.root / "models", opener=lambda *a, **k: Trickle(good), validator=mock.Mock())
        with mock.patch("nvr.model_store.model_spec", return_value=spec), \
             mock.patch("nvr.model_store.time.monotonic", side_effect=lambda: now[0]):
            store.start(spec.id)
            store._thread.join(2)
        self.assertFalse(store._thread.is_alive())
        self.assertEqual(store.job["state"], "error")
        self.assertIn("超时", store.job["error"])
        self.assertFalse(store.path_for(spec.id).exists())

    def test_https_trickled_status_and_headers_share_absolute_deadline_and_cancel(self):
        for phase in ("status", "headers"):
            for cancelled in (False, True):
                with self.subTest(phase=phase, cancelled=cancelled):
                    now, cancel = [0.], threading.Event()
                    guard = DownloadGuard(cancel, 2.)
                    prefix = [] if phase == "status" else [b"HTTP/1.1 200 OK\r\n"]
                    parts = prefix + [bytes((c,)) for c in b"X-Slow: trickled forever without newline"]
                    class Socket:
                        timeout = 10
                        def makefile(inner, *a, **kw):
                            return io.BytesIO()
                        def gettimeout(inner):
                            return inner.timeout
                        def settimeout(inner, value):
                            inner.timeout = value
                        def recv_into(inner, target):
                            now[0] += .4
                            if cancelled and now[0] >= 1.2:
                                cancel.set()
                            part = parts.pop(0)
                            target[:len(part)] = part
                            return len(part)
                    response = http.client.HTTPResponse(_ResponseSocket(Socket(), guard))
                    with mock.patch("nvr.model_transport.time.monotonic", side_effect=lambda: now[0]):
                        with self.assertRaises(InterruptedError if cancelled else ValueError):
                            response.begin()
                    response.close()
                    self.assertLessEqual(now[0], 2.4)

    def test_official_https_handler_uses_current_standard_library_signature(self):
        guard = DownloadGuard(threading.Event(), time.monotonic() + 2)
        handler = guarded_https_handler(guard)
        request = urllib.request.Request("https://github.com/model.onnx")
        with mock.patch.object(handler, "do_open", return_value="fixture-response") as opening:
            self.assertEqual(handler.https_open(request), "fixture-response")
        connection_type, actual_request = opening.call_args.args
        self.assertIs(actual_request, request)
        self.assertEqual(set(opening.call_args.kwargs), {"context"})
        connection = connection_type("github.com", timeout=10, **opening.call_args.kwargs)
        self.assertTrue(connection._context.check_hostname)
        self.assertNotEqual(connection._context.verify_mode, 0)
        connection.close()

    def test_connect_header_reads_restore_handshake_timeout_even_on_failure(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                cancel = threading.Event()
                client = WireSocket(b"HTTP/1.1 200 Connection established\r\n\r\n",
                                    cancel.set if failure else None)
                guard = DownloadGuard(cancel, time.monotonic() + 2)
                response = http.client.HTTPResponse(_ResponseSocket(client, guard))
                self.addCleanup(response.close)
                if failure:
                    with self.assertRaises(InterruptedError):
                        response.begin()
                else:
                    response.begin()
                self.assertEqual(client.gettimeout(), 10)

    def test_guarded_standard_and_chunked_body_close_owned_file(self):
        for chunked in (False, True):
            with self.subTest(chunked=chunked):
                header = b"Transfer-Encoding: chunked" if chunked else b"Content-Length: 4"
                body = b"4\r\ntest\r\n0\r\n\r\n" if chunked else b"test"
                client = WireSocket(b"HTTP/1.1 200 OK\r\n" + header + b"\r\n\r\n" + body)
                guard = DownloadGuard(threading.Event(), time.monotonic() + 2)
                response = http.client.HTTPResponse(_ResponseSocket(client, guard))
                self.addCleanup(response.close)
                response.begin()
                chunks = list(iter(lambda: response.read1(4), b""))
                self.assertEqual(b"".join(chunks), b"test")
                response.close()
                self.assertTrue(client.owner.closed)

    def test_open_worker_start_failure_returns_admission_slot(self):
        store = ModelStore(self.root / "start-failure", opener=mock.Mock())
        guard = DownloadGuard(threading.Event(), time.monotonic() + 1)
        with mock.patch("nvr.model_store.threading.Thread.start", side_effect=RuntimeError("fixture")):
            with self.assertRaises(RuntimeError):
                store._open_response(model_spec("yolox-nano"), guard)
        self.assertFalse(store._opening_lock.locked())

    def test_cancelled_open_returns_promptly_retains_slot_and_closes_late_response(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        response = Response(b"unused")
        original_close = response.close
        def close():
            original_close()
            closed.set()
        response.close = close
        def opener(*args, **kwargs):
            entered.set()
            release.wait(3)
            return response
        store = ModelStore(self.root / "blocked-open", opener=opener)
        store.start("yolox-nano")
        self.assertTrue(entered.wait(1))
        store.cancel()
        store._thread.join(.5)
        self.assertFalse(store._thread.is_alive())
        self.assertEqual(store.job["state"], "cancelled")
        self.assertTrue(store._opening_lock.locked())
        with self.assertRaises(ValueError):
            store.start("yolox-nano")
        release.set()
        self.assertTrue(closed.wait(1))
        self.wait_until(lambda: not store._opening_lock.locked())

    def test_open_deadline_bounds_caller_without_spawning_replacement_requests(self):
        release = threading.Event()
        self.addCleanup(release.set)
        opener = mock.Mock(side_effect=lambda *a, **k: (release.wait(3), Response(b""))[1])
        store = ModelStore(self.root / "deadline-open", opener=opener)
        guard = DownloadGuard(threading.Event(), time.monotonic() + .02)
        with self.assertRaises(ValueError):
            store._open_response(model_spec("yolox-nano"), guard)
        with self.assertRaises(ValueError):
            store._open_response(model_spec("yolox-nano"), guard)
        self.assertEqual(opener.call_count, 1)
        release.set()
        self.wait_until(lambda: not store._opening_lock.locked())

    def test_cancel_is_checked_between_short_read1_chunks(self):
        good = b"approved model"
        spec = self.tiny_spec(good)
        class Trickle(Response):
            def read1(inner, size=-1):
                store.cancel()
                return super().read1(1)
        store = ModelStore(self.root / "models", opener=lambda *a, **k: Trickle(good), validator=mock.Mock())
        with mock.patch("nvr.model_store.model_spec", return_value=spec):
            store.start(spec.id)
            self.wait_job(store)
        self.assertEqual(store.job["state"], "cancelled")
        self.assertFalse(store.path_for(spec.id).exists())


class AlgorithmApiTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        with mock.patch.object(Hub, "_start_threads"), mock.patch.object(Hub, "_boot_clawbot"):
            self.hub = Hub(self.cfg, log=lambda *args: None)
        self.hub._threads = []
        self.hub.model_store = ModelStore(self.root / "models", validator=mock.Mock())
        self.client = create_app(self.cfg, self.hub).test_client()

    def test_catalog_get_is_read_only_and_no_forward_or_download(self):
        with mock.patch.object(self.hub.model_store, "check_native") as native, \
             mock.patch.object(self.hub.model_store, "start") as download:
            result = self.client.get("/api/algorithms").get_json()
            self.assertEqual(len(result["catalog"]), 3)
            self.assertEqual(len(result["capabilities"]), 14)
            self.assertEqual(result["active"]["mode"], "auto")
            self.assertTrue(result["installed"]["nanodet-coco"]["installed"])
            native.assert_not_called()
            download.assert_not_called()

    def test_protected_download_cancel_apply_and_no_arbitrary_url(self):
        for route, payload in (("download", {"model_id": "yolox-nano"}), ("cancel", {}),
                               ("apply", DEFAULT_OPTIONS)):
            self.assertEqual(self.client.post("/api/algorithms/" + route, json=payload,
                                             headers={"Origin": "https://evil.test"}).status_code, 403)
            self.assertEqual(self.client.post("/api/algorithms/" + route, data="{}").status_code, 415)
        bad = self.client.post("/api/algorithms/download", json={"model_id": "yolox-nano", "url": "https://github.com/a"})
        self.assertEqual(bad.status_code, 400)
        self.cfg.set("server", "token", "fixture-secret", save=False)
        self.assertEqual(self.client.get("/api/algorithms").status_code, 401)
        self.assertEqual(self.client.post("/api/algorithms/apply", json=DEFAULT_OPTIONS).status_code, 401)

    def test_invalid_apply_and_generic_settings_bypass_rejected_atomically(self):
        before, disk = copy.deepcopy(self.cfg.data), self.cfg.path
        content = Path(disk).read_bytes()
        for options in ({"targets": []}, {"model_id": "bad"}, {"confidence": False}, {"targets": ["sofa", "unsupported"]}):
            self.assertEqual(self.client.post("/api/algorithms/apply", json=options).status_code, 400)
        for change in ({"algorithms": DEFAULT_OPTIONS}, {"detect": {"mode": "market"}}):
            self.assertEqual(self.client.post("/api/settings", json=change).status_code, 400)
        self.assertEqual(self.cfg.data, before)
        self.assertEqual(Path(disk).read_bytes(), content)

    def test_save_failure_preserves_runtime_and_current_recording(self):
        camera = SimpleNamespace(recording=mock.Mock(return_value=True), stop_record=mock.Mock(),
                                 reset_algorithm=mock.Mock())
        self.hub.cams = {"fixture": camera}
        before = copy.deepcopy(self.cfg.data)
        with mock.patch.object(self.hub.model_store, "prepare"), \
             mock.patch.object(self.cfg, "save", side_effect=OSError("fixture write failure")):
            result = self.client.post("/api/algorithms/apply", json=DEFAULT_OPTIONS)
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.cfg.data, before)
        camera.stop_record.assert_not_called()
        camera.reset_algorithm.assert_not_called()

    def test_successful_apply_closes_old_event_before_new_decisions_and_persists(self):
        events = []
        camera = SimpleNamespace(recording=lambda: True,
                                 stop_record=lambda: events.append("closed"),
                                 reset_algorithm=lambda: events.append("reset"))
        self.hub.cams = {"fixture": camera}
        with mock.patch.object(self.hub.model_store, "prepare"):
            result = self.client.post("/api/algorithms/apply", json={"targets": ["dog", "pets"], "confidence": .5})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(events, ["closed", "reset"])
        loaded = Config(self.cfg.path)
        self.assertEqual(loaded.get("detect", "mode"), "market")
        self.assertEqual(loaded.get("algorithms", "targets"), ["dog", "pets"])

    def test_older_slow_apply_cannot_override_newer_request(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        errors = []
        def prepare(model_id):
            if model_id == "yolox-nano":
                entered.set()
                release.wait(3)
        def apply_old():
            try:
                self.hub.apply_algorithms({"model_id": "yolox-nano", "targets": ["dog"]})
            except ValueError as exc:
                errors.append(str(exc))
        with mock.patch.object(self.hub.model_store, "prepare", side_effect=prepare):
            thread = threading.Thread(target=apply_old)
            thread.start()
            self.assertTrue(entered.wait(1))
            self.hub.apply_algorithms({"targets": ["phone"]})
            release.set()
            thread.join(3)
        self.assertTrue(errors)
        self.assertEqual(self.cfg.get("algorithms", "targets"), ["phone"])

    def test_invalid_persisted_objects_keep_catalog_recovery_available(self):
        for value in (None, 12, "bad", [], {"targets": []}):
            with self.subTest(value=value):
                self.cfg.data["algorithms"] = value
                result = self.client.get("/api/algorithms")
                self.assertEqual(result.status_code, 200)
                data = result.get_json()
                self.assertEqual(len(data["catalog"]), 3)
                self.assertTrue(data["configuration_error"])
                self.assertEqual(data["active"], {"mode": "auto"})
        for value in (None, 12, "bad", [], ["model_id"], [None]):
            self.assertEqual(self.client.post("/api/algorithms/download", json=value,
                             content_type="application/json").status_code, 400)

    def test_unchanged_explicit_legacy_mode_supersedes_pending_market_apply(self):
        entered, release, errors = threading.Event(), threading.Event(), []
        self.addCleanup(release.set)
        def prepare(*args):
            entered.set()
            release.wait(3)
        def apply():
            try:
                self.hub.apply_algorithms({"targets": ["dog"]})
            except ValueError as exc:
                errors.append(str(exc))
        with mock.patch.object(self.hub.model_store, "prepare", side_effect=prepare):
            worker = threading.Thread(target=apply)
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.client.post("/api/settings", json={"detect": {"mode": "auto"}}).status_code, 200)
            release.set()
            worker.join(2)
        self.assertTrue(errors)
        self.assertEqual(self.cfg.get("detect", "mode"), "auto")

    def test_market_entry_is_rechecked_after_waiting_for_algorithm_lock(self):
        self.cfg.set("detect", "mode", "market", save=False)
        waiting = threading.Event()
        lock = self.hub.algorithm_lock
        class ObservedLock:
            def __enter__(inner):
                waiting.set()
                return lock.__enter__()
            def __exit__(inner, *args):
                return lock.__exit__(*args)
        self.hub.algorithm_lock = ObservedLock()
        results = []
        def settings():
            with self.client.application.test_client() as client:
                results.append(client.post("/api/settings", json={"detect": {"mode": "market"}}).status_code)
        with lock:
            worker = threading.Thread(target=settings)
            worker.start()
            self.assertTrue(waiting.wait(1))
            self.cfg.set("detect", "mode", "auto", save=False)
        worker.join(2)
        self.assertEqual(results, [400])
        self.assertEqual(self.cfg.get("detect", "mode"), "auto")

    def test_camera_privacy_mutations_read_current_dict_under_config_lock(self):
        for suffix, body, field in (("toggle", {}, "enabled"), ("detect", {"on": False}, "detect")):
            with self.subTest(suffix=suffix):
                camera = {"id": "fixture", "enabled": True, "detect": True}
                self.cfg.data["cameras"] = [camera]
                self.cfg.save()
                waiting, results = threading.Event(), []
                original_get = self.cfg.get_camera
                def get(cid):
                    # A getter outside cfg.lock would retain the stale dictionary.
                    self.assertTrue(self.cfg.lock._is_owned())
                    waiting.set()
                    return original_get(cid)
                def mutate():
                    with self.client.application.test_client() as client:
                        results.append(client.post("/api/cameras/fixture/" + suffix, json=body).status_code)
                with mock.patch.object(self.cfg, "get_camera", side_effect=get):
                    with self.cfg.lock:
                        worker = threading.Thread(target=mutate)
                        worker.start()
                        self.cfg.data = copy.deepcopy(self.cfg.data)
                    worker.join(2)
                self.assertEqual(results, [200])
                self.assertTrue(waiting.is_set())
                self.assertFalse(self.cfg.get_camera("fixture")[field])
                self.assertFalse(Config(self.cfg.path).get_camera("fixture")[field])
