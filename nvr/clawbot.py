# -*- coding: utf-8 -*-
"""Experimental iLink adapter inherited from the supplied source.

This implementation handles QR login, polling and request/reply video delivery.
Only local mocked protocol/encryption tests have been run in this project;
current service availability, account eligibility and actual receipt remain
unverified. It is disabled by default and restricts replies to an owner or
explicit allowlist. Mail is a separate optional scheduled delivery mechanism.
"""
import base64
import datetime
import hashlib
import json
import os
import re
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import records_root

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
        self.cfg.set("clawbot", "token", "")
        self.cfg.set("clawbot", "login_at", 0)
        self.state = "idle"
        self.qr_url = ""
        self._buf = ""
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

    def send_video(self, to, ctx, path, caption=None):
        """上传并发送视频。失败降级为纯文本说明（不让客户干等）。"""
        name = os.path.basename(path)
        if caption:
            self.send_text(to, ctx, caption)
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except Exception as e:
            self.send_text(to, ctx, "读取录像失败：%s" % e)
            return False
        limit = int(self._sec("max_video_mb", 50) or 50) * 1024 * 1024
        if not raw:
            self.send_text(to, ctx, "录像文件是空的（%s）。" % name)
            return False
        if len(raw) > limit:
            mb = len(raw) / 1048576.0
            self.send_text(to, ctx,
                           "这段录像 %.1f MB，超过上限 %d MB，发不出去。"
                           "可以在设置里调大上限。" % (mb, limit // 1048576))
            return False

        try:
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
            up = r.get("upload_param") or ""
            full = r.get("upload_full_url") or ""
            if not up and not full:
                self.send_text(to, ctx, "视频上传通道申请失败：%s"
                               % (r.get("errmsg") or "未知原因"))
                return False
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
                self.send_text(to, ctx, "视频上传没拿到回执参数，发送中止。")
                return False

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
            self._post("sendmessage", body, timeout=60)
            self.log("[clawbot] 已发送录像 %s（%.1f MB）" % (name, rawsize / 1048576.0))
            return True
        except Exception as e:
            self.log("[clawbot] 发送视频失败：%s" % e)
            self.send_text(to, ctx, "发送录像时出错：%s" % e)
            return False

    def _video_seconds(self, path):
        """估算时长（抽帧视频：帧数 ÷ 输出帧率）。拿不到就返回 0。"""
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
        for c in cams:
            nm = (c.get("name") or "").strip()
            if nm and nm in t:
                return c
        m = re.search(r"(?:第)?\s*([0-9]+)\s*(?:号|路|个)?", t)
        if m:
            i = int(m.group(1)) - 1
            if 0 <= i < len(cams):
                return cams[i]
        return None

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
        low = text.lower()
        if any(k in text for k in ("帮助", "help", "?", "？", "菜单", "怎么用")):
            self.send_text(to, ctx, self.HELP)
            return
        if any(k in text for k in ("列表", "摄像头", "有哪些", "清单")):
            cams = self._cameras()
            if not cams:
                self.send_text(to, ctx, "还没有添加摄像头。")
                return
            lines = ["%d. %s" % (i + 1, c.get("name") or c.get("id"))
                     for i, c in enumerate(cams)]
            self.send_text(to, ctx, "当前摄像头：\n" + "\n".join(lines))
            return

        day = self._parse_day(text)
        if not day:
            self.send_text(to, ctx, self.HELP)
            return
        cam = self._parse_cam(text)
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
