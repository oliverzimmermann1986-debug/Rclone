"""Recover the checked, bounded ZIP from a central Sicherpfad quality report."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import zipfile
from pathlib import Path

MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_REPORT_BYTES = 15 * 1024 * 1024


def recover(report: Path, target: Path):
    if report.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("Quality report exceeds maximum size")
    data = json.loads(report.read_text(encoding="utf-8-sig"))
    artifacts = [
        artifact
        for artifact in data.get("artifacts", [])
        if artifact.get("name") == "rclone-ci-evidence.zip"
    ]
    if len(artifacts) != 1 or artifacts[0].get("encoding") != "base64":
        raise ValueError("Exactly one CI evidence archive required")
    artifact = artifacts[0]
    raw = base64.b64decode(artifact["content"], validate=True)
    if (
        not raw
        or len(raw) > MAX_ARCHIVE_BYTES
        or hashlib.sha256(raw).hexdigest() != artifact.get("sha256")
    ):
        raise ValueError("Evidence size or SHA-256 mismatch")
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        raise ValueError("Evidence is not a ZIP archive")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as output:
        output.write(raw)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    recover(args.report, args.target)
    print(args.target)
