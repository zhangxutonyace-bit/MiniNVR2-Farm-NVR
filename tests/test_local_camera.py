"""Offline local-camera adapter checks. Never open a physical camera."""
import hashlib
import io
import re
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np

from nvr import ffmpeg, local_camera
from nvr.camera import Cam
from nvr.detect import Detector
from nvr.hub import Hub
from nvr.preview import PreviewService, RemotePreviewService
from nvr.server import create_app
from tests.support import OfflineTestCase
from tests.test_api import FakeHub


def device(alias="@device_pnp_fixture-camera-a", name="Test Camera"):
    return {"device": alias, "device_id": hashlib.sha256(alias.encode()).hexdigest(), "name": name}


def spec(cid="local", **changes):
    item = device()
    result = {"id": cid, "name": "Computer camera", "source_type": "local", "url": "",
              "device": item["device"], "device_id": item["device_id"], "device_name": item["name"],
              "enabled": True, "detect": True}
    result.update(changes)
    return result


class DeviceTests(OfflineTestCase):
    def test_parser_pairs_explicit_video_alternative_names_and_preserves_duplicate_names(self):
        payload = '\n'.join([
            '[dshow @ x]    "Test Camera" (video)',
            '[dshow @ x]   Alternative name "@device_pnp_fixture-camera-a"',
            '[dshow @ x] "Microphone" (audio)',
            '[dshow @ x]   Alternative name "@device_cm_microphone"',
            '[dshow @ x] "Unknown" (none)',
            '[dshow @ x] Alternative name "@device_pnp_not-video"',
            '[dshow @ x] "Test Camera" (video)',
            '[dshow @ x]\t  Alternative name "@device_pnp_fixture-camera-b"',
            '[dshow @ x] "No stable alias" (video)',
            '[dshow @ x] "Odd type" (unknown)',
            '[dshow @ x] Alternative name "@device_pnp_reject"',
        ])
        found = local_camera.parse_devices(payload)
        self.assertEqual(len(found), 2)
        self.assertEqual([d["name"] for d in found], ["Test Camera", "Test Camera"])
        self.assertNotEqual(found[0]["device_id"], found[1]["device_id"])
        self.assertEqual(found[0]["device_id"], device()["device_id"])

    def test_enumeration_accepts_listing_nonzero_exit_without_opening_device(self):
        output = b'[dshow @ 0000020905d3e5c0] "Test Camera" (video)\n[dshow @ 0000020905d3e5c0]   Alternative name "@device_pnp_fixture-camera-a"'
        with mock.patch.object(local_camera.ffmpeg, "run", return_value=SimpleNamespace(
                returncode=1, stderr=output)) as runner:
            self.assertEqual(local_camera.enumerate_devices(), [device()])
        args, kwargs = runner.call_args
        self.assertEqual(args[0][-1], "dummy")
        self.assertNotIn("video=", " ".join(args[0]))
        self.assertEqual(kwargs["timeout"], 6)
        self.assertNotIn("shell", kwargs)

    def test_enumeration_timeout_has_actionable_error(self):
        with mock.patch.object(local_camera.ffmpeg, "run", side_effect=subprocess.TimeoutExpired("fixture", 6)):
            with self.assertRaisesRegex(ValueError, "超时"):
                local_camera.enumerate_devices()

    def test_capture_command_only_requests_video_and_record_command_uses_shared_pipe(self):
        capture = ffmpeg.cmd_local_stream(device()["device"])
        self.assertEqual(capture[capture.index("-i") + 1], "video=" + device()["device"])
        self.assertNotIn("audio=", " ".join(capture))
        self.assertNotIn("-re", capture)
        for bad in ("HD Camera", "@device_x\nsecond", '@device_x"', "@device_x\0"):
            with self.assertRaises(ValueError):
                ffmpeg.cmd_local_stream(bad)
        recorder = ffmpeg.cmd_jpeg_record("fixture.mp4")
        self.assertEqual(recorder[recorder.index("-i") + 1], "pipe:0")
        self.assertNotIn("dshow", recorder)
        self.assertIn("-nostdin", recorder)


class LocalApiTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.hub = FakeHub()
        self.app = create_app(self.cfg, self.hub)
        self.app.testing = True
        self.client = self.app.test_client()
        patch = mock.patch("nvr.local_camera.enumerate_devices", return_value=[device()])
        self.enumerator = patch.start()
        self.addCleanup(patch.stop)

    def add(self, **changes):
        body = {"source_type": "local", "device_id": device()["device_id"], "name": "Desk"}
        body.update(changes)
        return self.client.post("/api/cameras", json=body)

    def test_list_add_duplicate_and_public_status_never_expose_native_identifier(self):
        listing = self.client.get("/api/local-cameras").get_json()
        self.assertEqual(listing["devices"], [{"device_id": device()["device_id"], "name": "Test Camera", "added": False}])
        self.assertNotIn("@device_", str(listing))
        result = self.add().get_json()
        self.assertTrue(result["ok"])
        saved = self.cfg.get_camera(result["id"])
        self.assertEqual(saved["device"], device()["device"])
        self.assertEqual(saved["url"], "")
        self.assertEqual(self.hub.sync_requests, 1)
        self.assertEqual(self.add().status_code, 400)
        self.assertEqual(len(self.cfg.cameras), 1)
        self.assertTrue(self.client.get("/api/local-cameras").get_json()["devices"][0]["added"])
        for path in ("/api/settings", "/api/status"):
            self.assertNotIn("@device_", str(self.client.get(path).get_json()))

    def test_unknown_and_missing_devices_rejected_without_configuration_mutation(self):
        self.assertEqual(self.add(device_id="f" * 64).status_code, 400)
        self.enumerator.return_value = []
        self.assertEqual(self.add().status_code, 400)
        self.assertEqual(self.cfg.cameras, [])
        self.assertEqual(self.hub.sync_requests, 0)

    def test_source_type_and_local_switch_types_are_validated(self):
        for changes in ({"source_type": "bogus"}, {"device_id": ""}, {"detect": "false"}, {"enabled": 1}):
            with self.subTest(changes=changes):
                self.assertEqual(self.add(**changes).status_code, 400)
        self.assertEqual(self.cfg.cameras, [])

    def test_device_enumeration_and_add_keep_auth_origin_and_json_guards(self):
        self.cfg.set("server", "token", "offline-test-password", save=False)
        self.assertEqual(self.client.get("/api/local-cameras").status_code, 401)
        self.assertEqual(self.add().status_code, 401)
        self.enumerator.assert_not_called()
        headers = {"X-Token": "offline-test-password", "Origin": "https://other.invalid"}
        self.assertEqual(self.client.post("/api/cameras", json={}, headers=headers).status_code, 403)
        self.enumerator.assert_not_called()
        self.assertEqual(self.client.post("/api/cameras", data="x", headers={"X-Token": "offline-test-password"}).status_code, 415)
        self.assertEqual(self.client.get("/api/local-cameras", headers={"X-Token": "offline-test-password"}).status_code, 200)

    def test_legacy_rtsp_and_source_switch_remain_compatible(self):
        self.cfg.upsert_camera({"id": "original", "url": "rtsp://fixture.invalid/live", "name": "Original"})
        self.cfg.upsert_camera({"id": "original", "name": "Renamed"})
        self.assertEqual(self.cfg.get_camera("original")["url"], "rtsp://fixture.invalid/live")
        self.assertTrue(self.add(id="original").get_json()["ok"])
        self.assertEqual(self.cfg.get_camera("original")["source_type"], "local")
        self.cfg.upsert_camera({"id": "original", "url": "rtsp://fixture.invalid/changed"})
        self.assertNotIn("device", self.cfg.get_camera("original"))
        self.assertEqual(local_camera.source_identity(self.cfg.get_camera("original")), ("rtsp", "rtsp://fixture.invalid/changed"))


