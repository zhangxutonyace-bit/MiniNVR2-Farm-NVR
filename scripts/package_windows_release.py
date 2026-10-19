"""Package the existing Windows executable; never rebuild or launch it.

Run with the project's build environment so installed distribution licenses
match the executable. Only explicit release inputs are copied. Each run keeps
its own staging directory and never recursively removes an existing directory.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
NOTICE_NAME = re.compile(r"^(LICENSE|COPYING|NOTICE|AUTHORS)(?:[.\-_]|$)", re.I)
PRIVATE_PATH = re.compile(r"(?:[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]|/Users/|/home/)")
STARTUP_CHECKS = ("http_startup", "port_conflict", "ffmpeg_preview", "web_quit", "lan_requires_token")
EVENT_CHECKS = ("http_startup", "synthetic_loopback_delivery", "encrypted_event_integrity", "web_quit")
PREVIEW_CHECKS = ("http_startup", "management_anonymous_protection", "synthetic_preview_command",
                  "preview_assets", "camera_scoped_capability", "lan_live_jpeg",
                  "missing_and_wrong_camera_denied", "preview_routes_isolated", "web_quit",
                  "preview_listener_closed")
REMOTE_PHONE_CHECKS = ("phone_on_final_exe_verified", "phone_direct_click_verified",
                       "wechat_embedded_browser_verified", "phone_mobile_data_test_flow",
                       "synthetic_only")
EVENT_USER_CHECKS = ("box_follows_person_user_confirmed", "computer_playback_user_confirmed",
                     "wechat_video_playback_user_confirmed")
EVENT_USER_SCOPE = "current computer, camera, WeChat account and fresh event only"
OBJECT_USER_CHECKS = ("object_box_label_following_user_confirmed", "computer_playback_user_confirmed",
                      "wechat_list_user_confirmed", "wechat_video_playback_user_confirmed")


def validate_bundled_person_assets(executable: Path) -> dict:
    """Inspect bytes in the onefile archive without starting the executable."""
    from PyInstaller.archive.readers import CArchiveReader
    from nvr.person_localizer import MODEL_NAME, MODEL_BYTES, MODEL_SHA256
    archive = CArchiveReader(str(executable))
    names = {name.replace("\\", "/"): name for name in archive.toc}
    expected = {"models/" + MODEL_NAME: (MODEL_BYTES, MODEL_SHA256)}
    for name in ("LICENSE", "SOURCE.json"):
        source = REPO / "third_party" / "NanoDet" / name
        expected["third_party/NanoDet/" + name] = (source.stat().st_size, sha256(source))
    for relative, (size, digest) in expected.items():
        if relative not in names:
            raise ValueError("Executable is missing approved person asset: " + relative)
        data = archive.extract(names[relative])
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Executable person asset differs from approved bytes: " + relative)
    return {"name": MODEL_NAME, "bytes": MODEL_BYTES, "sha256": MODEL_SHA256,
            "scope": "Offline CPU person annotation only", "notices_in_exe": True}


def validate_bundled_algorithm_assets(executable: Path) -> dict:
    """Inspect fixed catalog, adapters and notices without executing the EXE."""
    from PyInstaller.archive.readers import CArchiveReader
    from nvr.algorithm_catalog import catalog_document, verify_catalog_asset
    from nvr.event_labels import FONT_NAME, FONT_BYTES, FONT_SHA256, verify_font
    verify_catalog_asset()
    verify_font()
    archive = CArchiveReader(str(executable))
    names = {name.replace("\\", "/"): name for name in archive.toc}
    for relative in ("third_party/AlgorithmCatalog/catalog.json", "third_party/YOLOX/LICENSE",
                     "third_party/YOLOX/SOURCE.json", "fonts/" + FONT_NAME,
                     "third_party/LabelFont/OFL.txt", "third_party/LabelFont/NOTICE.txt",
                     "third_party/LabelFont/SOURCE.json"):
        if relative not in names or archive.extract(names[relative]) != (REPO / relative).read_bytes():
            raise ValueError("Executable algorithm asset differs from approved source: " + relative)
    pyz = archive.open_embedded_archive("PYZ.pyz")
    modules = ("nvr.algorithm_catalog", "nvr.algorithm_types", "nvr.object_models",
               "nvr.model_store", "nvr.algorithm_runtime", "nvr.event_labels")
    if any(name not in pyz.toc for name in modules):
        raise ValueError("Executable is missing an algorithm-store adapter")
    return {"catalog": catalog_document(), "notices_in_exe": True,
            "chinese_label_font": {"name": FONT_NAME, "bytes": FONT_BYTES, "sha256": FONT_SHA256,
                                    "license": "OFL-1.1", "system_font_required": False},
            "scope": "Local CPU selected-class events; YOLOX weights download only after explicit action"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_record(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Evidence must be an ordinary JSON file: " + path.name)

    def unique_object(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate evidence field: " + key)
            result[key] = value
        return result

    value = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError("Evidence must contain a JSON object: " + path.name)
    return value


def validate_build(record: dict, version: str, digest: str, size: int) -> None:
    preview_release = tuple(map(int, version.split("."))) >= (2, 2, 3)
    if record.get("version", None if preview_release else version) != version:
        raise ValueError("Build evidence version does not match the release")
    for hash_key, size_key in (("exe_sha256", "exe_bytes"), ("sha256", "bytes")):
        if hash_key in record or size_key in record:
            if record.get(hash_key) != digest or type(record.get(size_key)) is not int \
                    or record[size_key] != size:
                raise ValueError("Executable differs from the previously verified build")
    required = ("exe_sha256", "exe_bytes") if preview_release else ("sha256", "bytes")
    if not all(key in record for key in required):
        raise ValueError("Build evidence is missing executable digest or size")
    if preview_release and any(record.get(key) is not True for key in
                               ("mobile_and_status_assets_match_source", "preview_module_included")):
        raise ValueError("Build evidence does not verify the preview module and source assets")


def validate_automatic(record: dict, version: str, checks: tuple[str, ...], label: str,
                       expected_digest: str, expected_size: int) -> dict:
    preview_release = tuple(map(int, version.split("."))) >= (2, 2, 3)
    identity_fields = ("exe_sha256", "exe_bytes")
    if preview_release or any(key in record for key in identity_fields):
        if record.get("exe_sha256") != expected_digest or type(record.get("exe_bytes")) is not int \
                or record["exe_bytes"] != expected_size:
            raise ValueError("EXE " + label + " evidence does not match this executable digest and size")
    if record.get("version") != version or any(record.get(key) != "PASS" for key in checks) \
            or any(value != "PASS" for key, value in record.items()
                   if key not in ("version", *identity_fields)):
        raise ValueError("Missing matching successful EXE " + label + " evidence")
    # Publish only the known checks, even if a future local report has more fields.
    public = {"version": version, **{key: "PASS" for key in checks}}
    if all(key in record for key in identity_fields):
        public.update(exe_sha256=expected_digest, exe_bytes=expected_size)
    return public


def validate_phone(record: dict, version: str, digest: str, size: int | None = None) -> dict:
    if record.get("version") != version or record.get("exe_sha256") != digest \
            or record.get("phone_on_final_exe_verified") is not True:
        raise ValueError("Missing user-confirmed phone preview evidence for this exact EXE")
    verified_date = record.get("verified_date")
    if not isinstance(verified_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", verified_date):
        raise ValueError("Phone preview evidence requires an ISO verification date")
    try:
        date.fromisoformat(verified_date)
    except ValueError as error:
        raise ValueError("Phone preview evidence has an invalid verification date") from error
    command = record.get("verified_command")
    if tuple(map(int, version.split("."))) >= (2, 3, 0):
        if record.get("mode") != "installed-windows-exe" \
                or type(record.get("exe_bytes")) is not int or record["exe_bytes"] != size \
                or any(record.get(key) is not True for key in REMOTE_PHONE_CHECKS):
            raise ValueError("Remote preview requires mobile-data, direct-click WeChat acceptance for this installed EXE")
        if not isinstance(command, str) or len(command) > 128 or not command.isprintable() \
                or not (re.fullmatch(r"预览(?:[1-9][0-9]*号)?", command)
                        or (command.startswith("预览 ") and len(command) > 3
                            and command[3:] == command[3:].strip())):
            raise ValueError("Remote preview evidence requires a supported preview command")
        return {"status": "PASS_USER_CONFIRMED", "verified_date": verified_date, "command": command,
                "mode": "installed-windows-exe", "synthetic_only": True,
                "phone_mobile_data_test_flow": True, "phone_direct_click_verified": True,
                "wechat_embedded_browser_verified": True,
                "scope": "One user-confirmed phone on mobile data; direct click in WeChat's embedded browser; continuously updating synthetic preview from this release EXE"}
    if not isinstance(command, str) or not re.fullmatch(r"预览(?:[1-9][0-9]*号)?", command):
        raise ValueError("Phone preview evidence requires a supported numbered preview command")
    return {"status": "PASS_USER_CONFIRMED", "verified_date": verified_date, "command": command,
            "scope": "One user-confirmed phone on the same LAN; continuously updating preview from this release EXE"}


def validate_event_user(record: dict, version: str, digest: str, size: int) -> dict:
    """Bind fresh, local-camera event acceptance to the exact released bytes."""
    if record.get("artifact_type") != "Windows executable" \
            or record.get("version") != version or record.get("exe_sha256") != digest \
            or type(record.get("exe_bytes")) is not int or record["exe_bytes"] != size \
            or any(record.get(key) is not True for key in EVENT_USER_CHECKS):
        raise ValueError("Missing user-confirmed person following and playback for this exact EXE")
    revision = record.get("implementation_commit")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Event acceptance requires its implementation commit")
    if record.get("camera_source_type") != "local-camera" or record.get("scope") != EVENT_USER_SCOPE:
        raise ValueError("Event acceptance is limited to one local built-in or USB camera and fresh event")
    recorded = record.get("recorded_at_utc")
    if not isinstance(recorded, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", recorded):
        raise ValueError("Event acceptance requires an ISO timestamp with timezone")
    try:
        verified = datetime.fromisoformat(recorded.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Event acceptance has an invalid timestamp") from error
    if verified.utcoffset() != timezone.utc.utcoffset(verified):
        raise ValueError("Event acceptance recorded_at_utc must use UTC")
    # Never copy free text, account identifiers, question references, or private
    # metadata from a local receipt into a public release manifest.
    return {"status": "PASS_USER_CONFIRMED", "verified_date": verified.date().isoformat(),
            "implementation_commit": revision, "exe_sha256": digest, "exe_bytes": size,
            "camera_source_type": "local-camera", **{key: True for key in EVENT_USER_CHECKS},
            "scope": "One built-in or USB camera on the current computer, one fresh event, and the current WeChat account; no RTSP, other-device, or long-running acceptance"}


def validate_object_user(record: dict, version: str, digest: str, size: int) -> dict:
    """Keep V3 object acceptance separate from legacy person-following proof."""
    from nvr.algorithm_catalog import validate_options
    targets = record.get("targets")
    confidence = record.get("confidence")
    options = validate_options({"model_id": record.get("model_id"), "targets": targets,
                                "confidence": confidence})
    if record.get("artifact_type") != "Windows executable" or record.get("version") != version \
            or record.get("exe_sha256") != digest or type(record.get("exe_bytes")) is not int \
            or record["exe_bytes"] != size or any(record.get(key) is not True for key in OBJECT_USER_CHECKS):
        raise ValueError("Missing user-confirmed object following and playback for this exact EXE")
    if record.get("camera_source_type") != "local-camera" or record.get("scope") != EVENT_USER_SCOPE \
            or len(set(targets)) != len(targets) \
            or record.get("object_class") not in targets:
        raise ValueError("Object acceptance requires a supported model, selected class and local-camera scope")
    revision = record.get("implementation_commit")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Object acceptance requires its implementation commit")
    recorded = record.get("recorded_at_utc")
    if not isinstance(recorded, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", recorded):
        raise ValueError("Object acceptance requires an ISO timestamp with timezone")
    try:
        verified = datetime.fromisoformat(recorded.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Object acceptance has an invalid timestamp") from error
    if verified.utcoffset() != timezone.utc.utcoffset(verified):
        raise ValueError("Object acceptance recorded_at_utc must use UTC")
    return {"status": "PASS_USER_CONFIRMED", "verified_date": verified.date().isoformat(),
            "implementation_commit": revision, "exe_sha256": digest, "exe_bytes": size,
            "camera_source_type": "local-camera", "model_id": record["model_id"],
            "confidence": options["confidence"], "targets": options["targets"], "object_class": record["object_class"],
            **{key: True for key in OBJECT_USER_CHECKS},
            "scope": "Current computer, local camera, selected model and tested object in a fresh event only; no all-class or all-scene accuracy claim",
            "phone_object_label": "NOT_VERIFIED"}


def write_windows_guide(source: Path, target: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError("Release guide must be an ordinary file: " + source.name)
    guide = source.read_text(encoding="utf-8-sig")
    # BOM and CRLF keep standalone Chinese guides readable in Notepad.
    target.write_bytes(b"\xef\xbb\xbf" + guide.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))


def copy_user_guides(root: Path, version: str, include_preview: bool = False) -> list[str]:
    guides = [("WINDOWS_DOWNLOAD.md", "先读我-使用说明.txt"),
              ("WECHAT_QUICK_START.txt", "微信ClawBot-操作步骤.txt")]
    if tuple(map(int, version.split("."))) >= (2, 2, 3) or include_preview:
        guides.append(("LAN_PREVIEW_QUICK_START.txt", "微信局域网预览-操作步骤.txt"))
    if tuple(map(int, version.split("."))) >= (2, 3, 0):
        guides.append(("REMOTE_PREVIEW.md", "微信远程预览-操作步骤.txt"))
    if tuple(map(int, version.split("."))) >= (2, 4, 0):
        guides.append(("LOCAL_CAMERA_QUICK_START.md", "电脑内置USB摄像头-操作步骤.txt"))
    if tuple(map(int, version.split("."))) >= (3, 0, 0):
        guides.append(("ALGORITHM_STORE_QUICK_START.md", "算法商城-操作步骤.txt"))
    for source, target in guides:
        write_windows_guide(REPO / "docs" / source, root / target)
    return [target for _, target in guides]


def checked_copy(source: Path, target: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError("Release input must be an ordinary file: " + source.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def is_x64_pe(path: Path) -> bool:
    with path.open("rb") as handle:
        if handle.read(2) != b"MZ":
            return False
        handle.seek(0x3C)
        offset = struct.unpack("<I", handle.read(4))[0]
        handle.seek(offset)
        return handle.read(6) == b"PE\0\0\x64\x86"


def runtime_requirements() -> list[tuple[str, str]]:
    result = []
    for line in (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)", line)
        if not match:
            raise ValueError("Expected exact runtime version pins")
        result.append(match.groups())
    return result


def collect_notices(root: Path, release_inputs: Path) -> list[dict]:
    components = []
    for name, pinned_version in runtime_requirements():
        dist = metadata.distribution(name)
        if dist.version != pinned_version:
            raise ValueError("Installed runtime version differs from requirements: " + name)
        files = []
        for relative in sorted(dist.files or [], key=str):
            # Metadata inventories also include modules and scripts: only use
            # the distribution's own license directory/files here.
            parts = Path(str(relative)).parts
            if not parts or not parts[0].endswith(".dist-info"):
                continue
            if not NOTICE_NAME.match(parts[-1]):
                continue
            source = Path(dist.locate_file(relative))
            destination = root / "THIRD_PARTY_NOTICES" / (name + "-" + dist.version) / Path(*parts[1:])
            checked_copy(source, destination)
            files.append(destination.relative_to(root).as_posix())
        if not files:
            raise ValueError("No distribution license found for " + name)
        components.append({"name": name, "version": dist.version, "notice_files": files})

    site_packages = Path(metadata.distribution("numpy").locate_file(""))
    # License files that accompany bundled native/runtime assets rather than
    # living solely in .dist-info metadata.
    extras = [
        (site_packages / "cv2" / "LICENSE.txt", "OpenCV-runtime/LICENSE.txt"),
        (site_packages / "cv2" / "LICENSE-3RD-PARTY.txt", "OpenCV-runtime/LICENSE-3RD-PARTY.txt"),
        (site_packages / "numpy" / "ma" / "LICENSE", "NumPy-runtime/ma-LICENSE"),
        (site_packages / "numpy" / "random" / "LICENSE.md", "NumPy-runtime/random-LICENSE.md"),
        (site_packages / "numpy" / "_core" / "include" / "numpy" / "random" / "LICENSE.txt", "NumPy-runtime/random-include-LICENSE.txt"),
        (site_packages / "werkzeug" / "debug" / "shared" / "ICON_LICENSE.md", "Werkzeug-runtime/ICON_LICENSE.md"),
        (Path(sys.base_prefix) / "LICENSE.txt", "Python-3.12/LICENSE.txt"),
    ]
    pyinstaller = metadata.distribution("PyInstaller")
    extras.append((Path(pyinstaller.locate_file("pyinstaller-" + pyinstaller.version + ".dist-info/licenses/COPYING.txt")), "PyInstaller-bootloader/COPYING.txt"))
    extras.extend([
        (release_inputs / "LICENSE", "FFmpeg/LICENSE-GPL-v3.txt"),
        (release_inputs / "README.txt", "FFmpeg/README-gyan-build.txt"),
        (release_inputs / "OPENSSL_4_0_3_LICENSE.txt", "OpenSSL-4.0.3/LICENSE.txt"),
    ])
    for source, relative in extras:
        checked_copy(source, root / "THIRD_PARTY_NOTICES" / relative)

    # These two files are prepared by the release-source inventory step. A
    # candidate may be generated before that step finishes; the report records
    # this so it cannot be mistaken for the final publication package.
    supplemental = [
        (REPO / "docs" / "THIRD_PARTY_SOURCES.md", "THIRD_PARTY_SOURCES.md"),
        (release_inputs / "FFMPEG_BUILD_CONFIGURATION.txt", "THIRD_PARTY_NOTICES/FFmpeg/BUILD_CONFIGURATION.txt"),
    ]
    for source, relative in supplemental:
        if source.is_file():
            checked_copy(source, root / relative)
    person_files = []
    for name in ("LICENSE", "SOURCE.json"):
        destination = root / "THIRD_PARTY_NOTICES" / "NanoDet" / name
        checked_copy(REPO / "third_party" / "NanoDet" / name, destination)
        person_files.append(destination.relative_to(root).as_posix())
    components.append({"name": "OpenCV Zoo NanoDet", "version": "2022nov",
                       "license": "Apache-2.0", "notice_files": person_files})
    if (REPO / "third_party" / "YOLOX").is_dir():
        files = []
        for name in ("LICENSE", "SOURCE.json"):
            destination = root / "THIRD_PARTY_NOTICES" / "YOLOX" / name
            checked_copy(REPO / "third_party" / "YOLOX" / name, destination)
            files.append(destination.relative_to(root).as_posix())
        checked_copy(REPO / "third_party" / "AlgorithmCatalog" / "catalog.json",
                     root / "THIRD_PARTY_NOTICES" / "YOLOX" / "catalog.json")
        components.append({"name": "YOLOX model adapters and curated catalog", "version": "0.1.1rc0 / Zoo 2022nov",
                           "license": "Apache-2.0", "notice_files": files})
    if (REPO / "third_party" / "LabelFont").is_dir():
        files = []
        for name in ("OFL.txt", "NOTICE.txt", "SOURCE.json"):
            destination = root / "THIRD_PARTY_NOTICES" / "LabelFont" / name
            checked_copy(REPO / "third_party" / "LabelFont" / name, destination)
            files.append(destination.relative_to(root).as_posix())
        components.append({"name": "MiniNVR Label Sans SC (Noto Sans CJK SC subset)",
                           "version": "2.004 subset", "license": "OFL-1.1", "notice_files": files})
    return components


def commit_id() -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO,
                            check=True, capture_output=True, text=True)
    value = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("Unexpected Git revision")
    return value


def validate_package_tree(root: Path) -> list[Path]:
    files = sorted((path for path in root.rglob("*") if path.is_file()), key=lambda path: path.relative_to(root).as_posix())
    for path in files:
        relative = path.relative_to(root)
        if path.is_symlink() or any(part.lower() in {"data", "data-demo", "records", "logs", ".venv"} for part in relative.parts):
            raise ValueError("Unexpected package member: " + relative.as_posix())
        if path.name.lower() in {"config.json", "nvr.log", ".env"}:
            raise ValueError("Private runtime state must not be packaged")
        if path.suffix.lower() in {".mp4", ".avi", ".mkv", ".log", ".pem", ".key"}:
            raise ValueError("Unexpected media, log, or key material in package")
        if path.suffix.lower() != ".exe":
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            if PRIVATE_PATH.search(text):
                raise ValueError("Private absolute path in release text: " + relative.as_posix())
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="2.2.2")
    parser.add_argument("--output-dir", type=Path, default=REPO.parent / "deliverables")
    parser.add_argument("--executable", type=Path, default=REPO / "dist" / "MiniNVR2.exe",
                        help="Exact previously verified executable; never rebuilds or modifies it")
    parser.add_argument("--build-record", type=Path, default=REPO / "validation-local" / "build-artifact.json")
    parser.add_argument("--startup-record", type=Path, default=REPO / "validation-local" / "startup-exe.json")
    parser.add_argument("--event-record", type=Path,
                        help="Defaults to validation-local/event-exe-vVERSION.json")
    parser.add_argument("--preview-record", type=Path, help="Required for version 2.2.3 and later")
    parser.add_argument("--phone-record", type=Path, help="User-confirmed preview receipt for the exact EXE")
    parser.add_argument("--event-user-record", type=Path,
                        help="Required since 2.4.0: fresh local-camera person following and playback receipt")
    parser.add_argument("--object-user-record", type=Path,
                        help="Required since 3.0.0: selected-object following, labels and playback receipt")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", args.version):
        parser.error("version must have three numeric components")
    if sys.version_info[:3] != (3, 12, 10) or struct.calcsize("P") != 8:
        raise ValueError("Use the verified Python 3.12.10 x64 build environment")
    preview_release = tuple(map(int, args.version.split("."))) >= (2, 2, 3)
    remote_release = tuple(map(int, args.version.split("."))) >= (2, 3, 0)
    local_camera_release = tuple(map(int, args.version.split("."))) >= (2, 4, 0)
    algorithm_release = tuple(map(int, args.version.split("."))) >= (3, 0, 0)
    if preview_release and (args.preview_record is None or
                            (not local_camera_release and args.phone_record is None)):
        raise ValueError("Preview releases require --preview-record and --phone-record")
    if local_camera_release and not algorithm_release and args.event_user_record is None:
        raise ValueError("Local-camera releases require --event-user-record for the exact EXE")
    if algorithm_release and args.object_user_record is None:
        raise ValueError("Algorithm-store releases require --object-user-record for the exact EXE")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    stem = "MiniNVR2-v" + args.version + "-windows-x64"
    if args.executable.is_symlink() or not args.executable.is_file():
        raise ValueError("Executable must be an ordinary file")
    executable = args.executable.resolve()
    if not is_x64_pe(executable):
        raise ValueError("Existing executable is not a Windows x64 PE file")
    original_hash = sha256(executable)
    original_size = executable.stat().st_size
    person_assets = validate_bundled_person_assets(executable)
    algorithm_assets = (validate_bundled_algorithm_assets(executable)
                        if tuple(map(int, args.version.split("."))) >= (3, 0, 0) else None)
    validate_build(read_record(args.build_record), args.version, original_hash, original_size)
    startup_checks = STARTUP_CHECKS + (("remote_preview_default_off",) if remote_release else ())
    startup = validate_automatic(read_record(args.startup_record), args.version, startup_checks, "startup",
                                 original_hash, original_size)
    event_path = args.event_record or REPO / "validation-local" / ("event-exe-v" + args.version + ".json")
    events = validate_automatic(read_record(event_path), args.version, EVENT_CHECKS, "ClawBot event simulation",
                                original_hash, original_size)
    preview = {"status": "NOT_VERIFIED"}
    if args.preview_record is not None:
        preview = validate_automatic(read_record(args.preview_record), args.version, PREVIEW_CHECKS, "LAN preview",
                                     original_hash, original_size)
    phone_preview = {"status": "NOT_VERIFIED"}
    if local_camera_release:
        phone_preview = {"status": "NOT_RETESTED",
                         "scope": "Remote phone preview was not retested on this EXE; historical v2.3.0 acceptance is documented in REMOTE_PREVIEW.md"}
    if args.phone_record is not None:
        phone_preview = validate_phone(read_record(args.phone_record), args.version, original_hash, original_size)
    event_user = None
    if args.event_user_record is not None:
        event_user = validate_event_user(read_record(args.event_user_record), args.version,
                                         original_hash, original_size)
    object_user = None
    if args.object_user_record is not None:
        object_user = validate_object_user(read_record(args.object_user_record), args.version,
                                           original_hash, original_size)

    staging = Path(tempfile.mkdtemp(prefix=stem + "-staging-", dir=output))
    root = staging / stem
    root.mkdir()
    checked_copy(executable, root / "MiniNVR2.exe")
    guides = copy_user_guides(root, args.version, include_preview=args.preview_record is not None)
    components = collect_notices(root, REPO / "validation-local" / "release-inputs")
    (root / "THIRD_PARTY_NOTICES" / "README.txt").write_text(
        "Third-party notices for MiniNVR2 " + args.version + " Windows x64\n\n"
        "License files are copied unchanged from the exact installed runtime distributions.\n"
        "OpenCV, NumPy, Pillow and Python files include notices for their bundled libraries.\n"
        "Python 3.12's LICENSE includes its bundled third-party license notices.\n"
        "PyInstaller's COPYING includes the bootloader linking/embedding exception.\n"
        "pystray includes both COPYING (GPL) and COPYING.LGPL.\n"
        "FFmpeg/ contains the GPL v3 text and the supplier's original build README.\n"
        "See THIRD_PARTY_SOURCES.md and the separate third-party source release asset.\n"
        "MiniNVR2 source is available in the GitHub repository; no new project license is granted here.\n",
        encoding="utf-8")
    live_delivery = {"status": "NOT_VERIFIED"}
    live_path = REPO / "validation-local" / "live-wechat-test.json"
    if not preview_release and live_path.is_file():
        live = json.loads(live_path.read_text(encoding="utf-8-sig"))
        if live.get("tested_exe_sha256") == original_hash \
                and live.get("phone_playback") == "PASS_USER_CONFIRMED":
            # Whitelist public evidence; never copy account/configuration data.
            live_delivery = {"status": "PASS_USER_CONFIRMED",
                             "verified_at": live["verified_at"],
                             "command": live["verified_command"],
                             "synthetic_video_seconds": live["event"]["seconds"],
                             "scope": "One scanned account and handset; synthetic event video"}
    phone_verification_key = "real_phone_remote_preview" if remote_release else "real_phone_lan_preview"
    real_camera = "NOT_VERIFIED"
    following = {"status": "NOT_VERIFIED"}
    if event_user is not None:
        identity = {key: event_user[key] for key in
                    ("status", "verified_date", "implementation_commit", "exe_sha256", "exe_bytes")}
        real_camera = {**identity, "source_type": "local-camera",
                       "scope": "One built-in or USB camera on the current computer; fresh event recording and playback only; RTSP not verified"}
        following = {**identity, "scope": "Person box following on one fresh local-camera event; other scenes, occlusion, night conditions and long-running load not verified"}
        live_delivery = {**identity, "scope": "One current WeChat account received and played the fresh local-camera event video; other accounts or devices not verified"}
    object_following = {"status": "NOT_VERIFIED"}
    if object_user is not None:
        identity = {key: object_user[key] for key in
                    ("status", "verified_date", "implementation_commit", "exe_sha256", "exe_bytes")}
        object_following = object_user
        real_camera = {**identity, "source_type": "local-camera",
                       "scope": "Current computer and local camera; selected-object fresh event recording and playback only"}
        live_delivery = {**identity, "commands": ["今日事件", "播放第一条"],
                         "scope": "Current WeChat account received a list and playable event video; object label appearance on the phone was not explicitly confirmed"}
    manifest = {
        "application": "MiniNVR2", "version": args.version, "target": "Windows 10/11 x64",
        "source_commit": commit_id(),
        "executable": {"name": "MiniNVR2.exe", "bytes": executable.stat().st_size, "sha256": original_hash},
        "python": {"version": ".".join(map(str, sys.version_info[:3])), "architecture": "x64"},
        "packager": {"name": "PyInstaller", "version": metadata.version("PyInstaller")},
        "runtime_components": components,
        "annotation_model": person_assets,
        "algorithm_store": algorithm_assets,
        "user_guides": guides,
        "verification": {"existing_local_exe_startup": startup, "clawbot_event_simulation": events,
                         "lan_preview_simulation": preview, phone_verification_key: phone_preview,
                         "real_wechat_event_delivery": live_delivery,
                         "real_person_following": following,
                         "real_object_following": object_following,
                         "other_windows_machines": "NOT_VERIFIED", "real_camera": real_camera},
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    write_json(root / "RELEASE_MANIFEST.json", manifest)
    files = validate_package_tree(root)
    (root / "SHA256SUMS.txt").write_text("".join(sha256(path) + "  " + path.relative_to(root).as_posix() + "\n" for path in files), encoding="utf-8")
    files = validate_package_tree(root)
    candidate = staging / (stem + ".zip")
    with zipfile.ZipFile(candidate, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in files:
            archive.write(path, arcname=stem + "/" + path.relative_to(root).as_posix())
    with zipfile.ZipFile(candidate) as archive:
        corrupt = archive.testzip()
        if corrupt:
            raise ValueError("ZIP integrity check failed: " + corrupt)
        digest = hashlib.sha256(archive.read(stem + "/MiniNVR2.exe")).hexdigest()
        if digest != original_hash or sha256(executable) != original_hash:
            raise ValueError("Executable bytes changed during packaging")
        member_count = len(archive.infolist())
    final = output / (stem + ".zip")
    candidate.replace(final)
    zip_hash = sha256(final)
    checksum = output / "SHA256SUMS.txt"
    checksum.write_text(zip_hash + "  " + final.name + "\n", encoding="utf-8")
    report = {
        "version": args.version, "archive": final.name, "archive_bytes": final.stat().st_size,
        "archive_sha256": zip_hash, "checksum_file": checksum.name, "members": member_count,
        "exe_sha256": original_hash, "exe_bytes_unchanged": True,
        "zip_testzip": "PASS", "x64_pe": "PASS", "private_state_and_path_check": "PASS",
        "existing_exe_startup_evidence": "PASS", "existing_event_simulation_evidence": "PASS",
        "existing_preview_simulation_evidence": "PASS" if args.preview_record is not None else "NOT_VERIFIED",
        phone_verification_key: phone_preview,
        "real_person_following": following,
        "real_object_following": object_following,
        "real_camera": real_camera,
        "new_feature_tests_run": False,
        "wechat_quick_start_included": (root / "微信ClawBot-操作步骤.txt").is_file(),
        "lan_preview_quick_start_included": (root / "微信局域网预览-操作步骤.txt").is_file(),
        "remote_preview_quick_start_included": (root / "微信远程预览-操作步骤.txt").is_file(),
        "local_camera_quick_start_included": (root / "电脑内置USB摄像头-操作步骤.txt").is_file(),
        "third_party_sources_doc_included": (root / "THIRD_PARTY_SOURCES.md").is_file(),
        "ffmpeg_build_configuration_included": (root / "THIRD_PARTY_NOTICES" / "FFmpeg" / "BUILD_CONFIGURATION.txt").is_file(),
        "staging_directory": staging.name,
    }
    write_json(REPO / "validation-local" / "windows-package.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
