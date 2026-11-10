from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app import (
    auth,
    bootstrap_export,
    cli,
    hooks,
    pbs_api,
    protection,
    ransomware_guard,
    rclone_args,
)
from app.db import Database
from app.routes import api_config, api_remotes


def test_audit_hash_chain_verifies_and_detects_tampering(tmp_path: Path):
    db = Database(tmp_path / "state.db")
    db.audit_add("config_saved", actor="admin", details={"rev": "1"})
    db.audit_add("scheduler_paused", actor="admin", details={"minutes": 30})
    db.audit_add("backup_requested", actor="admin", details={"dry_run": True})

    status = db.audit_verify_chain()
    assert status["ok"] is True
    assert status["checked"] == 3
    assert status["broken_at_id"] is None

    with db.conn() as conn:
        conn.execute("UPDATE audit_events SET actor = 'attacker' WHERE id = 2")

    tampered = db.audit_verify_chain()
    assert tampered["ok"] is False
    assert tampered["broken_at_id"] == 2


def test_readonly_sqlite_connection_and_single_query_stats(tmp_path: Path):
    db = Database(tmp_path / "state.db")
    job_id = db.job_start("backup", dry_run=True)
    db.job_finish(job_id, "ok", {"ok_count": 1})
    stats = db.stats()
    assert stats["jobs"] == 1
    assert stats["running"] == 0
    assert stats["bytes"] > 0


def test_rclone_conf_security_and_oauth_expiry_inspection(tmp_path: Path):
    conf = tmp_path / "rclone.conf"
    expired_iso = "2020-05-01T12:00:00Z"
    conf.write_text(
        f"[safe]\ntype = s3\nprovider = Cloudflare\n\n"
        f'[cloud]\ntype = drive\ntoken = {{"access_token":"secret","expiry":"{expired_iso}"}}\n',
        encoding="utf-8",
    )
    report = rclone_args.inspect_rclone_config(conf)
    assert report["found"] is True
    assert report["dangerous_directives"] == []
    assert len(report["expiring_tokens"]) == 1
    assert report["expiring_tokens"][0]["remote"] == "cloud"
    assert report["expiring_tokens"][0]["expired"] is True
    rclone_args.assert_safe_rclone_config(conf)

    # Gefährliche Ausführungs-Direktive in rclone.conf muss abgelehnt werden
    conf.write_text(
        "[evil]\ntype = sftp\nkey_use_agent = false\nssh = /tmp/evil.sh\n",
        encoding="utf-8",
    )
    with pytest.raises(rclone_args.UnsafeRcloneConfigError):
        rclone_args.assert_safe_rclone_config(conf)


def test_allowlisted_hooks_validation_and_discovery(tmp_path: Path):
    hooks_dir = tmp_path / "hooks.d"
    hooks_dir.mkdir()
    script = hooks_dir / "pre-check.sh"
    script.write_text("#!/bin/sh\necho hook-ok\n", encoding="utf-8")
    script.chmod(0o755)

    assert hooks.validate_hook_name("pre-check.sh") == "pre-check.sh"
    with pytest.raises(ValueError):
        hooks.validate_hook_name("../etc/passwd")
    with pytest.raises(ValueError):
        hooks.validate_hook_name("sub/script.sh")

    cfg = {"paths": {"hooks_dir": str(hooks_dir)}}
    discovered = hooks.list_available_hooks(cfg)
    assert len(discovered["hooks"]) == 1
    assert discovered["hooks"][0]["name"] == "pre-check.sh"


def test_encrypted_bootstrap_export_and_cli_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    filter_file = tmp_path / "filters.txt"
    filter_file.write_text("- *.tmp\n", encoding="utf-8")
    export_dir = tmp_path / "exports"

    secret_key = "0123456789abcdef0123456789abcdef"
    cfg = {
        "web": {"username": "admin", "secret_key": secret_key},
        "paths": {"data_dir": str(tmp_path)},
        "backup": {"filter_file": str(filter_file), "pairs": []},
        "maintenance": {
            "bootstrap_export": {
                "enabled": True,
                "target_dir": str(export_dir),
            }
        },
    }
    auto_res = bootstrap_export.write_automatic_bootstrap_export(
        cfg, revision="rev12345"
    )
    assert auto_res is not None
    assert auto_res["ok"] is True

    latest_path = Path(auto_res["path"])
    assert latest_path.is_file()

    restored = bootstrap_export.decrypt_bootstrap_bundle(
        json.loads(latest_path.read_text(encoding="utf-8")),
        passphrase=secret_key,
    )
    assert restored["config"]["web"]["username"] == "admin"
    assert restored["filter_file_content"] == "- *.tmp\n"

    with pytest.raises(bootstrap_export.BootstrapExportError):
        bootstrap_export.decrypt_bootstrap_bundle(
            json.loads(latest_path.read_text(encoding="utf-8")),
            passphrase="wrong-passphrase-12345",
        )

    restore_target = tmp_path / "restored-config.yaml"
    monkeypatch.setenv("RCLONE_SYNC_CONFIG", str(restore_target))
    rc = cli.main(
        [
            "bootstrap-restore",
            str(latest_path),
            "--passphrase",
            secret_key,
            "--yes",
        ]
    )
    assert rc == 0
    assert restore_target.is_file()


