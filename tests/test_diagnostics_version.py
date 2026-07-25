"""Tests für den rclone-Version-Check im Doctor-Endpoint."""

import subprocess
from types import SimpleNamespace

from app.routes import api_diagnostics


def _fake_run(stdout):
    def run(*args, **kwargs):
        return SimpleNamespace(stdout=stdout, stderr="")
    return run


def test_current_version_is_ok(monkeypatch):
    monkeypatch.setattr(api_diagnostics, "rclone_subprocess_env", lambda: {})
    monkeypatch.setattr(subprocess, "run", _fake_run("rclone v1.70.0\n- os/version: debian"))
    result = api_diagnostics._rclone_version_check()
    assert result["level"] == "ok"
    assert result["version"] == "1.70.0"


def test_old_version_warns(monkeypatch):
    monkeypatch.setattr(api_diagnostics, "rclone_subprocess_env", lambda: {})
    monkeypatch.setattr(subprocess, "run", _fake_run("rclone v1.65.2"))
    result = api_diagnostics._rclone_version_check()
    assert result["level"] == "warn"
    assert result["version"] == "1.65.2"


def test_newer_major_is_ok(monkeypatch):
    monkeypatch.setattr(api_diagnostics, "rclone_subprocess_env", lambda: {})
    monkeypatch.setattr(subprocess, "run", _fake_run("rclone v2.1.0"))
    assert api_diagnostics._rclone_version_check()["level"] == "ok"


def test_unparseable_output_warns(monkeypatch):
    monkeypatch.setattr(api_diagnostics, "rclone_subprocess_env", lambda: {})
    monkeypatch.setattr(subprocess, "run", _fake_run("no version here"))
    result = api_diagnostics._rclone_version_check()
    assert result["level"] == "warn"
    assert result["version"] == "unknown"


def test_missing_binary_errors(monkeypatch):
    monkeypatch.setattr(api_diagnostics, "rclone_subprocess_env", lambda: {})

    def raise_fnf(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", raise_fnf)
    result = api_diagnostics._rclone_version_check()
    assert result["level"] == "error"
    assert result["ok"] is False
