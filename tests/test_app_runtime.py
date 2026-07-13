from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app_runtime import (
    APP_VERSION,
    FFMPEG_SHA256,
    FFMPEG_VERSION,
    FFmpegUnavailableError,
    WINDOWS_FILE_VERSION,
    resolve_ffmpeg_path,
)


class AppRuntimeTests(unittest.TestCase):
    def test_release_identity_is_consistent(self):
        self.assertEqual(APP_VERSION, "1.1.0")
        self.assertEqual(WINDOWS_FILE_VERSION, f"{APP_VERSION}.0")
        self.assertEqual(FFMPEG_VERSION, "8.1.2")
        self.assertEqual(len(FFMPEG_SHA256), 64)

    def test_resolve_ffmpeg_uses_only_the_resource_directory(self):
        with TemporaryDirectory() as temporary_directory:
            resource_dir = Path(temporary_directory)
            expected = resource_dir / "ffmpeg.exe"
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
