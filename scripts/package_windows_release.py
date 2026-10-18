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
from datetime import datetime, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
NOTICE_NAME = re.compile(r"^(LICENSE|COPYING|NOTICE|AUTHORS)(?:[.\-_]|$)", re.I)
PRIVATE_PATH = re.compile(r"(?:[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]|/Users/|/home/)")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
    parser.add_argument("--version", default="2.2.1")
    parser.add_argument("--output-dir", type=Path, default=REPO.parent / "deliverables")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", args.version):
        parser.error("version must have three numeric components")
    if sys.version_info[:3] != (3, 12, 10) or struct.calcsize("P") != 8:
        raise ValueError("Use the verified Python 3.12.10 x64 build environment")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    stem = "MiniNVR2-v" + args.version + "-windows-x64"
    executable = REPO / "dist" / "MiniNVR2.exe"
    if not is_x64_pe(executable):
        raise ValueError("Existing executable is not a Windows x64 PE file")
    original_hash = sha256(executable)
    build_record = json.loads((REPO / "validation-local" / "build-artifact.json").read_text(encoding="utf-8-sig"))
    if original_hash != build_record["sha256"] or executable.stat().st_size != build_record["bytes"]:
        raise ValueError("Executable differs from the previously verified build")
    startup = json.loads((REPO / "validation-local" / "startup-exe.json").read_text(encoding="utf-8-sig"))
    if startup.get("version") != args.version or any(value != "PASS" for key, value in startup.items() if key != "version"):
        raise ValueError("Missing matching successful EXE startup evidence")

    staging = Path(tempfile.mkdtemp(prefix=stem + "-staging-", dir=output))
    root = staging / stem
    root.mkdir()
    checked_copy(executable, root / "MiniNVR2.exe")
    checked_copy(REPO / "docs" / "WINDOWS_DOWNLOAD.md", root / "先读我-使用说明.txt")
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
    manifest = {
        "application": "MiniNVR2", "version": args.version, "target": "Windows 10/11 x64",
        "source_commit": commit_id(),
        "executable": {"name": "MiniNVR2.exe", "bytes": executable.stat().st_size, "sha256": original_hash},
        "python": {"version": ".".join(map(str, sys.version_info[:3])), "architecture": "x64"},
        "packager": {"name": "PyInstaller", "version": metadata.version("PyInstaller")},
        "runtime_components": components,
        "verification": {"existing_local_exe_startup": startup, "other_windows_machines": "NOT_VERIFIED", "real_camera_and_message_delivery": "NOT_VERIFIED"},
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
        "existing_exe_startup_evidence": "PASS", "new_feature_tests_run": False,
        "third_party_sources_doc_included": (root / "THIRD_PARTY_SOURCES.md").is_file(),
        "ffmpeg_build_configuration_included": (root / "THIRD_PARTY_NOTICES" / "FFmpeg" / "BUILD_CONFIGURATION.txt").is_file(),
        "staging_directory": staging.name,
    }
    write_json(REPO / "validation-local" / "windows-package.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
