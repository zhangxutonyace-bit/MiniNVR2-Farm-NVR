"""Offline HTTP coverage for cached, annotated previews and source isolation.

The cameras here contain distinct raw/painted JPEGs and cannot capture or infer.
These are HTTP/capability assertions, not a claim about real-camera smoothness.
"""
import threading
import time
import urllib.parse
from types import SimpleNamespace
from unittest import mock

from nvr.event_overlay import FramePacket
from nvr.preview import FRAME_MAX_AGE, PreviewService, RemotePreviewService
from nvr.server import create_app, placeholder
from tests.support import OfflineTestCase


class _CachedCamera:
    def __init__(self, cid="farm", marker="first", seq=7):
        self.id = cid
        self.name = cid
        self.url = cid + "-fixture.mp4"
        self.enabled = True
        self.state = "online"
        self.last_error = ""
        self.lock = threading.RLock()
        self.seq = seq
        self.last_ts = time.time()
        self.packet_timestamp = self.last_ts
        self.raw = placeholder("raw-" + marker)
        self.painted = placeholder("painted-" + marker)
        self.last_jpeg = self.raw
        self.source_identity = ("rtsp", self.url)
        self._capture_epoch = 4
        self.packet_source = (self.source_identity, self._capture_epoch)
        self.snapshot = mock.Mock(side_effect=lambda: (self.raw, self.seq))
        self.preview_snapshot = mock.Mock(side_effect=self._preview_snapshot)
        self.preview_sample = mock.Mock(side_effect=self._preview_sample)

    def _preview_snapshot(self, boxes=True):
        return (self.painted if boxes else self.raw), self.seq

    def _preview_sample(self, boxes=True):
        return FramePacket(self.painted if boxes else self.raw, self.seq,
                           self.packet_timestamp, self.packet_source)


class _ChangingHub:
    def __init__(self, cameras):
        self.cameras = cameras
        self.get = mock.Mock(side_effect=cameras.get)


