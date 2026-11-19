"""Regression tests for Backend Security (SEC-01..SEC-09), Pre-Check, Hooks, and Completeness Fixes."""

from __future__ import annotations

import asyncio
import io
import json
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import bcrypt
import pytest
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import (
    auth,
    bootstrap_export,
    config_store,
    db as db_module,
    hooks,
    main,
    recovery_points,
    recovery_snapshots,
    time_travel,
)
from app.config_store import Config
from app.config_validation import ConfigValidationError, validate_config
from app.db import Database
from app.jobs import boot_drill, rclone_sync, restore_test, selective_restore
from app.rclone_args import (
    UnsafeRcloneConfigError,
    assert_safe_rclone_config,
    inspect_rclone_config,
    resolve_rclone_config_path,
)
from app.routes import (
    api_browse,
    api_config,
    api_crypt,
    api_jobs,
    api_recovery,
    api_remotes,
    api_storage,
    api_test,
    api_time_travel,
    api_webauthn,
)
from app.security import require_csrf


# ---------------------------------------------------------------------------
# SEC-01: rclone.conf inspection, remote setup hardening & route guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "conf_body,expected_key",
    [
        ("[loc]\ntype = local\n", "type=local"),
        ("[ali]\ntype = alias\nremote = /etc\n", "type=alias"),
        ("[sftp]\ntype = sftp\nlinks = true\n", "links"),
        ("[sftp2]\ntype = sftp\ncopy_links = 1\n", "copy_links"),
        ("[drv]\ntype = drive\ncustom_command = /bin/sh\n", "custom_command"),
    ],
)
def test_inspect_rclone_config_flags_local_alias_links_and_commands(
    tmp_path: Path, conf_body: str, expected_key: str
):
    conf = tmp_path / "rclone.conf"
    conf.write_text(conf_body, encoding="utf-8")
    report = inspect_rclone_config(conf)
    assert report["ok"] is False
    keys = [entry["key"] for entry in report["dangerous_directives"]]
    assert expected_key in keys
    with pytest.raises(UnsafeRcloneConfigError):
        assert_safe_rclone_config(conf)


def test_resolve_rclone_config_path_explicit_missing_does_not_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    live = tmp_path / "live.conf"
    live.write_text("[evil]\ntype = local\n", encoding="utf-8")
    monkeypatch.setenv("RCLONE_CONFIG", str(live))
    missing = tmp_path / "staged-not-created-yet.conf"
    assert resolve_rclone_config_path(missing) is None
    assert inspect_rclone_config(missing)["ok"] is True


