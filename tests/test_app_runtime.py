from pathlib import Path
import platform
from tempfile import TemporaryDirectory
import unittest

from app_runtime import (
    APP_VERSION,
    FFMPEG_FILENAME,
    FFMPEG_SHA256,
    FFMPEG_SHA256_BY_PLATFORM,
    FFMPEG_VERSION,
    FFmpegUnavailableError,
    SUPPORTED_PLATFORM,
    WINDOWS_FILE_VERSION,
    resolve_ffmpeg_path,
)


WINDOWS_SHA256 = "1326DDE4C84FF1F96FE6B8916C5BED29E163E9B5DCCF995F6F3DB069D143EC5E"
LINUX_SHA256 = "7E9CBECF3D568A411789EC73F6A28EABE4D37F6D2965B76CBD28AE98F018BA11"


class AppRuntimeTests(unittest.TestCase):
    def test_release_identity_is_consistent(self):
        self.assertEqual(APP_VERSION, "1.2.0")
        self.assertEqual(WINDOWS_FILE_VERSION, f"{APP_VERSION}.0")
        self.assertEqual(FFMPEG_VERSION, "8.1.2")
        self.assertEqual(FFMPEG_SHA256_BY_PLATFORM["Windows"], WINDOWS_SHA256)
        self.assertEqual(FFMPEG_SHA256_BY_PLATFORM["Linux"], LINUX_SHA256)
        self.assertEqual(FFMPEG_SHA256, FFMPEG_SHA256_BY_PLATFORM["Windows"])

    def test_ffmpeg_binary_name_follows_platform(self):
        # 显式列出各平台期望值（不用兜底默认值），映射表写错时测试会失败。
        expected = {"Windows": "ffmpeg.exe", "Darwin": "ffmpeg", "Linux": "ffmpeg"}[platform.system()]
        self.assertEqual(FFMPEG_FILENAME, expected)

    def test_supported_platform_follows_platform(self):
        expected = {
            "Windows": "Windows 10+ x64",
            "Darwin": "macOS 11+",
            "Linux": "Linux x64",
        }[platform.system()]
        self.assertEqual(SUPPORTED_PLATFORM, expected)

    def test_resolve_ffmpeg_uses_only_the_resource_directory(self):
        with TemporaryDirectory() as temporary_directory:
            resource_dir = Path(temporary_directory)
            expected = resource_dir / FFMPEG_FILENAME
            expected.touch()

            self.assertEqual(resolve_ffmpeg_path(resource_dir), expected)

    def test_missing_ffmpeg_has_actionable_error(self):
        with TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(
                FFmpegUnavailableError,
                "重新解压完整便携包.*不会使用系统 PATH",
            ):
                resolve_ffmpeg_path(Path(temporary_directory))


if __name__ == "__main__":
    unittest.main()