def test_live_filter_tester_evaluates_rules():
    body = api_config.FilterTestRequest(
        rules="- *.tmp\n- node_modules/**\n+ *.pdf\n- *\n",
        paths=["bericht.pdf", "cache.tmp", "app/node_modules/pkg/index.js", "foto.jpg"],
    )
    res = api_config.test_filter_rules(body)
    by_path = {item["path"]: item for item in res["results"]}
    assert by_path["bericht.pdf"]["included"] is True
    assert by_path["cache.tmp"]["included"] is False
    assert by_path["app/node_modules/pkg/index.js"]["included"] is False
    assert by_path["foto.jpg"]["included"] is False


def test_transfer_volume_anomaly_trend_guard(tmp_path: Path):
    db = Database(tmp_path / "state.db")
    pair = {
        "name": "Fotos",
        "direction": "push",
        "mode": "sync",
        "local": "/mnt/fotos",
        "remote": "cloud:/Fotos",
    }
    for _ in range(4):
        jid = db.job_start("backup", dry_run=False)
        db.job_finish(
            jid,
            "ok",
            {
                "dry_run": False,
                "pairs": [
                    {
                        "name": "Fotos",
                        "ok": True,
                        "dry_run": False,
                        "transferred": "20 MiB",
                    }
                ],
            },
        )

    res = protection.evaluate_transfer_trend(
        db,
        pair=pair,
        current_transferred="200 MiB",
    )
    assert res["evaluated"] is True
    assert res["spike_detected"] is True
    assert res["ratio"] == 10.0

    parsed = protection.parse_transferred_bytes("100 MiB")
    assert parsed == 100 * 1024**2


def test_webauthn_stepup_token_single_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from starlette.requests import Request

    db = Database(tmp_path / "webauthn.db")
    monkeypatch.setattr(auth, "get_db", lambda: db)
    monkeypatch.setattr(
        auth, "account_generation", lambda: "fixed-generation-0123456789abcdef"
    )

    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/webauthn/reauth/verify",
            "headers": [(b"user-agent", b"pytest")],
            "client": ("127.0.0.1", 12345),
        }
    )
    token = auth.issue_webauthn_reauth_token(req, "admin")
    assert token.startswith(auth.WEBAUTHN_REAUTH_PREFIX)
    gen = auth.consume_webauthn_reauth_token(req, "admin", token)
    assert isinstance(gen, str) and len(gen) >= 16
    assert auth.consume_webauthn_reauth_token(req, "admin", token) is None


def test_incremental_ransomware_scan_and_canary_nofollow(tmp_path: Path):
    src = tmp_path / "source"
    src.mkdir()
    for idx in range(35):
        (src / f"file_{idx:02d}.txt").write_text("ok", encoding="utf-8")
    pair = {
        "name": "Dok",
        "direction": "push",
        "local": str(src),
        "canary_file": ".canary.txt",
    }
    init_res = ransomware_guard.init_canary(pair)
    assert init_res["ok"] is True

    chk = ransomware_guard.check_canary(pair)
    assert chk["ok"] is True

    ransomware_guard._DIR_WATERMARK_CACHE.clear()
    scan1 = ransomware_guard.scan_suspect_extensions(str(src))
    scan2 = ransomware_guard.scan_suspect_extensions(str(src))
    assert scan1["blocked"] is False and scan2["blocked"] is False
    assert scan2["cached_dirs"] >= 1


def test_pbs_verify_summary_and_remote_oauth_extraction(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        pbs_api,
        "_fetch",
        lambda _cfg, _path, _typ: (
            [
                {
                    "verification": {"state": "ok", "upid": "UPID:1"},
                    "backup-time": int(time.time()),
                },
                {"verification": {"state": "failed"}, "backup-time": int(time.time())},
                {"backup-time": int(time.time())},
            ],
            None,
        ),
    )
    summary = pbs_api.verify_summary({"enabled": True})
    assert summary["ok"] is False
    assert summary["total"] == 3
    assert summary["total_snapshots"] == 3
    assert summary["verified"] == 1
    assert summary["verified_ok"] == 1
    assert summary["failed"] == 1
    assert summary["unverified"] == 1

    setup = api_remotes.Setup(
        owner="owner1",
        name="gdrive",
        provider="drive",
        directory=None,
        state="question",
        option={
            "Name": "config_token",
            "Help": (
                "Use web browser to automatically authenticate rclone with remote?\n"
                "If your browser doesn't open automatically go to the following link: http://127.0.0.1:53682/auth?state=abc\n"
                'Or run: rclone authorize "drive" "eyJ..."'
            ),
        },
    )
    view = api_remotes._view("key1", setup)
    assert view["option"]["auth_url"] == "http://127.0.0.1:53682/auth?state=abc"
    assert 'rclone authorize "drive"' in view["option"]["authorize_command"]


