"""Offline message-to-event tests; no private configuration or real transport."""
import base64
import builtins
import datetime
import hashlib
import os
import subprocess
import threading
import time
from pathlib import Path
from unittest import mock

from nvr import ffmpeg
from nvr.clawbot import ClawBot, _pkcs7_size
from tests.support import OfflineTestCase


class EventHub:
    def __init__(self):
        self.active_paths = set()

    def recording_paths(self):
        return set(self.active_paths)


class EventFixture(OfflineTestCase):
    owner = "fixture-owner"
    context = "fixture-context"

    def setUp(self):
        super().setUp()
        self.cfg.set("clawbot", "enabled", True, save=False)
        self.cfg.set("clawbot", "user_id", self.owner, save=False)
        self.cfg.set("clawbot", "allowed_users", ["fixture-helper"], save=False)
        for cid, name, enabled in (("farm", "农场", True), ("coop", "鸡舍", True),
                                    ("disabled", "停用", False)):
            self.cfg.upsert_camera({"id": cid, "name": name,
                                    "url": "fixture.mp4", "enabled": enabled})
        self.hub = EventHub()
        self.bot = ClawBot(self.cfg, hub=self.hub)
        self.today = datetime.date.today().isoformat()
        self.yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        self.texts = mock.patch.object(self.bot, "send_text", return_value={"ret": 0}).start()
        self.addCleanup(mock.patch.stopall)
        self.typing = mock.patch.object(self.bot, "_typing").start()

    def event(self, cid="farm", day=None, name="120000.mp4", size=4096):
        path = self.root / "records" / cid / (day or self.today) / "event" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"offline-event" * (size // 13) + b"x" * (size % 13))
        return path

    def request(self, text, peer=None, ctx=None, **changes):
        message = {"message_type": 1, "from_user_id": peer or self.owner,
                   "context_token": self.context if ctx is None else ctx,
                   "item_list": [{"type": 1, "text_item": {"text": text}}]}
        message.update(changes)
        self.bot.on_message(message)

    def text(self):
        return "\n".join(str(call.args[2]) for call in self.texts.call_args_list)

    def assert_sent_path(self, video, path):
        video.assert_called_once()
        self.assertEqual(os.path.realpath(video.call_args.args[2]),
                         os.path.realpath(path))


class ClawBotEventTests(EventFixture):
    def test_event_list_defaults_to_today_and_does_not_upload(self):
        self.event()
        self.event(day=self.yesterday)
        self.event(cid="disabled")
        with mock.patch.object(self.bot, "send_video") as video:
            self.request("事件")
        video.assert_not_called()
        self.assertIn(self.today, self.text())
        self.assertIn("农场", self.text())
        self.assertEqual(len(self.bot._event_lists[self.owner]["items"]), 1)

    def test_event_route_precedes_camera_list_and_filters_by_day_and_camera(self):
        selected = self.event(cid="coop", day="2020-01-02")
        self.event(day="2020-01-02")
        self.event(cid="coop")
        with mock.patch.object(self.bot, "send_video", return_value=True) as video:
            self.request("事件 2020-01-02 2号摄像头 第1条")
        self.assert_sent_path(video, selected)
        self.assertNotIn("当前摄像头", self.text())

    def test_relative_dates_and_camera_name_select_correct_events(self):
        previous = (datetime.date.today() - datetime.timedelta(days=2)).isoformat()
        for day, word in ((self.today, "今天"), (self.yesterday, "昨天"), (previous, "前天")):
            selected = self.event(cid="coop", day=day)
            self.event(day=day)
            with self.subTest(word=word), \
                 mock.patch.object(self.bot, "send_video", return_value=True) as video:
                self.request(word + "事件 鸡舍 第1段")
            self.assert_sent_path(video, selected)

    def test_invalid_dates_cameras_and_indices_never_fall_back(self):
        self.event()
        commands = ("事件 2026-02-30", "最新事件 2026-13-01", "事件 99号",
                    "事件 0号", "昨天事件 不存在的摄像头", "今天事件 第0条",
                    "今天事件 第99条", "今天事件 第-1条",
                    "最新事件 " + self.today + "1", "最新事件 1" + self.today)
        for command in commands:
            with self.subTest(command=command), \
                 mock.patch.object(self.bot, "send_video") as video, \
                 mock.patch.object(self.bot, "_timelapse") as timelapse:
                self.texts.reset_mock()
                self.request(command)
                video.assert_not_called()
                timelapse.assert_not_called()
                self.assertTrue(self.text())
                self.assertNotIn(str(self.root), self.text())

    def test_latest_and_explicit_index_send_exactly_one_in_time_order(self):
        oldest = self.event(name="080000.mp4")
        middle = self.event(name="090000.mp4")
        newest = self.event(name="100000.mp4")
        os.utime(oldest, (time.time() + 1000, time.time() + 1000))
        for command, selected in (("最新事件 农场", newest),
                                  ("今天事件 农场 第2条", middle)):
            with self.subTest(command=command), \
                 mock.patch.object(self.bot, "send_video", return_value=True) as video:
                self.request(command)
                self.assert_sent_path(video, selected)

    def test_pagination_uses_global_indices_and_stable_snapshot(self):
        paths = [self.event(name="%02d0000.mp4" % hour) for hour in range(1, 13)]
        with mock.patch.object(self.bot, "send_video", return_value=True) as video:
            self.request("今天事件 农场")
            video.assert_not_called()
            original = self.bot._event_lists[self.owner]
            initial_page = original["page"]
            self.request("下一页")
            self.assertEqual(self.bot._event_lists[self.owner]["page"], initial_page + 1)
            self.assertRegex(self.text(), r"11\s*[.、．]")
            self.request("下一页")
            self.assertEqual(self.bot._event_lists[self.owner]["page"], initial_page + 1)
            self.request("上一页")
            self.assertEqual(self.bot._event_lists[self.owner]["page"], initial_page)
            self.request("上一页")
            self.assertEqual(self.bot._event_lists[self.owner]["page"], initial_page)
            self.event(name="235959.mp4")
            self.request("播放第11条")
            self.assert_sent_path(video, paths[1])

    def test_failed_page_delivery_preserves_page_for_retry(self):
        for number in range(21):
            self.event(name="120000_%09d.mp4" % number)
        for failure in ({"ret": 7}, OSError("offline fixture")):
            with self.subTest(failure=type(failure).__name__):
                self.request("今天事件 农场")
                snapshot = self.bot._event_lists[self.owner]
                if isinstance(failure, Exception):
                    with mock.patch.object(self.bot, "send_text", side_effect=failure), \
                         self.assertRaises(OSError):
                        self.request("下一页")
                else:
                    with mock.patch.object(self.bot, "send_text", return_value=failure):
                        self.request("下一页")
                self.assertEqual(snapshot["page"], 0)
                self.texts.reset_mock()
                self.request("下一页")
                self.assertEqual(snapshot["page"], 1)
                self.assertIn("第2/3页", self.text())
                self.assertRegex(self.text(), r"11\s*[.、．]")
                with mock.patch.object(self.bot, "send_text", return_value={"ret": 7}):
                    self.request("上一页")
                self.assertEqual(snapshot["page"], 1)
                self.texts.reset_mock()
                self.request("上一页")
                self.assertEqual(snapshot["page"], 0)
                self.assertIn("第1/3页", self.text())

    def test_cached_lists_are_isolated_per_authorized_user(self):
        farm = self.event()
        coop = self.event(cid="coop")
        self.request("今天事件 农场")
        self.request("今天事件 鸡舍", peer="fixture-helper")
        for peer, selected in ((self.owner, farm), ("fixture-helper", coop)):
            with self.subTest(peer=peer), \
                 mock.patch.object(self.bot, "send_video", return_value=True) as video:
                self.request("播放第1段", peer=peer)
                self.assert_sent_path(video, selected)
                self.assertEqual(video.call_args.args[:2], (peer, self.context))

    def test_chinese_playback_and_event_suffix_use_the_same_selected_snapshot(self):
        selected = self.event(cid="coop", day=self.yesterday)
        self.event(cid="coop")
        self.event()
        self.event(day=self.yesterday)
        commands = ("播放第一条", "播放第一段", "播放第一条事件", "播放第一段事件录像",
                    "播放第1条事件", "播放第1段事件录像", "播放 第 一 条 事件录像")
        for command in commands:
            with self.subTest(command=command), \
                 mock.patch.object(self.bot, "send_video", return_value=True) as video:
                self.request("昨天事件 鸡舍")
                snapshot = self.bot._event_lists[self.owner]
                self.request(command)
                self.assert_sent_path(video, selected)
                self.assertIs(self.bot._event_lists[self.owner], snapshot)
                self.assertEqual(snapshot["day"], self.yesterday)
                self.assertEqual(snapshot["camera"], "鸡舍")

    def test_canonical_chinese_ordinals_cover_event_list_boundaries(self):
        cases = {"一": 1, "九": 9, "十": 10, "十一": 11, "二十": 20, "二十一": 21,
                 "九十九": 99, "一百": 100, "一百零一": 101, "一百一十": 110,
                 "一百一十一": 111, "一百九十九": 199, "二百": 200, "二百零五": 205,
                 "二百一十": 210, "四百九十九": 499, "五百": 500,
                 "五百零一": 501, "九百九十九": 999}
        for token, expected in cases.items():
            with self.subTest(token=token):
                self.assertEqual(self.bot._event_ordinal(token), expected)

    def test_invalid_playback_ordinals_never_query_or_send_another_event(self):
        self.event(cid="coop", day=self.yesterday)
        self.event()
        self.request("昨天事件 鸡舍")
        snapshot = self.bot._event_lists[self.owner]
        commands = ("播放第零条", "播放第0条事件", "播放第-1段事件录像", "播放第二条",
                    "播放第999条事件", "播放第五百零一条", "播放第十一一条", "播放第一百一条",
                    "播放第一百零十条事件", "播放第十百段事件录像", "播放第1条第2条事件",
                    "播放第一条第二条", "播放第一条 第2条事件", "播放第1条和第一条事件")
        with mock.patch("nvr.clawbot.list_events") as query, \
             mock.patch.object(self.bot, "_timelapse") as daily, \
             mock.patch.object(self.bot, "send_video") as video:
            for command in commands:
                with self.subTest(command=command):
                    self.texts.reset_mock()
                    self.request(command)
                    self.assertTrue(self.text())
                    self.assertIs(self.bot._event_lists[self.owner], snapshot)
            query.assert_not_called()
            daily.assert_not_called()
            video.assert_not_called()

    def test_chinese_playback_is_isolated_and_requires_authorization_and_context(self):
        farm = self.event(day=self.yesterday)
        coop = self.event(cid="coop")
        self.request("昨天事件 农场")
        self.request("今天事件 鸡舍", peer="fixture-helper")
        snapshots = dict(self.bot._event_lists)
        for peer, selected in ((self.owner, farm), ("fixture-helper", coop)):
            with self.subTest(peer=peer), \
                 mock.patch.object(self.bot, "send_video", return_value=True) as video:
                self.request("播放第一条事件录像", peer=peer)
                self.assert_sent_path(video, selected)
                self.assertEqual(video.call_args.args[:2], (peer, self.context))
        for changes in ({"peer": "stranger"}, {"ctx": ""}, {"message_type": 2}):
            with self.subTest(changes=changes), \
                 mock.patch.object(self.bot, "send_video") as video:
                self.texts.reset_mock()
                self.request("播放第一条", **changes)
                self.texts.assert_not_called()
                video.assert_not_called()
                self.assertEqual(self.bot._event_lists, snapshots)

    def test_chinese_playback_with_missing_or_expired_snapshot_never_requeries(self):
        self.event()
        # Another user's current list must not fill in a missing owner list.
        self.request("今天事件", peer="fixture-helper")
        with mock.patch("nvr.clawbot.list_events") as query, \
             mock.patch.object(self.bot, "send_video") as video:
            for command in ("播放第一条", "播放第1条事件", "播放第一段事件录像"):
                self.request(command)
            self.assertNotIn(self.owner, self.bot._event_lists)
            query.assert_not_called()
            video.assert_not_called()
        with mock.patch("nvr.clawbot.time.time", return_value=10000):
            self.request("今天事件")
        with mock.patch("nvr.clawbot.time.time", return_value=10901), \
             mock.patch("nvr.clawbot.list_events") as query, \
             mock.patch.object(self.bot, "send_video") as video:
            for command in ("播放第一条", "播放第1条事件", "播放第一段事件录像"):
                self.request(command)
            query.assert_not_called()
            video.assert_not_called()
        self.assertIn("列表已过期", self.text())

    def test_chinese_ordinal_camera_names_are_preserved_in_queries(self):
        self.cfg.upsert_camera({"id": "named", "name": "第一条仓库", "url": "fixture.mp4"})
        selected = self.event(cid="named", day=self.yesterday)
        self.event(day=self.yesterday)
        with mock.patch.object(self.bot, "send_video", return_value=True) as video:
            self.request("昨天事件 第一条仓库 第1条")
            self.assert_sent_path(video, selected)
        with mock.patch.object(self.bot, "send_video", return_value=True) as video:
            self.request("播放第一条")
            self.assert_sent_path(video, selected)
        with mock.patch.object(self.bot, "_timelapse", return_value=str(selected)) as daily, \
             mock.patch.object(self.bot, "send_video", return_value=True):
            self.request("昨天 第一条仓库")
            daily.assert_called_once_with("named", self.yesterday)

    def test_missing_and_expired_snapshot_never_send(self):
        self.event()
        with mock.patch.object(self.bot, "send_video") as video:
            self.request("播放第1条")
            self.request("下一页")
            video.assert_not_called()
            with mock.patch("nvr.clawbot.time.time", return_value=10000):
                self.request("今天事件")
            with mock.patch("nvr.clawbot.time.time", return_value=10901):
                self.request("播放第1条")
                self.request("下一页")
            video.assert_not_called()
        self.assertTrue(self.text())

    def test_event_authorization_and_context_guards_do_not_create_lists(self):
        self.event()
        cases = (({"peer": "stranger"}, True), ({"ctx": ""}, True),
                 ({"message_type": 2}, True), ({}, False))
        for changes, enabled in cases:
            with self.subTest(changes=changes, enabled=enabled), \
                 mock.patch.object(self.bot, "send_video") as video:
                self.cfg.set("clawbot", "enabled", enabled, save=False)
                self.typing.reset_mock()
                self.texts.reset_mock()
                self.request("最新事件", **changes)
                self.typing.assert_not_called()
                self.texts.assert_not_called()
                video.assert_not_called()
                self.assertFalse(self.bot._event_lists)

    def test_active_remuxing_temporary_empty_and_invalid_files_are_excluded(self):
        completed = self.event(name="080000.mp4")
        active = self.event(name="090000.mp4")
        remuxing = self.event(name="100000.mp4")
        self.event(name="110000.fs.mp4")
        self.event(name="120000.part.mp4")
        self.event(name="130000.mp4", size=0)
        self.event(name="240000.mp4")
        self.event(name="not-an-event.mp4")
        self.event(cid="disabled", name="230000.mp4")
        self.hub.active_paths.add(str(active))
        with mock.patch("nvr.camera.remuxing_paths", return_value={str(remuxing)}), \
             mock.patch.object(self.bot, "send_video", return_value=True) as video:
            self.request("最新事件")
            self.assert_sent_path(video, completed)

    def test_snapshot_revalidation_rejects_new_active_remuxing_and_changed_files(self):
        for state in ("active", "remuxing", "changed", "deleted"):
            with self.subTest(state=state):
                selected = self.event()
                self.hub.active_paths.clear()
                self.request("今天事件 农场")
                remuxing = set()
                if state == "active":
                    self.hub.active_paths.add(str(selected))
                elif state == "remuxing":
                    remuxing.add(str(selected))
                elif state == "changed":
                    selected.write_bytes(b"replaced" * 1000)
                else:
                    selected.unlink()
                with mock.patch("nvr.camera.remuxing_paths", return_value=remuxing), \
                     mock.patch.object(self.bot, "send_video") as video:
                    self.request("播放第1条")
                    video.assert_not_called()
                self.assertNotIn(str(self.root), self.text())

    def test_unknown_recording_state_does_not_make_files_sendable(self):
        self.event()
        with mock.patch.object(self.hub, "recording_paths", side_effect=RuntimeError("busy")), \
             mock.patch.object(self.bot, "send_video") as video:
            self.request("最新事件")
            video.assert_not_called()

    def test_recording_started_during_scan_is_excluded(self):
        from nvr import recordings
        completed = self.event(name="080000.mp4")
        active = self.event(name="090000.mp4")
        original_stat = recordings._event_stat

        def begin_recording(*args, **kwargs):
            result = original_stat(*args, **kwargs)
            self.hub.active_paths.add(str(active))
            return result

        with mock.patch("nvr.recordings._event_stat", side_effect=begin_recording), \
             mock.patch.object(self.bot, "send_video", return_value=True) as video:
            self.request("最新事件")
        self.assert_sent_path(video, completed)

    def test_hub_handoff_keeps_path_busy_after_recorder_process_is_cleared(self):
        from nvr.hub import Hub
        from types import SimpleNamespace
        selected = str(self.event())
        hub = Hub.__new__(Hub)
        hub.lock = threading.RLock()
        hub.cams = {"farm": SimpleNamespace(rec_proc=None, rec_path=selected)}
        with mock.patch("nvr.camera.remuxing_paths", return_value=set()):
            self.assertIn(selected, hub.recording_paths())
        remuxing = set()

        class HandoffCamera:
            @property
            def rec_path(self):
                # Deterministically move between the two busy-state owners
                # while Hub reads camera state; remux must be read afterward.
                remuxing.add(selected)
                return ""

        hub.cams = {"farm": HandoffCamera()}
        with mock.patch("nvr.camera.remuxing_paths", side_effect=lambda: set(remuxing)):
            self.assertIn(selected, hub.recording_paths())

    def test_hub_excludes_event_until_recorder_launch_finishes(self):
        self.cfg.set("record", "target_boxes", False, save=False)
        from nvr.camera import Cam
        from nvr.hub import Hub
        cam = Cam(self.cfg.get_camera("farm"), self.cfg)
        hub = Hub.__new__(Hub)
        hub.lock = threading.RLock()
        hub.cams = {"farm": cam}
        self.bot.hub = hub
        process = mock.Mock()
        process.poll.return_value = None

        def launch(cmd, **kwargs):
            # A child may write while its parent is still inside popen.
            selected = Path(cmd[-1])
            selected.write_bytes(b"synthetic-in-progress" * 2048)
            self.assertIn(str(selected), hub.recording_paths())
            self.request("最新事件 农场")
            return process

        with mock.patch("nvr.camera.popen", side_effect=launch), \
             mock.patch.object(self.bot, "send_video") as video:
            cam.start_record()
            video.assert_not_called()
        self.assertIs(cam.rec_proc, process)
        self.assertIn(cam.rec_path, hub.recording_paths())

    def test_recorder_launch_failure_clears_busy_state(self):
        self.cfg.set("record", "target_boxes", False, save=False)
        from nvr.camera import Cam
        from nvr.hub import Hub
        cam = Cam(self.cfg.get_camera("farm"), self.cfg)
        hub = Hub.__new__(Hub)
        hub.lock = threading.RLock()
        hub.cams = {"farm": cam}
        observed = []

        def fail_launch(cmd, **kwargs):
            observed.append((cmd[-1], hub.recording_paths()))
            raise OSError("Synthetic recorder launch failure")

        with mock.patch("nvr.camera.popen", side_effect=fail_launch) as launch:
            cam.start_record()
        launch.assert_called_once()
        self.assertIn(observed[0][0], observed[0][1])
        self.assertIsNone(cam.rec_proc)
        self.assertEqual(cam.rec_path, "")
        self.assertEqual(cam.rec_t0, 0.0)
        self.assertFalse(hub.recording_paths())

    def test_recording_directory_symlink_cannot_escape_root(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "235959.mp4").write_bytes(b"outside" * 1024)
        target = self.root / "records" / "farm" / self.today / "event"
        target.parent.mkdir(parents=True)
        try:
            target.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if os.name != "nt":
                self.skipTest("This account cannot create directory links: %s" % exc)
            # NTFS junctions exercise realpath containment without the Windows
            # administrator/developer privilege required for symbolic links.
            env = dict(os.environ, MININVR_TEST_LINK=str(target),
                       MININVR_TEST_TARGET=str(outside))
            result = subprocess.run([
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "New-Item -ItemType Junction -Path $env:MININVR_TEST_LINK "
                "-Target $env:MININVR_TEST_TARGET -ErrorAction Stop | Out-Null",
            ], env=env, capture_output=True, timeout=10)
            if result.returncode:
                self.skipTest("This Windows sandbox denies both symlink and NTFS junction creation")
        with mock.patch.object(self.bot, "send_video") as video:
            self.request("最新事件 农场")
            video.assert_not_called()
        self.assertNotIn(str(outside), self.text())

    def test_cached_path_cannot_redirect_send_outside_records(self):
        selected = self.event()
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"outside" * 1024)
        self.request("今天事件")
        self.bot._event_lists[self.owner]["items"][0]["path"] = str(outside)
        with mock.patch.object(self.bot, "send_video", return_value=True) as video:
            self.request("播放第1条")
            for call in video.call_args_list:
                self.assertEqual(os.path.realpath(call.args[2]), os.path.realpath(selected))
        self.assertNotIn(str(outside), self.text())

    def test_cached_identity_fields_cannot_traverse_recording_root(self):
        self.event()
        for field, value in (("cam", "../outside"), ("date", "../../outside"),
                             ("name", "../../outside.mp4")):
            with self.subTest(field=field):
                self.request("今天事件")
                self.bot._event_lists[self.owner]["items"][0][field] = value
                with mock.patch.object(self.bot, "send_video") as video:
                    self.request("播放第1条")
                    video.assert_not_called()

    def test_event_snapshot_is_bounded_and_preserves_newest_rows(self):
        for number in range(501):
            self.event(name="120000_%d.mp4" % number, size=1024)
        self.request("今天事件 农场")
        snapshot = self.bot._event_lists[self.owner]
        self.assertEqual(len(snapshot["items"]), 500)
        self.assertTrue(snapshot["truncated"])
        self.assertEqual(snapshot["items"][0]["name"], "120000_500.mp4")
        self.assertNotIn("120000_0.mp4", [row["name"] for row in snapshot["items"]])
        with mock.patch.object(self.bot, "send_video", return_value=True) as video:
            self.request("播放第五百条事件录像")
            self.assert_sent_path(video, self.root / "records" / "farm" / self.today /
                                  "event" / "120000_1.mp4")

    def test_peer_snapshot_cache_is_bounded(self):
        self.event()
        peers = ["fixture-peer-%02d" % number for number in range(70)]
        self.cfg.set("clawbot", "allowed_users", peers, save=False)
        with mock.patch("nvr.clawbot.time.time", side_effect=range(10000, 20000)):
            for peer in peers:
                self.request("今天事件", peer=peer)
        self.assertLessEqual(len(self.bot._event_lists), 64)
        self.assertIn(peers[-1], self.bot._event_lists)

    def test_missing_events_do_not_trigger_timelapse_or_upload(self):
        self.event(day=self.yesterday)
        for command in ("今天事件", "最新事件", "今天事件 第1条"):
            with self.subTest(command=command), \
                 mock.patch.object(self.bot, "_post") as post, \
                 mock.patch.object(self.bot, "_timelapse") as timelapse:
                self.request(command)
                post.assert_not_called()
                timelapse.assert_not_called()
        self.assertIn("已完成", self.text())

    def test_oversize_video_is_rejected_before_file_read_or_upload(self):
        selected = self.event(size=1024 * 1024 + 1)
        self.cfg.set("clawbot", "max_video_mb", 1, save=False)
        original_open = builtins.open

        def guarded_open(path, *args, **kwargs):
            if os.path.realpath(path) == os.path.realpath(selected):
                self.fail("Oversize video was opened before its size was rejected")
            return original_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=guarded_open), \
             mock.patch.object(self.bot, "_post") as post:
            self.request("最新事件 农场")
            post.assert_not_called()
        self.assertIn("上限", self.text())

    def test_legacy_timelapse_command_remains_available(self):
        fixture = self.root / "daily.mp4"
        fixture.write_bytes(b"daily" * 1024)
        for command, cid in (("昨天 农场", "farm"), ("昨天 第2个", "coop"),
                             ("昨天 第2", "coop"), ("昨天 2个", "coop")):
            with self.subTest(command=command), \
                 mock.patch.object(self.bot, "_timelapse", return_value=str(fixture)) as daily, \
                 mock.patch.object(self.bot, "send_video", return_value=True) as video:
                self.request(command)
                daily.assert_called_once_with(cid, self.yesterday)
                self.assert_sent_path(video, fixture)
        for token in ("2026-10-18", "第2条", "第2段"):
            with self.subTest(token=token):
                self.assertIsNone(self.bot._parse_cam(token))


