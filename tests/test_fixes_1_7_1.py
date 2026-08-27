"""Regressionstests für die Fixes in 1.7.1."""

import re
from pathlib import Path

import bcrypt
import yaml
from fastapi.testclient import TestClient

from app import config_store, db
from app.config_store import Config
from app.db import Database
from app.jobs import rclone_sync, runtime_state


def _config(password: str, tmp_path: Path) -> dict:
    return {
        "web": {
            "username": "admin",
            "password": "",
            "password_hash": bcrypt.hashpw(
                password.encode(), bcrypt.gensalt(rounds=4)
            ).decode(),
            "secret_key": "test-secret-which-is-long-enough-123456789",
            "session_version": 1,
            "allowed_hosts": ["testserver"],
            "local_browse_roots": [str(tmp_path)],
        },
        "paths": {
            "data_dir": str(tmp_path),
            "logs_dir": str(tmp_path),
            "temp_dir": str(tmp_path),
        },
        "backup": {"enabled": True, "pairs": [], "default_schedule": "manual"},
    }


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(_config("very-secure-password", tmp_path)), encoding="utf-8"
    )
    store = Config(config_path)
    database = Database(tmp_path / "app.db")
    monkeypatch.setattr(config_store, "_config", store)
    monkeypatch.setattr(db, "_db", database)
    monkeypatch.setattr(runtime_state, "STATE_DIR", tmp_path / "runtime")
    monkeypatch.setattr(
        runtime_state, "RUN_FILE", tmp_path / "runtime" / "current-run.json"
    )
    monkeypatch.setattr(
        runtime_state, "CANCEL_FILE", tmp_path / "runtime" / "cancel.requested"
    )
    monkeypatch.setattr(runtime_state, "PROCS_DIR", tmp_path / "runtime" / "processes")
    from app import main

    return TestClient(main.app, base_url="http://testserver")


def _login(client: TestClient) -> None:
    login_page = client.get("/login")
    match = re.search(r'name="login_csrf" value="([^"]+)"', login_page.text)
    assert match
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "very-secure-password",
            "login_csrf": match.group(1),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_origin_null_cross_site_is_rejected(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _login(client)
        csrf = client.cookies.get("rclone_sync_csrf")
        response = client.post(
            "/logout",
            headers={
                "Origin": "null",
                "Sec-Fetch-Site": "cross-site",
                "X-CSRF-Token": csrf or "",
            },
            follow_redirects=False,
        )
        assert response.status_code == 403
        assert "Origin" in response.json()["detail"]


def test_origin_null_without_sec_fetch_site_is_allowed(tmp_path, monkeypatch):
    """Alte Browser/Webviews/Privacy-Extensions: Origin=null ohne Sec-Fetch-Site.

    Der Double-Submit-CSRF-Schutz mit SameSite=strict bleibt die maßgebliche
    Verteidigung; die Origin-Prüfung darf legitime Logins nicht blockieren.
    """
    with _client(tmp_path, monkeypatch) as client:
        login_page = client.get("/login")
        match = re.search(r'name="login_csrf" value="([^"]+)"', login_page.text)
        assert match
        response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "very-secure-password",
                "login_csrf": match.group(1),
            },
            headers={"Origin": "null"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"


def test_origin_null_same_origin_login_still_works(tmp_path, monkeypatch):
    """Firefox sendet bei Referrer-Policy no-referrer auch same-origin Origin: null."""
    with _client(tmp_path, monkeypatch) as client:
        login_page = client.get("/login")
        match = re.search(r'name="login_csrf" value="([^"]+)"', login_page.text)
        assert match
        response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "very-secure-password",
                "login_csrf": match.group(1),
            },
            headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"


