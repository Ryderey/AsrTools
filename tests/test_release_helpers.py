import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_POWERSHELL = shutil.which("powershell.exe")


@unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL, "requires Windows PowerShell")
class ReleaseHelperTests(unittest.TestCase):
    def test_read_utf8_json_preserves_chinese_without_bom(self):
        with TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "workflow report.json"
            report_path.write_text(
                json.dumps({"task_status": "已处理"}, ensure_ascii=False),
                encoding="utf-8",
            )

            helper_path = REPO_ROOT / "scripts" / "release_helpers.ps1"
            script = f"""
$ErrorActionPreference = "Stop"
. '{str(helper_path).replace("'", "''")}'
$report = Read-Utf8Json -Path '{str(report_path).replace("'", "''")}'
$expected = [string]([char]0x5DF2) + [char]0x5904 + [char]0x7406
if ($report.task_status -cne $expected) {{
    throw "UTF-8 JSON text was decoded incorrectly."
}}
"""
            encoded_command = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            completed = subprocess.run(
                [WINDOWS_POWERSHELL, "-NoProfile", "-EncodedCommand", encoded_command],
                capture_output=True,
                check=False,
            )

            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr.decode("utf-8", errors="replace"),
            )


if __name__ == "__main__":
    unittest.main()
