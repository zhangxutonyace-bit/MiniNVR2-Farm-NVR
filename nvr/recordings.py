"""Bounded, local-only lookup of finished event recordings."""
import datetime
import heapq
import os
import re
import stat

from .config import records_root

_CAMERA_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_EVENT_NAME = re.compile(r"(?:[01]\d|2[0-3])[0-5]\d[0-5]\d(?:_\d{1,9})?\.mp4")


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        datetime.date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _cameras(cfg):
    return {c["id"]: c for c in (cfg.cameras or [])
            if c.get("enabled", True) and isinstance(c.get("id"), str)
            and _CAMERA_ID.fullmatch(c["id"])}


def _busy_paths(hub):
    # A failed state lookup must not make a possibly active file sendable.
    from .camera import remuxing_paths
    try:
        paths = set(remuxing_paths())
        if hub is not None:
            paths.update(hub.recording_paths())
        return {os.path.normcase(os.path.realpath(p)) for p in paths}
    except Exception:
        return None


def _event_stat(root, cid, day, name, busy):
    if busy is None or not _CAMERA_ID.fullmatch(cid) or not _day(day) \
            or not _EVENT_NAME.fullmatch(name):
        return None
    path = os.path.abspath(os.path.join(root, cid, day, "event", name))
    # Reject child symlinks/junctions as well as traversal outside the root.
    real = os.path.realpath(path)
    if os.path.normcase(real) != os.path.normcase(path):
        return None
    if os.path.commonpath((root, real)) != root or os.path.normcase(real) in busy:
        return None
    try:
        info = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_size < 1024:
            return None
        return path, info
    except OSError:
        return None


def list_events(cfg, hub=None, day=None, camera_id=None, limit=500):
    """Return (items, truncated), newest first, using at most limit rows of memory."""
    day = day or datetime.date.today().isoformat()
    if not _day(day):
        return [], False
    cameras = _cameras(cfg)
    if camera_id is not None:
        cameras = {camera_id: cameras[camera_id]} if camera_id in cameras else {}
    limit = max(1, min(500, int(limit)))
    root = os.path.realpath(os.path.abspath(records_root(cfg)))
    busy = _busy_paths(hub)
    if busy is None:
        return [], False
    chosen = []
    count = 0
    for cid, camera in cameras.items():
        directory = os.path.join(root, cid, day, "event")
        if os.path.normcase(os.path.realpath(directory)) != os.path.normcase(directory):
            continue
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    # A recorder may have started since the directory scan began.
                    busy = _busy_paths(hub)
                    if busy is None:
                        return [], False
                    found = _event_stat(root, cid, day, entry.name, busy)
                    if not found:
                        continue
                    path, info = found
                    item = {"cam": cid, "cam_name": camera.get("name") or cid,
                            "date": day, "type": "event", "name": entry.name,
                            "path": path, "size": info.st_size,
                            "mtime": info.st_mtime, "mtime_ns": info.st_mtime_ns}
                    # Filename timestamps survive remux/copy mtime changes.
                    stamp = entry.name[:-4].split("_", 1)
                    key = (day, stamp[0], int(stamp[1]) if len(stamp) > 1 else 0, cid)
                    count += 1
                    row = (key, count, item)
                    if len(chosen) < limit:
                        heapq.heappush(chosen, row)
                    elif key > chosen[0][0]:
                        heapq.heapreplace(chosen, row)
        except OSError:
            continue
    busy = _busy_paths(hub)
    if busy is None:
        return [], False
    return [row[2] for row in sorted(chosen, reverse=True)
            if os.path.normcase(row[2]["path"]) not in busy], count > limit


def validate_event(cfg, item, hub=None):
    """Revalidate a cached row immediately before sending; never trust its path."""
    cid, day, name = item.get("cam"), item.get("date"), item.get("name")
    if not all(isinstance(x, str) for x in (cid, day, name)) or cid not in _cameras(cfg):
        return ""
    root = os.path.realpath(os.path.abspath(records_root(cfg)))
    found = _event_stat(root, cid, day, name, _busy_paths(hub))
    if not found:
        return ""
    path, info = found
    if info.st_size != item.get("size") or info.st_mtime_ns != item.get("mtime_ns"):
        return ""
    return path
