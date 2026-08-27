from __future__ import annotations

import hashlib
import io
import subprocess
import zipfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from scripts.install_pinned_xcodegen import (
    ToolVerificationError,
    XCODEGEN_SHA256,
    XCODEGEN_URL,
    XCODEGEN_VERSION,
    install_verified_archive,
)


def _archive(*, unsafe_name: str | None = None) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(unsafe_name or "xcodegen/bin/xcodegen", "binary")
        archive.writestr("xcodegen/share/xcodegen/SettingPresets/base.yml", "{}")
    return payload.getvalue()


def _opener(payload: bytes):
    def open_payload(_request):
        return io.BytesIO(payload)

    return open_payload


def _runner(version: str, *, returncode: int = 0):
    def run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        assert Path(args[0]).is_file()
        assert args[1:] == ["--version"]
        return subprocess.CompletedProcess(
            args, returncode, f"Version: {version}\n", ""
        )

    return run


def test_repository_pin_is_immutable_release_asset():
    assert XCODEGEN_VERSION == "2.46.0"
    assert XCODEGEN_URL.endswith("/2.46.0/xcodegen.zip")
    assert XCODEGEN_SHA256 == (
        "4d9e34b62172d645eed6457cac13fc222569974098ef4ee9c3368bedf0196806"
    )


def test_verified_archive_is_installed_and_exact_version_is_executed(tmp_path: Path):
    payload = _archive()

    binary = install_verified_archive(
        tmp_path / "tools",
        url="https://example.invalid/xcodegen.zip",
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        expected_version="2.46.0",
        opener=_opener(payload),
        runner=_runner("2.46.0"),
    )

    assert binary == tmp_path / "tools" / "xcodegen" / "bin" / "xcodegen"
    assert binary.is_file()


def test_checksum_mismatch_fails_before_extraction(tmp_path: Path):
    payload = _archive()
    destination = tmp_path / "tools"

    with pytest.raises(ToolVerificationError, match="Prüfsumme"):
        install_verified_archive(
            destination,
            url="https://example.invalid/xcodegen.zip",
            expected_sha256="0" * 64,
            expected_version="2.46.0",
            opener=_opener(payload),
            runner=_runner("2.46.0"),
        )

    assert not destination.exists()


def test_unexpected_binary_version_fails_closed(tmp_path: Path):
    payload = _archive()

    with pytest.raises(ToolVerificationError, match="Version"):
        install_verified_archive(
            tmp_path / "tools",
            url="https://example.invalid/xcodegen.zip",
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            expected_version="2.46.0",
            opener=_opener(payload),
            runner=_runner("2.47.0"),
        )


@pytest.mark.parametrize(
    "unsafe_name", ["../outside/xcodegen", "..\\outside\\xcodegen"]
)
def test_archive_path_traversal_is_rejected(tmp_path: Path, unsafe_name: str):
    payload = _archive(unsafe_name=unsafe_name)

    with pytest.raises(ToolVerificationError, match="unsichere Pfade"):
        install_verified_archive(
            tmp_path / "tools",
            url="https://example.invalid/xcodegen.zip",
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            expected_version="2.46.0",
            opener=_opener(payload),
            runner=_runner("2.46.0"),
        )

    assert not (tmp_path / "outside").exists()
