# -*- coding: utf-8 -*-
"""Experimental iLink adapter inherited from the supplied source.

This implementation handles QR login, polling and request/reply video delivery.
Mocked protocol/encryption tests and one live Windows event-video delivery
were verified on 2026-10-18; the scanning user confirmed handset playback of
a six-second synthetic clip. Other accounts/devices and future protocol
compatibility remain unverified. It is disabled by default and restricts
replies to an owner or explicit allowlist. Mail is a separate optional
scheduled delivery mechanism.
"""
import base64
import datetime
import hashlib
import json
import math
import os
import re
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import records_root
from .recordings import list_events, validate_event

# ---------------- 协议常量 ----------------

DEFAULT_BASE = "https://ilinkai.weixin.qq.com"
DEFAULT_CDN = "https://novac2c.cdn.weixin.qq.com/c2c"
CHANNEL_VERSION = "1.0.2"

MEDIA_IMAGE, MEDIA_VIDEO, MEDIA_FILE, MEDIA_VOICE = 1, 2, 3, 4
ITEM_TEXT, ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO = 1, 2, 3, 4, 5
MSG_USER, MSG_BOT = 1, 2
MSG_FINISH = 2
TYPING_ON, TYPING_OFF = 1, 2
SESSION_SECONDS = 24 * 3600          # UI估算窗口；实际会话状态以服务响应为准


def _uin():
    """X-WECHAT-UIN：随机 uint32 的十进制字符串再 base64，每次请求重新生成。"""
    return base64.b64encode(str(random.randint(0, 4294967295)).encode()).decode()


def _now_ms():
    return int(time.time() * 1000)


def _rand_id(prefix="nvr"):
    return "%s-%s" % (prefix, os.urandom(8).hex())


# ---------------- AES-128-ECB（CDN 媒体加解密） ----------------

