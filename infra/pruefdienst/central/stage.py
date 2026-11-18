"""Stage the pinned controller extension inside disposable Prüfdienst CI only.

This is never the host deployment command. The root-owned controller snapshot
in the image contains no credentials. Validate every base hash and replacement
before creating a new, unprivileged copy under /work for the Node test suite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil


HERE = Path(__file__).resolve().parent
SOURCE = Path("/opt/rclone-ci/controller")
ADDITIONS = ("pull-requests.cjs", "pull-requests.unit.test.cjs")


def patched_sources(source: Path, manifest: dict) -> dict[str, bytes]:
    if manifest.get("version") != 1:
        raise ValueError("Unsupported controller patch manifest")
    patches = manifest.get("files")
    if not isinstance(patches, list) or not patches:
        raise ValueError("Controller patch plan is empty")
    prepared = {}
    for patch in patches:
        name = patch.get("name", "")
        expected = patch.get("sha256Before", "")
        if not re.fullmatch(r"[a-z][a-z0-9.-]*\.cjs", name) or name in prepared:
            raise ValueError("Invalid or duplicate controller source")
        if not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise ValueError("Controller source digest missing")
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Controller source missing: {name}")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(f"Controller base changed: {name}")
        text = raw.decode("utf-8")
        replacements = patch.get("replacements")
        if not isinstance(replacements, list) or not replacements:
            raise ValueError(f"Controller replacement plan missing: {name}")
        for replacement in replacements:
            before, after = replacement.get("before"), replacement.get("after")
            if not isinstance(before, str) or not before or not isinstance(after, str):
                raise ValueError(f"Invalid controller replacement: {name}")
            if text.count(before) != 1:
                raise ValueError(f"Controller replacement is not unique: {name}")
            text = text.replace(before, after, 1)
        prepared[name] = text.encode("utf-8")
    return prepared


def stage(source: Path, destination: Path) -> list[str]:
    if os.name != "posix" or os.environ.get("CI") != "true":
        raise ValueError("Controller tests require isolated Linux CI")
    if source != SOURCE or source.is_symlink() or not source.is_dir():
        raise ValueError("Use the image-provided controller source")
    if source.stat().st_uid != 0 or os.access(source, os.W_OK):
        raise ValueError("Controller snapshot must be root-owned and read-only")
    destination = destination.resolve()
    if not destination.is_relative_to(Path("/work")) or destination == Path("/work"):
        raise ValueError("Controller staging must stay below /work")
    if destination.exists():
        raise ValueError("Controller staging destination already exists")
    manifest = json.loads((HERE / "patch-manifest.json").read_text(encoding="utf-8"))
    prepared = patched_sources(source, manifest)
    originals = sorted(source.glob("*.cjs"))
    if not originals or len(originals) > 64:
        raise ValueError("Controller snapshot file inventory is invalid")
    if any(p.is_symlink() or not p.is_file() or p.stat().st_size > 2_000_000 for p in originals):
        raise ValueError("Controller snapshot contains an invalid file")
    if not any(p.name.endswith(".unit.test.cjs") for p in originals):
        raise ValueError("Original controller regression tests are missing")
    additions = {}
    for name in ADDITIONS:
        path = HERE / name
        if path.is_symlink() or not path.is_file() or (source / name).exists():
            raise ValueError(f"Controller extension source is invalid: {name}")
        additions[name] = path.read_bytes()
    destination.mkdir(parents=True)
    for path in originals:
        shutil.copy2(path, destination / path.name)
    for name, content in {**prepared, **additions}.items():
        (destination / name).write_bytes(content)
    return [p.name for p in originals] + list(additions)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        names = stage(args.source, args.destination)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Controller staging failed: {error}\n")
    print(json.dumps({"controllerStaged": str(args.destination), "files": sorted(names)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