def test_remote_setup_blocks_local_alias_and_dangerous_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    live_conf = tmp_path / "rclone.conf"
    live_conf.write_text("[safe]\ntype = s3\n", encoding="utf-8")
    monkeypatch.setenv("RCLONE_CONFIG", str(live_conf))

    staged_params: dict[str, str] = {"type": "webdav", "url": "https://dav.example"}

    def fake_rc(method: str, payload: dict | None = None, config: str | None = None):
        payload = payload or {}
        if method == "config/providers":
            return {
                "providers": [
                    {"Name": "local", "Description": "Local Disk"},
                    {"Name": "alias", "Description": "Alias"},
                    {"Name": "webdav", "Description": "WebDAV"},
                ]
            }
        if method == "config/listremotes":
            return {"remotes": ["safe:"]}
        if method == "config/create":
            if config:
                return {
                    "State": "step1",
                    "Option": {"Name": "password_command", "Required": False},
                }
            return {}
        if method == "config/update":
            return {"State": ""}
        if method == "config/get":
            return dict(staged_params)
        if method == "config/delete":
            return {}
        raise AssertionError(method)

    monkeypatch.setattr(api_remotes, "_rc", fake_rc)
    app = FastAPI()
    app.include_router(api_remotes.router)
    app.dependency_overrides[auth.require_auth] = lambda: "admin"
    app.dependency_overrides[require_csrf] = lambda: None
    client = TestClient(app)
    client.cookies.set(auth.SESSION_COOKIE, "sess-1")

    try:
        providers = client.get("/api/remotes/providers").json()["providers"]
        assert [p["name"] for p in providers] == ["webdav"]

        for blocked in ("local", "alias"):
            resp = client.post(
                "/api/remotes/setup", json={"name": "badrem", "provider": blocked}
            )
            assert resp.status_code == 400

        started = client.post(
            "/api/remotes/setup", json={"name": "davrem", "provider": "webdav"}
        )
        assert started.status_code == 200
        view = started.json()

        # Answering a dangerous *_command option is rejected with HTTP 400
        ans_bad = client.post(
            f"/api/remotes/setup/{view['id']}/answer",
            json={"step": view["step"], "answer": "/usr/bin/id"},
        )
        assert ans_bad.status_code == 400

        # Switch option to harmless field, then inject dangerous key in staged config/get
        api_remotes._setups[view["id"]].option = {"Name": "vendor"}
        ans_ok = client.post(
            f"/api/remotes/setup/{view['id']}/answer",
            json={"step": view["step"], "answer": "other"},
        )
        assert ans_ok.status_code == 200 and ans_ok.json()["ready"] is True

        staged_params["token_command"] = "/usr/bin/id"
        save_bad = client.post(f"/api/remotes/setup/{view['id']}/save")
        assert save_bad.status_code == 400
    finally:
        for k in list(api_remotes._setups):
            api_remotes._discard(k)


def test_unsafe_rclone_config_blocks_routes_and_jobs_before_spawning_rclone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    unsafe_conf = tmp_path / "unsafe-rclone.conf"
    unsafe_conf.write_text(
        "[evil]\ntype = sftp\nssh = /tmp/evil.sh\n", encoding="utf-8"
    )
    monkeypatch.setenv("RCLONE_CONFIG", str(unsafe_conf))

    def never_run(*_a, **_kw):
        raise AssertionError("rclone darf bei unsicherer rclone.conf nicht starten")

    monkeypatch.setattr(subprocess, "run", never_run)
    monkeypatch.setattr(subprocess, "Popen", never_run)

    # 1. api_browse
    with pytest.raises(HTTPException) as exc_browse:
        api_browse.browse_rclone("")
    assert exc_browse.value.status_code == 400

    # 2. api_test
    with pytest.raises(HTTPException) as exc_test:
        api_test.test_rclone(api_test.RcloneTest())
    assert exc_test.value.status_code == 400

    # 3. api_storage
    monkeypatch.setattr(
        api_storage,
        "get_config",
        lambda: rclone_sync._SnapshotConfig({"backup": {"pairs": []}}),
    )
    with pytest.raises(HTTPException) as exc_storage:
        api_storage.overview(include_remote=True)
    assert exc_storage.value.status_code == 400

    # 4. api_crypt
    with pytest.raises(HTTPException) as exc_crypt:
        api_crypt.verify("mycrypt")
    assert exc_crypt.value.status_code == 400

    # 5. selective_restore
    with pytest.raises(UnsafeRcloneConfigError):
        selective_restore._run(["rclone", "version"], timeout=5)

    # 6. time_travel & api_time_travel
    pair = {
        "id": "p1",
        "name": "CloudDocs",
        "local": str(tmp_path / "docs"),
        "remote": "evil:/docs",
        "direction": "push",
        "mode": "copy",
    }
    monkeypatch.setattr(
        api_time_travel,
        "get_config",
        lambda: SimpleNamespace(snapshot=lambda: {"backup": {"pairs": [pair]}}),
    )
    with pytest.raises(HTTPException) as exc_tt:
        api_time_travel.timetravel_browse(identity="CloudDocs")
    assert exc_tt.value.status_code == 400

    with pytest.raises(recovery_points.RecoveryPointError):
        time_travel.diff_point_files(
            {"backup": {"pairs": [pair]}},
            pair,
            "readme.txt",
            from_point="current",
            to_point="current",
        )


