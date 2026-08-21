"""Validate an ios-vX.Y.Z tag and apply it to XcodeGen settings."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

TAG_PATTERN = re.compile(r"^ios-v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SETTING_PATTERN = re.compile(r'(?m)^(\s*MARKETING_VERSION:\s*)"[^"]+"\s*$')


def version_from_tag(tag: str) -> str:
    match = TAG_PATTERN.fullmatch(tag)
    if not match:
        raise ValueError(
            "Release-Tag muss exakt ios-vX.Y.Z mit Zahlen ohne führende Nullen entsprechen"
        )
    return ".".join(match.groups())


def apply_marketing_version(project_file: Path, version: str) -> None:
    source = project_file.read_text(encoding="utf-8")
    updated, replacements = SETTING_PATTERN.subn(rf'\g<1>"{version}"', source)
    if replacements != 1:
        raise ValueError(
            f"Genau ein MARKETING_VERSION-Eintrag erwartet, gefunden: {replacements}"
        )
    project_file.write_text(updated, encoding="utf-8")


def configured_marketing_version(project_file: Path) -> str:
    source = project_file.read_text(encoding="utf-8")
    matches = re.findall(r'(?m)^\s*MARKETING_VERSION:\s*"([^"]+)"\s*$', source)
    if len(matches) != 1:
        raise ValueError(
            f"Genau ein MARKETING_VERSION-Eintrag erwartet, gefunden: {len(matches)}"
        )
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    parser.add_argument("project_file", type=Path)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    version = version_from_tag(args.tag)
    if args.write:
        apply_marketing_version(args.project_file, version)
    configured = configured_marketing_version(args.project_file)
    if configured != version:
        parser.error(
            f"MARKETING_VERSION ist {configured}, erwartet aus {args.tag}: {version}"
        )
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
