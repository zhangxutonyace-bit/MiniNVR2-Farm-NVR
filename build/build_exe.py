# -*- coding: utf-8 -*-
"""
打包 MiniNVR2 为单个 exe（无控制台黑框 + 托盘常驻）。

用法：
    python build/build_exe.py            # 打包
    python build/build_exe.py --clean    # 先清缓存再打包

准备：使用项目虚拟环境安装 requirements-build.txt 后执行本脚本。
--clean 只清理 build 下的打包缓存，保留 dist 下的录像及用户配置。
"""
import os
import sys
import shutil
import subprocess
from pathlib import Path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(REPO, "dist")
NAME = "MiniNVR2"
ENTRY = os.path.join(REPO, "main.py")


def clean_work_cache(paths):
    """Delete only our two known build cache directories, after resolution."""
    build_root = (Path(REPO) / "build").resolve()
    checked = []
    for path in paths:
        target = Path(path)
        resolved = target.resolve()
        if resolved.parent != build_root or resolved.name not in {"_work", "_spec"}:
            raise ValueError("拒绝清理 build 缓存目录之外的路径: %s" % target)
        if target.is_symlink():
            raise ValueError("拒绝清理符号链接: %s" % target)
        checked.append(target)
    for target in checked:
        if target.exists():
            shutil.rmtree(target)


def ffmpeg_path():
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    from shutil import which
    return which("ffmpeg.exe") or which("ffmpeg") or ""


def build(clean=False):
    work = os.path.join(REPO, "build", "_work")
    spec = os.path.join(REPO, "build", "_spec")
    if clean:
        clean_work_cache((work, spec))

    try:
        import PyInstaller
        import cv2
    except ImportError as e:
        print(">>> 缺少打包依赖，请先安装 requirements-build.txt: %s" % e)
        return 1

    # Fail before invoking the packager if the approved offline asset drifted.
    sys.path.insert(0, REPO)
    from nvr.person_localizer import verify_model
    try:
        person_model = verify_model()
    except RuntimeError as exc:
        print(">>> 人物框模型校验失败: %s" % exc)
        return 1
    person_notices = Path(REPO) / "third_party" / "NanoDet"
    if not all((person_notices / name).is_file() for name in ("LICENSE", "SOURCE.json")):
        print(">>> 缺少人物框模型许可证或来源清单")
        return 1

    ff = ffmpeg_path()
    if not ff:
        print(">>> 未找到 FFmpeg，请安装 requirements-build.txt")
        return 1
    assets = os.path.join(REPO, "build", "_assets")
    os.makedirs(assets, exist_ok=True)
    # imageio-ffmpeg ships a versioned basename; runtime looks specifically for
    # _MEIPASS/ffmpeg.exe, so normalize the packaged asset before collecting it.
    packaged_ff = os.path.join(assets, "ffmpeg.exe")
    if os.path.abspath(ff) != os.path.abspath(packaged_ff):
        shutil.copy2(ff, packaged_ff)
    haar_dir = getattr(getattr(cv2, "data", None), "haarcascades", "")
    if not haar_dir or not all(os.path.isfile(os.path.join(haar_dir, name))
                               for name in ("haarcascade_fullbody.xml",
                                            "haarcascade_upperbody.xml")):
        print(">>> 缺少 OpenCV Haar 数据，无法生成完整检测功能的程序")
        return 1
    sep = ";" if os.name == "nt" else ":"
    datas = [
        os.path.join(REPO, "web") + sep + "web",   # 前端页面 → 包根/web
        haar_dir.rstrip("/\\") + sep + "cv2/data",
        str(person_model) + sep + "models",
        str(person_notices) + sep + "third_party/NanoDet",
    ]
    binaries = [packaged_ff + sep + "."]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onefile", "--windowed",     # windowed = 无黑框
        "--name", NAME,
        "--distpath", DIST,
        "--workpath", work,
        "--specpath", spec,
        "--paths", REPO,
        "--collect-submodules", "nvr",
    ]
    for d in datas:
        cmd += ["--add-data", d]
    for b in binaries:
        cmd += ["--add-binary", b]
    # qrcode / cryptography：微信 ClawBot 插件要用（二维码登录 + CDN 视频 AES 加密）
    for h in ("cv2", "numpy", "flask", "pystray", "PIL",
              "imageio_ffmpeg", "jinja2", "werkzeug", "email", "smtplib",
              "qrcode", "cryptography"):
        cmd += ["--hidden-import", h]
    # 画框模型仅使用现有 OpenCV DNN CPU，不打包额外推理运行时。
    for m in ("torch", "torchvision", "ultralytics", "matplotlib", "scipy",
              "pandas", "tensorflow", "tkinter", "IPython", "notebook",
              "sklearn", "pytest", "PyQt5", "PySide2", "onnxruntime"):
        cmd += ["--exclude-module", m]
    cmd.append(ENTRY)

    print(">>> building", NAME, "with", os.path.basename(sys.executable),
          "PyInstaller", PyInstaller.__version__, flush=True)
    env = os.environ.copy()
    temp = os.path.join(REPO, ".tmp")
    os.makedirs(temp, exist_ok=True)
    env.update({"TEMP": temp, "TMP": temp, "PYTHONUTF8": "1"})
    hidden = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    r = subprocess.run(cmd, cwd=REPO, env=env,
                       stdout=sys.stdout, stderr=sys.stderr, **hidden)
    if r.returncode == 0:
        exe = os.path.join(DIST, NAME + ".exe")
        if os.path.exists(exe):
            print(">>> OK:", exe, "(%.1f MB)" % (os.path.getsize(exe) / 1048576.0))
        else:
            print(">>> 构建结束但未找到 exe")
            return 1
    else:
        print(">>> 构建失败 rc=%s" % r.returncode)
    return r.returncode


if __name__ == "__main__":
    try:
        sys.exit(build(clean=("--clean" in sys.argv)))
    except Exception as exc:
        print(">>> 构建异常: %s" % exc, file=sys.stderr)
        sys.exit(1)