def test_sensitive_config_changed_detects_bootstrap_export_and_hooks():
    base = {
        "web": {"username": "admin"},
        "paths": {"data_dir": "/opt/rclone-sync/data"},
        "maintenance": {"bootstrap_export": {"enabled": False, "target_dir": ""}},
        "backup": {
            "pairs": [
                {
                    "name": "Dok",
                    "direction": "push",
                    "mode": "copy",
                    "local": "/mnt/dok",
                    "remote": "cloud:/Dok",
                    "pre_hook": "",
                    "post_hook": "",
                }
            ]
        },
    }
    assert api_config._sensitive_config_changed(base, base) is False

    changed_export = json.loads(json.dumps(base))
    changed_export["maintenance"]["bootstrap_export"]["enabled"] = True
    assert api_config._sensitive_config_changed(base, changed_export) is True

    changed_dir = json.loads(json.dumps(base))
    changed_dir["maintenance"]["bootstrap_export"]["target_dir"] = "/mnt/usb/boot"
    assert api_config._sensitive_config_changed(base, changed_dir) is True

    changed_hook = json.loads(json.dumps(base))
    changed_hook["backup"]["pairs"][0]["pre_hook"] = "freeze.sh"
    assert api_config._sensitive_config_changed(base, changed_hook) is True


