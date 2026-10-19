"""Offline ownership, readiness and failure checks for the NetBird controller."""
import base64
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
from types import SimpleNamespace
from unittest import mock

from nvr import remote_preview
from tests.support import OfflineTestCase


class _Pipe:
    def __init__(self):
        self.items = queue.Queue()
        self.closed = False

    def readline(self, limit):
        item = self.items.get(timeout=5)
        return item[:limit]

    def write_line(self, value):
        self.items.put(value + "\n")

    def close(self):
        self.closed = True


class _Process:
    def __init__(self, events):
        self.stdout = _Pipe()
        self.events = events
        self.returncode = None
        self.terminated = self.killed = 0

    def poll(self):
        return self.returncode

    def exit(self, code=1):
        self.returncode = code
        self.stdout.items.put("")

    def terminate(self):
        self.events.append("terminate")
        self.terminated += 1
        self.exit(0)

    def kill(self):
        self.events.append("kill")
        self.killed += 1
        self.exit(-1)

    def wait(self, timeout):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fixture", timeout)
        return self.returncode


class _Viewer:
    def __init__(self, events):
        self.events = events
        self.origin = self.url = ""
        self.links = []

    def start_origin(self):
        self.events.append("origin")
        self.origin = "127.0.0.1"
        return 18803

    def set_public_url(self, url):
        self.events.append("ready")
        self.url = url

    def revoke(self):
        self.events.append("revoke")
        self.links.clear()

    def stop(self):
        self.events.append("close")
        self.origin = self.url = ""

    def make_link(self, cid):
        self.links.append(cid)
        return self.url + "/preview#t=fixture-only"

    def status(self):
        return {"running": bool(self.origin)}


class _Bridge:
    def __init__(self, events, address, origin_port):
        self.events, self.address, self.origin_port = events, address, origin_port
        self.running = False

    def start(self):
        self.events.append("bridge_start")
        self.running = True
        return 18804

    def stop(self):
        self.events.append("bridge_stop")
        self.running = False

    def status(self):
        return self.running


class RemotePreviewControllerTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.cfg.set("remote_preview", "enabled", True, save=False)
        self.events, self.logs, self.processes = [], [], []
        self.viewer = _Viewer(self.events)
        self.find = self.patch("_find_client", return_value="C:/Program Files/NetBird/netbird.exe")
        self.verify = self.patch("_verify_client", return_value=True)
        self.query = self.patch("_query_address", return_value="100.100.1.2")
        self.launch = self.patch("_launch_client", side_effect=self.launch_process)
        self.patch("RemotePreviewService", return_value=self.viewer)
        self.bridges = []
        patcher = mock.patch("nvr.remote_bridge.PreviewBridge", side_effect=self.make_bridge)
        self.bridge_factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.controller = remote_preview.RemotePreviewController(self.cfg, lambda cid: None, self.logs.append)
        self.addCleanup(self.controller.stop)

    def patch(self, name, **kwargs):
        patcher = mock.patch.object(remote_preview, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def launch_process(self, path, port):
        self.events.append("launch")
        process = _Process(self.events)
        self.processes.append(process)
        return process

    def make_bridge(self, address, origin_port):
        bridge = _Bridge(self.events, address, origin_port)
        self.bridges.append(bridge)
        return bridge

    def start_process(self):
        self.controller.start()
        self.wait_until(lambda: bool(self.processes), description="fake expose process")
        return self.processes[-1]

    def ready(self):
        process = self.start_process()
        process.stdout.write_line("  URL:      https://mininvr-fixture.example.com")
        self.wait_until(lambda: self.controller.status()["state"] == "ready", description="remote ready")
        return process

    def wait_error(self):
        self.wait_until(lambda: self.controller.status()["state"] == "error", description="remote error")
        return self.controller.status()

    def test_missing_client_never_starts_origin_or_process(self):
        self.find.return_value = None
        self.controller.start()
        result = self.wait_error()
        self.assertFalse(result["installed"])
        self.assertIn("尚未安装", result["error"])
        self.launch.assert_not_called()
        self.assertNotIn("origin", self.events)

    def test_unsigned_or_wrong_publisher_never_executes(self):
        self.verify.return_value = False
        self.controller.start()
        self.assertIn("数字签名", self.wait_error()["error"])
        self.launch.assert_not_called()
        self.assertNotIn("origin", self.events)

    def test_ready_requires_exact_url_line_and_clean_https_origin(self):
        process = self.start_process()
        process.stdout.write_line("Account fixture-secret https://wrong.example.com")
        self.assertEqual(self.controller.status()["state"], "starting")
        process.stdout.write_line("  URL: https://mininvr-fixture.example.com")
        self.wait_until(lambda: self.controller.status()["running"], description="HTTPS readiness")
        result = self.controller.status()
        self.assertEqual(set(result), {"state", "running", "url", "error", "installed"})
        self.assertEqual(result["url"], "https://mininvr-fixture.example.com")
        self.assertEqual(self.controller.make_link("farm"), result["url"] + "/preview#t=fixture-only")
        self.assertNotIn("fixture-secret", "\n".join(self.logs))
        self.assertNotIn(result["url"], "\n".join(self.logs))

    def test_malformed_url_closes_origin_and_only_owned_child(self):
        for url in ("http://example.com", "https://u:secret@example.com", "https://example.com/a",
                    "https://example.com#t=secret", "https://127.0.0.1", "https://example.com more"):
            with self.subTest(url=url):
                process = self.start_process()
                process.stdout.write_line("URL: " + url)
                result = self.wait_error()
                self.assertEqual(result["url"], "")
                self.assertFalse(result["running"])
                self.assertEqual(self.viewer.origin, "")
                self.assertEqual(process.terminated, 1)
                self.assertNotIn("secret", "\n".join(self.logs))
                with self.assertRaises(ValueError):
                    self.controller.make_link("farm")
                self.controller.stop()
                self.processes.clear()

    def test_timeout_is_finite_and_revokes_before_process_termination(self):
        with mock.patch.object(remote_preview, "STARTUP_SECONDS", 0.08):
            process = self.start_process()
            self.assertIn("超时", self.wait_error()["error"])
        self.assertEqual(process.terminated, 1)
        self.assertLess(self.events.index("bridge_stop"), self.events.index("revoke"))
        self.assertLess(self.events.index("revoke"), self.events.index("close"))
        self.assertLess(self.events.index("close"), self.events.index("terminate"))
        self.assertEqual(self.viewer.origin, "")

    def test_exited_client_reports_only_constant_safe_permission_hint(self):
        process = self.start_process()
        process.stdout.write_line("rpc error: PermissionDenied fixture-secret-owner secret-token")
        process.exit()
        result = self.wait_error()
        self.assertIn("Peer Expose", result["error"])
        self.assertNotIn("fixture-secret", str(result) + "\n".join(self.logs))
        self.assertEqual(self.viewer.origin, "")

    def test_client_exit_after_readiness_revokes_and_rejects_new_link(self):
        process = self.ready()
        self.controller.make_link("farm")
        process.exit()
        self.wait_error()
        self.assertEqual(self.viewer.links, [])
        self.assertEqual(self.viewer.origin, "")
        with self.assertRaises(ValueError):
            self.controller.make_link("farm")

    def test_dead_origin_while_child_alive_revokes_and_stops_child(self):
        process = self.ready()
        self.controller.make_link("farm")
        self.viewer.origin = ""
        result = self.controller.status()
        self.assertEqual(result["state"], "error")
        self.assertFalse(result["running"])
        self.assertEqual(result["url"], "")
        self.assertEqual(process.terminated, 1)
        self.assertEqual(self.viewer.links, [])

    def test_dedicated_adapter_bridge_failure_revokes_and_stops_child(self):
        process = self.ready()
        self.assertEqual(self.bridges[-1].address, "100.100.1.2")
        self.assertEqual(self.bridges[-1].origin_port, 18803)
        self.launch.assert_called_once_with("C:/Program Files/NetBird/netbird.exe", 18804)
        self.bridges[-1].running = False
        self.assertFalse(self.controller.status()["running"])
        self.assertEqual(self.viewer.origin, "")
        self.assertEqual(process.terminated, 1)

    def test_disconnected_or_invalid_local_peer_address_never_opens_origin(self):
        self.query.side_effect = ValueError("请先在电脑上的 NetBird 登录并连接")
        self.controller.start()
        self.assertIn("登录并连接", self.wait_error()["error"])
        self.bridge_factory.assert_not_called()
        self.launch.assert_not_called()
        self.assertNotIn("origin", self.events)

    def test_make_link_detects_dead_child_before_worker_poll(self):
        process = self.ready()
        process.returncode = 9
        with self.assertRaisesRegex(ValueError, "中断"):
            self.controller.make_link("farm")
        self.assertEqual(self.viewer.origin, "")

    def test_output_is_bounded_and_never_echoed(self):
        process = self.start_process()
        process.stdout.write_line("fixture-secret" * 400)
        self.assertIn("状态异常", self.wait_error()["error"])
        self.assertEqual(process.terminated, 1)
        self.assertNotIn("fixture-secret", "\n".join(self.logs))

    def test_stop_is_idempotent_and_start_does_not_duplicate_child(self):
        process = self.ready()
        self.controller.start()
        self.controller.start()
        self.launch.assert_called_once()
        self.controller.stop()
        self.controller.stop()
        self.assertEqual(process.terminated, 1)
        self.assertEqual(self.controller.status()["state"], "stopped")
        self.assertEqual(self.controller.status()["url"], "")
        self.assertTrue(process.stdout.closed)
        self.processes.clear()
        fresh = self.ready()
        self.assertIsNot(fresh, process)

    def test_config_disable_revokes_and_never_starts_automatically(self):
        self.assertEqual(self.controller.status()["state"], "stopped")
        self.launch.assert_not_called()
        process = self.ready()
        self.cfg.set("remote_preview", "enabled", False, save=False)
        with self.assertRaisesRegex(ValueError, "停用"):
            self.controller.make_link("farm")
        self.assertEqual(process.terminated, 1)
        with self.assertRaisesRegex(ValueError, "启用"):
            self.controller.start()

    def test_stop_during_signature_check_cannot_reopen_origin(self):
        entered, release = threading.Event(), threading.Event()
        def verify(path):
            entered.set()
            release.wait(3)
            return True
        self.verify.side_effect = verify
        self.controller.start()
        self.assertTrue(entered.wait(1))
        worker = self.controller._worker
        self.controller.stop()
        release.set()
        worker.join(2)
        self.launch.assert_not_called()
        self.assertNotIn("origin", self.events)
        self.assertEqual(self.controller.status()["state"], "stopped")

    def test_stale_signature_failure_cannot_close_new_session(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def verify(path):
            calls.append(path)
            if len(calls) == 1:
                entered.set()
                release.wait(4)
                return False
            return True
        self.verify.side_effect = verify
        self.controller.start()
        self.assertTrue(entered.wait(1))
        old_worker = self.controller._worker
        self.controller.stop()
        process = self.ready()
        release.set()
        old_worker.join(2)
        self.assertTrue(self.controller.status()["running"])
        self.assertEqual(process.terminated, 0)
        self.assertEqual(self.viewer.origin, "127.0.0.1")

    def test_changed_url_closes_old_capabilities_instead_of_switching(self):
        process = self.ready()
        self.controller.make_link("farm")
        process.stdout.write_line("URL: https://different.example.com")
        self.wait_error()
        self.assertEqual(self.viewer.links, [])
        self.assertEqual(process.terminated, 1)

    def test_spawn_failure_closes_origin(self):
        self.launch.side_effect = OSError("fixture-secret")
        self.controller.start()
        result = self.wait_error()
        self.assertEqual(self.viewer.origin, "")
        self.assertNotIn("fixture-secret", str(result) + "\n".join(self.logs))


class TrustedNetBirdLaunchTests(OfflineTestCase):
    def test_discovery_only_uses_program_files_not_working_directory_or_path(self):
        official_root = self.root / "Program Files"
        directory = official_root / "NetBird"
        directory.mkdir(parents=True)
        official = directory / "netbird.exe"
        arbitrary = self.root / "netbird.exe"
        arbitrary.write_bytes(b"untrusted")
        with mock.patch.object(remote_preview, "_IS_WINDOWS", True), mock.patch.dict(
                os.environ, {"ProgramFiles": str(official_root), "ProgramW6432": "", "PATH": str(self.root)}):
            self.assertIsNone(remote_preview._find_client())
            official.write_bytes(b"fixture")
            self.assertEqual(remote_preview._find_client(), str(official.resolve()))

    def test_signature_check_uses_trusted_inspector_and_exact_publisher_names(self):
        result = SimpleNamespace(returncode=0, stdout=b"trusted")
        with mock.patch.object(remote_preview, "_IS_WINDOWS", True), mock.patch.object(
                remote_preview.subprocess, "run", return_value=result) as run:
            self.assertTrue(remote_preview._verify_client("C:/Program Files/NetBird/netbird.exe"))
        args, kwargs = run.call_args
        command = args[0]
        script = base64.b64decode(command[-1]).decode("utf-16le")
        self.assertIn("Get-AuthenticodeSignature -LiteralPath", script)
        self.assertIn("$s.Status -eq 'Valid'", script)
        self.assertIn("$n -ieq 'NetBird GmbH'", script)
        self.assertIn("Import-Module ($PSHOME+", script)
        self.assertEqual(kwargs["env"]["PSModulePath"], str(Path(command[0]).parent / "Modules"))
        self.assertNotIn("Start-Process", script)
        self.assertLessEqual(kwargs["timeout"], 15)
        result.stdout = b"wrong"
        with mock.patch.object(remote_preview, "_IS_WINDOWS", True), mock.patch.object(
                remote_preview.subprocess, "run", return_value=result):
            self.assertFalse(remote_preview._verify_client("fixture"))

    def test_launch_is_fixed_foreground_http_target_and_no_shell(self):
        with mock.patch.object(remote_preview.subprocess, "Popen") as popen:
            remote_preview._launch_client("C:/Program Files/NetBird/netbird.exe", 18803)
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["C:/Program Files/NetBird/netbird.exe", "expose", "18803",
                                   "--protocol", "http", "--with-name-prefix", "mininvr"])
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("up", args[0])
        if remote_preview._IS_WINDOWS:
            self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)


