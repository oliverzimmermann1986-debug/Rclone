import copy
import re
from pathlib import Path

import bcrypt
import yaml
from fastapi.testclient import TestClient

from app import __version__ as app_version, config_store, db
from app.config_store import Config
from app.db import Database
from app.jobs import runtime_state


def _config(password: str) -> dict:
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
            "local_browse_roots": ["/tmp"],
        },
        "paths": {"data_dir": "/tmp", "logs_dir": "/tmp", "temp_dir": "/tmp"},
        "backup": {"enabled": True, "pairs": [], "default_schedule": "manual"},
        "notifications": {"custom_delivery": {"api_key": "private-api-key-canary"}},
    }


def test_login_csrf_config_revision_and_secret_redaction(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_data = _config("very-secure-password")
    config_data["paths"] = {
        "data_dir": str(tmp_path),
        "logs_dir": str(tmp_path),
        "temp_dir": str(tmp_path),
    }
    config_data["web"]["local_browse_roots"] = [str(tmp_path)]
    config_path.write_text(yaml.safe_dump(config_data), encoding="utf-8")
    store = Config(config_path)
    store.update(
        lambda data: data["backup"].update(
            {"filter_file": str(tmp_path / "rclone-filters.txt")}
        )
    )
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

    with TestClient(main.app, base_url="http://testserver") as client:
        login_page = client.get("/login")
        assert login_page.status_code == 200
        match = re.search(r'name="login_csrf" value="([^"]+)"', login_page.text)
        assert match

        bad = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "very-secure-password",
                "login_csrf": "wrong-token" * 3,
            },
            follow_redirects=False,
        )
        assert bad.status_code == 303
        assert "error=csrf" in bad.headers["location"]

        # Neues Formular, weil der erste Request absichtlich einen falschen Token nutzte.
        login_page = client.get("/login")
        token = re.search(r'name="login_csrf" value="([^"]+)"', login_page.text).group(
            1
        )
        logged_in = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "very-secure-password",
                "login_csrf": token,
            },
            follow_redirects=False,
        )
        assert logged_in.status_code == 303

        filter_response = client.get("/api/config/filter-file")
        assert filter_response.status_code == 200
        filter_data = filter_response.json()
        saved_filter = client.put(
            "/api/config/filter-file",
            json={"content": "- *.tmp\n", "revision": filter_data["revision"]},
            headers={"X-CSRF-Token": client.cookies.get("rclone_sync_csrf")},
        )
        assert saved_filter.status_code == 200
        stale_filter_revision = saved_filter.json()["revision"]
        (tmp_path / "rclone-filters.txt").write_text("- external\n", encoding="utf-8")
        filter_conflict = client.put(
            "/api/config/filter-file",
            json={"content": "- newer\n", "revision": stale_filter_revision},
            headers={"X-CSRF-Token": client.cookies.get("rclone_sync_csrf")},
        )
        assert filter_conflict.status_code == 409

        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["version"] == app_version

        overview = client.get("/api/diagnostics/overview")
        assert overview.status_code == 200
        overview_data = overview.json()
        assert overview_data["app"]["version"] == app_version
        assert "system" in overview_data
        assert "stats_24h" in overview_data["jobs"]

        search = client.get("/api/jobs/search?limit=10&status=ok")
        assert search.status_code == 200
        assert search.json()["total"] == 0

        cfg_response = client.get("/api/config")
        assert cfg_response.status_code == 200
        cfg = cfg_response.json()

        missing_revision = copy.deepcopy(cfg)
        missing_revision.pop("_revision")
        precondition = client.put(
            "/api/config",
            json={"config": missing_revision},
            headers={"X-CSRF-Token": client.cookies.get("rclone_sync_csrf")},
        )
        assert precondition.status_code == 428
        assert cfg["web"]["password_hash"] == "***SET***"
        assert cfg["notifications"]["custom_delivery"]["api_key"] == "***SET***"
        assert "webhooks" not in cfg["notifications"]
        stale = cfg["_revision"]

        csrf = client.cookies.get("rclone_sync_csrf")
        cfg["backup"]["max_parallel"] = 3
        saved = client.put(
            "/api/config", json={"config": cfg}, headers={"X-CSRF-Token": csrf}
        )
        assert saved.status_code == 200
        assert (
            store.get("notifications", "custom_delivery", "api_key")
            == "private-api-key-canary"
        )

        cfg["backup"]["max_parallel"] = 4
        cfg["_revision"] = stale
        conflict = client.put(
            "/api/config", json={"config": cfg}, headers={"X-CSRF-Token": csrf}
        )
        assert conflict.status_code == 409

        logout = client.post(
            "/logout", headers={"X-CSRF-Token": csrf}, follow_redirects=False
        )
        assert logout.status_code == 303


