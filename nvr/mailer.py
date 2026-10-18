# -*- coding: utf-8 -*-
"""抽帧合成 + 邮件定时推送。

每天把各摄像头前一天的抽帧 JPEG 合成 1 份 1fps 视频（120 帧 = 2 分钟），
作为附件发到指定邮箱。JPEG 是取流时顺手存的，合成只在每天一次，成本可忽略。
"""
import os
import smtplib
import ssl
import subprocess
import time
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .config import records_root
from .ffmpeg import cmd_merge, run as run_ffmpeg, write_concat
from .safety import redact_urls


def timelapse_dir(cfg, cid, day):
    return os.path.join(records_root(cfg), cid, day, "tl")


def merge_day(cfg, cid, day, log=None):
    """把某路某天的抽帧合成 mp4，返回路径；没有素材返回 ''。"""
    log = log or (lambda *a: None)
    d = timelapse_dir(cfg, cid, day)
    if not os.path.isdir(d):
        return ""
    files = sorted(x for x in os.listdir(d) if x.lower().endswith(".jpg"))
    if len(files) < 2:
        return ""
    out = os.path.join(records_root(cfg), cid, "timelapse_%s.mp4" % day)
    if os.path.exists(out) and os.path.getsize(out) > 512:
        return out                       # 幂等：已合过就不再合
    fps = int(cfg.get("record", "out_fps", default=1) or 1)
    paths = [os.path.join(d, x) for x in files]
    listf = os.path.join(records_root(cfg), cid, "tl_%s.list.txt" % day)
    write_concat(paths, listf, fps=fps)
    # 临时文件也必须以 .mp4 结尾，否则 ffmpeg 无法识别封装格式（-movflags 也救不了）
    tmp = out[:-4] + ".part.mp4"
    cmd = cmd_merge(listf, tmp, fps=fps)   # 先写临时文件
    try:
        p = run_ffmpeg(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=1800)
        if p.returncode == 0 and os.path.getsize(tmp) > 1024:
            os.replace(tmp, out)            # 原子替换：最终文件出现即完整（回放不会拿到半截文件）
            log("[合成] %s %s：%d 帧 → %s" %
                (cid, day, len(files), os.path.basename(out)))
            return out
        log("[合成] %s %s 失败 rc=%s" % (cid, day, p.returncode))
    except Exception as e:
        log("[合成] %s %s 异常: %s" % (cid, day, e))
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
    return ""


def build_attachments(cfg, day, log=None):
    out = []
    for c in (cfg.cameras or []):
        if not c.get("enabled", True):
            continue
        p = merge_day(cfg, c.get("id"), day, log=log)
        if p:
            out.append((c.get("name") or c.get("id"), p))
    return out


def send_mail(cfg, subject, body, files, log=None):
    """files: [(显示名, 路径)]。返回 (ok, msg)。"""
    log = log or (lambda *a: None)
    m = cfg.get("mail")
    host = m.get("smtp_host", "")
    port = int(m.get("smtp_port", 465) or 465)
    user = m.get("user", "")
    pwd = m.get("pass", "")
    to = [x.strip() for x in (m.get("to", "") or "").replace(";", ",").split(",")
          if x.strip()]
    if not host or not user or not pwd or not to:
        return False, "邮件未配置完整（服务器/账号/密码/收件人）"

    max_mb = float(m.get("attach_max_mb", 40) or 40)
    msg = MIMEMultipart()
    msg["From"] = user
    msg["To"] = ", ".join(to)
    msg["Subject"] = Header(subject, "utf-8")
    msg.attach(MIMEText(body or "", "plain", "utf-8"))

    total = 0
    attached = 0
    for name, path in files:
        try:
            sz = os.path.getsize(path)
        except Exception:
            continue
        if total + sz > max_mb * 1024 * 1024:
            log("[邮件] %s 会超过总附件限制 %.0fMB，跳过" % (name, max_mb))
            continue
        try:
            with open(path, "rb") as f:
                part = MIMEApplication(f.read())
            fn = "%s_%s" % (name, os.path.basename(path))
            part.add_header("Content-Disposition", "attachment",
                            filename=("utf-8", "", fn))
            msg.attach(part)
            total += sz
            attached += 1
        except Exception as e:
            log("[邮件] 附加 %s 失败: %s" % (name, e))

    s = None
    try:
        context = ssl.create_default_context()
        if m.get("ssl", True) and port != 587:
            s = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
        else:
            s = smtplib.SMTP(host, port, timeout=30)
            s.ehlo()
            s.starttls(context=context)
            s.ehlo()
        s.login(user, pwd)
        s.sendmail(user, to, msg.as_string())
        return True, "已发送 %d 个附件（%.1fMB）" % (
            attached, total / 1048576.0)
    except Exception as e:
        return False, redact_urls(e)[:300]
    finally:
        if s is not None:
            try:
                s.quit()
            except Exception:
                try:
                    s.close()
                except Exception:
                    pass


def push_yesterday(cfg, log=None):
    """推送前一天的抽帧录像。返回 (ok, msg)。"""
    log = log or (lambda *a: None)
    day = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    files = build_attachments(cfg, day, log=log)
    if not files:
        return False, "%s 没有可推送的抽帧（可能还没攒够帧）" % day
    site = cfg.get("site", "name", default="轻量录像机")
    subject = (cfg.get("mail", "subject", default="[{site}] {date} 抽帧录像")
               or "").replace("{site}", site).replace("{date}", day)
    body = ("%s\n\n共 %d 路，每路为全天 1fps 抽帧视频（约 2 分钟）。\n" %
            (day, len(files)))
    body += "\n".join("%s：%s" % (n, os.path.basename(p)) for n, p in files)
    ok, msg = send_mail(cfg, subject, body, files, log=log)
    log("[邮件] %s：%s" % ("成功" if ok else "失败", msg))
    return ok, msg