def _pkcs7_size(n):
    """明文 n 字节 → PKCS7 补齐后的密文长度。"""
    return (int(n) // 16 + 1) * 16


def _aes_ecb_encrypt(data, key16):
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    p = padding.PKCS7(128).padder()
    padded = p.update(data) + p.finalize()
    c = Cipher(algorithms.AES(key16), modes.ECB()).encryptor()
    return c.update(padded) + c.finalize()


def _media_aes_key(hexkey):
    """★ 易错点：base64(hex 字符串的 ascii 字节)，不是 base64(hex 解码后的 16 字节)。"""
    return base64.b64encode(hexkey.encode("ascii")).decode()


class ClawBot(threading.Thread):
    """一个微信 ClawBot 连接：扫码登录 + 长轮询 + 命令应答。"""

    def __init__(self, cfg, hub=None, log=None):
        threading.Thread.__init__(self)
        self.daemon = True
        self.cfg = cfg
        self.hub = hub
        self.log = log or (lambda *a: None)

        self._stop_event = threading.Event()
        self._lock = threading.RLock()

        # 登录状态：idle / wait / scaned / confirmed / expired / error
        self.state = "idle"
        self.qr_url = ""          # 二维码内容（给微信扫码的链接）
        self.qr_token = ""
        self.error = ""
        self.login_at = float(cfg.get("clawbot", "login_at", default=0) or 0)

        self._buf = ""            # getupdates 游标
        self._tickets = {}        # user -> typing_ticket
        self._running = False
        self.last_peer = ("", "")  # 最近一个对话者 (user_id, context_token)，给测试用
        self._event_lists = {}    # Per-user immutable query snapshots; never store context tokens.

    # ---------------- 配置 ----------------

    def _sec(self, k, d=None):
        return self.cfg.get("clawbot", k, default=d)

    @property
    def base(self):
        return (self._sec("base_url") or DEFAULT_BASE).rstrip("/")

    @property
    def cdn(self):
        return (self._sec("cdn_url") or DEFAULT_CDN).rstrip("/")

    @property
    def token(self):
        return self._sec("token") or ""

    def logged_in(self):
        return bool(self.token)

    def remain_sec(self):
        if not self.token or not self.login_at:
            return 0
        return int(max(0, SESSION_SECONDS - (time.time() - self.login_at)))

    # ---------------- HTTP ----------------

    def _url(self, ep):
        return "%s/ilink/bot/%s" % (self.base, ep)

    def _post(self, ep, body, token=None, timeout=40):
        body = dict(body or {})
        body.setdefault("base_info", {"channel_version": CHANNEL_VERSION})
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self._url(ep), data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("AuthorizationType", "ilink_bot_token")
        req.add_header("X-WECHAT-UIN", _uin())
        tk = token if token is not None else self.token
        if tk:
            req.add_header("Authorization", "Bearer %s" % tk)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", "replace")
        try:
            return json.loads(txt)
        except Exception:
            return {"ret": -1, "errmsg": "响应不是 JSON: %s" % txt[:120]}

    def _get(self, path, timeout=15):
        req = urllib.request.Request("%s%s" % (self.base, path), method="GET")
        req.add_header("iLink-App-ClientVersion", "1")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    # ---------------- 扫码登录 ----------------

    def begin_login(self):
        """取二维码并后台等待扫码。返回 (ok, qr_url)。"""
        if not self._sec("enabled", False):
            self.error = "请先启用微信功能并保存设置。"
            return False, ""
        try:
            bt = self._sec("bot_type", 3)
            r = self._get("/ilink/bot/get_bot_qrcode?bot_type=%s" % bt)
        except Exception as e:
            self.state = "error"
            self.error = "取二维码失败：%s" % e
            self.log("[clawbot] %s" % self.error)
            return False, ""
        self.qr_token = r.get("qrcode") or ""
        # qrcode_img_content 是给微信扫的链接；没有就用 qrcode 兜底
        self.qr_url = r.get("qrcode_img_content") or self.qr_token
        if not self.qr_token:
            self.state = "error"
            self.error = "服务端没返回二维码"
            return False, ""
        self.state = "wait"
        self.error = ""
        self.log("[clawbot] 等待微信扫码（二维码仅在本机网页显示）")
        threading.Thread(target=self._poll_login, daemon=True).start()
        return True, self.qr_url

    def _poll_login(self):
        deadline = time.time() + 600      # 10 分钟扫不上就放弃
        last = ""
        while time.time() < deadline and not self._stop_event.is_set():
            try:
                r = self._get("/ilink/bot/get_qrcode_status?qrcode=%s"
                              % urllib.parse.quote(self.qr_token), timeout=40)
            except Exception:
                time.sleep(2)
                continue
            st = r.get("status") or ""
            if st != last:
                last = st
                self.log("[clawbot] 扫码状态：%s" % st)
            if st == "confirmed":
                self._on_confirmed(r)
                return
            if st == "expired":
                self.state = "expired"
                self.error = "二维码已过期，请重新扫码"
                return
            time.sleep(1.5)
        if self.state == "wait":
            self.state = "expired"
            self.error = "等待扫码超时"

    def _on_confirmed(self, r):
        tok = r.get("bot_token") or ""
        if not tok:
            self.state = "error"
            self.error = "登录确认了但没拿到 token"
            return
        if self.hub is not None and hasattr(self.hub, "revoke_preview"):
            self.hub.revoke_preview()
        self.cfg.set("clawbot", "token", tok)
        self.cfg.set("clawbot", "bot_id", r.get("ilink_bot_id") or "")
        self.cfg.set("clawbot", "user_id", r.get("ilink_user_id") or "")
        if r.get("baseurl"):
            self.cfg.set("clawbot", "base_url", r.get("baseurl"))
        self.cfg.set("clawbot", "login_at", time.time())
        self.login_at = time.time()
        self.state = "confirmed"
        self.error = ""
        self.log("[clawbot] ✅ 登录成功，bot_id=%s" % (r.get("ilink_bot_id") or "-"))
        self.start_loop()

    def logout(self):
        self.stop()
        if self.hub is not None and hasattr(self.hub, "revoke_preview"):
            self.hub.revoke_preview()
        self.cfg.set("clawbot", "token", "")
        self.cfg.set("clawbot", "login_at", 0)
        self.state = "idle"
        self.qr_url = ""
        self._buf = ""
        with self._lock:
            self._event_lists.clear()
        self.log("[clawbot] 已退出登录")

    # ---------------- 消息循环 ----------------

    def start_loop(self):
        if self._running or not self._sec("enabled", False) or not self.token:
            return
        self._running = True
        self._stop_event.clear()
        try:
            self.start()          # 线程只能 start 一次
        except RuntimeError:
            self._running = False

    def stop(self):
        self._stop_event.set()
        self._running = False

    def run(self):
        self.log("[clawbot] 开始接收消息")
        backoff = 2
        while not self._stop_event.is_set():
            try:
                r = self._post("getupdates", {"get_updates_buf": self._buf},
                               timeout=40)
            except Exception as e:
                self.log("[clawbot] 收消息异常：%s" % e)
                self._stop_event.wait(min(30, backoff))
                backoff = min(30, backoff * 2)
                continue
            backoff = 2
            ret = r.get("ret", 0)
            if ret != 0 or r.get("errcode"):
                code = r.get("errcode")
                if code == -14 or ret != 0:
                    self.log("[clawbot] 连接失效（errcode=%s），需要重新扫码" % code)
                    self.state = "expired"
                    self.error = "登录已过期，请重新扫码"
                    self._running = False
                    return
                time.sleep(3)
                continue
            if self._stop_event.is_set():
                break
            self._buf = r.get("get_updates_buf") or self._buf
            for m in (r.get("msgs") or []):
                try:
                    self.on_message(m)
                except Exception as e:
                    self.log("[clawbot] 处理消息异常：%s" % e)
        self._running = False
        self.log("[clawbot] 已停止接收")

    # ---------------- 收发 ----------------

    def send_text(self, to, ctx, text):
        body = {"msg": {
            "from_user_id": "",
            "to_user_id": to,
            "client_id": _rand_id("nvr"),
            "message_type": MSG_BOT,
            "message_state": MSG_FINISH,
            "context_token": ctx,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }}
        return self._post("sendmessage", body, timeout=20)

    def _typing(self, to, on=True):
        try:
            tk = self._tickets.get(to)
            if not tk:
                r = self._post("getconfig", {"ilink_user_id": to}, timeout=15)
                tk = r.get("typing_ticket") or ""
                if tk:
                    self._tickets[to] = tk
            if tk:
                self._post("sendtyping", {"ilink_user_id": to,
                                          "typing_ticket": tk,
                                          "status": TYPING_ON if on else TYPING_OFF},
                           timeout=10)
        except Exception:
            pass

    def send_video(self, to, ctx, path, caption=None, event=None):
        """上传并发送视频。失败降级为纯文本说明（不让客户干等）。"""
        def fail(message):
            try:
                self.send_text(to, ctx, message)
            except Exception:
                self.log("[clawbot] 无法发送失败提示，请检查连接状态")
            return False

        name = os.path.basename(path)
        try:
            limit_mb = int(self._sec("max_video_mb", 50) or 50)
            limit = max(1, limit_mb) * 1024 * 1024
            info = os.stat(path)
            if info.st_size > limit:
                return fail("这段录像 %.1f MB，超过上限 %d MB，无法发送。"
                            "请回电脑查看，或在设置里调整单视频上限。"
                            % (info.st_size / 1048576.0, limit // 1048576))
            if not info.st_size:
                return fail("录像文件是空的，暂时无法发送。")
            # Bound reads even if a file unexpectedly starts growing after stat.
            with open(path, "rb") as f:
                if event is not None:
                    # Recheck after open and before reading: a replaced directory
                    # must not redirect a previously validated event elsewhere.
                    verified = validate_event(self.cfg, event, self.hub)
                    if not verified or not os.path.samestat(os.stat(verified), os.fstat(f.fileno())):
                        return fail("录像已变化或尚未完成，请重新查询已完成事件。")
                raw = f.read(limit + 1)
                after = os.fstat(f.fileno())
            if len(raw) > limit:
                return fail("录像超过单视频大小上限，请回电脑查看。")
            if len(raw) != info.st_size or after.st_size != info.st_size \
                    or after.st_mtime_ns != info.st_mtime_ns:
                return fail("录像文件正在变化，请稍后重新查询。")
            if caption and not self._response_ok(self.send_text(to, ctx, caption)):
                return fail("录像说明发送失败，请检查微信连接后重试。")
            key = os.urandom(16)
            hexkey = key.hex()
            rawsize = len(raw)
            md5 = hashlib.md5(raw).hexdigest()
            filesize = _pkcs7_size(rawsize)
            cipher = _aes_ecb_encrypt(raw, key)
            filekey = _rand_id("fk")

            r = self._post("getuploadurl", {
                "filekey": filekey, "media_type": MEDIA_VIDEO,
                "to_user_id": to, "rawsize": rawsize, "rawfilemd5": md5,
                "filesize": filesize, "aeskey": hexkey, "no_need_thumb": True,
            }, timeout=25)
            if not self._response_ok(r):
                return fail("视频上传通道申请失败，请检查微信连接后重试。")
            up = r.get("upload_param") or ""
            full = r.get("upload_full_url") or ""
            if not up and not full:
                return fail("视频上传通道申请失败，请稍后重试。")
            if full:
                url = full
            else:
                url = "%s/upload?encrypted_query_param=%s&filekey=%s" % (
                    self.cdn, urllib.parse.quote(up), urllib.parse.quote(filekey))

            req = urllib.request.Request(url, data=cipher, method="POST")
            req.add_header("Content-Type", "application/octet-stream")
            with urllib.request.urlopen(req, timeout=180) as resp:
                xenc = resp.headers.get("x-encrypted-param") or ""
            if not xenc:
                return fail("视频上传没拿到回执参数，发送中止。")

            secs = self._video_seconds(path)
            item = {"type": ITEM_VIDEO, "video_item": {
                "media": {"encrypt_query_param": xenc,
                          "aes_key": _media_aes_key(hexkey),
                          "encrypt_type": 1},
                "video_size": rawsize,
                "video_md5": md5,
            }}
            if secs:
                item["video_item"]["play_length"] = secs
            body = {"msg": {
                "from_user_id": "", "to_user_id": to,
                "client_id": _rand_id("nvr"),
                "message_type": MSG_BOT, "message_state": MSG_FINISH,
                "context_token": ctx, "item_list": [item],
            }}
            if not self._response_ok(self._post("sendmessage", body, timeout=60)):
                return fail("微信服务未接受这段录像，请检查登录状态后重试。")
            self.log("[clawbot] 微信接口已接受录像 %s（%.1f MB）" % (name, rawsize / 1048576.0))
            return True
        except Exception as e:
            # Network exceptions may contain signed URLs or account tokens.
            self.log("[clawbot] 发送视频失败（%s）" % type(e).__name__)
            return fail("发送录像失败，请检查文件和微信连接后重试。")

    @staticmethod
    def _response_ok(response):
        # iLink allows omitted zero return codes. Explicit malformed codes are
        # rejected; API acceptance alone does not prove delivery to a handset.
        return isinstance(response, dict) and all(
            field not in response or type(response[field]) in (int, str)
            and response[field] in (0, "0") for field in ("ret", "errcode"))

    def _video_seconds(self, path):
        """Read duration only from a local file, with the old timelapse fallback."""
        try:
            if not os.path.isfile(path):
                return 0
            import cv2
            capture = cv2.VideoCapture(os.path.abspath(path))
            try:
                fps = capture.get(cv2.CAP_PROP_FPS)
                frames = capture.get(cv2.CAP_PROP_FRAME_COUNT)
                seconds = frames / fps if fps > 0 else 0
                if math.isfinite(seconds) and seconds > 0:
                    return max(1, int(math.ceil(seconds)))
            finally:
                capture.release()
        except Exception:
            pass
        try:
            fps = float(self.cfg.get("record", "out_fps", default=1) or 1)
            d = os.path.dirname(path)
            tl = os.path.join(d, os.path.basename(path)
                              .replace("timelapse_", "").replace(".mp4", ""), "tl")
            if os.path.isdir(tl):
                n = len([x for x in os.listdir(tl)
                         if x.lower().endswith(".jpg")])
                if n:
                    return int(round(n / fps))
        except Exception:
            pass
        return 0

    # ---------------- 录像定位 ----------------

    def _cameras(self):
        return [c for c in (self.cfg.cameras or [])
                if c.get("enabled", True)]

    def _timelapse(self, cid, day):
        """取某路某天的抽帧视频；没有就当场合成一份。"""
        p = os.path.join(records_root(self.cfg), cid, "timelapse_%s.mp4" % day)
        try:
            if os.path.exists(p) and os.path.getsize(p) > 512:
                return p
        except Exception:
            pass
        try:
            from . import mailer
            return mailer.merge_day(self.cfg, cid, day, log=self.log) or ""
        except Exception as e:
            self.log("[clawbot] 合成 %s %s 失败：%s" % (cid, day, e))
        return ""

    # ---------------- 命令 ----------------

    HELP = (
        "可以直接这样说：\n"
        "· 预览1号 / 预览 农场 —— 同一Wi-Fi打开该路实时画面\n"
        "· 预览 —— 一路时直接发链接，多路时显示可选项\n"
        "· 今天事件 / 昨天事件 农场 —— 列出已完成事件录像\n"
        "· 事件 2026-10-18 1号 —— 指定日期和摄像头\n"
        "· 最新事件 [日期/摄像头] —— 发最新一段（默认今天）\n"
        "· 今天事件 第2条 —— 查询后直接发第2段\n"
        "· 播放第2条 / 播放第2段 —— 按本人最近列表取回一段\n"
        "· 下一页 / 上一页 —— 每页10条，序号固定，列表15分钟有效\n"
        "· 昨天 —— 发昨天的全天抽帧录像（约 2 分钟）\n"
        "· 前天 / 2026-09-26 —— 指定某一天\n"
        "· 昨天 农场 —— 只看某一路（可写摄像头名或序号）\n"
        "· 列表 —— 看看有哪些摄像头\n"
        "只有已授权的用户可请求录像；不识别的内容只显示本说明。"
    )

    @staticmethod
    def _day_offset(n):
        return (datetime.date.today() +
                datetime.timedelta(days=n)).strftime("%Y-%m-%d")

    def _parse_day(self, t):
        m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", t)
        if m:
            try:
                return datetime.date(int(m.group(1)), int(m.group(2)),
                                     int(m.group(3))).strftime("%Y-%m-%d")
            except Exception:
                pass
        if "前天" in t:
            return self._day_offset(-2)
        if "今天" in t:
            return self._day_offset(0)
        if "昨天" in t:
            return self._day_offset(-1)
        return None

    def _parse_cam(self, t):
        """从文本里认出摄像头（名字包含 / 序号）。返回 spec 或 None。"""
        cams = self._cameras()
        if not cams:
            return None
        # Dates and recording indexes must never become camera ordinals.
        t = re.sub(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", " ", t)
        t = re.sub(r"(?:播放\s*)?第\s*-?\d+\s*(?:条|段)", " ", t)
        for c in sorted(cams, key=lambda c: len(c.get("name") or ""), reverse=True):
            nm = (c.get("name") or "").strip()
            if nm and nm in t:
                return c
        m = re.search(r"(?:第\s*)?([0-9]+)\s*(?:号|路)(?:摄像头)?", t)
        if not m:
            m = re.search(r"摄像头\s*([0-9]+)", t)
        if not m:
            # Preserve the original short form, but only as a complete token.
            m = re.search(r"(?:^|\s)(?:第\s*)?([0-9]+)\s*(?:个)?(?=\s|$)", t)
        if m:
            i = int(m.group(1)) - 1
            if 0 <= i < len(cams):
                return cams[i]
        return None

    def _selection(self, text, default_day=None):
        """Resolve an entire selector; residual input is an error, never all cameras."""
        date_pattern = r"(?<!\d)\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?!\d)"
        dates = list(re.finditer(date_pattern, text))
        relative = re.findall(r"前天|今天|昨天", text)
        if len(dates) + len(relative) > 1:
            return None, None, "请一次只指定一个日期。"
        day = self._parse_day(text)
        if dates and not day:
            return None, None, "日期无效，请使用 YYYY-MM-DD，例如 2026-10-18。"
        remaining = re.sub(date_pattern + r"|前天|今天|昨天", " ", text)
        cam = self._parse_cam(remaining)
        if cam:
            name = (cam.get("name") or "").strip()
            if name and name in remaining:
                remaining = remaining.replace(name, " ", 1)
            else:
                remaining = re.sub(r"(?:第\s*)?\d+\s*(?:号|路)(?:摄像头)?|摄像头\s*\d+|(?:^|\s)(?:第\s*)?\d+\s*(?:个)?(?=\s|$)",
                                   " ", remaining, count=1)
        remaining = re.sub(r"事件录像|事件列表|最新事件|事件|抽帧录像|抽帧|录像|回放|查看|播放|摄像头|列表", " ", remaining)
        remaining = remaining.strip(" \t\r\n，,。:：")
        if remaining:
            return None, None, "未找到指定摄像头或命令有误，请发送“列表”查看名称和序号。"
        return day or default_day, cam, None

    EVENT_PAGE_SIZE = 10
    EVENT_LIST_SECONDS = 15 * 60
    EVENT_MAX_USERS = 64
    EVENT_MAX_ITEMS = 500

    def _event_snapshot(self, to):
        now = time.time()
        with self._lock:
            for peer in list(self._event_lists):
                if now - self._event_lists[peer]["created"] >= self.EVENT_LIST_SECONDS:
                    del self._event_lists[peer]
            return self._event_lists.get(to)

    def _save_events(self, to, day, cam, items, truncated):
        self._event_snapshot(to)  # Expire old users before applying the cache bound.
        with self._lock:
            if to not in self._event_lists and len(self._event_lists) >= self.EVENT_MAX_USERS:
                oldest = min(self._event_lists, key=lambda peer: self._event_lists[peer]["created"])
                del self._event_lists[oldest]
            snapshot = {"created": time.time(), "items": items, "day": day,
                        "camera": cam.get("name") or cam.get("id") if cam else "全部摄像头",
                        "page": 0, "truncated": truncated}
            self._event_lists[to] = snapshot
            return snapshot

    def _event_page(self, to, ctx, snapshot, page=None):
        items = snapshot["items"]
        page = snapshot["page"] if page is None else page
        pages = max(1, (len(items) + self.EVENT_PAGE_SIZE - 1) // self.EVENT_PAGE_SIZE)
        start = page * self.EVENT_PAGE_SIZE
        lines = ["%s %s：已完成事件录像，共%d条，第%d/%d页。"
                 % (snapshot["day"], snapshot["camera"], len(items), page + 1, pages)]
        limit = max(1, int(self._sec("max_video_mb", 50) or 50)) * 1048576
        for index, item in enumerate(items[start:start + self.EVENT_PAGE_SIZE], start + 1):
            name = item["name"][:6]
            clock = "%s:%s:%s" % (name[:2], name[2:4], name[4:6])
            lines.append("%d. %s %s %.1f MB%s" %
                         (index, item["cam_name"], clock, item["size"] / 1048576.0,
                          "（超过发送上限）" if item["size"] > limit else ""))
        if snapshot["truncated"]:
            lines.append("结果超过%d条，仅保留最新%d条，请指定摄像头缩小范围。"
                         % (self.EVENT_MAX_ITEMS, self.EVENT_MAX_ITEMS))
        lines.append("回复“播放第N条”取回一段；“下一页/上一页”翻页。序号固定，列表15分钟有效。")
        return self._response_ok(self.send_text(to, ctx, "\n".join(lines)))

    def _play_event(self, to, ctx, snapshot, index):
        if index < 1 or index > len(snapshot["items"]):
            self.send_text(to, ctx, "事件序号超出范围，请按最近列表中的序号选择。")
            return
        item = snapshot["items"][index - 1]
        path = validate_event(self.cfg, item, self.hub)
        if not path:
            self.send_text(to, ctx, "这段录像已删除、已变化或尚未完成，请重新查询已完成事件。")
            return
        clock = item["name"][:6]
        caption = "%s %s %s:%s:%s 事件录像" % (item["cam_name"], item["date"],
                                                clock[:2], clock[2:4], clock[4:6])
        self.send_video(to, ctx, path, caption=caption, event=item)

    def _answer_events(self, to, ctx, text):
        if text in ("下一页", "上一页"):
            snapshot = self._event_snapshot(to)
            if not snapshot:
                self.send_text(to, ctx, "没有有效事件列表或列表已过期，请先发送“今天事件”。")
                return True
            step = 1 if text == "下一页" else -1
            page = snapshot["page"] + step
            if page < 0 or page * self.EVENT_PAGE_SIZE >= len(snapshot["items"]):
                self.send_text(to, ctx, "已经是第一页。" if step < 0 else "已经是最后一页。")
                return True
            if self._event_page(to, ctx, snapshot, page=page):
                snapshot["page"] = page
            return True
        play = re.fullmatch(r"播放\s*第\s*(-?\d+)\s*(?:条|段)", text)
        if play:
            snapshot = self._event_snapshot(to)
            if not snapshot:
                self.send_text(to, ctx, "没有有效事件列表或列表已过期，请先发送“今天事件”。")
            else:
                self._play_event(to, ctx, snapshot, int(play.group(1)))
            return True
        if "事件" not in text:
            return False
        picks = list(re.finditer(r"(?:播放\s*)?第\s*(-?\d+)\s*(?:条|段)", text))
        if len(picks) > 1:
            self.send_text(to, ctx, "请一次只选择一个事件序号。")
            return True
        index = int(picks[0].group(1)) if picks else (1 if "最新事件" in text else None)
        selector = re.sub(r"(?:播放\s*)?第\s*-?\d+\s*(?:条|段)", " ", text)
        day, cam, error = self._selection(selector, self._day_offset(0))
        if error:
            self.send_text(to, ctx, error)
            return True
        if not self._cameras():
            self.send_text(to, ctx, "还没有启用的摄像头，请先添加摄像头。")
            return True
        items, truncated = list_events(self.cfg, self.hub, day,
                                       cam.get("id") if cam else None, self.EVENT_MAX_ITEMS)
        snapshot = self._save_events(to, day, cam, items, truncated)
        if not items:
            self.send_text(to, ctx, "%s %s没有已完成事件录像；录像中或重封装中的片段稍后才能查看。"
                           % (day, snapshot["camera"]))
        elif index is not None:
            self._play_event(to, ctx, snapshot, index)
        else:
            self._event_page(to, ctx, snapshot)
        return True

    def on_message(self, msg):
        if not self._sec("enabled", False) or self._stop_event.is_set():
            return
        if msg.get("message_type") not in (None, MSG_USER):
            return                        # 只处理用户发来的
        items = msg.get("item_list") or []
        text = ""
        for it in items:
            if (it.get("type") == ITEM_TEXT and
                    isinstance(it.get("text_item"), dict)):
                text = (it.get("text_item") or {}).get("text") or ""
                break
        text = text.strip()
        to = msg.get("from_user_id") or ""
        ctx = msg.get("context_token") or ""
        allowed = self._sec("allowed_users", [])
        allowed = set(allowed) if isinstance(allowed, list) else set()
        owner = self._sec("user_id") or ""
        if owner:
            allowed.add(owner)
        if not to or not ctx or to not in allowed:
            return
        self.last_peer = (to, ctx)
        self.log("[clawbot] 收到已授权用户的请求")

        self._typing(to, True)
        try:
            self._answer(to, ctx, text)
        finally:
            self._typing(to, False)

    def _answer(self, to, ctx, text):
        if any(k in text for k in ("帮助", "help", "?", "？", "菜单", "怎么用")):
            self.send_text(to, ctx, self.HELP)
            return
        if self._answer_preview(to, ctx, text):
            return
        if self._answer_events(to, ctx, text):
            return
        if text in ("列表", "摄像头", "摄像头列表", "有哪些", "有哪些摄像头", "清单"):
            cams = self._cameras()
            if not cams:
                self.send_text(to, ctx, "还没有添加摄像头。")
                return
            lines = ["%d. %s" % (i + 1, c.get("name") or c.get("id"))
                     for i, c in enumerate(cams)]
            self.send_text(to, ctx, "当前摄像头：\n" + "\n".join(lines))
            return

        day, cam, error = self._selection(text)
        if error:
            self.send_text(to, ctx, error)
            return
        if not day:
            self.send_text(to, ctx, self.HELP)
            return
        targets = [cam] if cam else self._cameras()
        if not targets:
            self.send_text(to, ctx, "还没有添加摄像头，先在系统里加一路吧。")
            return

        sent = 0
        for c in targets:
            cid = c.get("id")
            nm = c.get("name") or cid
            path = self._timelapse(cid, day)
            if not path:
                self.send_text(to, ctx, "%s：%s 没有抽帧录像（可能那天没运行，"
                                        "或录像还没合成完）。" % (nm, day))
                continue
            if self.send_video(to, ctx, path,
                               caption="%s %s 全天抽帧录像" % (nm, day)):
                sent += 1
        if sent == 0 and len(targets) > 1:
            self.send_text(to, ctx, "%s 这天没有任何一路的抽帧录像。" % day)

    def _answer_preview(self, to, ctx, text):
        match = re.match(r"^(?:查看实时预览|实时预览|查看预览|预览)(.*)$", text)
        if not match:
            return False
        selector = match.group(1).strip(" \t\r\n，,。:：")
        cameras = self._cameras()
        if not cameras:
            self.send_text(to, ctx, "还没有启用的摄像头，请先添加摄像头。")
            return True
        camera = None
        if not selector:
            if len(cameras) == 1:
                camera = cameras[0]
            else:
                lines = ["请选择要预览的摄像头："]
                lines.extend("%d. %s" % (i, c.get("name") or c.get("id"))
                             for i, c in enumerate(cameras, 1))
                lines.append("请单独发送“预览1号”或“预览 摄像头名称”。手机须与电脑连接同一Wi-Fi。")
                self.send_text(to, ctx, "\n".join(lines))
                return True
        else:
            for candidate in cameras:
                name = str(candidate.get("name") or "").strip()
                if name and selector == name:
                    camera = candidate
                    break
            if camera is None:
                ordinal = re.fullmatch(r"(?:第\s*)?([0-9]+)\s*(?:号|路|个)?(?:摄像头)?|摄像头\s*([0-9]+)", selector)
                if ordinal:
                    index = int(ordinal.group(1) or ordinal.group(2)) - 1
                    if 0 <= index < len(cameras):
                        camera = cameras[index]
        if camera is None:
            self.send_text(to, ctx, "未找到指定摄像头或预览命令有误。请发送“列表”，再单独发送“预览1号”或“预览 摄像头名称”。")
            return True
        if self.hub is None or not hasattr(self.hub, "preview_link"):
            self.send_text(to, ctx, "局域网预览未就绪，请先在电脑设置中启用“微信局域网预览”。")
            return True
        try:
            link = self.hub.preview_link(camera["id"])
        except ValueError as exc:
            self.send_text(to, ctx, "暂时不能生成预览链接：%s" % exc)
            return True
        self.send_text(to, ctx, "%s 实时预览\n手机和电脑须连接同一Wi-Fi，链接10分钟有效，请勿转发。\n%s"
                       % (camera.get("name") or camera["id"], link))
        return True
