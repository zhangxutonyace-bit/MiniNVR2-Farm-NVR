# -*- coding: utf-8 -*-
"""MiniNVR2 desktop entry point."""
import argparse
import os
import signal
import socket
import sys
import threading
import webbrowser

from werkzeug.serving import ThreadedWSGIServer
from nvr import __version__
from nvr.config import Config, data_dir
from nvr.hub import Hub
from nvr.server import create_app


class LocalServer(ThreadedWSGIServer):
    # Windows SO_REUSEADDR can let another instance claim the same listening
    # port. Reserve it exclusively so startup can report conflicts reliably.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def run(argv=None):
    parser = argparse.ArgumentParser(description="MiniNVR2 农场轻量录像机")
    parser.add_argument("--windowed", action="store_true", help="兼容打包入口")
    parser.add_argument("--no-tray", action="store_true", help="不显示托盘")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开网页")
    parser.add_argument("--host", help="覆盖监听地址，默认仅本机")
    parser.add_argument("--port", type=int, help="覆盖监听端口")
    parser.add_argument("--config", help="指定配置文件")
    parser.add_argument("--data-dir", help="指定独立数据目录（含日志和录像）")
    args = parser.parse_args(argv)
    if args.data_dir:
        os.environ["NVR_DATA_DIR"] = os.path.abspath(args.data_dir)
    cfg = Config(path=os.path.abspath(args.config) if args.config else None)
    host = args.host or cfg.get("server", "host", default="127.0.0.1")
    port = args.port if args.port is not None else int(cfg.get("server", "port", default=8800))
    if not 1 <= port <= 65535:
        parser.error("端口必须是 1–65535")
    if host not in ("127.0.0.1", "localhost", "::1") and not cfg.get("server", "token"):
        parser.error("启用局域网监听前，请先在本机设置访问口令。")
    cfg.set("server", "host", host, save=False)
    cfg.set("server", "port", port, save=False)
    cfg.save()
    stopped = threading.Event()
    icon = None
    hub = Hub(cfg)
    app = create_app(cfg, hub, on_quit=stopped.set)
    try:
        server = LocalServer(host, port, app)
    except (OSError, SystemExit):
        hub.log("[系统] 无法启动 Web 服务，请检查地址和端口是否已占用。")
        hub.stop_all()
        return 1
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    address = "127.0.0.1" if host in ("0.0.0.0", "localhost") else host
    if ":" in address:
        address = "[" + address + "]"
    url = "http://%s:%d" % (address, port)
    hub.log("[系统] v%s 已启动：%s  数据目录：%s" % (__version__, url, data_dir()))
    hub.request_sync()
    if not args.no_browser:
        webbrowser.open(url)
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stopped.set())
    try:
        if not args.no_tray:
            try:
                import pystray
                from PIL import Image, ImageDraw
                img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)
                draw.ellipse((6, 6, 58, 58), fill=(200, 118, 31, 255))
                draw.ellipse((20, 20, 44, 44), fill=(250, 245, 235, 255))
                icon = pystray.Icon("MiniNVR2", img, "MiniNVR2 农场录像机", menu=pystray.Menu(
                    pystray.MenuItem("打开网页", lambda *_: webbrowser.open(url)),
                    pystray.MenuItem("退出", lambda *_: stopped.set())))
                def stop_icon():
                    stopped.wait()
                    if icon:
                        icon.stop()
                threading.Thread(target=stop_icon, daemon=True).start()
                icon.run()
            except Exception as exc:
                hub.log("[托盘] 不可用，仍可从网页退出：%s" % exc)
                stopped.wait()
        else:
            stopped.wait()
    except KeyboardInterrupt:
        stopped.set()
    finally:
        stopped.set()
        if icon:
            icon.stop()
        server.shutdown()
        server.server_close()
        hub.stop_all()
        worker.join(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(run())