def test_request_body_limit_applies_without_content_length(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(_config("test-password-long")), encoding="utf-8"
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

    with TestClient(main.app, base_url="http://testserver") as client:
        page = client.get("/login")
        login_token = re.search(r'name="login_csrf" value="([^"]+)"', page.text).group(
            1
        )
        logged_in = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "test-password-long",
                "login_csrf": login_token,
            },
            follow_redirects=False,
        )
        assert logged_in.status_code == 303
        csrf = client.cookies.get("rclone_sync_csrf")
        chunks = iter(
            [
                b'{"config":{"padding":"',
                b"x" * (1024 * 1024),
                b"y" * (1024 * 1024 + 1),
                b'"}}',
            ]
        )
        response = client.put(
            "/api/config",
            content=chunks,
            headers={"Content-Type": "application/json", "X-CSRF-Token": csrf},
        )
    assert response.status_code == 413
    assert response.json()["detail"] == "Anfrage ist zu groß"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_gui_validation_job_exports_snapshots_and_support_bundle(
    tmp_path: Path, monkeypatch
):
    import io
    import zipfile

    config_data = _config("another-secure-password")
    config_data["paths"] = {
        "data_dir": str(tmp_path / "data"),
        "logs_dir": str(tmp_path / "logs"),
        "temp_dir": str(tmp_path / "tmp"),
    }
    config_data["web"]["local_browse_roots"] = [str(tmp_path)]
    config_data["backup"]["filter_file"] = str(tmp_path / "data" / "rclone-filters.txt")
    for name in ("data", "logs", "tmp"):
        (tmp_path / name).mkdir()

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config_data, allow_unicode=True), encoding="utf-8"
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

    with TestClient(main.app, base_url="http://testserver") as client:
        login_page = client.get("/login")
        login_token = re.search(
            r'name="login_csrf" value="([^"]+)"', login_page.text
        ).group(1)
        logged_in = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "another-secure-password",
                "login_csrf": login_token,
            },
            follow_redirects=False,
        )
        assert logged_in.status_code == 303
        csrf = client.cookies.get("rclone_sync_csrf")

        cfg = client.get("/api/config").json()
        validated = client.post(
            "/api/config/validate",
            json={"config": cfg},
            headers={"X-CSRF-Token": csrf},
        )
        assert validated.status_code == 200
        assert validated.json()["ok"] is True
        assert validated.json()["revision_matches"] is True

        invalid = copy.deepcopy(cfg)
        invalid["backup"]["default_schedule"] = "definitely-not-a-cron"
        invalid_result = client.post(
            "/api/config/validate",
            json={"config": invalid},
            headers={"X-CSRF-Token": csrf},
        )
        assert invalid_result.status_code == 422
        assert store.get("backup", "default_schedule") == "manual"

        log_path = tmp_path / "logs" / "job-fotos.log"
        log_path.write_text("Fotos synchronisiert\n", encoding="utf-8")
        job_id = database.job_start("backup")
        database.job_set_log_file(job_id, str(log_path))
        database.job_finish(
            job_id,
            "ok",
            {"pairs": [{"name": "Fotos", "ok": True, "transferred": "2 GiB"}]},
        )

        search = client.get("/api/jobs/search?q=Fotos&limit=10")
        assert search.status_code == 200
        assert search.json()["total"] == 1
        assert search.json()["items"][0]["id"] == job_id

        exported = client.get("/api/jobs/export.csv?q=Fotos")
        assert exported.status_code == 200
        assert exported.content.startswith(b"\xef\xbb\xbf")
        assert b"Fotos" in exported.content
        assert "attachment;" in exported.headers["content-disposition"]

        downloaded_log = client.get(f"/api/jobs/{job_id}/log/download")
        assert downloaded_log.status_code == 200
        assert downloaded_log.text.replace("\r\n", "\n") == "Fotos synchronisiert\n"
        assert "attachment;" in downloaded_log.headers["content-disposition"]

        snapshot = client.post(
            "/api/maintenance/config/snapshots",
            headers={"X-CSRF-Token": csrf},
        )
        assert snapshot.status_code == 200
        snapshots = client.get("/api/maintenance/config/snapshots")
        assert snapshots.status_code == 200
        assert (
            snapshots.json()["snapshots"][0]["name"]
            == snapshot.json()["snapshot"]["name"]
        )

        support = client.get("/api/maintenance/support-bundle")
        assert support.status_code == 200
        with zipfile.ZipFile(io.BytesIO(support.content)) as archive:
            redacted = archive.read("config-redacted.yaml").decode("utf-8")
            assert "private-api-key-canary" not in redacted
            assert "***REDACTED***" in redacted
