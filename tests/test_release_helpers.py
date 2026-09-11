import base64
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_POWERSHELL = shutil.which("powershell.exe")
_spec = importlib.util.spec_from_file_location(
    "release_payload", REPO_ROOT / "scripts" / "release_payload.py"
)
release_payload = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_payload)


class FunASRNuitkaCompatibilityTests(unittest.TestCase):
    def test_registry_patch_keeps_compiled_module_body_running(self):
        register_source = """\
import inspect

class RegisterTables:
    tokenizer_classes = {}

    def register(self, register_tables_key, key=None):
        def decorator(target_class):
            registry = getattr(self, register_tables_key)
            registry_key = key if key is not None else target_class.__name__
            registry[registry_key] = target_class
            register_tables_key_meta = register_tables_key + "_meta"
            if not hasattr(self, register_tables_key_meta):
                setattr(self, register_tables_key_meta, {})
            registry_meta = getattr(self, register_tables_key_meta)
            class_file = inspect.getfile(target_class)
            class_line = inspect.getsourcelines(target_class)[1]
            meta_data = [registry_key, target_class.__name__, f"{class_file}:{class_line}"]
            registry_meta[registry_key] = meta_data
            return target_class
        return decorator

tables = RegisterTables()
"""
        tokenizer_source = """\
@tables.register("tokenizer_classes")
class CharTokenizer:
    def __init__(self):
        self.seg_dict = load_seg_dict()

def load_seg_dict():
    return {"离线": "ok"}
"""
        with TemporaryDirectory() as temporary_directory:
            register_path = Path(temporary_directory) / "register.py"
            register_path.write_text(register_source, encoding="utf-8")
            release_payload.patch_funasr_registry(register_path)
            patched_source = register_path.read_text(encoding="utf-8")
            release_payload.patch_funasr_registry(register_path)
            self.assertEqual(register_path.read_text(encoding="utf-8"), patched_source)

            spec = importlib.util.spec_from_file_location("funasr_register_test", register_path)
            register_module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = register_module
            try:
                spec.loader.exec_module(register_module)
                namespace = {
                    "__name__": "funasr.tokenizer.char_tokenizer",
                    "tables": register_module.tables,
                }
                with patch.object(register_module.inspect, "getsourcelines", side_effect=OSError(
                    "source unavailable in Nuitka standalone"
                )):
                    exec(tokenizer_source, namespace)
                tokenizer = register_module.tables.tokenizer_classes["CharTokenizer"]()
            finally:
                sys.modules.pop(spec.name, None)

        self.assertEqual(tokenizer.seg_dict, {"离线": "ok"})
        self.assertIn("load_seg_dict", namespace)


class DependencyWalkerPreflightTests(unittest.TestCase):
    def test_empty_project_cache_reuses_existing_tools(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / 'existing'
            destination = Path(directory) / 'project'
            source.mkdir()
            for name in ('depends.exe', 'depends.dll'):
                (source / name).write_bytes(name.encode())
            release_payload.prepare_dependency_walker(destination, source)
            for name in ('depends.exe', 'depends.dll'):
                self.assertEqual((destination / name).read_bytes(), (source / name).read_bytes())
            # A populated project cache must work without the profile cache.
            release_payload.prepare_dependency_walker(destination, Path(directory) / 'absent')

    def test_missing_or_incomplete_tools_fail_before_compilation(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / 'existing'
            destination = Path(directory) / 'project'
            source.mkdir()
            (source / 'depends.exe').write_bytes(b'incomplete')
            with self.assertRaisesRegex(FileNotFoundError, 'compilation has not started'):
                release_payload.prepare_dependency_walker(destination, source)
            self.assertFalse(destination.exists())


@unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL, "requires Windows PowerShell")
class ReleaseHelperTests(unittest.TestCase):
    def test_collection_paths_preserve_single_line_python_output(self):
        source = (REPO_ROOT / "scripts" / "build_release.ps1").read_text(
            encoding="utf-8-sig"
        )
        with TemporaryDirectory(prefix="release paths ") as directory:
            package = Path(directory) / "funasr"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            for variable, end, expected in (
                ("FunasrRoot", "$FunasrTarget =", str(package)),
                ("SitePackages", "Get-ChildItem -LiteralPath $SitePackages",
                 sysconfig.get_paths()["purelib"]),
            ):
                with self.subTest(variable=variable):
                    start = source.index(f"${variable}Output =")
                    fragment = source[start:source.index(end, start)]
                    script = (
                        "$ErrorActionPreference = 'Stop'\n"
                        "$env:PYTHONIOENCODING = 'utf-8'\n"
                        f"$VenvPython = '{sys.executable.replace(chr(39), chr(39) * 2)}'\n"
                        + fragment + f"\nWrite-Output ${variable}\n"
                    )
                    completed = subprocess.run(
                        [WINDOWS_POWERSHELL, "-NoProfile", "-EncodedCommand",
                         base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
                        cwd=directory, capture_output=True, check=False,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    actual = completed.stdout.decode().strip()
                    self.assertEqual(Path(actual), Path(expected))

    def test_background_worker_drops_sensitive_environment(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scripts = root / "scripts"
            run_directory = root / "build" / "background" / "test-run"
            scripts.mkdir()
            run_directory.mkdir(parents=True)
            shutil.copy2(REPO_ROOT / "scripts" / "start_build.ps1", scripts)
            (run_directory / "request.json").write_text('{"Jobs": 1}', encoding="utf-8")
            (scripts / "build_release.ps1").write_text(
                """\
param([int]$Jobs, [string]$ResultPath)
if ($env:ASRTOOLS_TEST_SECRET) { throw 'Sensitive environment leaked to build.' }
@{ deliveryDirectory = $env:ASRTOOLS_TEST_SAFE } |
    ConvertTo-Json | Set-Content -LiteralPath $ResultPath -Encoding UTF8
""",
                encoding="utf-8-sig",
            )
            environment = os.environ.copy()
            environment["ASRTOOLS_TEST_SECRET"] = "must-not-leak"
            environment["ASRTOOLS_TEST_SAFE"] = "kept"
            completed = subprocess.run(
                [
                    WINDOWS_POWERSHELL,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(scripts / "start_build.ps1"),
                    "-Worker",
                    "-RunDirectory",
                    str(run_directory),
                ],
                capture_output=True,
                check=False,
                env=environment,
            )
            status = json.loads(
                (run_directory / "status.json").read_text(encoding="utf-8-sig")
            )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(status["deliveryDirectory"], "kept")

    def test_nuitka_cache_is_project_local(self):
        script = (REPO_ROOT / "scripts" / "build_release.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn(
            '$env:NUITKA_CACHE_DIR = Join-Path $RepoRoot "build\\nuitka-cache"', script
        )

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