# ---------------------------------------------------------------------------
# SEC-03: Hook call signature in _sync_pair, hooks_dir lockdown & POSIX root uid
# ---------------------------------------------------------------------------


def test_sync_pair_executes_pre_and_post_hooks_and_handles_security_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    hook_calls: list[dict] = []
    raise_on_pre = False

    def fake_run_hook(hook_name: str, **kwargs):
        hook_calls.append({"hook_name": hook_name, **kwargs})
        if raise_on_pre and kwargs.get("phase") == "pre":
            raise hooks.HookSecurityError("Hook gehört nicht root")
        return {
            "ok": True,
            "hook": hook_name,
            "phase": kwargs["phase"],
            "returncode": 0,
        }

    monkeypatch.setattr(hooks, "run_hook", fake_run_hook)
    monkeypatch.setattr(rclone_sync, "_precheck_pair", lambda _p: (True, "ok"))
    monkeypatch.setattr(rclone_sync, "_prepare_local_destinations", lambda _p: None)
    monkeypatch.setattr(rclone_sync, "_capture_local_endpoint_guards", lambda _p: {})
    monkeypatch.setattr(rclone_sync, "_set_active_pair_log", lambda *_a: None)
    monkeypatch.setattr(rclone_sync, "is_cancelled", lambda *_a: False)
    monkeypatch.setattr(
        rclone_sync,
        "_run_rclone_command",
        lambda _cmd, log_file, **_kw: (
            Path(log_file).write_text("", encoding="utf-8") and 0
        ),
    )
    monkeypatch.setattr(rclone_sync, "get_db", lambda: None)
    monkeypatch.setattr(
        rclone_sync.runtime_state, "update_pair", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(
        rclone_sync.protection, "preflight_anomaly_guard", lambda *_a, **_kw: {}
    )
    monkeypatch.setattr(
        rclone_sync.protection, "record_anomaly_baseline", lambda *_a, **_kw: None
    )

    pair = {
        "name": "Dok",
        "local": str(tmp_path / "dok"),
        "remote": "cloud:/Dok",
        "direction": "push",
        "mode": "copy",
        "pre_hook": "freeze.sh",
        "post_hook": "thaw.sh",
    }
    snap = {"paths": {}, "backup": {}}
    summary = rclone_sync._sync_pair(
        pair, [], tmp_path, False, 60, None, "run-1", snap, None
    )
    assert summary["ok"] is True
    assert len(hook_calls) == 2
    assert hook_calls[0] == {
        "hook_name": "freeze.sh",
        "phase": "pre",
        "pair_name": "Dok",
        "direction": "push",
        "mode": "copy",
        "dry_run": False,
        "status": "",
        "config_snapshot": snap,
    }
    assert hook_calls[1] == {
        "hook_name": "thaw.sh",
        "phase": "post",
        "pair_name": "Dok",
        "direction": "push",
        "mode": "copy",
        "dry_run": False,
        "status": "ok",
        "config_snapshot": snap,
    }

    # Security error in pre_hook fails the pair cleanly without crashing _sync_pair
    hook_calls.clear()
    raise_on_pre = True
    failed_summary = rclone_sync._sync_pair(
        pair, [], tmp_path, False, 60, None, "run-2", snap, None
    )
    assert failed_summary["ok"] is False
    assert failed_summary["skipped"] is True
    assert "Hook gehört nicht root" in failed_summary["error"]
    assert len(hook_calls) == 1


def test_config_validation_locks_hooks_dir_to_etc_default(tmp_path: Path):
    base = {
        "web": {"username": "admin", "local_browse_roots": [str(tmp_path)]},
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "logs_dir": str(tmp_path / "logs"),
            "temp_dir": str(tmp_path / "temp"),
            "hooks_dir": "/etc/rclone-sync/hooks.d",
        },
        "backup": {"pairs": [], "default_schedule": "manual"},
    }
    normalized, _ = validate_config(base)
    assert normalized["paths"]["hooks_dir"] == "/etc/rclone-sync/hooks.d"

    base["paths"]["hooks_dir"] = str(tmp_path / "custom-hooks")
    with pytest.raises(ConfigValidationError, match="hooks_dir"):
        validate_config(base)


