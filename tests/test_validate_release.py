import importlib.util
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "validate_release", REPO_ROOT / "scripts" / "validate_release.py"
)
validate_release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate_release)


class SafeExtractZipTests(unittest.TestCase):
    def test_traversal_and_absolute_members_are_rejected(self):
        for unsafe_name in ("../escaped.txt", "/etc/passwd", "a/../../b.txt"):
            with TemporaryDirectory() as temporary_directory:
                destination = Path(temporary_directory) / "extract"
                destination.mkdir()
                zip_path = Path(temporary_directory) / "evil.zip"
                with zipfile.ZipFile(zip_path, "w") as archive:
                    archive.writestr("ok.txt", "fine")
                    archive.writestr(unsafe_name, "pwned")
                with zipfile.ZipFile(zip_path) as archive, self.assertRaises(SystemExit):
                    validate_release.safe_extract_zip(archive, destination)
                escaped = Path(temporary_directory) / "escaped.txt"
                self.assertFalse(escaped.exists())

    def test_normal_members_extract_and_keep_exec_bit(self):
        with TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "extract"
            destination.mkdir()
            zip_path = Path(temporary_directory) / "package.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                info = zipfile.ZipInfo("ASRTools")
                info.external_attr = (0o755 & 0xFFFF) << 16
                archive.writestr(info, "#!/bin/sh\necho hi\n")
            with zipfile.ZipFile(zip_path) as archive:
                validate_release.safe_extract_zip(archive, destination)
            extracted = destination / "ASRTools"
            self.assertTrue(extracted.is_file())
            if os.name != "nt":
                self.assertTrue(extracted.stat().st_mode & stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