class LocalLifecycleTests(OfflineTestCase):
    def idle_hub(self):
        with mock.patch.object(Hub, "_start_threads"), mock.patch.object(Hub, "_boot_clawbot"):
            hub = Hub(self.cfg, log=lambda message: None)
        hub._threads = []
        return hub

    def test_configuration_mutation_cannot_switch_an_old_workers_device(self):
        saved = self.cfg.upsert_camera(spec())
        old_worker = Cam(saved, self.cfg)
        replacement = device("@device_pnp_fixture-camera-b")
        self.cfg.upsert_camera(spec(device=replacement["device"], device_id=replacement["device_id"]))
        new_worker = Cam(self.cfg.get_camera("local"), self.cfg)
        for worker, expected in ((old_worker, device()["device"]), (new_worker, replacement["device"])):
            capture = SimpleNamespace(stdout=io.BytesIO(), poll=lambda: 1)
            with mock.patch("nvr.camera.popen", return_value=capture) as opener:
                with self.assertRaisesRegex(RuntimeError, "电脑摄像头无画面"):
                    worker._pump()
            command = opener.call_args.args[0]
            self.assertEqual(command[command.index("-i") + 1], "video=" + expected)
        self.assertNotEqual(old_worker.source_identity, new_worker.source_identity)

    def test_constructor_derives_capture_and_identity_from_the_same_snapshot(self):
        saved = self.cfg.upsert_camera(spec())
        replacement = device("@device_pnp_fixture-camera-b")
        def concurrent_update(snapshot):
            # Reproduce an API source update in the constructor's copy/identity
            # window, without opening hardware or relying on thread timing.
            saved.update(device=replacement["device"], device_id=replacement["device_id"])
            return local_camera.source_identity(snapshot)
        with mock.patch("nvr.camera.source_identity", side_effect=concurrent_update):
            worker = Cam(saved, self.cfg)
        self.assertEqual(worker.spec["device"], device()["device"])
        self.assertEqual(worker.source_identity, local_camera.source_identity(worker.spec))
        ticket_for_b = {"cid": "local", "camera": PreviewService._camera_key(saved)}
        self.assertFalse(PreviewService._camera_matches(worker, ticket_for_b))

    def test_same_id_device_change_rebuilds_worker_and_disable_releases_it(self):
        current = self.cfg.upsert_camera(spec())
        hub = self.idle_hub()
        self.addCleanup(hub.stop_all)
        old = mock.Mock(id="local", url="", source_identity=local_camera.source_identity(current))
        hub.cams["local"] = old
        replacement = device("@device_pnp_fixture-camera-b")
        self.cfg.upsert_camera(spec(device=replacement["device"], device_id=replacement["device_id"]))
        worker = mock.Mock()
        worker.source_identity = local_camera.source_identity(self.cfg.get_camera("local"))
        with mock.patch("nvr.hub.Cam", return_value=worker):
            hub.sync()
        old.stop.assert_called_once()
        worker.start.assert_called_once()
        self.cfg.get_camera("local")["enabled"] = False
        hub.sync()
        worker.stop.assert_called_once()
        self.assertNotIn("local", hub.cams)

    def test_local_source_uses_exact_existing_detector_modes_and_parameters(self):
        camera = Cam(spec(), self.cfg)
        for mode in ("auto", "haar", "motion"):
            self.cfg.set("detect", "mode", mode, save=False)
            self.cfg.set("detect", "min_area", 0.07, save=False)
            detector = camera.ensure_detector()
            self.assertIs(type(detector), Detector)
            self.assertEqual(detector.mode, mode)
            self.assertEqual(detector.min_area, 0.07)

    def test_existing_event_confirmation_and_departure_rules_apply_to_local_source(self):
        self.cfg.set("detect", "confirm", 2, save=False)
        self.cfg.set("detect", "gone_sec", 1, save=False)
        self.cfg.set("detect", "cooldown", 0, save=False)
        camera = Cam(spec(), self.cfg)
        camera.state = "online"
        camera.frame_bgr = mock.Mock(return_value=object())
        detector = mock.Mock()
        camera.ensure_detector = mock.Mock(return_value=detector)
        active = [False]
        camera.start_record = mock.Mock(side_effect=lambda: active.__setitem__(0, True))
        camera.stop_record = mock.Mock(side_effect=lambda: active.__setitem__(0, False))
        camera.recording = mock.Mock(side_effect=lambda: active[0])
        hub = self.idle_hub()
        hub.cams["local"] = camera
        hub._stop = mock.Mock()
        hub._stop.is_set.return_value = False
        hub._stop.wait.return_value = True
        for seq, positive, now in ((1, True, 100), (2, True, 100.5), (3, False, 102)):
            camera.seq = seq
            detector.process.return_value = positive
            with mock.patch("nvr.hub.time.time", return_value=now):
                hub._detect_loop()
            if seq == 1:
                camera.start_record.assert_not_called()
        camera.start_record.assert_called_once()
        camera.stop_record.assert_called_once()
        self.assertFalse(camera.person)
        self.assertFalse(active[0])

    def test_both_preview_services_reject_previous_worker_after_source_change(self):
        first, second = spec(), spec(device=device("@device_pnp_fixture-camera-b")["device"],
                                    device_id=device("@device_pnp_fixture-camera-b")["device_id"])
        old_worker = Cam(first, self.cfg)
        new_worker = Cam(second, self.cfg)
        for service in (PreviewService, RemotePreviewService):
            ticket = {"cid": "local", "camera": service._camera_key(second)}
            self.assertFalse(service._camera_matches(old_worker, ticket))
            self.assertTrue(service._camera_matches(new_worker, ticket))
            self.assertNotEqual(service._camera_key(first), service._camera_key(second))

    def test_issued_lan_and_remote_tickets_revoke_when_local_device_changes(self):
        for service_type in (PreviewService, RemotePreviewService):
            with self.subTest(service=service_type.__name__):
                for section, key, value in (("preview", "enabled", True),
                                            ("preview", "address", "192.168.1.10"),
                                            ("remote_preview", "enabled", True),
                                            ("clawbot", "enabled", True),
                                            ("clawbot", "user_id", "fixture-owner")):
                    self.cfg.set(section, key, value, save=False)
                first = self.cfg.upsert_camera(spec())
                old = Cam(first, self.cfg)
                old.state, old.last_ts, old.last_jpeg = "online", time.time(), b"fixture-jpeg"
                holder = [old]
                service = service_type(self.cfg, lambda cid: holder[0])
                thread = mock.Mock()
                thread.is_alive.return_value = True
                with mock.patch("nvr.preview._bindable_address", return_value=True), \
                        mock.patch.object(service, "_start_listener", return_value=(mock.Mock(server_port=49123), thread)):
                    try:
                        if service_type is RemotePreviewService:
                            service.start_origin()
                            service.set_public_url("https://viewer.fixture.example")
                        link = service.make_link("local")
                        token = urllib.parse.parse_qs(urllib.parse.urlsplit(link).fragment)["t"][0]
                        client = service.app.test_client()
                        headers = {"Authorization": "Bearer " + token}
                        self.assertEqual(client.get("/frame/local", headers=headers).status_code, 200)
                        replacement = device("@device_pnp_fixture-camera-b")
                        self.cfg.upsert_camera(spec(device=replacement["device"], device_id=replacement["device_id"]))
                        self.assertEqual(client.get("/frame/local", headers=headers).status_code, 410)
                        link = service.make_link("local")
                        token = urllib.parse.parse_qs(urllib.parse.urlsplit(link).fragment)["t"][0]
                        headers = {"Authorization": "Bearer " + token}
                        self.assertEqual(client.get("/frame/local", headers=headers).status_code, 503)
                        current = Cam(self.cfg.get_camera("local"), self.cfg)
                        current.state, current.last_ts, current.last_jpeg = "online", time.time(), b"new-fixture-jpeg"
                        holder[0] = current
                        self.assertEqual(client.get("/frame/local", headers=headers).data, b"new-fixture-jpeg")
                    finally:
                        service.stop()