def test_hooks_posix_requires_root_uid_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    script = tmp_path / "hook.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    fake_stat = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o755,
        st_uid=1000,
        st_size=18,
    )
    monkeypatch.setattr(hooks.os, "name", "posix")
    monkeypatch.setattr(hooks.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(Path, "is_symlink", lambda _self: False)
    monkeypatch.setattr(Path, "lstat", lambda _self: fake_stat)

    rejected = hooks.inspect_hook_file(script)
    assert rejected["ok"] is False
    assert "root" in rejected["reason"]

    allowed_for_test = hooks.inspect_hook_file(script, allow_current_uid=True)
    assert allowed_for_test["ok"] is True

    fake_stat.st_uid = 0
    allowed_root = hooks.inspect_hook_file(script)
    assert allowed_root["ok"] is True


# ---------------------------------------------------------------------------
# SEC-04: Boot drill QEMU escaping, PBS snapshot metadata & _ALLOWED_JOB_KINDS
# ---------------------------------------------------------------------------


def test_boot_drill_sec04_hardening(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert "bootdrill" in api_jobs._ALLOWED_JOB_KINDS

    base = {
        "web": {"username": "admin", "local_browse_roots": [str(tmp_path)]},
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "logs_dir": str(tmp_path / "logs"),
            "temp_dir": str(tmp_path / "temp"),
        },
        "backup": {"pairs": [], "default_schedule": "manual"},
        "pbs": {
            "enabled": False,
            "boot_drill": {"scratch_dir": "/opt/rclone-sync/bad,dir"},
        },
    }
    with pytest.raises(ConfigValidationError, match="scratch_dir"):
        validate_config(base)

    base["pbs"]["boot_drill"]["scratch_dir"] = "/opt/rclone-sync/bad:dir"
    with pytest.raises(ConfigValidationError, match="scratch_dir"):
        validate_config(base)

    # Malicious PBS snapshot metadata with leading '-' or traversal archive must fail before restore
    monkeypatch.setattr(boot_drill.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(boot_drill, "notify", lambda *_a, **_kw: None)
    restore_called = False

    def unexpected_restore(*_a, **_kw):
        nonlocal restore_called
        restore_called = True
        return 0

    monkeypatch.setattr(boot_drill, "_run_rclone_command", unexpected_restore)

    cfg = {
        "paths": {
            "logs_dir": str(tmp_path / "logs"),
            "temp_dir": str(tmp_path / "temp"),
        },
        "backup": {"timezone": "Europe/Berlin"},
        "pbs": {
            "enabled": True,
            "repository": "drill@pbs!tok@pbs.test:store",
            "password": "sec",
            "boot_drill": {"group": "vm/100"},
        },
    }

    bad_group_snaps = [
        {
            "backup-type": "vm",
            "backup-id": "-evil",
            "backup-time": 1700100000,
            "files": [{"filename": "drive-scsi0.img.fidx", "size": 512}],
        }
    ]
    res1 = boot_drill.run_boot_drill(
        config_snapshot=cfg,
        run=lambda cmd, **_kw: subprocess.CompletedProcess(
            cmd, 0, json.dumps(bad_group_snaps), ""
        ),
    )
    assert res1["ok"] is False and "Snapshot-Gruppe" in res1["error"]
    assert restore_called is False

    bad_archive_snaps = [
        {
            "backup-type": "vm",
            "backup-id": "100",
            "backup-time": 1700100000,
            "files": [{"filename": "--option.img.fidx", "size": 512}],
        }
    ]
    res2 = boot_drill.run_boot_drill(
        config_snapshot=cfg,
        run=lambda cmd, **_kw: subprocess.CompletedProcess(
            cmd, 0, json.dumps(bad_archive_snaps), ""
        ),
    )
    assert res2["ok"] is False and "Archivname" in res2["error"]
    assert restore_called is False


def test_boot_drill_escapes_commas_in_qemu_drive_and_chardev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    scratch_with_comma = tmp_path / "temp,comma"
    scratch_with_comma.mkdir()
    captured_cmds: list[list[str]] = []

    monkeypatch.setattr(boot_drill.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(boot_drill, "notify", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        boot_drill,
        "_run_rclone_command",
        lambda cmd, _lf, **_kw: Path(cmd[4]).write_bytes(b"\0" * 512) and 0,
    )

    class _DummyProc:
        pid = 2**22 + 9
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return -15

    def fake_popen(cmd, **_kw):
        captured_cmds.append(cmd)
        return _DummyProc()

    class _PingOkSock:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def sendall(self, _d):
            pass

        def recv(self, _n):
            return b'{"return": {}}\n'

    cfg = {
        "paths": {
            "logs_dir": str(tmp_path / "logs"),
            "temp_dir": str(scratch_with_comma),
        },
        "backup": {"timezone": "Europe/Berlin"},
        "pbs": {
            "enabled": True,
            "repository": "drill@pbs!tok@pbs.test:store",
            "password": "sec",
            "boot_drill": {"group": "vm/100"},
        },
    }
    snaps = [
        {
            "backup-type": "vm",
            "backup-id": "100",
            "backup-time": 1700100000,
            "files": [{"filename": "drive-scsi0.img.fidx", "size": 512}],
        }
    ]
    res = boot_drill.run_boot_drill(
        config_snapshot=cfg,
        run=lambda cmd, **_kw: subprocess.CompletedProcess(
            cmd, 0, json.dumps(snaps), ""
        ),
        popen=fake_popen,
        connect=lambda _p: _PingOkSock(),
    )
    assert res["ok"] is True
    qemu_cmd = captured_cmds[0]
    drive_arg = qemu_cmd[qemu_cmd.index("-drive") + 1]
    chardev_arg = qemu_cmd[qemu_cmd.index("-chardev") + 1]
    assert "temp,,comma" in drive_arg
    assert "temp,,comma" in chardev_arg


# ---------------------------------------------------------------------------
# SEC-05, SEC-07, SEC-08, SEC-09
# ---------------------------------------------------------------------------


def test_api_test_restricts_unsaved_local_paths_to_browse_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    saved_outside = tmp_path / "saved-outside"
    saved_outside.mkdir()
    forbidden_outside = tmp_path / "forbidden-outside"
    forbidden_outside.mkdir()

    safe_rclone_conf = tmp_path / "rclone.conf"
    safe_rclone_conf.write_text("[cloud]\ntype = s3\n", encoding="utf-8")
    monkeypatch.setenv("RCLONE_CONFIG", str(safe_rclone_conf))

    cfg_path = tmp_path / "config.yaml"
    raw = {
        "web": {
            "username": "admin",
            "secret_key": "api-test-browse-root-secret-0123456789ABCDEF",
            "local_browse_roots": [str(allowed_root)],
        },
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "logs_dir": str(tmp_path / "logs"),
            "temp_dir": str(tmp_path / "temp"),
        },
        "backup": {
            "enabled": True,
            "default_schedule": "manual",
            "pairs": [
                {
                    "name": "SavedPair",
                    "local": str(saved_outside),
                    "remote": "cloud:/saved",
                    "direction": "push",
                    "mode": "copy",
                }
            ],
        },
    }
    norm, _ = validate_config(raw)
    cfg_path.write_text(yaml.safe_dump(norm), encoding="utf-8")
    monkeypatch.setattr(config_store, "_config", Config(cfg_path))
    monkeypatch.setattr(
        api_test.subprocess,
        "run",
        lambda cmd, **_kw: subprocess.CompletedProcess(
            cmd,
            0,
            "cloud:\n" if cmd[1] == "listremotes" else '{"count":1,"bytes":10}',
            "",
        ),
    )

    # Unsaved pair pointing to forbidden_outside -> 403
    with pytest.raises(HTTPException) as exc_forb:
        api_test.test_rclone(
            api_test.RcloneTest(
                pair={
                    "name": "Probe",
                    "local": str(forbidden_outside),
                    "remote": "cloud:/x",
                    "direction": "push",
                    "mode": "copy",
                }
            )
        )
    assert exc_forb.value.status_code == 403

    # Unsaved pair reusing already saved path -> 200 OK
    ok_saved = api_test.test_rclone(
        api_test.RcloneTest(
            pair={
                **norm["backup"]["pairs"][0],
                "remote": "cloud:/saved-new",
            }
        )
    )
    assert ok_saved["ok"] is True


def test_bootstrap_export_kdf_cap_mkstemp_and_include_rclone_conf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rclone_conf = tmp_path / "rclone.conf"
    rclone_conf.write_text("[cloud]\ntype = s3\n", encoding="utf-8")
    monkeypatch.setenv("RCLONE_CONFIG", str(rclone_conf))

    pw_hash = bcrypt.hashpw(
        b"correct-admin-password", bcrypt.gensalt(rounds=4)
    ).decode()
    cfg_path = tmp_path / "config.yaml"
    raw = {
        "web": {
            "username": "admin",
            "password_hash": pw_hash,
            "secret_key": "bootstrap-export-secret-0123456789ABCDEFGH",
            "local_browse_roots": [str(tmp_path)],
        },
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "logs_dir": str(tmp_path / "logs"),
            "temp_dir": str(tmp_path / "temp"),
        },
        "backup": {"pairs": [], "default_schedule": "manual"},
        "maintenance": {
            "bootstrap_export": {"enabled": True, "target_dir": str(tmp_path / "auto")}
        },
    }
    norm, _ = validate_config(raw)
    cfg_path.write_text(yaml.safe_dump(norm), encoding="utf-8")
    monkeypatch.setattr(config_store, "_config", Config(cfg_path))
    database = Database(tmp_path / "boot.db")
    monkeypatch.setattr(db_module, "_db", database)

    # 1. Iterations > 1_000_000 rejected before PBKDF2 runs
    bundle = bootstrap_export.create_encrypted_bootstrap_bundle(
        norm, passphrase="strong-passphrase-123"
    )
    tampered = {**bundle, "iterations": 1_000_001}
    with pytest.raises(
        bootstrap_export.BootstrapExportError, match="Ungültige KDF-Iterationen"
    ):
        bootstrap_export.decrypt_bootstrap_bundle(
            tampered, passphrase="strong-passphrase-123"
        )

    # 2. Automatic bootstrap export uses mkstemp in target_dir
    mkstemp_dirs: list[str | None] = []
    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(*args, **kwargs):
        mkstemp_dirs.append(kwargs.get("dir"))
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(bootstrap_export.tempfile, "mkstemp", spy_mkstemp)
    auto_out = bootstrap_export.write_automatic_bootstrap_export(norm, revision="rev99")
    assert auto_out is not None and auto_out["ok"] is True
    assert mkstemp_dirs == [str(tmp_path / "auto"), str(tmp_path / "auto")]

    # 3. API include_rclone_conf requires reauth and includes rclone.conf when True
    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/config/bootstrap-export",
            "headers": [],
            "client": ("127.0.0.1", 5000),
        }
    )
    with pytest.raises(HTTPException) as bad_reauth:
        api_config.export_encrypted_bootstrap(
            req,
            api_config.BootstrapExportRequest(
                current_password="wrong-password",
                passphrase="strong-passphrase-123",
                include_rclone_conf=True,
            ),
            user="admin",
        )
    assert bad_reauth.value.status_code == 403

    ok_resp = api_config.export_encrypted_bootstrap(
        req,
        api_config.BootstrapExportRequest(
            current_password="correct-admin-password",
            passphrase="strong-passphrase-123",
            include_rclone_conf=True,
        ),
        user="admin",
    )
    decrypted = bootstrap_export.decrypt_bootstrap_bundle(
        ok_resp["bundle"], passphrase="strong-passphrase-123"
    )
    assert "[cloud]" in decrypted["rclone_conf"]


