"""Create a clearly synthetic, local-only demo without touching production data."""
import datetime
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nvr.config import Config
from nvr.ffmpeg import ffmpeg_exe, cmd_merge, write_concat


def prepare():
    data = ROOT / "data-demo"
    data.mkdir(exist_ok=True)
    source = data / "synthetic-demo.mp4"
    ff = ffmpeg_exe()
    flags = {"creationflags": 0x08000000} if os.name == "nt" else {}
    if not source.exists():
        subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15:duration=60",
                        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart", str(source)], check=True, **flags)
    # Distinct, fixed demo paths. Existing regular data/config.json is never opened.
    cfg = Config(str(data / "config.json"))
    cfg.data["cameras"] = [{"id": "demo", "name": "本机合成画面 · 非真实摄像头",
                            "url": str(source), "enabled": True, "detect": False}]
    for section, values in {
        "site": {"name": "MiniNVR2 · 本机演示（合成视频）"},
        "server": {"host": "127.0.0.1", "port": 8801, "token": ""},
        "stream": {"auto_pick": False, "fps": 8, "width": 480},
        "record": {"root": str(data / "records"), "timelapse_sec": 5, "keep_days": 7},
        "detect": {"enabled": False},
        "mail": {"enabled": False},
        "clawbot": {"enabled": False, "auto_start": False, "token": ""},
    }.items():
        for key, value in values.items():
            cfg.set(section, key, value, save=False)
    cfg.save()
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    camera_dir = data / "records" / "demo"
    event_dir = camera_dir / yesterday / "event"
    event_dir.mkdir(parents=True, exist_ok=True)
    event = event_dir / "120000.mp4"
    if not event.exists():
        subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
                        "-t", "10", "-c", "copy", "-movflags", "+faststart", str(event)],
                       check=True, **flags)
    frames = camera_dir / yesterday / "tl"
    frames.mkdir(parents=True, exist_ok=True)
    merged = camera_dir / ("timelapse_%s.mp4" % yesterday)
    if not merged.exists():
        subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
                        "-vf", "fps=1/5,scale=480:-2", "-frames:v", "12", str(frames / "%06d.jpg")],
                       check=True, **flags)
        listing = data / "demo-concat.txt"
        write_concat([str(p) for p in sorted(frames.glob("*.jpg"))], str(listing), fps=1)
        subprocess.run(cmd_merge(str(listing), str(merged), fps=1), check=True, **flags)
    print("Demo prepared: synthetic video only; no camera, email or WeChat connection.")


if __name__ == "__main__":
    prepare()