class ClawBotEventMediaTests(EventFixture):
    """A real MP4 travels through the actual encryption and message builder."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.media_tmp = tempfile.TemporaryDirectory(prefix="mininvr-clawbot-media-")
        cls.addClassCleanup(cls.media_tmp.cleanup)
        cls.source = Path(cls.media_tmp.name) / "synthetic.mp4"
        result = subprocess.run([
            ffmpeg.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10", "-t", "2.4",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(cls.source),
        ], capture_output=True, timeout=20)
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", "replace"))

    def real_event(self):
        target = self.event()
        target.write_bytes(self.source.read_bytes())
        return target

    def transport(self, upload_result=None, send_result=None):
        seen = {}

        def post(endpoint, body, **kwargs):
            seen[endpoint] = body
            if endpoint == "getuploadurl":
                return {"upload_param": "OFFLINE-UPLOAD"} if upload_result is None else upload_result
            if endpoint == "sendmessage":
                return {"ret": 0} if send_result is None else send_result
            raise AssertionError("Unexpected protocol endpoint: " + endpoint)

        response = mock.MagicMock()
        response.__enter__.return_value.headers = {"x-encrypted-param": "OFFLINE-RECEIPT"}
        return seen, post, response

    def test_completed_annotated_event_query_upload_and_playback_share_exact_mp4(self):
        import cv2
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from nvr.camera import Cam
        from nvr.event_overlay import Detection, FramePacket
        from nvr.server import create_app
        from tests.test_api import FakeHub
        from tests.test_event_overlay import colored_pixels, image_packet

        camera = Cam(self.cfg.get_camera("farm"), self.cfg)
        self.addCleanup(camera.stop)
        packet = image_packet(detection=Detection(((70, 20, 80, 130),), "haar", (320, 180)))
        camera.state, camera.last_jpeg, camera.seq, camera.last_ts = "online", packet.jpeg, 1, packet.timestamp
        _, paired = camera.frame_sample()
        paired = FramePacket(paired.jpeg, paired.seq, paired.timestamp, paired.source, packet.detection)
        camera.start_record(paired)
        part = Path(camera.rec_path)
        self.wait_until(lambda: camera._jpeg_recorder.frames >= 5)
        camera.stop_record()
        completed = part.with_name(part.name.replace(".part.mp4", ".mp4"))
        original = completed.read_bytes()
        client = create_app(self.cfg, FakeHub()).test_client()
        listing = client.get("/api/recordings?type=event").get_json()
        self.assertEqual(listing["count"], 1)
        playback = client.get("/api/rec/farm/%s/event/%s" % (self.today, completed.name))
        self.assertEqual(playback.data, original)
        playback.close()
        seen, post, response = self.transport()
        with mock.patch.object(self.bot, "_post", side_effect=post), \
             mock.patch("urllib.request.urlopen", return_value=response) as upload:
            self.request("今天事件")
            upload.assert_not_called()
            self.request("播放第一条")
        upload.assert_called_once()
        encrypted = upload.call_args.args[0].data
        decryptor = Cipher(algorithms.AES(bytes.fromhex(seen["getuploadurl"]["aeskey"])), modes.ECB()).decryptor()
        padded = decryptor.update(encrypted) + decryptor.finalize()
        restored = padded[:-padded[-1]]
        self.assertEqual(restored, original)
        recovered = self.root / "annotated-round-trip.mp4"
        recovered.write_bytes(restored)
        video = cv2.VideoCapture(str(recovered))
        try:
            ok, frame = video.read()
            self.assertTrue(ok)
            self.assertTrue(colored_pixels(frame, "#F51616")[18:24, 80:140].any())
        finally:
            video.release()

    def test_real_event_message_upload_decrypts_to_original_mp4(self):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        selected = self.real_event()
        raw = selected.read_bytes()
        seen, post, response = self.transport()
        with mock.patch.object(self.bot, "_post", side_effect=post), \
             mock.patch("urllib.request.urlopen", return_value=response) as upload:
            self.request("最新事件 农场")
        upload.assert_called_once()
        request = upload.call_args.args[0]
        claim = seen["getuploadurl"]
        self.assertEqual(claim["rawsize"], len(raw))
        self.assertEqual(claim["rawfilemd5"], hashlib.md5(raw).hexdigest())
        self.assertEqual(len(request.data), _pkcs7_size(len(raw)))
        self.assertEqual(claim["filesize"], len(request.data))
        key = bytes.fromhex(claim["aeskey"])
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        padded = decryptor.update(request.data) + decryptor.finalize()
        restored = padded[:-padded[-1]]
        self.assertEqual(restored, raw)
        recovered = self.root / "recovered.mp4"
        recovered.write_bytes(restored)
        self.assertEqual(ffmpeg.probe(str(recovered))[:2], (320, 180))
        message = seen["sendmessage"]["msg"]
        self.assertEqual(message["context_token"], self.context)
        self.assertEqual(message["to_user_id"], self.owner)
        self.assertEqual(message["item_list"][0]["type"], 5)
        video = message["item_list"][0]["video_item"]
        self.assertEqual(video["video_size"], len(raw))
        self.assertEqual(video["video_md5"], hashlib.md5(raw).hexdigest())
        self.assertEqual(base64.b64decode(video["media"]["aes_key"]).decode("ascii"),
                         claim["aeskey"])
        self.assertEqual(video["media"]["encrypt_query_param"], "OFFLINE-RECEIPT")
        self.assertGreaterEqual(video["play_length"], 2)
        self.assertLessEqual(video["play_length"], 3)
        self.assertEqual(self.typing.call_args_list[-1].args, (self.owner, False))

    def test_upload_service_rejection_never_uploads_or_claims_success(self):
        self.real_event()
        for rejected in ({"ret": 7, "errmsg": "fixture rejected", "upload_param": "UNTRUSTED"},
                         {"errcode": 9, "errmsg": "fixture rejected"}, {}):
            with self.subTest(rejected=rejected):
                seen, post, response = self.transport(upload_result=rejected)
                with mock.patch.object(self.bot, "_post", side_effect=post), \
                     mock.patch("urllib.request.urlopen", return_value=response) as upload:
                    self.request("最新事件 农场")
                upload.assert_not_called()
                self.assertNotIn("sendmessage", seen)

    def test_upload_receipt_and_sendmessage_rejections_are_failures(self):
        selected = self.real_event()
        for receipt, result in (("", {"ret": 0}),
                                ("OFFLINE-RECEIPT", {"ret": 3, "errmsg": "fixture rejected"}),
                                ("OFFLINE-RECEIPT", {"ret": 0, "errcode": 12}),
                                ("OFFLINE-RECEIPT", {"ret": None}),
                                ("OFFLINE-RECEIPT", {"ret": False}),
                                ("OFFLINE-RECEIPT", {"errcode": None})):
            with self.subTest(receipt=receipt, result=result):
                _, post, response = self.transport(send_result=result)
                response.__enter__.return_value.headers = {"x-encrypted-param": receipt}
                with mock.patch.object(self.bot, "_post", side_effect=post), \
                     mock.patch("urllib.request.urlopen", return_value=response):
                    self.assertFalse(self.bot.send_video(self.owner, self.context, str(selected)))

    def test_event_changed_between_validation_and_open_is_not_read_or_uploaded(self):
        selected = self.real_event()
        self.request("今天事件 农场")
        event = self.bot._event_lists[self.owner]["items"][0]
        # Simulate a directory replacement after the caller's validation.
        with mock.patch("nvr.clawbot.validate_event", return_value=""), \
             mock.patch.object(self.bot, "_post") as post, \
             mock.patch("urllib.request.urlopen") as upload:
            self.assertFalse(self.bot.send_video(self.owner, self.context, str(selected), event=event))
        post.assert_not_called()
        upload.assert_not_called()