def test_download_staging_archive_spools_into_configured_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "target"
    target.mkdir()
    (target / "hello.txt").write_bytes(b"hello world")
    configured_temp = tmp_path / "private-temp"
    pair = {
        "id": "spool-pair",
        "name": "SpoolPair",
        "direction": "push",
        "mode": "copy",
        "local": str(tmp_path / "src"),
        "remote": str(target),
    }
    config = {
        "paths": {
            "data_dir": str(tmp_path / "server"),
            "temp_dir": str(configured_temp),
            "recovery_dir": str(tmp_path / "staging"),
            "recovery_snapshots_dir": str(tmp_path / "points"),
        },
        "backup": {"pairs": [pair]},
    }
    database = Database(tmp_path / "spool.db")
    point = recovery_snapshots.capture_snapshot(config, pair, max_total_mb=1)
    job_id = database.job_start("recovery")
    restored = recovery_snapshots.run_snapshot_restore(
        database, config, pair, point_id=point["id"], max_total_mb=1, job_id=job_id
    )
    monkeypatch.setattr(api_recovery, "get_db", lambda: database)
    monkeypatch.setattr(
        api_recovery, "get_config", lambda: SimpleNamespace(snapshot=lambda: config)
    )

    seen_dirs: list[str | None] = []
    real_spooled = tempfile.SpooledTemporaryFile

    def spy_spooled(*args, **kwargs):
        seen_dirs.append(kwargs.get("dir"))
        return real_spooled(*args, **kwargs)

    monkeypatch.setattr(api_recovery.tempfile, "SpooledTemporaryFile", spy_spooled)
    response = api_recovery.download_staging_archive(restored["id"], user="admin")

    async def consume():
        return b"".join([chunk async for chunk in response.body_iterator])

    raw_zip = asyncio.run(consume())
    assert seen_dirs == [str(configured_temp)]
    assert configured_temp.is_dir()
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
        assert zf.read("hello.txt") == b"hello world"


