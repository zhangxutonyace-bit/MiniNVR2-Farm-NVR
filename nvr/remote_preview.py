# -*- coding: utf-8 -*-
"""Own one temporary NetBird expose session for the isolated camera viewer.

This module never installs or connects NetBird, changes an account/policy, or
executes a program found through PATH. Raw client output is never retained.
"""
import base64
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import socket
import subprocess
import threading
import time

from .preview import RemotePreviewService, validate_public_origin


STARTUP_SECONDS = 35
MAX_LINE = 2048
MAX_PENDING_LINES = 128
MAX_STATUS_BYTES = 128 * 1024
STATUS_SECONDS = 8
_IS_WINDOWS = os.name == "nt"
_URL_LINE = re.compile(r"^\s*URL:\s*(\S+)\s*$")


def _find_client():
    """Only the official machine installation directory is eligible."""
    if not _IS_WINDOWS:
        return None
    roots = (os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles"))
    for root in roots:
        if not root or not Path(root).is_absolute():
            continue
        candidate = Path(root) / "NetBird" / "netbird.exe"
        try:
            if candidate.is_file() and not candidate.is_symlink():
                return str(candidate.resolve())
        except OSError:
            continue
    return None


def _verify_client(path):
    """Check the Windows trust result and publisher without executing NetBird."""
    if not _IS_WINDOWS:
        return False
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
        "System32/WindowsPowerShell/v1.0/powershell.exe")
    # A quoted literal inside an encoded command is independent of shell syntax.
    literal = str(path).replace("'", "''")
    script = ("$ErrorActionPreference='Stop';"
              "Import-Module ($PSHOME+'\\Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop;"
              "$s=Get-AuthenticodeSignature -LiteralPath '" + literal + "';"
              "if($s.Status -eq 'Valid' -and $s.SignerCertificate){"
              "$n=$s.SignerCertificate.GetNameInfo(0,$false);"
              "if($n -ieq 'NetBird' -or $n -ieq 'NetBird GmbH'){"
              "[Console]::Write('trusted');exit 0}};exit 1")
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    env = dict(os.environ)
    # A PS7 host can otherwise inject incompatible modules into Windows PS5.
    env["PSModulePath"] = str(powershell.parent / "Modules")
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=12, check=False, creationflags=subprocess.CREATE_NO_WINDOW, env=env)
        return result.returncode == 0 and result.stdout == b"trusted"
    except (OSError, subprocess.SubprocessError):
        return False


def _launch_client(path, port):
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
              "stderr": subprocess.STDOUT, "text": True, "encoding": "utf-8",
              "errors": "replace", "bufsize": 1, "close_fds": True,
              "cwd": str(Path(path).parent), "shell": False}
    if _IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(
        [path, "expose", str(port), "--protocol", "http", "--with-name-prefix", "mininvr"],
        **kwargs)


def _parse_status(payload):
    """Accept only the connected local peer's CGNAT IPv4, never a peer list."""
    try:
        value = json.loads(payload)
        if (not isinstance(value, dict) or value.get("daemonStatus") != "Connected"
                or not isinstance(value.get("management"), dict)
                or value["management"].get("connected") is not True):
            raise ValueError
        address = value.get("netbirdIp")
        if not isinstance(address, str) or len(address) > 24 or address != address.strip():
            raise ValueError
        parsed = ipaddress.ip_interface(address).ip
        if not isinstance(parsed, ipaddress.IPv4Address) or parsed not in ipaddress.IPv4Network("100.64.0.0/10"):
            raise ValueError
        return str(parsed)
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("请先在电脑上的 NetBird 登录并连接，确认已分配专用 IPv4 地址后再试。") from None


def _bindable_address(address):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((address, 0))
        return True
    except OSError:
        return False


def _query_address(path, cancel):
    """Read the signed installed client's local status with bounded output/time."""
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
              "stderr": subprocess.DEVNULL, "close_fds": True, "shell": False,
              "cwd": str(Path(path).parent)}
    if _IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen([path, "status", "--json"], **kwargs)
    chunks, done, failed = [], threading.Event(), threading.Event()

    def read():
        size = 0
        try:
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_STATUS_BYTES:
                    failed.set()
                    break
                chunks.append(chunk)
        except (OSError, ValueError):
            failed.set()
        finally:
            done.set()

    reader = threading.Thread(target=read, name="nvr-remote-client-status", daemon=True)
    reader_started = False
    try:
        reader.start()
        reader_started = True
        deadline = time.monotonic() + STATUS_SECONDS
        while not done.wait(0.05):
            if cancel.is_set() or time.monotonic() >= deadline:
                raise ValueError("读取 NetBird 连接状态超时，请检查电脑客户端后重试。")
        if cancel.is_set() or failed.is_set():
            raise ValueError("无法读取 NetBird 连接状态，请检查官方客户端后重试。")
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            raise ValueError("NetBird 状态检查未结束，请检查电脑客户端后重试。") from None
        if process.returncode != 0:
            raise ValueError("请先打开电脑上的 NetBird，并由你登录账号、确认已连接，再启动远程预览。")
        address = _parse_status(b"".join(chunks))
        if not _bindable_address(address):
            raise ValueError("NetBird 专用网卡尚未就绪，请确认电脑客户端已连接后重试。")
        return address
    finally:
        RemotePreviewController._terminate(process)
        if reader_started:
            reader.join(timeout=1)
        if not reader_started or not reader.is_alive():
            process.stdout.close()


