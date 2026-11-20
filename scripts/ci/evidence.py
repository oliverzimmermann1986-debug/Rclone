"""Validate a downloaded central Sicherpfad evidence ZIP against its report."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_REPORT_BYTES = 15 * 1024 * 1024
ARTIFACT_FIELDS = {"name", "path", "encoding", "sha256", "bytes", "files"}


def validate(report: Path, archive: Path) -> dict:
    """Read only: never extract or overwrite the downloaded archive."""
    if report.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("Quality report exceeds maximum size")
    data = json.loads(report.read_text(encoding="utf-8-sig"))
    artifacts = data.get("artifacts", []) if isinstance(data, dict) else None
    if (
        not isinstance(artifacts, list)
        or len(artifacts) != 1
        or not isinstance(artifacts[0], dict)
        or artifacts[0].get("name") != "rclone-ci-evidence.zip"
    ):
        raise ValueError("Exactly one CI evidence archive required")
    artifact = artifacts[0]
    if (
        set(artifact) != ARTIFACT_FIELDS
        or artifact["encoding"] != "binary"
        or artifact["path"] != "reports/rclone-ci-evidence.zip"
        or type(artifact["bytes"]) is not int
        or not 0 < artifact["bytes"] <= MAX_ARCHIVE_BYTES
        or type(artifact["files"]) is not int
        or artifact["files"] <= 0
        or not isinstance(artifact["sha256"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", artifact["sha256"])
    ):
        raise ValueError("Invalid external CI evidence metadata")
    names = data.get("evidenceFiles")
    if (
        not isinstance(names, list)
        or len(names) != artifact["files"]
        or any(
            not isinstance(name, str)
            or not name
            or PurePosixPath(name).is_absolute()
            or ".." in PurePosixPath(name).parts
            or "\\" in name
            for name in names
        )
        or len(set(names)) != len(names)
    ):
        raise ValueError("Invalid evidence file manifest")
    if (
        archive.is_symlink()
        or not archive.is_file()
        or archive.stat().st_size != artifact["bytes"]
    ):
        raise ValueError("Downloaded evidence size mismatch or nonregular archive")
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != artifact["sha256"]:
        raise ValueError("Downloaded evidence SHA-256 mismatch")
    if not zipfile.is_zipfile(archive):
        raise ValueError("Evidence is not a ZIP archive")
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if sorted(member.filename for member in members) != sorted(names):
            raise ValueError("Downloaded evidence file manifest mismatch")
        if any(
            member.file_size > 16 * 1024 * 1024 or member.is_dir() for member in members
        ):
            raise ValueError("Downloaded evidence member exceeds the file size limit")
    return {
        "name": artifact["name"],
        "bytes": artifact["bytes"],
        "sha256": artifact["sha256"],
        "files": len(names),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    print(json.dumps(validate(args.report, args.archive), indent=2))