def test_webauthn_reauth_lockout_revokes_session_and_returns_429(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "web": {
                    "username": "admin",
                    "secret_key": "webauthn-reauth-lockout-secret-0123456789AB",
                    "session_version": 1,
                    "login_max_failures": 3,
                    "login_window_seconds": 300,
                    "login_lock_seconds": 120,
                }
            }
        ),
        encoding="utf-8",
    )
    store = Config(cfg_path)
    database = Database(tmp_path / "reauth.db")
    monkeypatch.setattr(config_store, "_config", store)
    monkeypatch.setattr(db_module, "_db", database)
    monkeypatch.setattr(
        api_webauthn,
        "finish_authentication",
        lambda *_a, **_kw: (_ for _ in ()).throw(ValueError("bad assertion")),
    )

    session_token = auth.create_session("admin")
    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/webauthn/reauth/verify",
            "headers": [
                (b"cookie", f"{auth.SESSION_COOKIE}={session_token}".encode("ascii"))
            ],
            "client": ("198.51.100.42", 44321),
        }
    )

    for attempt in range(3):
        with pytest.raises(HTTPException) as exc:
            api_webauthn.complete_reauth(
                api_webauthn.CeremonyResponse(challenge_id="c" * 24, credential={}),
                req,
                user="admin",
            )
        if attempt < 2:
            assert exc.value.status_code == 400
        else:
            assert exc.value.status_code == 429
            assert exc.value.headers["Retry-After"]

    # Existing session must now be revoked because session_version was bumped
    assert auth.session_user(session_token) is None
    assert store.get("web", "session_version") == 2