def test_doctor_and_system_bundle_with_rclone_conf_and_audit_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.routes import api_diagnostics, api_maintenance, api_ransomware

    db = Database(tmp_path / "diag.db")
    db.audit_add("config_saved", actor="admin", details={"rev": "1"})

    conf_file = tmp_path / "rclone.conf"
    conf_file.touch(mode=0o600)
    conf_file.chmod(0o600)
    conf_file.write_text(
        '[cloud]\ntype = drive\ntoken = {"access_token":"x","expiry":"2020-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )

    snap_data = {
        "web": {"username": "admin"},
        "paths": {
            "data_dir": str(tmp_path),
            "logs_dir": str(tmp_path),
            "temp_dir": str(tmp_path),
        },
        "backup": {"pairs": [], "jobs": []},
        "maintenance": {},
    }

    class _DummyCfg:
        def snapshot(self):
            return snap_data

        def get(self, *keys: str, default=None):
            return api_diagnostics._SnapshotConfig(snap_data).get(
                *keys, default=default
            )

    dummy_cfg = _DummyCfg()

    monkeypatch.setattr(api_diagnostics, "get_db", lambda: db)
    monkeypatch.setattr(api_diagnostics, "get_config", lambda: dummy_cfg)
    monkeypatch.setattr(api_maintenance, "get_db", lambda: db)
    monkeypatch.setattr(api_maintenance, "get_config", lambda: dummy_cfg)
    monkeypatch.setattr(api_ransomware, "get_db", lambda: db)
    monkeypatch.setattr(api_ransomware, "get_config", lambda: dummy_cfg)
    monkeypatch.setattr(
        api_diagnostics,
        "inspect_rclone_config",
        lambda: rclone_args.inspect_rclone_config(conf_file),
    )

    doc = api_diagnostics.doctor()
    check_names = {c["name"]: c for c in doc["checks"]}
    assert "Audit-Hash-Kette" in check_names
    assert check_names["Audit-Hash-Kette"]["level"] == "ok"
    assert "1 verkettete Ereignisse" in check_names["Audit-Hash-Kette"]["message"]
    assert "rclone.conf Sicherheit" in check_names
    assert check_names["rclone.conf Sicherheit"]["level"] == "ok"
    assert "OAuth-Token (cloud)" in check_names
    assert check_names["OAuth-Token (cloud)"]["level"] == "error"

    bundle = api_diagnostics.system_bundle()
    assert "audit" in bundle and "events" in bundle["audit"]
    assert bundle["audit"]["chain"]["ok"] is True
    assert "snapshots" in bundle and "snapshots" in bundle["snapshots"]
    assert "ransomware" in bundle and "pairs" in bundle["ransomware"]


def test_cli_bootstrap_export_verify_audit_and_api_bootstrap_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db = Database(tmp_path / "cli_audit.db")
    db.audit_add("config_saved", actor="admin", details={"rev": "1"})
    monkeypatch.setattr(cli, "get_db", lambda: db)

    assert cli.main(["verify-audit"]) == 0

    hooks_dir = tmp_path / "hooks.d"
    hooks_dir.mkdir()
    snap_data = {
        "web": {"username": "admin", "secret_key": "0123456789abcdef0123456789abcdef"},
        "paths": {"data_dir": str(tmp_path), "hooks_dir": str(hooks_dir)},
        "backup": {
            "pairs": [{"name": "Fotos", "local": "/a", "remote": "b:/c"}],
            "jobs": [],
        },
        "maintenance": {},
    }

    class _Cfg:
        def snapshot(self):
            return snap_data

    monkeypatch.setattr(cli, "get_config", lambda: _Cfg())
    assert cli.main(["hooks"]) == 0

    out_bundle = tmp_path / "exported.enc.json"
    rc = cli.main(
        [
            "bootstrap-export",
            str(out_bundle),
            "--passphrase",
            "strong-passphrase-12345",
            "--without-rclone-conf",
        ]
    )
    assert rc == 0
    assert out_bundle.is_file()

    envelope = json.loads(out_bundle.read_text(encoding="utf-8"))
    verified = api_config.verify_encrypted_bootstrap(
        api_config.BootstrapVerifyRequest(
            bundle=envelope, passphrase="strong-passphrase-12345"
        ),
        user="admin",
    )
    assert verified["ok"] is True
    assert verified["pairs_count"] == 1
    assert verified["include_secrets"] is False


def test_google_drive_express_token_extraction_and_service_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from pydantic import SecretStr

    raw_paste = (
        "Paste the following into your remote machine --->\n"
        '{"access_token": "ya29.test", "token_type": "Bearer", "expiry": "2030-01-01T00:00:00Z"}\n'
        "<---End paste"
    )
    cleaned = api_remotes._normalize_oauth_token_input(raw_paste)
    assert (
        cleaned
        == '{"access_token":"ya29.test","token_type":"Bearer","expiry":"2030-01-01T00:00:00Z"}'
    )

    wrapped_b64 = "Paste the following into your remote machine --->\neyJ0b2tlbiI6\nImFiYyJ9\n<---End paste"
    assert (
        api_remotes._normalize_oauth_token_input(wrapped_b64) == "eyJ0b2tlbiI6ImFiYyJ9"
    )

    created_remotes: dict[str, dict] = {}

    def fake_rc(method: str, payload: dict | None = None, config: str | None = None):
        payload = payload or {}
        if method == "config/listremotes":
            return {"remotes": list(created_remotes)}
        if method == "config/create":
            created_remotes[payload["name"]] = payload
            return {}
        return {}

    class _Cfg:
        def snapshot(self):
            return {"paths": {"data_dir": str(tmp_path)}}

    monkeypatch.setattr(api_remotes, "_rc", fake_rc)
    monkeypatch.setattr(api_remotes, "get_config", lambda: _Cfg())

    sa_json = json.dumps(
        {
            "type": "service_account",
            "project_id": "demo-proj",
            "private_key_id": "key1",
            "private_key": "-----BEGIN PRIVATE KEY-----\nMIIB...\n-----END PRIVATE KEY-----\n",
            "client_email": "backup-bot@demo-proj.iam.gserviceaccount.com",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )
    res = api_remotes.create_google_drive_service_account(
        api_remotes.GoogleDriveServiceAccountPayload(
            name="gdrive-sa",
            service_account_json=SecretStr(sa_json),
            scope="drive",
            root_folder_id="1AbCdEfGhIjKlMnOp",
        )
    )
    assert res["ok"] is True
    assert res["path"] == "gdrive-sa:"
    assert res["client_email"] == "backup-bot@demo-proj.iam.gserviceaccount.com"
    assert "gdrive-sa" in created_remotes
    assert (
        created_remotes["gdrive-sa"]["parameters"]["root_folder_id"]
        == "1AbCdEfGhIjKlMnOp"
    )
    sa_saved = Path(created_remotes["gdrive-sa"]["parameters"]["service_account_file"])
    assert sa_saved.is_file()

    # Browser callback URL / code extraction without local rclone
    q1 = api_remotes._extract_callback_query(
        "http://127.0.0.1:53682/?state=abc123&code=4/0AeanS0xyz&scope=https://www.googleapis.com/auth/drive",
        "fallback_state",
    )
    assert q1 is not None and "code=4%2F0AeanS0xyz" in q1 and "state=abc123" in q1

    q2 = api_remotes._extract_callback_query("4/0AeanS0barecode", "state99")
    assert q2 is not None and "code=4%2F0AeanS0barecode" in q2 and "state=state99" in q2