def test_logout_invalidates_existing_session_tokens(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _login(client)
        session_token = client.cookies.get("rclone_sync_session")
        csrf = client.cookies.get("rclone_sync_csrf")
        assert session_token and csrf

        response = client.post(
            "/logout",
            headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303

        # Ein vor dem Logout kopierter Token darf nicht mehr gültig sein.
        client.cookies.set("rclone_sync_session", session_token)
        replay = client.get("/", follow_redirects=False)
        assert replay.status_code == 303
        assert replay.headers["location"] == "/login"


def test_logout_revokes_apns_devices_and_pending_delivery(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _login(client)
        database = db.get_db()
        token = "ab" * 32
        database.push_device_upsert(token, "production")
        assert database.push_outbox_enqueue(
            event="sync_error",
            title="Fehler",
            message="Nicht mehr zustellen",
            payload={},
            dedupe_key="logout-revocation",
            retention_seconds=3600,
        ) == 1
        csrf = client.cookies.get("rclone_sync_csrf")

        response = client.post(
            "/logout",
            headers={"Origin": "http://testserver", "X-CSRF-Token": csrf or ""},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert database.push_devices() == []
        assert database.push_outbox_status()["pending"] == 0


def test_logout_revocation_failure_is_explicit_and_clears_local_cookies(
    tmp_path, monkeypatch
):
    with _client(tmp_path, monkeypatch) as client:
        from app import main

        _login(client)
        csrf = client.cookies.get("rclone_sync_csrf")
        assert client.cookies.get("rclone_sync_session")
        assert csrf

        def fail_revocation():
            raise OSError("config is read-only")

        monkeypatch.setattr(main, "bump_session_version", fail_revocation)
        response = client.post(
            "/logout",
            headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
            follow_redirects=False,
        )

        assert response.status_code == 503
        assert response.json() == {
            "ok": False,
            "partial": True,
            "global_revocation": False,
            "local_session_cleared": True,
            "detail": (
                "Lokale Sitzung beendet, aber nicht alle bestehenden Sitzungen "
                "konnten widerrufen werden. Bitte später erneut anmelden und "
                "noch einmal abmelden."
            ),
        }
        set_cookies = "\n".join(response.headers.get_list("set-cookie"))
        assert "rclone_sync_session=" in set_cookies
        assert "rclone_sync_csrf=" in set_cookies
        assert client.cookies.get("rclone_sync_session") is None
        assert client.cookies.get("rclone_sync_csrf") is None
        audit = db.get_db().audit_list(event_type="logout_revocation_failed")
        assert len(audit) == 1
        assert audit[0]["details"]["global_revocation"] is False
        assert audit[0]["details"]["local_cookies_cleared"] is True
        assert audit[0]["details"]["error_type"] == "OSError"


def test_cancel_endpoints_return_503_when_persistent_signal_fails(
    tmp_path, monkeypatch
):
    with _client(tmp_path, monkeypatch) as client:
        _login(client)
        csrf = client.cookies.get("rclone_sync_csrf")
        database = db.get_db()
        database.job_start("backup")
        database.job_start("pbs")
        failure = {
            "ok": False,
            "killed": 1,
            "scope": "backup",
            "signal_persisted": False,
            "process_scan_ok": True,
            "error": "Abbruchsignal konnte nicht zuverlässig übermittelt werden",
            "error_code": "cancel_signal_persist_failed",
        }
        monkeypatch.setattr(rclone_sync, "cancel_job", lambda *_args: dict(failure))

        backup = client.post(
            "/api/jobs/backup/cancel",
            headers={"X-CSRF-Token": csrf},
        )
        pbs = client.post(
            "/api/pbs/cancel",
            headers={"X-CSRF-Token": csrf},
        )

        assert backup.status_code == 503
        assert backup.json()["ok"] is False
        assert backup.json()["error_code"] == "cancel_signal_persist_failed"
        assert pbs.status_code == 503
        assert pbs.json()["ok"] is False
        assert pbs.json()["error_code"] == "cancel_signal_persist_failed"
        backup_audit = database.audit_list(event_type="backup_cancel_requested")
        pbs_audit = database.audit_list(event_type="pbs_cancel_requested")
        assert backup_audit[0]["details"]["signal_persisted"] is False
        assert pbs_audit[0]["details"]["signal_persisted"] is False


def test_unhandled_exception_response_has_security_headers(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch):
        from app import main

        @main.app.get("/__boom_test__")
        def boom():  # pragma: no cover - wird über den Client ausgelöst
            raise RuntimeError("kaputt")

        try:
            crash_client = TestClient(
                main.app, base_url="http://testserver", raise_server_exceptions=False
            )
            with crash_client:
                response = crash_client.get("/__boom_test__")
            assert response.status_code == 500
            assert response.headers.get("X-Content-Type-Options") == "nosniff"
            assert response.headers.get("Cache-Control") == "no-store"
            assert "request_id" in response.json()
        finally:
            main.app.router.routes = [
                route
                for route in main.app.router.routes
                if getattr(route, "path", "") != "/__boom_test__"
            ]


def test_run_quick_creates_new_local_target_when_protection_disabled(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(_config("pw-irrelevant-here", tmp_path)), encoding="utf-8"
    )
    store = Config(config_path)
    monkeypatch.setattr(config_store, "_config", store)
    monkeypatch.setattr(runtime_state, "STATE_DIR", tmp_path / "runtime")
    monkeypatch.setattr(
        runtime_state, "CANCEL_FILE", tmp_path / "runtime" / "cancel.requested"
    )
    monkeypatch.setattr(runtime_state, "PROCS_DIR", tmp_path / "runtime" / "processes")

    remote = str(tmp_path / "source")
    Path(remote).mkdir()
    (Path(remote) / "file.txt").write_text("x", encoding="utf-8")
    target = tmp_path / "brand-new-target"
    assert not target.exists()

    captured: dict = {}

    def fake_run(cmd, log_file, **kwargs):
        captured["cmd"] = cmd
        Path(log_file).write_text("", encoding="utf-8")
        return 0

    monkeypatch.setattr(rclone_sync, "_run_rclone_command", fake_run)

    result = rclone_sync.run_quick(
        remote_path=remote,
        local_path=str(target),
        direction="pull",
        mode="copy",
        dry_run=True,
        min_local_files=0,
    )
    assert result["ok"], result
    assert target.is_dir()
    assert "copy" in captured["cmd"]


def test_run_quick_keeps_mount_protection_by_default(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(_config("pw-irrelevant-here", tmp_path)), encoding="utf-8"
    )
    store = Config(config_path)
    monkeypatch.setattr(config_store, "_config", store)

    remote = str(tmp_path / "source")
    Path(remote).mkdir()
    target = tmp_path / "missing-mount"

    result = rclone_sync.run_quick(
        remote_path=remote,
        local_path=str(target),
        direction="pull",
        mode="copy",
        dry_run=True,
    )
    assert not result["ok"]
    assert result.get("skipped")
    assert not target.exists()


def test_origin_scheme_mismatch_behind_tls_proxy_is_allowed(tmp_path, monkeypatch):
    """TLS-Proxy ohne vertrauenswürdiges X-Forwarded-Proto: https-Origin, http-Backend."""
    with _client(tmp_path, monkeypatch) as client:
        _login(client)
        csrf = client.cookies.get("rclone_sync_csrf")
        response = client.post(
            "/logout",
            headers={"Origin": "https://testserver", "X-CSRF-Token": csrf or ""},
            follow_redirects=False,
        )
        assert response.status_code == 303


def test_origin_foreign_host_is_still_rejected(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _login(client)
        csrf = client.cookies.get("rclone_sync_csrf")
        response = client.post(
            "/logout",
            headers={"Origin": "https://evil.example", "X-CSRF-Token": csrf or ""},
            follow_redirects=False,
        )
        assert response.status_code == 403
