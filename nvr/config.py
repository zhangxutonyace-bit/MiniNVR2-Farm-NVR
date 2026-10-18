# -*- coding: utf-8 -*-
"""配置：单一 config.json。缺字段自动补全，改动立刻落盘（换台电脑也记得住）。

原则：任何环境相关的值（网段 / 路径 / 端口 / 账号 / 阈值）一律走配置，
源码里不出现写死的 IP、路径、设备名。
"""
import json
import os
import re
import sys
import threading
import uuid

# ---------------- 路径（变量化，frozen 时跟着 exe 走） ----------------

def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir():
    override = os.environ.get("NVR_DATA_DIR")
    if override:
        d = os.path.abspath(os.path.expanduser(override))
        os.makedirs(d, exist_ok=True)
        return d
    d = os.path.join(app_dir(), "data")
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        pass
    d = os.path.join(os.path.expanduser("~"), "NVRData")
    os.makedirs(d, exist_ok=True)
    return d


def records_root(cfg=None):
    root = "records"
    if cfg is not None:
        root = (cfg.get("record", "root") or "records")
    if not os.path.isabs(root):
        root = os.path.join(data_dir(), root)
    os.makedirs(root, exist_ok=True)
    return root


def log_path():
    return os.path.join(data_dir(), "nvr.log")


# ---------------- 默认值 ----------------

_DEFAULTS = {
    "site": {"name": "轻量录像机", "accent": "#c8761f"},

    "server": {"host": "127.0.0.1", "port": 8800, "token": ""},

    "cameras": [],   # [{id,name,url,enabled,detect}]

    # 取流：只拉一条小流，一帧三用
    "stream": {
        "fps": 8,             # 向摄像头取的帧率（预览够流畅即可）
        "width": 480,         # 取流宽度（高度自适应），越小越省
        "quality": 6,         # MJPEG 质量 2~31，越小越清晰
        "transport": "tcp",   # tcp / udp
        "timeout_sec": 10,    # ffmpeg 读写超时（秒）
        "retry_sec": 5,       # 断线重连间隔（秒）
        "stall_sec": 25,      # 超过这么久没新帧就强制重连（防 ffmpeg 假死）
        "auto_pick": True,    # 自动挑最小分辨率码流
    },

    # 人形检测：全局串行，同一时刻只算 1 路
    "detect": {
        "enabled": True,
        "mode": "auto",       # auto(优先Haar) / haar / motion(最省)
        "interval": 0.5,      # 每路检测间隔（秒）
        "confirm": 2,         # 连续几次判定有人才算真有人
        "gone_sec": 8,        # 连续多久没人 → 停止录像
        "cooldown": 10,       # 两次事件之间的最小间隔（秒）
        "min_area": 0.02,     # 前景轮廓占画面比例下限
    },

    "record": {
        "root": "records",
        "keep_days": 7,
        "timelapse_sec": 720,   # 每 N 现实秒存 1 帧（720 → 全天 120 帧 = 2 分钟）
        "out_fps": 1,           # 抽帧视频输出帧率
        "max_event_min": 30,    # 单次事件最长录像（分钟）
    },

    "mail": {
        "enabled": False,
        "time": "08:00",        # 每天推送时刻
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "ssl": True,
        "user": "", "pass": "", "to": "",
        "subject": "[{site}] {date} 抽帧录像",
        "attach_max_mb": 40,
        "last_sent": "",        # 已推送过的日期，防止重复发
    },

    "scan": {
        "range": "",            # 留空 = 按本机所有网卡自动推导
        "port": 554,
        "users": "admin",
        "pwds": "admin123,123456,admin,888888,12345",
        "timeout": 5,
    },

    # 微信 ClawBot 插件（腾讯 iLink 协议）：用户在微信里查询抽帧或已结束事件录像
    "clawbot": {
        "enabled": False,
        "bot_type": 3,          # 扫码登录的 bot_type，原实现取 3，以实际服务为准
        "base_url": "",         # 留空使用原实现服务地址：ilinkai.weixin.qq.com（调试可指向假服务）
        "cdn_url": "",          # 留空使用原实现服务地址：novac2c.cdn.weixin.qq.com/c2c
        "token": "",            # 扫码后写入，24h 有效
        "bot_id": "", "user_id": "",
        "allowed_users": [],     # 明确允许的额外微信用户 ID；默认仅登录所有者
        "login_at": 0,          # 登录时间戳，用于算剩余有效期
        "max_video_mb": 50,     # 单个视频超过就不发（防微信慢/失败）
        "auto_start": False,    # 用户明确开启后才自动接收消息
    },

    "system": {"autostart": False, "close_to_tray": True},
}


def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config(object):
    def __init__(self, path=None):
        self.path = path or os.path.join(data_dir(), "config.json")
        self.lock = threading.RLock()
        self.data = json.loads(json.dumps(_DEFAULTS))
        self.load()

    # ---- 读写 ----
    def load(self):
        with self.lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = _deep_merge(self.data, json.load(f))
            except Exception:
                pass
            return self.data

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            return True

    def get(self, section, key=None, default=None):
        with self.lock:
            sec = self.data.get(section)
            if not isinstance(sec, dict):
                return {} if key is None else default
            if key is None:
                return sec
            v = sec.get(key, default)
            return default if v is None else v

    def set(self, section, key, value, save=True):
        with self.lock:
            self.data.setdefault(section, {})[key] = value
            if save:
                self.save()
            return True

    # ---- 摄像头 ----
    @property
    def cameras(self):
        return self.data.get("cameras") or []

    def get_camera(self, cid):
        for c in self.cameras:
            if c.get("id") == cid:
                return c
        return None

    def upsert_camera(self, spec):
        with self.lock:
            cid = spec.get("id") or ("cam" + uuid.uuid4().hex[:16])
            if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cid):
                raise ValueError("摄像头 ID 只能包含字母、数字、下划线或短横线，最长 64 字符")
            old = self.get_camera(cid)
            item = dict(old or {})
            item.update({
                "id": cid,
                "name": spec.get("name") or spec.get("url") or cid,
                "url": spec.get("url") or item.get("url") or "",
                "enabled": bool(spec.get("enabled", True)),
                "detect": bool(spec.get("detect", True)),
            })
            if old is None:
                self.data.setdefault("cameras", []).append(item)
            else:
                old.update(item)
            self.save()
            return item

    def remove_camera(self, cid):
        with self.lock:
            self.data["cameras"] = [c for c in self.cameras if c.get("id") != cid]
            self.save()
            return True
