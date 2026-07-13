"""Release identity and bundled runtime resource resolution."""

from __future__ import annotations

from pathlib import Path


APP_VERSION = "1.1.0"
WINDOWS_FILE_VERSION = f"{APP_VERSION}.0"
SUPPORTED_PLATFORM = "Windows 10+ x64"

FFMPEG_VERSION = "8.1.2"
FFMPEG_SHA256 = "1326DDE4C84FF1F96FE6B8916C5BED29E163E9B5DCCF995F6F3DB069D143EC5E"


class FFmpegUnavailableError(RuntimeError):
    """Raised when the release-bundled FFmpeg executable cannot be used."""


def application_resource_dir() -> Path:
    """Return the directory containing bundled application resources.

    Nuitka keeps ``__file__`` beside data files in the portable ``_runtime``
    directory. Source runs use the repository root. Deliberately do not inspect
    PATH: releases must use the FFmpeg binary that was verified at build time.
    """

    return Path(__file__).resolve().parent


def resolve_ffmpeg_path(resource_dir: Path | None = None) -> Path:
    """Resolve the verified, bundled ``ffmpeg.exe`` or raise an actionable error."""

    base_dir = Path(resource_dir).resolve() if resource_dir is not None else application_resource_dir()
    ffmpeg_path = base_dir / "ffmpeg.exe"
    if not ffmpeg_path.is_file():
        raise FFmpegUnavailableError(
            f"未找到随应用提供的 FFmpeg {FFMPEG_VERSION}。请重新解压完整便携包，"
            f"或向销售方重新获取 v{APP_VERSION} 应用包；本程序不会使用系统 PATH 中的 FFmpeg。"
        )
    return ffmpeg_path
