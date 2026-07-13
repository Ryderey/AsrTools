"""Collect Python runtime package metadata and bundled license files."""

from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path
import shutil
import sys


LICENSE_NAMES = ("license", "copying", "notice", "authors")


def safe_name(value: str) -> str:
    return "".join(character if character.isalnum() or character in ".-_" else "_" for character in value)


def collect(output_directory: Path) -> None:
    output_directory.mkdir(parents=True, exist_ok=True)
    manifest_lines = [
        "Python runtime and build package inventory",
        f"Python: {sys.version.splitlines()[0]}",
        "",
    ]

    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, output_directory / "Python-LICENSE.txt")

    for distribution in sorted(metadata.distributions(), key=lambda item: item.metadata["Name"].lower()):
        name = distribution.metadata["Name"]
        version = distribution.version
        license_expression = distribution.metadata.get("License-Expression") or distribution.metadata.get("License")
        home_page = distribution.metadata.get("Home-page") or distribution.metadata.get("Project-URL") or ""
        manifest_lines.append(f"{name}=={version}")
        manifest_lines.append(f"  License metadata: {license_expression or 'not declared'}")
        if home_page:
            manifest_lines.append(f"  Project: {home_page}")

        copied = 0
        for relative_file in distribution.files or ():
            filename = Path(str(relative_file)).name.lower()
            if not filename.startswith(LICENSE_NAMES):
                continue
            source = Path(distribution.locate_file(relative_file))
            if not source.is_file():
                continue
            destination = output_directory / (
                f"{safe_name(name)}-{safe_name(version)}-{copied + 1}-{safe_name(source.name)}"
            )
            shutil.copy2(source, destination)
            copied += 1
        manifest_lines.append(f"  Bundled license files: {copied}")
        manifest_lines.append("")

    (output_directory / "PYTHON-PACKAGES.txt").write_text(
        "\n".join(manifest_lines), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    collect(arguments.output.resolve())


if __name__ == "__main__":
    main()
