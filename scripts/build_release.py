"""Cross-platform release build for macOS and Linux.

Mirrors the staged flow of scripts/build_release.ps1 (Windows chain stays
untouched) using only the Python standard library plus subprocess calls to
uv and Nuitka. Nuitka cannot cross-compile, so this script must run natively
on the platform it builds for.
"""

from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
from pathlib import Path
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app_runtime import (  # noqa: E402
    APP_VERSION,
    FFMPEG_FILENAME,
    FFMPEG_SHA256_BY_PLATFORM,
    FFMPEG_VERSION,
    SUPPORTED_PLATFORM,
    WINDOWS_FILE_VERSION,
)

SYSTEM = platform.system()
ARCH = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
    platform.machine().lower(), platform.machine()
)
PLATFORM_TAG = {"Darwin": f"macOS-{ARCH}", "Linux": f"Linux-{ARCH}"}.get(SYSTEM)


def fail(message: str) -> None:
    raise SystemExit(f"build_release.py: {message}")


def run_checked(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(command, **kwargs)
    if result.returncode != 0:
        fail(f"Command failed with exit code {result.returncode}: {' '.join(map(str, command))}")
    return result


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


def expand_template(source: Path, destination: Path, ffmpeg_sha256: str) -> None:
    content = source.read_text(encoding="utf-8")
    content = content.replace("{APP_VERSION}", APP_VERSION)
    content = content.replace("{FFMPEG_VERSION}", FFMPEG_VERSION)
    content = content.replace("{FFMPEG_SHA256}", ffmpeg_sha256)
    if re.search(r"\{(?:APP_VERSION|FFMPEG_VERSION|FFMPEG_SHA256)\}", content):
        fail(f"Unexpanded release template token in {source}")
    destination.write_text(content, encoding="utf-8")


def verify_ffmpeg(ffmpeg_path: Path) -> str:
    if SYSTEM == "Windows":
        fail("Windows releases are built by build.bat / scripts/build_release.ps1.")
    if not ffmpeg_path.is_file():
        fail(f"FFmpeg input does not exist: {ffmpeg_path}. Provide the verified FFmpeg {FFMPEG_VERSION} binary explicitly.")
    actual_hash = sha256_of(ffmpeg_path)
    expected_hash = FFMPEG_SHA256_BY_PLATFORM.get(SYSTEM)
    if expected_hash is None:
        fail(
            f"FFmpeg SHA-256 for {SYSTEM} is not fixed in app_runtime.FFMPEG_SHA256_BY_PLATFORM yet.\n"
            f"Verified binary: {ffmpeg_path}\n"
            f"Actual SHA-256: {actual_hash}\n"
            f"Add this hash to FFMPEG_SHA256_BY_PLATFORM[\"{SYSTEM}\"] after verifying the binary, then rerun."
        )
    if actual_hash != expected_hash.upper():
        fail(f"FFmpeg SHA-256 mismatch. Expected {expected_hash}, got {actual_hash}.")
    version_output = run_checked([str(ffmpeg_path), "-version"], capture_output=True, text=True)
    version_line = version_output.stdout.splitlines()[0] if version_output.stdout else ""
    # Accept the exact release version (e.g. "8.1.2-essentials_build") or the
    # release-branch git-describe form shipped by BtbN Linux static builds
    # (e.g. "n8.1.2-44-g7c533d0f86"), which is the locked release plus
    # upstream bugfix commits.
    if not re.match(rf"^ffmpeg version n?{re.escape(FFMPEG_VERSION)}(?=[-\s])", version_line):
        fail(f"FFmpeg version mismatch. Expected {FFMPEG_VERSION}, got: {version_line}")
    return version_line


def python_pin() -> str:
    pin_file = REPO_ROOT / ".python-version"
    return pin_file.read_text(encoding="utf-8").strip() if pin_file.is_file() else "3.12"


def prepare_venv(uv_command: str) -> Path:
    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    lock_file = REPO_ROOT / "requirements-release.lock"
    if not lock_file.is_file():
        fail(f"Required release input is missing: {lock_file}")
    if not venv_python.is_file():
        run_checked([uv_command, "venv", "--python", python_pin(), str(REPO_ROOT / ".venv")])
    run_checked([uv_command, "pip", "sync", "--python", str(venv_python), "--require-hashes", str(lock_file)])
    return venv_python


def build_runtime(venv_python: Path, build_root: Path) -> Path:
    """Build the Nuitka standalone runtime and return its .dist directory."""
    portable_build = build_root / "portable"
    portable_build.mkdir(parents=True, exist_ok=True)
    jobs = max(1, (os.cpu_count() or 2) - 1)
    arguments = [
        str(venv_python), "-m", "nuitka",
        "--assume-yes-for-downloads",
        "--enable-plugin=pyqt5",
        "--include-package=qfluentwidgets",
        "--include-package=bk_asr",
        "--lto=yes",
        f"--jobs={jobs}",
        "--remove-output",
        "--mode=standalone",
        f"--output-dir={portable_build}",
        "--output-filename=ASRTools",
        str(REPO_ROOT / "asr_gui.py"),
    ]
    if SYSTEM == "Darwin":
        icon_icns = build_root / "app_icon.icns"
        create_icns_from_png(REPO_ROOT / "resources" / "app_icon.png", icon_icns)
        arguments += [
            "--macos-create-app-bundle",
            f"--macos-app-icon={icon_icns}",
            "--macos-app-name=ASRTools",
            "--macos-app-version=1.1.0",
        ]
    run_checked(arguments, cwd=REPO_ROOT)
    dist_dirs = list(portable_build.glob("*.dist"))
    if len(dist_dirs) != 1:
        fail(f"Expected exactly one Nuitka standalone .dist directory, found {len(dist_dirs)}.")
    return dist_dirs[0]


def create_icns_from_png(png_path: Path, icns_path: Path) -> None:
    """Convert the bundled PNG icon to .icns using macOS sips/iconutil."""
    if not png_path.is_file():
        fail(f"Required release input is missing: {png_path}")
    iconset = icns_path.with_suffix(".iconset")
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()
    for size in (16, 32, 64, 128, 256, 512):
        run_checked(["sips", "-z", str(size), str(size), str(png_path), "--out", str(iconset / f"icon_{size}x{size}.png")],
                    capture_output=True)
        if size <= 512:
            run_checked(["sips", "-z", str(size * 2), str(size * 2), str(png_path), "--out", str(iconset / f"icon_{size}x{size}@2x.png")],
                        capture_output=True)
    run_checked(["iconutil", "-c", "icns", str(iconset), "-o", str(icns_path)], capture_output=True)
    if not icns_path.is_file():
        fail("Failed to create the macOS .icns application icon.")


def collect_ffmpeg_license_material(ffmpeg_path: Path, destination: Path) -> None:
    """Copy FFmpeg license/readme material shipped beside the verified binary."""
    destination.mkdir(parents=True, exist_ok=True)
    candidates = []
    for root in (ffmpeg_path.parent.parent, ffmpeg_path.parent):
        for entry in sorted(root.iterdir()) if root.is_dir() else ():
            if entry.is_file() and re.match(r"(?i)^(license|copying|readme)", entry.name):
                candidates.append(entry)
    if not candidates:
        fail(
            "No FFmpeg license material (LICENSE/COPYING/README) found beside the verified binary. "
            "Provide an FFmpeg distribution that ships its license text."
        )
    for entry in candidates:
        shutil.copy2(entry, destination / entry.name)


def assemble_docs(delivery_root: Path, venv_python: Path, ffmpeg_path: Path, ffmpeg_sha256: str) -> None:
    licenses = delivery_root / "licenses"
    run_checked([str(venv_python), str(REPO_ROOT / "scripts" / "collect_licenses.py"),
                 "--output", str(licenses / "python")])
    collect_ffmpeg_license_material(ffmpeg_path, licenses / "ffmpeg")
    shutil.copy2(REPO_ROOT / "LICENSE", delivery_root / "LICENSE.txt")
    release_assets = REPO_ROOT / "release-assets"
    expand_template(release_assets / "THIRD_PARTY_NOTICES.template.txt", delivery_root / "THIRD_PARTY_NOTICES.txt", ffmpeg_sha256)
    expand_template(release_assets / "SOURCE_CODE.template.txt", delivery_root / "SOURCE_CODE.txt", ffmpeg_sha256)
    readme_template = release_assets / f"README-{SYSTEM}.template.txt"
    if not readme_template.is_file():
        fail(f"Required release template is missing: {readme_template}")
    expand_template(readme_template, delivery_root / f"README-{'macOS' if SYSTEM == 'Darwin' else 'Linux'}.txt", ffmpeg_sha256)


def assemble_portable_stage(
    build_root: Path, dist_dir: Path, ffmpeg_path: Path, delivery_root: Path
) -> Path:
    portable_folder = f"ASRTools-{PLATFORM_TAG}-v{APP_VERSION}-portable"
    stage = build_root / portable_folder
    runtime = stage / "_runtime"
    docs = stage / "docs"
    reset_known_directory(stage)
    runtime.mkdir(parents=True)
    docs.mkdir()

    if SYSTEM == "Darwin":
        app_dirs = list(dist_dir.glob("*.app"))
        if len(app_dirs) != 1:
            fail(f"Expected exactly one Nuitka .app bundle, found {len(app_dirs)}.")
        shutil.copytree(app_dirs[0], runtime / "ASRTools.app", symlinks=True)
        shutil.copy2(ffmpeg_path, runtime / "ASRTools.app" / "Contents" / "MacOS" / FFMPEG_FILENAME)
        launcher = stage / "ASRTools"
        launcher.write_text(
            '#!/bin/sh\nexec open "$(dirname "$0")/_runtime/ASRTools.app" --args "$@"\n', encoding="utf-8"
        )
        launcher.chmod(0o755)
    else:
        shutil.copytree(dist_dir, runtime, dirs_exist_ok=True)
        shutil.copy2(ffmpeg_path, runtime / FFMPEG_FILENAME)
        runtime_exe = runtime / "ASRTools"
        if not runtime_exe.is_file():
            fail(f"Nuitka standalone output is missing: {runtime_exe}")
        runtime_exe.chmod(0o755)
        (runtime / FFMPEG_FILENAME).chmod(0o755)
        launcher = stage / "ASRTools"
        launcher.write_text('#!/bin/sh\nexec "$(dirname "$0")/_runtime/ASRTools" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)

    readme_name = f"README-{'macOS' if SYSTEM == 'Darwin' else 'Linux'}.txt"
    shutil.copy2(delivery_root / readme_name, stage / readme_name)
    for sidecar in ("LICENSE.txt", "THIRD_PARTY_NOTICES.txt", "SOURCE_CODE.txt"):
        shutil.copy2(delivery_root / sidecar, docs / sidecar)
    shutil.copytree(delivery_root / "licenses", docs / "licenses")
    return stage


def package_artifacts(delivery_root: Path, stage: Path) -> tuple[Path, Path]:
    portable_zip = delivery_root / f"{stage.name}.zip"
    with zipfile.ZipFile(portable_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in sorted(stage.rglob("*")):
            archive.write(entry, entry.relative_to(stage))
    source_tar = delivery_root / f"ASRTools-v{APP_VERSION}-source.tar.gz"
    source_items = (
        ".python-version", ".gitignore", "API", "LICENSE", "README.md", "CONTEXT.md",
        "app_runtime.py", "asr_gui.py", "bk_asr", "build.bat", "example.py",
        "release-assets", "requirements-release.in", "requirements-release.lock",
        "requirements.txt", "resources", "scripts", "tests",
    )
    with tarfile.open(source_tar, "w:gz") as archive:
        for relative in source_items:
            source = REPO_ROOT / relative
            if source.exists():
                archive.add(source, arcname=f"ASRTools-v{APP_VERSION}-source/{relative}")
    return portable_zip, source_tar


def write_manifest_and_hashes(
    delivery_root: Path, uv_command: str, venv_python: Path,
    ffmpeg_path: Path, ffmpeg_version_line: str, ffmpeg_sha256: str,
    artifacts: tuple[Path, Path],
) -> None:
    git_commit = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                                capture_output=True, text=True).stdout.strip() or "unknown"
    git_status = subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--short"],
                                capture_output=True, text=True).stdout.strip() or "(clean)"
    python_identity = subprocess.run(
        [str(venv_python), "-c", "import sys, platform; print(sys.version.replace(chr(10), ' ')); print(platform.architecture()[0])"],
        capture_output=True, text=True).stdout.replace("\n", " / ").strip()
    nuitka_version = subprocess.run([str(venv_python), "-m", "nuitka", "--version"],
                                    capture_output=True, text=True).stdout.splitlines()[0]
    uv_version = subprocess.run([uv_command, "--version"], capture_output=True, text=True).stdout.strip()
    dependency_freeze = subprocess.run([uv_command, "pip", "freeze", "--python", str(venv_python)],
                                       capture_output=True, text=True).stdout.strip()
    manifest = f"""ASRTools release build manifest
App version: {APP_VERSION}
Target: {SUPPORTED_PLATFORM}
Built at UTC: {datetime.now(timezone.utc).isoformat()}
Build OS: {platform.platform()}
uv: {uv_version}
Python: {python_identity}
Nuitka: {nuitka_version}
FFmpeg input: {ffmpeg_path}
FFmpeg version: {ffmpeg_version_line}
FFmpeg SHA-256: {ffmpeg_sha256}
Git commit: {git_commit}
Git working tree status at build time:
{git_status}

Locked Python environment:
{dependency_freeze}

Build entry point: python3 scripts/build_release.py --ffmpeg-path <verified-ffmpeg>
"""
    (delivery_root / "BUILD-MANIFEST.txt").write_text(manifest, encoding="utf-8")
    hash_lines = [f"{sha256_of(artifact)}  {artifact.name}" for artifact in artifacts]
    (delivery_root / "SHA256SUMS.txt").write_text("\n".join(hash_lines) + "\n", encoding="ascii")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the ASRTools portable release (macOS/Linux).")
    parser.add_argument("--ffmpeg-path", required=True, type=Path,
                        help="Path to the verified FFmpeg binary for this platform.")
    arguments = parser.parse_args()

    ffmpeg_path = arguments.ffmpeg_path.resolve()
    ffmpeg_version_line = verify_ffmpeg(ffmpeg_path)
    ffmpeg_sha256 = FFMPEG_SHA256_BY_PLATFORM[SYSTEM].upper()

    uv_command = shutil.which("uv")
    if not uv_command:
        fail("uv is required. Install uv, then rerun this script. No packages are installed into system Python.")
    if SYSTEM == "Linux" and not shutil.which("patchelf"):
        fail("patchelf is required for Nuitka standalone on Linux (e.g. 'uv tool install patchelf').")

    print("[1/5] Preparing locked virtual environment...")
    venv_python = prepare_venv(uv_command)

    build_root = REPO_ROOT / "build" / f"release-v{APP_VERSION}"
    delivery_root = REPO_ROOT / "dist" / f"ASRTools-{PLATFORM_TAG}-v{APP_VERSION}"
    reset_known_directory(build_root)
    reset_known_directory(delivery_root)

    print("[2/5] Building portable standalone runtime...")
    dist_dir = build_runtime(venv_python, build_root)

    print("[3/5] Collecting licenses and release documentation...")
    assemble_docs(delivery_root, venv_python, ffmpeg_path, ffmpeg_sha256)
    stage = assemble_portable_stage(build_root, dist_dir, ffmpeg_path, delivery_root)

    print("[4/5] Creating portable and corresponding-source archives...")
    artifacts = package_artifacts(delivery_root, stage)

    print("[5/5] Writing build manifest and artifact hashes...")
    write_manifest_and_hashes(delivery_root, uv_command, venv_python,
                              ffmpeg_path, ffmpeg_version_line, ffmpeg_sha256, artifacts)

    print(f"Release build complete: {delivery_root}")
    for entry in sorted(delivery_root.iterdir()):
        print(f"  {entry.name}")


if __name__ == "__main__":
    main()
