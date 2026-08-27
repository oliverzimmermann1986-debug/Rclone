"""Install the repository-pinned XcodeGen archive after integrity checks."""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import stat
import subprocess
import urllib.request
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import BinaryIO

XCODEGEN_VERSION = "2.46.0"
XCODEGEN_URL = (
    "https://github.com/yonaskolb/XcodeGen/releases/download/"
    f"{XCODEGEN_VERSION}/xcodegen.zip"
)
XCODEGEN_SHA256 = "4d9e34b62172d645eed6457cac13fc222569974098ef4ee9c3368bedf0196806"

BinaryOpener = Callable[[urllib.request.Request], BinaryIO]
ToolRunner = Callable[
    [Sequence[str]], subprocess.CompletedProcess[str]
]


class ToolVerificationError(ValueError):
    """Raised when a downloaded tool does not match its immutable pin."""


def _open(request: urllib.request.Request) -> BinaryIO:
    return urllib.request.urlopen(request, timeout=60)


def _run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _download(request: urllib.request.Request, opener: BinaryOpener) -> bytes:
    chunks: list[bytes] = []
    with opener(request) as response:
        while chunk := response.read(1024 * 1024):
            chunks.append(chunk)
    return b"".join(chunks)


def _safe_archive_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    for member in members:
        path = PurePosixPath(member.filename.replace("\\", "/"))
        mode = member.external_attr >> 16
        if path.is_absolute() or ".." in path.parts or stat.S_ISLNK(mode):
            raise ToolVerificationError("XcodeGen-Archiv enthält unsichere Pfade")
    return members


def verify_xcodegen_version(
    binary: Path,
    expected_version: str,
    *,
    runner: ToolRunner = _run,
) -> None:
    result = runner([str(binary), "--version"])
    output = (result.stdout or result.stderr).strip()
    allowed = re.compile(rf"(?:Version:\s*)?{re.escape(expected_version)}")
    if result.returncode != 0 or not allowed.fullmatch(output):
        raise ToolVerificationError(
            f"XcodeGen-Version weicht vom Pin {expected_version} ab"
        )


def install_verified_archive(
    destination: Path,
    *,
    url: str,
    expected_sha256: str,
    expected_version: str,
    opener: BinaryOpener = _open,
    runner: ToolRunner = _run,
) -> Path:
    """Download, verify, safely extract and version-check one XcodeGen archive."""

    destination = destination.resolve()
    tool_root = destination / "xcodegen"
    if tool_root.exists():
        raise ToolVerificationError("XcodeGen-Zielverzeichnis existiert bereits")

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "rclone-sync-codemagic-release"},
    )
    payload = _download(request, opener)
    digest = hashlib.sha256(payload).hexdigest()
    if not secrets_compare_digest(digest, expected_sha256.casefold()):
        raise ToolVerificationError("XcodeGen-Archiv-Prüfsumme stimmt nicht")

    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = _safe_archive_members(archive)
        archive.extractall(destination, members=members)

    binary = tool_root / "bin" / "xcodegen"
    if not binary.is_file():
        raise ToolVerificationError("XcodeGen-Binary fehlt im verifizierten Archiv")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    verify_xcodegen_version(binary, expected_version, runner=runner)
    return binary


def secrets_compare_digest(actual: str, expected: str) -> bool:
    """Keep checksum comparison constant-time without importing application code."""

    import secrets

    return secrets.compare_digest(actual, expected)


def install_pinned_xcodegen(
    destination: Path,
    *,
    opener: BinaryOpener = _open,
    runner: ToolRunner = _run,
) -> Path:
    return install_verified_archive(
        destination,
        url=XCODEGEN_URL,
        expected_sha256=XCODEGEN_SHA256,
        expected_version=XCODEGEN_VERSION,
        opener=opener,
        runner=runner,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        binary = install_pinned_xcodegen(args.destination)
    except (OSError, ToolVerificationError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    print(
        f"XcodeGen {XCODEGEN_VERSION} verifiziert "
        f"(sha256:{XCODEGEN_SHA256}) unter {binary}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