def test_same_origin_rejects_http_origin_when_request_is_https(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "web": {
                    "username": "admin",
                    "secret_key": "same-origin-https-secret-0123456789ABCDEFG",
                    "allowed_hosts": ["backup.example.de"],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_store, "_config", Config(cfg_path))

    def make_req(scheme: str, origin: str, host: str = "backup.example.de") -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "scheme": scheme,
                "path": "/api/jobs/backup/run",
                "headers": [
                    (b"host", host.encode("ascii")),
                    (b"origin", origin.encode("ascii")),
                ],
                "client": ("127.0.0.1", 12345),
            }
        )

    # HTTP sibling against HTTPS request is rejected (both same netloc and allowed_hosts fallback)
    assert main._same_origin(make_req("https", "http://backup.example.de")) is False
    assert (
        main._same_origin(
            make_req("https", "http://backup.example.de:8080", host="127.0.0.1:8000")
        )
        is False
    )
    # Matching HTTPS origin succeeds
    assert main._same_origin(make_req("https", "https://backup.example.de")) is True
    # Reverse-proxy case (browser sends https Origin, backend sees http) still succeeds
    assert main._same_origin(make_req("http", "https://backup.example.de")) is True


# ---------------------------------------------------------------------------
# Task 7: Restore-Drill Egress Throttling (scheduled vs manual)
# ---------------------------------------------------------------------------