class _CachedRouteTest(OfflineTestCase):
    def setUp(self):
        super().setUp()
        # This suite tests live config/HTTP behavior, not filesystem persistence.
        save = mock.patch.object(self.cfg, "save")
        save.start()
        self.addCleanup(save.stop)
        # Any accidental capture/model work on an HTTP request fails immediately.
        for target in ("nvr.algorithm_runtime.AlgorithmRunner.infer",
                       "nvr.ffmpeg.popen", "cv2.VideoCapture", "cv2.dnn.readNet"):
            patcher = mock.patch(target, side_effect=AssertionError(
                "Preview HTTP requests must only read prepared JPEG caches"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cameras = {cid: _CachedCamera(cid, cid) for cid in ("farm", "coop")}
        for cid, camera in self.cameras.items():
            self.cfg.upsert_camera({"id": cid, "name": cid, "url": camera.url,
                                    "enabled": True})


class MainPreviewRouteTests(_CachedRouteTest):
    def setUp(self):
        super().setUp()
        self.hub = _ChangingHub(self.cameras)
        self.app = create_app(self.cfg, self.hub)
        self.app.testing = True
        self.client = self.app.test_client()
        patcher = mock.patch("nvr.server.time.sleep", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stream(self, suffix="", headers=None):
        response = self.client.get("/live/farm" + suffix, headers=headers or {}, buffered=False)
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        self.assertIn("multipart/x-mixed-replace", response.content_type)
        return iter(response.response)

    def assert_frame(self, body, wanted, unwanted):
        self.assertIn(b"Content-Type: image/jpeg", body)
        self.assertIn(wanted, body)
        self.assertNotIn(unwanted, body)

    def fresh_camera(self, marker):
        camera = _CachedCamera("farm", marker)
        self.cameras["farm"] = camera
        self.cfg.upsert_camera({"id": "farm", "name": "farm", "url": camera.url,
                                "enabled": True})
        return camera

    @staticmethod
    def change_after_next_read(camera, change):
        def changed(boxes=True):
            previous = camera._preview_snapshot(boxes)
            camera.preview_snapshot.side_effect = camera._preview_snapshot
            change()
            return previous
        camera.preview_snapshot.side_effect = changed

    def test_live_defaults_to_annotated_cached_jpeg(self):
        camera = self.cameras["farm"]
        self.assert_frame(next(self.stream()), camera.painted, camera.raw)
        camera.preview_snapshot.assert_called_once_with(boxes=True)
        camera.preview_sample.assert_not_called()
        camera.snapshot.assert_not_called()

    def test_live_explicit_boxes_zero_returns_raw_cached_jpeg(self):
        camera = self.cameras["farm"]
        self.assert_frame(next(self.stream("?boxes=0")), camera.raw, camera.painted)
        self.assertFalse(any(call.kwargs.get("boxes", True)
                             for call in camera.preview_snapshot.call_args_list))

    def test_live_explicit_boxes_one_returns_annotated_cached_jpeg(self):
        camera = self.cameras["farm"]
        self.assert_frame(next(self.stream("?boxes=1")), camera.painted, camera.raw)
        camera.preview_snapshot.assert_called_once_with(boxes=True)

    def test_snapshot_remains_raw_unless_annotation_explicitly_requested(self):
        camera = self.cameras["farm"]
        response = self.client.get("/snap/farm")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertEqual(response.data, camera.raw)
        response.close()
        self.assertFalse(any(call.kwargs.get("boxes", True)
                             for call in camera.preview_snapshot.call_args_list))
        response = self.client.get("/snap/farm?boxes=1")
        self.assertEqual(response.data, camera.painted)
        call = camera.preview_snapshot.call_args
        self.assertTrue(call.kwargs.get("boxes", call.args[0] if call.args else True))
        response.close()

    def test_stream_resolves_new_worker_and_drops_old_frame_even_at_same_sequence(self):
        old = self.cameras["farm"]
        frames = self.stream()
        self.assertIn(old.painted, next(frames))
        replacement = _CachedCamera("farm", "replacement", old.seq)
        self.cameras["farm"] = replacement
        self.assert_frame(next(frames), replacement.painted, old.painted)
        self.assertGreaterEqual(self.hub.get.call_count, 2)
        replacement.preview_snapshot.assert_called_once_with(boxes=True)

    def test_stream_publishes_newly_painted_cache_without_requiring_new_capture_sequence(self):
        camera = self.cameras["farm"]
        frames = self.stream()
        previous = camera.painted
        self.assertIn(previous, next(frames))
        # The background painter may finish after the raw frame was displayed.
        # Its packet keeps the capture sequence, while the JPEG bytes change.
        camera.painted = placeholder("completed-annotation-same-capture")
        self.assert_frame(next(frames), camera.painted, previous)

    def test_unchanged_frame_uses_half_second_heartbeat_instead_of_poll_rate_retransmit(self):
        camera = self.cameras["farm"]
        clock = [100.]

        def advance(seconds):
            clock[0] += seconds

        with mock.patch("nvr.server.time.monotonic", side_effect=lambda: clock[0]), \
                mock.patch("nvr.server.time.sleep", side_effect=advance):
            frames = self.stream()
            first = next(frames)
            first_stamp = clock[0]
            second = next(frames)
            second_stamp = clock[0]
            self.assertEqual(first, second)
            self.assertGreaterEqual(second_stamp - first_stamp, .5)
            self.assertLess(second_stamp - first_stamp, .55)
            # Several cheap cache polls may run, but no intermediate duplicate
            # MJPEG frame should reach the viewer before the heartbeat.
            self.assertGreater(camera.preview_snapshot.call_count, 10)
            self.assertLess(camera.preview_snapshot.call_count, 30)

    def test_stream_removed_worker_never_reuses_old_annotated_cache(self):
        old = self.cameras["farm"]
        frames = self.stream()
        self.assertIn(old.painted, next(frames))
        del self.cameras["farm"]
        body = next(frames)
        self.assertNotIn(old.painted, body)
        self.assertNotIn(old.raw, body)

    def test_stream_disabled_worker_never_reuses_old_annotated_cache(self):
        camera = self.cameras["farm"]
        frames = self.stream()
        self.assertIn(camera.painted, next(frames))
        self.cfg.upsert_camera(dict(self.cfg.get_camera("farm"), enabled=False))
        camera.enabled = False
        body = next(frames)
        self.assertFalse(camera.painted in body, "disabled worker leaked painted cache")
        self.assertFalse(camera.raw in body, "disabled worker leaked raw cache")

    def test_stream_configuration_source_change_drops_old_worker_cache(self):
        camera = self.cameras["farm"]
        frames = self.stream()
        self.assertIn(camera.painted, next(frames))
        self.cfg.upsert_camera(dict(self.cfg.get_camera("farm"),
                                    url="new-source-fixture.mp4"))
        body = next(frames)
        self.assertFalse(camera.painted in body, "previous source leaked painted cache")
        self.assertFalse(camera.raw in body, "previous source leaked raw cache")

    def test_stream_config_change_during_cache_read_discards_current_and_heartbeat_bytes(self):
        for change in ("disable", "delete", "source"):
            with self.subTest(change=change):
                camera = self.fresh_camera("mid-read-" + change)
                frames = self.stream()
                self.assertIn(camera.painted, next(frames))

                def mutate():
                    if change == "delete":
                        self.cfg.remove_camera("farm")
                    else:
                        spec = dict(self.cfg.get_camera("farm"))
                        spec.update({"enabled": False} if change == "disable"
                                    else {"url": "replacement-source.mp4"})
                        self.cfg.upsert_camera(spec)

                self.change_after_next_read(camera, mutate)
                following = next(frames)
                self.assertFalse(camera.painted in following, "mid-read config change leaked old cache")
                self.assertFalse(camera.raw in following, "mid-read config change leaked old raw frame")

    def test_stream_worker_replaced_during_cache_read_sends_only_replacement(self):
        camera = self.cameras["farm"]
        frames = self.stream()
        self.assertIn(camera.painted, next(frames))
        replacement = _CachedCamera("farm", "mid-read-replacement", camera.seq)
        self.change_after_next_read(camera, lambda: self.cameras.__setitem__("farm", replacement))
        self.assert_frame(next(frames), replacement.painted, camera.painted)

    def test_stream_capture_epoch_changed_during_cache_read_accepts_restarted_sequence(self):
        camera = self.cameras["farm"]
        frames = self.stream()
        previous = camera.painted
        self.assertIn(previous, next(frames))

        def reconnect():
            camera._capture_epoch += 1
            camera.seq = 1
            camera.painted = placeholder("new-capture-epoch")

        self.change_after_next_read(camera, reconnect)
        self.assert_frame(next(frames), camera.painted, previous)

    def test_stream_capture_epoch_changed_between_iterations_accepts_restarted_sequence(self):
        camera = self.cameras["farm"]
        frames = self.stream()
        previous = camera.painted
        self.assertIn(previous, next(frames))
        camera._capture_epoch += 1
        camera.seq = 1
        camera.painted = placeholder("epoch-between-iterations")
        polls = [0]

        def bounded_sleep(seconds):
            polls[0] += 1
            self.assertLess(polls[0], 5, "new capture view must not wait for its old sequence")

        with mock.patch("nvr.server.time.sleep", side_effect=bounded_sleep):
            self.assert_frame(next(frames), camera.painted, previous)

    def test_stream_model_context_changed_between_iterations_resets_heartbeat_and_sequence(self):
        camera = self.cameras["farm"]
        camera._preview_context = mock.Mock(return_value="previous-model-view")
        frames = self.stream()
        previous = camera.painted
        self.assertIn(previous, next(frames))
        camera._preview_context.return_value = "applied-model-view"
        camera.seq = 1
        camera.painted = placeholder("model-between-iterations")
        polls = [0]

        def bounded_sleep(seconds):
            polls[0] += 1
            self.assertLess(polls[0], 5, "new model view must not retain its old heartbeat")

        with mock.patch("nvr.server.time.sleep", side_effect=bounded_sleep):
            self.assert_frame(next(frames), camera.painted, previous)

    def test_stream_model_revision_changed_during_cache_read_never_sends_old_overlay(self):
        camera = self.cameras["farm"]
        camera._preview_context = mock.Mock(
            side_effect=lambda: ("fixture-model", self.cfg.get("algorithms", "confidence")))
        frames = self.stream()
        previous = camera.painted
        self.assertIn(previous, next(frames))

        def apply_model_options():
            self.cfg.set("algorithms", "confidence", .65, save=False)
            camera.painted = placeholder("new-model-context")

        self.change_after_next_read(camera, apply_model_options)
        self.assert_frame(next(frames), camera.painted, previous)

    def test_explicit_annotated_snapshot_rejects_stale_worker_before_cache_read(self):
        for change in ("disable", "delete", "source"):
            with self.subTest(change=change):
                camera = self.fresh_camera("snapshot-before-" + change)
                if change == "delete":
                    self.cfg.remove_camera("farm")
                else:
                    spec = dict(self.cfg.get_camera("farm"))
                    spec.update({"enabled": False} if change == "disable"
                                else {"url": "changed-source.mp4"})
                    self.cfg.upsert_camera(spec)
                response = self.client.get("/snap/farm?boxes=1")
                self.assertEqual(response.status_code, 503)
                self.assertFalse(camera.painted in response.data)
                response.close()
                camera.preview_snapshot.assert_not_called()
                camera.snapshot.assert_not_called()
                # This patch deliberately preserves the existing raw API.
                raw = self.client.get("/snap/farm")
                self.assertEqual(raw.data, camera.raw)
                raw.close()

    def test_explicit_annotated_snapshot_discards_generation_changes_during_cache_read(self):
        for change in ("disable", "delete", "source", "worker", "epoch", "revision"):
            with self.subTest(change=change):
                camera = self.fresh_camera("snapshot-after-" + change)
                revision = ["previous"]
                camera._preview_context = mock.Mock(side_effect=lambda: revision[0])

                def mutate():
                    if change == "delete":
                        self.cfg.remove_camera("farm")
                    elif change in ("disable", "source"):
                        spec = dict(self.cfg.get_camera("farm"))
                        spec.update({"enabled": False} if change == "disable"
                                    else {"url": "changed-source.mp4"})
                        self.cfg.upsert_camera(spec)
                    elif change == "worker":
                        self.cameras["farm"] = _CachedCamera("farm", "new-worker")
                    elif change == "epoch":
                        camera._capture_epoch += 1
                    else:
                        revision[0] = "applied-new-revision"

                self.change_after_next_read(camera, mutate)
                response = self.client.get("/snap/farm?boxes=1")
                self.assertEqual(response.status_code, 503)
                self.assertFalse(camera.painted in response.data, "stale annotated snapshot was sent")
                response.close()
                camera.preview_snapshot.assert_called_once_with(boxes=True)

    def test_explicit_annotated_snapshot_rejects_token_rotation_during_cache_read(self):
        camera = self.cameras["farm"]
        self.change_after_next_read(camera,
            lambda: self.cfg.set("server", "token", "rotated-during-snapshot", save=False))
        response = self.client.get("/snap/farm?boxes=1")
        self.assertEqual(response.status_code, 401)
        self.assertFalse(camera.painted in response.data)
        response.close()

    def test_token_rotation_during_post_read_context_check_cannot_send_pixels(self):
        for path in ("/live/farm", "/snap/farm?boxes=1"):
            with self.subTest(path=path):
                self.cfg.set("server", "token", "", save=False)
                camera = self.fresh_camera("post-read-token")
                contexts = [0]

                def context():
                    contexts[0] += 1
                    if contexts[0] == 2:
                        self.cfg.set("server", "token", "new-post-read-token", save=False)
                    return "same-model-revision"

                camera._preview_context = mock.Mock(side_effect=context)
                response = self.client.get(path, buffered=False)
                try:
                    if path.startswith("/live/"):
                        with self.assertRaises(StopIteration):
                            next(iter(response.response))
                    else:
                        self.assertEqual(response.status_code, 401)
                        self.assertFalse(camera.painted in response.data)
                finally:
                    response.close()

    def test_viewer_auth_rejects_before_reading_any_camera_cache(self):
        self.cfg.set("server", "token", "fixture-viewer-token", save=False)
        for path in ("/live/farm", "/live/farm?boxes=0", "/snap/farm?boxes=1"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)
                response.close()
        self.hub.get.assert_not_called()
        for camera in self.cameras.values():
            camera.preview_snapshot.assert_not_called()
            camera.preview_sample.assert_not_called()
            camera.snapshot.assert_not_called()

    def test_token_rotated_between_authentication_and_route_cannot_bind_new_generation(self):
        @self.app.before_request
        def rotate_after_authentication():
            self.cfg.set("server", "token", "new-generation", save=False)

        for old in ("old-generation", ""):
            with self.subTest(old_password_set=bool(old)):
                self.cfg.set("server", "token", old, save=False)
                response = self.client.get("/live/farm", headers={"X-Token": old}, buffered=False)
                try:
                    self.assertEqual(response.status_code, 200)
                    with self.assertRaises(StopIteration):
                        next(iter(response.response))
                finally:
                    response.close()
        self.hub.get.assert_not_called()
        self.cameras["farm"].preview_snapshot.assert_not_called()

    def test_authenticated_stream_ends_when_access_token_is_changed(self):
        camera = self.cameras["farm"]
        self.cfg.set("server", "token", "fixture-old-access", save=False)
        frames = self.stream(headers={"X-Token": "fixture-old-access"})
        self.assertIn(camera.painted, next(frames))
        reads_before_rotation = camera.preview_snapshot.call_count
        self.cfg.set("server", "token", "fixture-new-access", save=False)
        with self.assertRaises(StopIteration):
            next(frames)
        self.assertEqual(camera.preview_snapshot.call_count, reads_before_rotation)

    def test_local_passwordless_stream_ends_when_access_token_is_enabled(self):
        camera = self.cameras["farm"]
        frames = self.stream()
        self.assertIn(camera.painted, next(frames))
        reads_before_rotation = camera.preview_snapshot.call_count
        self.cfg.set("server", "token", "fixture-now-protected", save=False)
        with self.assertRaises(StopIteration):
            next(frames)
        self.assertEqual(camera.preview_snapshot.call_count, reads_before_rotation)


class CapabilityPreviewCacheTests(_CachedRouteTest):
    """Run each payload/isolation assertion against LAN and remote origins."""
    def setUp(self):
        super().setUp()
        for section, key, value in (
            ("preview", "enabled", True), ("preview", "address", "192.168.1.10"),
            ("preview", "port", 8803), ("remote_preview", "enabled", True),
            ("clawbot", "enabled", True), ("clawbot", "user_id", "fixture-owner"),
            ("clawbot", "token", "fixture-only-bot-token"),
        ):
            self.cfg.set(section, key, value, save=False)
        patcher = mock.patch("nvr.preview._bindable_address", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.viewers = []
        for service_type, origin, peer in (
            (PreviewService, "http://192.168.1.10:8803", "192.168.1.20"),
            (RemotePreviewService, "https://viewer.fixture.example", "127.0.0.1"),
        ):
            service = service_type(self.cfg, self.cameras.get)
            listener = mock.Mock(server_port=49123)
            thread = mock.Mock()
            thread.is_alive.return_value = True
            patcher = mock.patch.object(service, "_start_listener",
                                        return_value=(listener, thread))
            patcher.start()
            self.addCleanup(patcher.stop)
            self.addCleanup(service.stop)
            if isinstance(service, RemotePreviewService):
                service.start_origin()
                service.set_public_url(origin)
            service.app.testing = True
            self.viewers.append((service, service.app.test_client(), origin, peer))

    def ticket(self, viewer, cid="farm"):
        parsed = urllib.parse.urlsplit(viewer[0].make_link(cid))
        return urllib.parse.parse_qs(parsed.fragment)["t"][0]

    def get(self, viewer, cid="farm", ticket=None):
        _, client, origin, peer = viewer
        headers = {"Authorization": "Bearer " + ticket} if ticket else {}
        response = client.get("/frame/" + cid, headers=headers, base_url=origin,
                              environ_overrides={"REMOTE_ADDR": peer})
        self.addCleanup(response.close)
        return response

    def assert_no_cache_reads(self):
        for camera in self.cameras.values():
            camera.preview_sample.assert_not_called()
            camera.preview_snapshot.assert_not_called()
            camera.snapshot.assert_not_called()

    def test_valid_lan_and_remote_tickets_read_annotated_packet_cache_only(self):
        camera = self.cameras["farm"]
        for viewer in self.viewers:
            with self.subTest(service=type(viewer[0]).__name__):
                response = self.get(viewer, ticket=self.ticket(viewer))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "image/jpeg")
                self.assertEqual(response.data, camera.painted)
                self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        self.assertEqual(camera.preview_sample.call_count, 2)
        for call in camera.preview_sample.call_args_list:
            self.assertTrue(call.kwargs.get("boxes", call.args[0] if call.args else True))
        camera.preview_snapshot.assert_not_called()
        camera.snapshot.assert_not_called()
        self.cameras["coop"].preview_sample.assert_not_called()

    def test_missing_invalid_and_wrong_camera_ticket_never_read_any_cache(self):
        for viewer in self.viewers:
            ticket = self.ticket(viewer)
            for cid, supplied, status in (("farm", None, 401),
                                           ("farm", "x" * 43, 401),
                                           ("coop", ticket, 403)):
                with self.subTest(service=type(viewer[0]).__name__, cid=cid, status=status):
                    response = self.get(viewer, cid, supplied)
                    self.assertEqual(response.status_code, status)
                    self.assertNotEqual(response.mimetype, "image/jpeg")
        self.assert_no_cache_reads()

    def test_revoked_camera_ticket_rejects_before_reading_any_cache(self):
        tickets = [(viewer, self.ticket(viewer)) for viewer in self.viewers]
        self.cfg.upsert_camera(dict(self.cfg.get_camera("farm"), enabled=False))
        for viewer, ticket in tickets:
            self.assertEqual(self.get(viewer, ticket=ticket).status_code, 410)
        self.assert_no_cache_reads()

    def test_ticket_for_replaced_source_never_reads_old_worker_cache(self):
        old = self.cameras["farm"]
        old_spec = dict(self.cfg.get_camera("farm"))
        self.cfg.upsert_camera(dict(old_spec, url="replacement-fixture.mp4"))
        for viewer in self.viewers:
            response = self.get(viewer, ticket=self.ticket(viewer))
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(old.painted, response.data)
        self.assert_no_cache_reads()

    def test_worker_disabled_after_ticket_issue_never_reads_old_cache(self):
        tickets = [(viewer, self.ticket(viewer)) for viewer in self.viewers]
        self.cameras["farm"].enabled = False
        for viewer, ticket in tickets:
            self.assertEqual(self.get(viewer, ticket=ticket).status_code, 503)
        self.assert_no_cache_reads()

    def test_worker_identity_change_during_cache_read_is_rejected(self):
        camera = self.cameras["farm"]
        original = camera.url
        for viewer in self.viewers:
            with self.subTest(service=type(viewer[0]).__name__):
                camera.url = original
                ticket = self.ticket(viewer)

                def changed_source(boxes=True):
                    packet = camera._preview_sample(boxes)
                    camera.url = "other-camera-fixture.mp4"
                    return packet

                camera.preview_sample.side_effect = changed_source
                response = self.get(viewer, ticket=ticket)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(camera.painted, response.data)
        camera.snapshot.assert_not_called()

    def test_model_or_detection_context_change_during_capability_read_discards_old_labels(self):
        camera = self.cameras["farm"]
        for viewer in self.viewers:
            for next_context in (None, ("new-model", "new-targets")):
                with self.subTest(service=type(viewer[0]).__name__, next_context=next_context):
                    current = [("old-model", "old-targets")]
                    camera._preview_context = lambda: current[0]
                    ticket = self.ticket(viewer)
                    def changed_context(boxes=True):
                        packet = camera._preview_sample(boxes)
                        current[0] = next_context
                        return packet
                    camera.preview_sample.side_effect = changed_context
                    response = self.get(viewer, ticket=ticket)
                    self.assertEqual(response.status_code, 503)
                    self.assertNotIn(camera.painted, response.data)
                    # A subsequent read may use its new stable context.
                    camera.preview_sample.side_effect = camera._preview_sample
                    self.assertEqual(self.get(viewer, ticket=ticket).status_code, 200)
        camera.snapshot.assert_not_called()

    def test_stale_annotated_packet_is_rejected_even_when_capture_is_fresh(self):
        camera = self.cameras["farm"]
        camera.last_ts = time.time()
        camera.packet_timestamp = time.time() - FRAME_MAX_AGE - 60
        for viewer in self.viewers:
            with self.subTest(service=type(viewer[0]).__name__):
                response = self.get(viewer, ticket=self.ticket(viewer))
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(camera.painted, response.data)
        self.assertEqual(camera.preview_sample.call_count, 2)

    def test_annotated_packet_from_an_old_source_is_never_forwarded(self):
        camera = self.cameras["farm"]
        camera.packet_source = (("rtsp", "previous-source.mp4"), camera._capture_epoch)
        for viewer in self.viewers:
            with self.subTest(service=type(viewer[0]).__name__):
                response = self.get(viewer, ticket=self.ticket(viewer))
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(camera.painted, response.data)

    def test_annotated_packet_from_a_previous_capture_epoch_is_never_forwarded(self):
        camera = self.cameras["farm"]
        camera.packet_source = (camera.source_identity, camera._capture_epoch - 1)
        for viewer in self.viewers:
            with self.subTest(service=type(viewer[0]).__name__):
                response = self.get(viewer, ticket=self.ticket(viewer))
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(camera.painted, response.data)

    def test_legacy_snapshot_adapter_keeps_raw_preview_support(self):
        raw = self.cameras["farm"].raw
        adapter = SimpleNamespace(id="farm", name="farm", url="farm-fixture.mp4",
                                  enabled=True, state="online", last_ts=time.time(),
                                  snapshot=mock.Mock(return_value=(raw, 3)))
        self.cameras["farm"] = adapter
        for viewer in self.viewers:
            response = self.get(viewer, ticket=self.ticket(viewer))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, raw)
        self.assertEqual(adapter.snapshot.call_count, 2)