def _output_hint(line):
    """Map known errors to constant messages; never echo client/account output."""
    value = line.casefold()
    if "permission denied" in value or "permissiondenied" in value:
        return "请在 NetBird 控制台的 Settings → Clients 中启用 Peer Expose，并允许这台电脑。"
    if "block inbound" in value or "block-inbound" in value:
        return "NetBird 当前阻止传入连接，请先在其客户端检查此设置后重试。"
    if any(part in value for part in ("client is not running", "not connected", "run 'netbird up'")):
        return "请先打开电脑上的 NetBird，并由你登录账号、确认已连接，再启动远程预览。"
    if "unknown command" in value or "unknown flag" in value:
        return "当前 NetBird 版本不支持临时远程预览，请从官网更新后重试。"
    if "maximum number of active expose" in value:
        return "NetBird 的临时连接数量已达上限，请在其控制台检查后稍后重试。"
    return ""


class RemotePreviewController:
    """Explicit start only; every restart requires a new user action."""
    def __init__(self, cfg, camera_getter, log=None):
        self.cfg = cfg
        self.log = log or (lambda message: None)
        self.viewer = RemotePreviewService(cfg, camera_getter, self.log)
        self._lock = threading.RLock()
        self._lifecycle = threading.RLock()
        self._generation = 0
        self._state = "stopped"
        self._url = ""
        self._error = ""
        self._process = None
        self._bridge = None
        self._worker = None
        self._cancel = threading.Event()

    def _enabled(self):
        return self.cfg.get("remote_preview", "enabled", default=False) is True

    def status(self):
        with self._lifecycle:
            with self._lock:
                generation, state, process = self._generation, self._state, self._process
            if state == "ready" and (not self._enabled() or process is None
                                      or process.poll() is not None
                                      or not self.viewer.status().get("running")
                                      or self._bridge is None or not self._bridge.status()):
                self._finish(generation, "远程连接或本机预览入口已停止，请在电脑设置中重新启动。")
            with self._lock:
                return {"state": self._state, "running": self._state == "ready",
                        "url": self._url, "error": self._error,
                        "installed": bool(_find_client())}

    def start(self):
        with self._lifecycle:
            if not self._enabled():
                raise ValueError("请先在电脑设置中启用远程预览。")
            with self._lock:
                if self._state in ("starting", "ready"):
                    return self.status()
                self._generation += 1
                generation = self._generation
                self._cancel = threading.Event()
                cancel = self._cancel
                self._state, self._url, self._error = "starting", "", ""
                worker = threading.Thread(target=self._run, args=(generation, cancel),
                                          name="nvr-remote-preview", daemon=True)
                self._worker = worker
                try:
                    worker.start()
                except Exception:
                    self._state = "error"
                    self._error = "远程预览启动失败，请稍后重试。"
                    self._worker = None
            return self.status()

    def _current(self, generation, cancel):
        with self._lock:
            return (generation == self._generation and not cancel.is_set()
                    and self._enabled())

    @staticmethod
    def _terminate(process):
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.SubprocessError):
                pass
        except (OSError, subprocess.SubprocessError):
            pass

    def _close_viewer(self):
        bridge, self._bridge = self._bridge, None
        try:
            if bridge is not None:
                bridge.stop()
        finally:
            try:
                self.viewer.revoke()
            finally:
                self.viewer.stop()

    def _finish(self, generation, message):
        # This ordering closes images even if the remote service takes time to expire.
        with self._lifecycle:
            with self._lock:
                if generation != self._generation:
                    return
                self._cancel.set()
                process, self._process = self._process, None
                self._state, self._url, self._error = "error", "", message
            try:
                self._close_viewer()
            finally:
                self._terminate(process)
            self.log("[远程预览] 临时连接已关闭，请查看设置中的状态。")

    def stop(self):
        with self._lifecycle:
            with self._lock:
                self._generation += 1
                self._cancel.set()
                process, self._process = self._process, None
                worker, self._worker = self._worker, None
                self._state, self._url, self._error = "stopped", "", ""
            try:
                self._close_viewer()
            finally:
                self._terminate(process)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1)
        return self.status()

    def revoke(self):
        self.viewer.revoke()

    def make_link(self, cid):
        with self._lifecycle:
            with self._lock:
                state, process, error = self._state, self._process, self._error
                generation = self._generation
            if not self._enabled():
                self.stop()
                raise ValueError("远程预览已停用，请先在电脑设置中启用。")
            if state != "ready" or process is None:
                raise ValueError(error or "远程预览尚未就绪，请在电脑设置中点击启动，并等待连接成功。")
            if process.poll() is not None:
                self._finish(generation, "远程连接已中断，请在电脑设置中重新启动远程预览。")
                raise ValueError(self.status()["error"])
            if (not self.viewer.status().get("running") or self._bridge is None
                    or not self._bridge.status()):
                self._finish(generation, "本机预览入口已停止，请在电脑设置中重新启动远程预览。")
                raise ValueError(self.status()["error"])
            return self.viewer.make_link(cid)

    @staticmethod
    def _read_output(stream, pending, cancel, overflow):
        try:
            while not cancel.is_set():
                line = stream.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE:
                    overflow.set()
                    break
                try:
                    pending.put_nowait(line.rstrip("\r\n"))
                except queue.Full:
                    overflow.set()
                    break
        except (OSError, ValueError):
            if not cancel.is_set():
                overflow.set()

    def _run(self, generation, cancel):
        reader = process = None
        try:
            path = _find_client()
            if not path:
                self._finish(generation, "尚未安装 NetBird。请先从官网在电脑安装，并由你登录账号；手机无需安装。")
                return
            if not _verify_client(path):
                self._finish(generation, "NetBird 的官方数字签名未通过验证，请从官网重新安装后重试。")
                return
            try:
                address = _query_address(path, cancel)
            except ValueError as exc:
                self._finish(generation, str(exc))
                return
            with self._lifecycle:
                if not self._current(generation, cancel):
                    return
                port = self.viewer.start_origin()
                from .remote_bridge import PreviewBridge
                self._bridge = PreviewBridge(address, port)
                bridge_port = self._bridge.start()
                process = _launch_client(path, bridge_port)
                with self._lock:
                    self._process = process
            pending, overflow = queue.Queue(MAX_PENDING_LINES), threading.Event()
            reader = threading.Thread(target=self._read_output,
                                      args=(process.stdout, pending, cancel, overflow),
                                      name="nvr-remote-client-output", daemon=True)
            reader.start()
            deadline = time.monotonic() + STARTUP_SECONDS
            hint, public_url = "", ""
            while self._current(generation, cancel):
                if overflow.is_set():
                    self._finish(generation, "NetBird 返回的状态异常，连接已关闭，请更新官方客户端后重试。")
                    return
                if (not self.viewer.status().get("running") or self._bridge is None
                        or not self._bridge.status()):
                    self._finish(generation, "本机预览入口已停止，请在电脑设置中重新启动远程预览。")
                    return
                if process.poll() is not None:
                    # A short-lived CLI can print its useful error and exit before
                    # this worker first observes the queue. Drain only safe hints.
                    reader.join(timeout=0.1)
                    while True:
                        try:
                            hint = _output_hint(pending.get_nowait()) or hint
                        except queue.Empty:
                            break
                    self._finish(generation, hint or "远程连接已中断。请确认电脑上的 NetBird 已登录、已连接且允许 Peer Expose，再重新启动。")
                    return
                if not public_url and time.monotonic() >= deadline:
                    self._finish(generation, hint or "连接超时，请检查 NetBird 登录、Peer Expose 权限和电脑网络，然后重试。")
                    return
                try:
                    line = pending.get(timeout=0.05)
                except queue.Empty:
                    continue
                hint = _output_hint(line) or hint
                if line.lstrip().startswith("URL:"):
                    match = _URL_LINE.fullmatch(line)
                    try:
                        value = validate_public_origin(match.group(1)) if match else None
                    except (ValueError, TypeError):
                        value = None
                    if not value or (public_url and value != public_url):
                        self._finish(generation, "NetBird 返回的 HTTPS 地址无效，连接已关闭，请更新官方客户端后重试。")
                        return
                    with self._lifecycle:
                        if not self._current(generation, cancel):
                            return
                        if process.poll() is not None:
                            continue
                        self.viewer.set_public_url(value)
                        with self._lock:
                            self._state, self._url, self._error = "ready", value, ""
                        public_url = value
                    self.log("[远程预览] 临时 HTTPS 连接已就绪，仅提供授权单路画面。")
            # Configuration may have been disabled without a direct stop call.
            if generation == self._generation and not self._enabled():
                self._finish(generation, "远程预览已停用。")
        except Exception:
            self._finish(generation, "远程预览启动失败，请检查官方 NetBird 客户端和电脑网络后重试。")
        finally:
            if reader is not None and reader is not threading.current_thread():
                reader.join(timeout=1)
            if process is not None and process.stdout is not None and (reader is None or not reader.is_alive()):
                try:
                    process.stdout.close()
                except (OSError, ValueError):
                    pass