class LocalMediaTests(OfflineTestCase):
    def jpeg(self):
        image = np.full((180, 320, 3), 55, np.uint8)
        cv2.rectangle(image, (100, 20), (160, 160), (10, 150, 240), -1)
        return cv2.imencode(".jpg", image)[1].tobytes()

    def test_one_device_open_shared_preview_and_small_playable_event(self):
        camera = Cam(spec(), self.cfg)
        self.addCleanup(camera.stop)
        capture = SimpleNamespace(stdout=io.BytesIO(self.jpeg() * 4), poll=lambda: None)
        with mock.patch("nvr.camera.popen", return_value=capture) as capture_open:
            with self.assertRaisesRegex(RuntimeError, "流已断"):
                camera._pump()
            self.assertEqual(camera.seq, 4)
            self.assertIsNotNone(camera.frame_bgr())
            camera.start_record()
            self.assertTrue(camera.recording())
            path = Path(camera.rec_path)
            self.assertTrue(path.name.endswith(".part.mp4"))
            recorder = camera._jpeg_recorder
            self.wait_until(lambda: recorder.frames >= 5, description="shared JPEG event frames")
            camera.stop_record()
            capture_open.assert_called_once()
        self.assertFalse(path.exists())
        path = path.with_name(path.name.replace(".part.mp4", ".mp4"))
        self.assertTrue(path.is_file())
        self.assertLess(path.stat().st_size, 20 * 1024, "Short valid webcam events must not use the RTSP size cutoff")
        self.assertEqual(ffmpeg.probe(str(path))[:2], (320, 180))
        self.assertIn(b"moov", path.read_bytes()[:65536])
        self.assertIsNotNone(recorder.proc.poll())
        self.assertFalse(recorder.thread.is_alive())
        self.assertEqual(camera.rec_path, "")
        self.assertFalse(camera.recording())
        self.assertTrue(camera.snapshot()[0])

    def test_slow_shared_frame_delivery_preserves_elapsed_video_time(self):
        jpeg = self.jpeg()
        arrivals = []
        def slow_snapshot():
            time.sleep(0.25)  # Four deliveries/sec to an eight-fps recorder.
            arrivals.append(time.monotonic())
            return jpeg, len(arrivals)
        path = str(self.root / "slow-arrivals.mp4")
        recorder = local_camera.SharedJpegRecorder(path, slow_snapshot, fps=8)
        self.addCleanup(lambda: recorder.stop() if recorder.thread.is_alive() else None)
        self.wait_until(lambda: recorder.frames >= 13, timeout=8,
                        description="deliberately slow shared JPEG delivery")
        self.assertTrue(recorder.stop())
        written = arrivals[:recorder.frames]
        wall_span = written[-1] - written[0]
        decoded = ffmpeg.run([ffmpeg.ffmpeg_exe(), "-hide_banner", "-i", path,
                              "-f", "null", "-"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(decoded.returncode, 0)
        metadata = decoded.stderr.decode("utf-8", "replace")
        duration = re.search(r"Duration: (\d+):(\d+):([\d.]+)", metadata)
        self.assertIsNotNone(duration, metadata)
        video_seconds = int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])
        # The old frame-count timestamps yielded ~1.63 s for this >=3 s span.
        # Allow one packet of demux latency and FFmpeg's 1/fps quantization.
        self.assertGreaterEqual(wall_span, 3.0)
        self.assertAlmostEqual(video_seconds, wall_span, delta=0.5)

    def test_offline_or_empty_source_does_not_start_event_encoder(self):
        camera = Cam(spec(), self.cfg)
        with mock.patch("nvr.camera.SharedJpegRecorder") as recorder:
            camera.start_record()
            camera.state = "online"
            camera.start_record()
            recorder.assert_not_called()

    def test_failed_encoder_with_no_output_still_sets_visible_error(self):
        logs = []
        camera = Cam(spec(), self.cfg, log=logs.append)
        camera.rec_path = str(self.root / "missing.part.mp4")
        camera._jpeg_recorder = mock.Mock()
        camera._jpeg_recorder.stop.return_value = False
        camera.stop_record()
        self.assertIn("编码未完成", camera.last_error)
        self.assertTrue(logs)
        self.assertEqual(camera.rec_path, "")

    def test_unfinished_local_recording_is_not_listed_or_directly_playable(self):
        self.cfg.upsert_camera(spec())
        path = self.root / "records" / "local" / time.strftime("%Y-%m-%d") / "event" / "123456.part.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"unfinished" * 1000)
        app = create_app(self.cfg, FakeHub())
        client = app.test_client()
        self.assertEqual(client.get("/api/recordings").get_json()["count"], 0)
        self.assertEqual(client.get("/api/rec/local/%s/event/%s" % (time.strftime("%Y-%m-%d"), path.name)).status_code, 400)

    def test_encoder_launch_failure_is_visible_and_busy_path_is_cleared(self):
        camera = Cam(spec(), self.cfg)
        camera.state, camera.last_jpeg = "online", self.jpeg()
        with mock.patch("nvr.camera.SharedJpegRecorder", side_effect=OSError("fixture error")):
            camera.start_record()
        self.assertIn("录像启动失败", camera.last_error)
        self.assertFalse(camera.recording())
        self.assertEqual(camera.rec_path, "")

    def test_blocked_encoder_is_terminated_without_blocking_snapshot_or_shutdown(self):
        blocked, release = threading.Event(), threading.Event()
        process = mock.Mock()
        process.returncode = None
        process.poll.side_effect = lambda: process.returncode
        def write(payload):
            blocked.set()
            release.wait(8)
            raise BrokenPipeError("fixture closed")
        process.stdin.write.side_effect = write
        def terminate():
            process.returncode = -15
            release.set()
        process.terminate.side_effect = terminate
        with mock.patch("nvr.local_camera.ffmpeg.popen", return_value=process):
            recorder = local_camera.SharedJpegRecorder("fixture.mp4", lambda: (self.jpeg(), 1))
        try:
            self.assertTrue(blocked.wait(2))
            started = time.monotonic()
            self.assertFalse(recorder.stop())
            self.assertLess(time.monotonic() - started, 4)
            self.assertFalse(recorder.thread.is_alive())
            process.terminate.assert_called_once()
            process.stdin.close.assert_called_once()
        finally:
            release.set()

    def test_pipe_failure_records_failure_instead_of_claiming_success(self):
        process = mock.Mock(returncode=1)
        process.stdin.write.side_effect = BrokenPipeError("fixture encoder exited")
        with mock.patch("nvr.local_camera.ffmpeg.popen", return_value=process):
            recorder = local_camera.SharedJpegRecorder("fixture.mp4", lambda: (self.jpeg(), 1))
        recorder.thread.join(2)
        self.assertTrue(recorder.failed)
        self.assertFalse(recorder.stop())