def test_restore_test_throttles_scheduled_runs_only_not_manual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database = Database(tmp_path / "quota.db")
    monkeypatch.setattr(db_module, "_db", database)
    # Exceed 1 GiB monthly egress budget
    jid = database.job_start("restoretest")
    database.job_finish(jid, "ok", {"ok": True, "total_bytes": 2 * (1024**3)})

    pair_calls: list[str] = []
    monkeypatch.setattr(
        restore_test,
        "run_pair_restore_test",
        lambda pair, **_kw: (
            pair_calls.append(pair["name"])
            or {"name": pair["name"], "ok": True, "verified": 1, "sample_size": 1}
        ),
    )
    monkeypatch.setattr(restore_test, "reset_cancel", lambda *_a, **_kw: None)
    monkeypatch.setattr(restore_test, "is_cancelled", lambda *_a, **_kw: False)
    monkeypatch.setattr(restore_test, "_notify_result", lambda *_a, **_kw: None)

    snapshot = {
        "paths": {
            "temp_dir": str(tmp_path / "temp"),
            "logs_dir": str(tmp_path / "logs"),
        },
        "backup": {
            "restore_test": {"enabled": True, "schedule": "0 4 * * *"},
            "transfer_budget": {
                "enabled": True,
                "monthly_egress_gb": 1,
                "monthly_upload_gb": 0,
                "warn_percent": 80,
                "throttle_optional_jobs": True,
            },
            "pairs": [
                {
                    "name": "Docs",
                    "local": str(tmp_path / "docs"),
                    "remote": "cloud:/Docs",
                    "direction": "push",
                    "mode": "copy",
                    "enabled": True,
                }
            ],
        },
    }

    # Scheduled run is throttled
    scheduled_res = restore_test.run_restore_test(
        trigger="scheduler", config_snapshot=snapshot
    )
    assert scheduled_res["throttled"] is True
    assert scheduled_res["skipped"] is True
    assert pair_calls == []

    # Manual run executes normally despite budget
    manual_res = restore_test.run_restore_test(
        trigger="manual", config_snapshot=snapshot
    )
    assert manual_res["ok"] is True
    assert manual_res.get("throttled") is not True
    assert pair_calls == ["Docs"]


def test_remote_reachable_explains_disabled_google_drive_api(
    monkeypatch: pytest.MonkeyPatch,
):
    stderr = (
        "2026/11/19 19:02:14 NOTICE: Failed to lsjson: couldn't list directory: "
        "googleapi: Error 403: Google Drive API has not been used in project "
        "590686565400 before or it is disabled."
    )
    monkeypatch.setattr(
        rclone_sync.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, stdout="", stderr=stderr),
    )
    ok, msg = rclone_sync._remote_reachable("gdrive:/Fotos", allow_missing=True)
    assert ok is False
    assert "590686565400" in msg
    assert "Google Drive API" in msg
    assert "aktivieren" in msg
