"""Cross-platform release validation for macOS and Linux.

Mirrors the checks of scripts/validate_release.ps1 (Windows chain stays
untouched) using only the Python standard library.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app_runtime import (  # noqa: E402
    APP_VERSION,
    FFMPEG_FILENAME,
    FFMPEG_SHA256_BY_PLATFORM,
    FFMPEG_VERSION,
    SUPPORTED_PLATFORM,
)

SYSTEM = platform.system()
ARCH = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
    platform.machine().lower(), platform.machine()
)
PLATFORM_TAG = {"Darwin": f"macOS-{ARCH}", "Linux": f"Linux-{ARCH}"}.get(SYSTEM)
README_NAME = f"README-{'macOS' if SYSTEM == 'Darwin' else 'Linux'}.txt"


def fail(message: str) -> None:
    raise SystemExit(f"validate_release.py: {message}")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def reset_known_directory(path: Path) -> None:
    if REPO_ROOT not in path.resolve().parents:
        fail(f"Refusing to reset a directory outside the repository: {path}")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def safe_extract_zip(archive: zipfile.ZipFile, destination: Path) -> None:
    """Extract with a zip-slip guard and restore recorded Unix permission bits."""
    root = destination.resolve()
    for info in archive.infolist():
        try:
            target = (root / info.filename).resolve()
            inside = os.path.commonpath([root, target]) == os.fspath(root)
        except ValueError:
            inside = False
        if not inside:
            fail(f"Unsafe member path in portable zip: {info.filename}")
    archive.extractall(destination)
    for info in archive.infolist():
        mode = (info.external_attr >> 16) & 0o777
        target = destination / info.filename
        if mode and target.is_file():
            target.chmod(mode)


def verify_checksums(delivery_root: Path, portable_zip: Path, source_archive: Path) -> None:
    checksum_file = delivery_root / "SHA256SUMS.txt"
    if not checksum_file.is_file():
        fail(f"Release delivery file is missing: {checksum_file}")
    expected_names = {portable_zip.name, source_archive.name}
    recorded_names = set()
    for line in checksum_file.read_text(encoding="ascii").splitlines():
        if not line.strip():
            continue
        match = re.match(r"^([0-9A-Fa-f]{64})\s{2}([^\\/:]+)$", line)
        if not match:
            fail(f"Invalid SHA256SUMS.txt line: {line}")
        recorded_hash, recorded_name = match[1].upper(), match[2]
        if recorded_name in recorded_names:
            fail(f"Duplicate SHA256SUMS.txt entry: {recorded_name}")
        recorded_names.add(recorded_name)
        target = delivery_root / recorded_name
        if not target.is_file():
            fail(f"SHA256SUMS.txt references a missing artifact: {recorded_name}")
        if sha256_of(target) != recorded_hash:
            fail(f"Artifact SHA-256 mismatch for {recorded_name}.")
    if recorded_names != expected_names:
        fail("SHA256SUMS.txt entries do not match the required release artifacts.")


def verify_layout(package_root: Path) -> tuple[Path, Path]:
    """Check the portable package layout and return (root launcher, runtime FFmpeg)."""
    expected_root = {"_runtime", "ASRTools", "docs", README_NAME}
    actual_root = {entry.name for entry in package_root.iterdir()}
    if actual_root != expected_root:
        fail(f"Portable package root layout is invalid. Expected: {sorted(expected_root)}. Actual: {sorted(actual_root)}.")

    launcher = package_root / "ASRTools"
    runtime = package_root / "_runtime"
    if SYSTEM == "Darwin":
        runtime_exe = runtime / "ASRTools.app" / "Contents" / "MacOS" / "ASRTools"
        bundled_ffmpeg = runtime / "ASRTools.app" / "Contents" / "MacOS" / FFMPEG_FILENAME
    else:
        runtime_exe = runtime / "ASRTools"
        bundled_ffmpeg = runtime / FFMPEG_FILENAME

    required_files = [
        launcher,
        runtime_exe,
        bundled_ffmpeg,
        package_root / README_NAME,
        package_root / "docs" / "LICENSE.txt",
        package_root / "docs" / "THIRD_PARTY_NOTICES.txt",
        package_root / "docs" / "SOURCE_CODE.txt",
        package_root / "docs" / "licenses" / "python" / "PYTHON-PACKAGES.txt",
    ]
    for required in required_files:
        if not required.is_file():
            fail(f"Portable package file is missing: {required}")
    ffmpeg_licenses = list((package_root / "docs" / "licenses" / "ffmpeg").glob("*"))
    if not ffmpeg_licenses:
        fail("Portable package is missing FFmpeg license material under docs/licenses/ffmpeg.")
    return launcher, bundled_ffmpeg


def run_packaged_check(launcher: Path, arguments: list[str], report_path: Path) -> dict:
    sanitized_path = "/usr/bin:/bin" if SYSTEM != "Windows" else ""
    environment = {"PATH": sanitized_path, "HOME": str(Path.home())}
    process = subprocess.run(
        [str(launcher), *arguments],
        cwd=launcher.parent,
        env=environment,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0 or not report_path.is_file():
        detail = report_path.read_text(encoding="utf-8") if report_path.is_file() else (process.stderr or "No report generated.")
        fail(f"Packaged check failed (exit {process.returncode}): {detail}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("status") != "passed" or report.get("app_version") != APP_VERSION:
        fail(f"Packaged check report is invalid: {report_path}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate the ASRTools portable release (macOS/Linux).")
    parser.add_argument("--ffmpeg-path", required=True, type=Path,
                        help="Path to the verified FFmpeg binary for this platform.")
    arguments = parser.parse_args()

    if SYSTEM == "Windows":
        fail("Windows releases are validated by scripts/validate_release.ps1.")

    expected_hash = FFMPEG_SHA256_BY_PLATFORM.get(SYSTEM)
    if expected_hash is None:
        fail(f"FFmpeg SHA-256 for {SYSTEM} is not fixed in app_runtime.FFMPEG_SHA256_BY_PLATFORM yet.")
    expected_hash = expected_hash.upper()

    ffmpeg_path = arguments.ffmpeg_path.resolve()
    if not ffmpeg_path.is_file():
        fail(f"Verified FFmpeg input is missing: {ffmpeg_path}")
    if sha256_of(ffmpeg_path) != expected_hash:
        fail("FFmpeg SHA-256 mismatch during validation.")

    delivery_root = REPO_ROOT / "dist" / f"ASRTools-{PLATFORM_TAG}-v{APP_VERSION}"
    if not delivery_root.is_dir():
        fail(f"Release delivery directory is missing: {delivery_root}")
    validation_root = REPO_ROOT / "build" / f"release-v{APP_VERSION}" / "validation"
    reset_known_directory(validation_root)

    portable_zip = delivery_root / f"ASRTools-{PLATFORM_TAG}-v{APP_VERSION}-portable.zip"
    source_archive = delivery_root / f"ASRTools-v{APP_VERSION}-source.tar.gz"
    for artifact in (portable_zip, source_archive):
        if not artifact.is_file():
            fail(f"Release artifact is missing: {artifact}")

    print("[1/4] Verifying artifact checksums...")
    verify_checksums(delivery_root, portable_zip, source_archive)

    print("[2/4] Verifying portable package layout and licenses...")
    portable_extract = validation_root / "portable-extracted"
    with zipfile.ZipFile(portable_zip) as archive:
        safe_extract_zip(archive, portable_extract)
    launcher, bundled_ffmpeg = verify_layout(portable_extract)
    if sha256_of(bundled_ffmpeg) != expected_hash:
        fail(f"Bundled FFmpeg SHA-256 mismatch. Expected {expected_hash}, got {sha256_of(bundled_ffmpeg)}.")

    print("[3/4] Running packaged smoke checks...")
    test_audio = REPO_ROOT / "resources" / "test.mp3"
    if not test_audio.is_file():
        fail(f"Authorized test audio is missing: {test_audio}")
    test_video = validation_root / "conversion-input.mp4"
    fixture = subprocess.run(
        [str(ffmpeg_path), "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=black:s=320x240:d=3",
         "-i", str(test_audio), "-t", "3", "-c:v", "libx264", "-c:a", "aac",
         "-shortest", "-y", str(test_video)],
        capture_output=True, text=True,
    )
    if fixture.returncode != 0 or not test_video.is_file():
        fail(f"Unable to create the local video conversion fixture: {fixture.stderr}")

    variant_root = validation_root / "portable"
    variant_root.mkdir(parents=True, exist_ok=True)
    ffmpeg_report_path = variant_root / "ffmpeg.json"
    ffmpeg_report = run_packaged_check(launcher, ["--release-check", "ffmpeg", "--report", str(ffmpeg_report_path)], ffmpeg_report_path)
    if not re.match(rf"^ffmpeg version n?{re.escape(FFMPEG_VERSION)}(?=[-\s])", ffmpeg_report.get("ffmpeg_version", "")):
        fail(f"Packaged build resolved an unexpected FFmpeg version: {ffmpeg_report.get('ffmpeg_version')}")

    converted_audio = variant_root / "converted.mp3"
    convert_report_path = variant_root / "convert.json"
    convert_report = run_packaged_check(
        launcher,
        ["--release-check", "convert", "--input", str(test_video),
         "--output", str(converted_audio), "--report", str(convert_report_path)],
        convert_report_path,
    )
    if convert_report.get("output_bytes", 0) <= 0:
        fail("Packaged build generated an empty converted audio file.")

    print("[4/4] Writing validation report...")
    report = f"""# ASRTools v{APP_VERSION} 开发侧验证记录

- Validation time UTC: {datetime.now(timezone.utc).isoformat()}
- Development OS: {platform.platform()}
- Target: {SUPPORTED_PLATFORM}
- PATH during packaged checks: minimal system directories only; no FFmpeg directory
- Verified FFmpeg input SHA-256: {expected_hash}

## portable

- Executable: {launcher}
- Package root layout: ASRTools, {README_NAME}, _runtime/, docs/
- Bundled FFmpeg: {ffmpeg_report['ffmpeg_version']}
- Video to audio: passed ({convert_report['output_bytes']} bytes)
- GUI B interface workflow: not run (online recognition requires explicit Windows-chain validation flow)

Nuitka cannot cross-compile; this report covers the native {SYSTEM} build only.
"""
    (delivery_root / "VALIDATION-REPORT.md").write_text(report, encoding="utf-8")
    print(f"Validation report updated: {delivery_root / 'VALIDATION-REPORT.md'}")


if __name__ == "__main__":
    main()