class NetBirdLocalStatusTests(OfflineTestCase):
    def payload(self, **changes):
        value = {"daemonStatus": "Connected", "management": {"connected": True},
                 "netbirdIp": "100.100.1.2/16", "publicKey": "fixture-secret"}
        value.update(changes)
        return json.dumps(value).encode()

    def test_parse_uses_connected_local_peer_only_and_cgnat_range(self):
        self.assertEqual(remote_preview._parse_status(self.payload()), "100.100.1.2")
        self.assertEqual(remote_preview._parse_status(self.payload(netbirdIp="100.100.1.2")), "100.100.1.2")
        for change in ({"daemonStatus": "NeedsLogin"}, {"management": {"connected": False}},
                       {"management": {"connected": 1}}, {"netbirdIp": "127.0.0.1"},
                       {"netbirdIp": "192.168.1.4"}, {"netbirdIp": "100.128.0.1"},
                       {"netbirdIp": "100.63.255.255"}, {"netbirdIp": "::1"},
                       {"netbirdIp": "100.100.1.2/16 "}, {"netbirdIp": None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                remote_preview._parse_status(self.payload(**change))
        for payload in (b"[]", b"bad fixture-secret", b'{"peers":{"details":[{"netbirdIp":"100.100.1.2"}]}}'):
            with self.subTest(payload=payload), self.assertRaises(ValueError) as error:
                remote_preview._parse_status(payload)
            self.assertNotIn("fixture-secret", str(error.exception))

    def status_process(self, payload, code=0):
        process = _Process([])
        process.stdout = io.BytesIO(payload)
        process.returncode = code
        return process

    def test_status_query_is_readonly_bounded_and_requires_locally_bound_ip(self):
        process = self.status_process(self.payload())
        with mock.patch.object(remote_preview.subprocess, "Popen", return_value=process) as popen, mock.patch.object(
                remote_preview, "_bindable_address", return_value=True) as bindable:
            address = remote_preview._query_address("C:/Program Files/NetBird/netbird.exe", threading.Event())
        self.assertEqual(address, "100.100.1.2")
        self.assertEqual(popen.call_args.args[0], ["C:/Program Files/NetBird/netbird.exe", "status", "--json"])
        self.assertIs(popen.call_args.kwargs["shell"], False)
        bindable.assert_called_once_with(address)
        process = self.status_process(self.payload())
        with mock.patch.object(remote_preview.subprocess, "Popen", return_value=process), mock.patch.object(
                remote_preview, "_bindable_address", return_value=False), self.assertRaisesRegex(ValueError, "网卡"):
            remote_preview._query_address("fixture", threading.Event())

    def test_status_output_over_limit_is_rejected_without_echoing_it(self):
        process = self.status_process(b"fixture-secret" * 20000)
        with mock.patch.object(remote_preview.subprocess, "Popen", return_value=process), self.assertRaises(ValueError) as error:
            remote_preview._query_address("fixture", threading.Event())
        self.assertNotIn("fixture-secret", str(error.exception))

    def test_status_reader_start_failure_cleans_owned_process_and_pipe(self):
        process = _Process([])
        with mock.patch.object(remote_preview.subprocess, "Popen", return_value=process), \
                mock.patch.object(remote_preview.threading.Thread, "start", side_effect=RuntimeError("fixture")), \
                self.assertRaises(RuntimeError):
            remote_preview._query_address("fixture", threading.Event())
        self.assertEqual(process.terminated, 1)
        self.assertIsNotNone(process.returncode)
        self.assertTrue(process.stdout.closed)

    def test_status_cancel_and_nonzero_exit_are_actionable_and_do_not_connect(self):
        for cancelled, code in ((True, 0), (False, 1)):
            process = self.status_process(self.payload(), code=code)
            cancel = threading.Event()
            if cancelled:
                cancel.set()
            with mock.patch.object(remote_preview.subprocess, "Popen", return_value=process), self.assertRaises(ValueError):
                remote_preview._query_address("fixture", cancel)


