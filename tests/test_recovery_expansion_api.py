import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.auth import require_auth
from app.db import Database
from app.routes import api_recovery
from app.security import CSRF_COOKIE


class Config:
    def __init__(self, data):
        self.data = data

    def snapshot(self):
        return self.data


def fixture(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    (target / "proof.txt").write_bytes(b"full recovery proof")
    pair = {
        "id": "documents",
        "name": "Dokumente",
        "direction": "push",
        "local": str(tmp_path / "source"),
        "remote": str(target),
    }
    config = {
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "recovery_dir": str(tmp_path / "staging"),
        },
        "backup": {"pairs": [pair]},
    }
    database = Database(tmp_path / "state.db")
    monkeypatch.setattr(api_recovery, "get_config", lambda: Config(config))
    monkeypatch.setattr(api_recovery, "get_db", lambda: database)
    app = FastAPI()
    app.include_router(api_recovery.router)
    app.dependency_overrides[require_auth] = lambda: "owner"
    client = TestClient(app)
    client.cookies.set(CSRF_COOKIE, "test-csrf")
    client.headers["X-CSRF-Token"] = "test-csrf"
    return client, database, config, target


def test_snapshot_capture_browse_and_full_restore_api(tmp_path, monkeypatch):
    client, database, config, target = fixture(tmp_path, monkeypatch)
    response = client.post(
        "/api/recovery/snapshots", json={"identity": "documents", "max_total_mb": 1}
    )
    assert response.status_code == 202
    assert database.job_get(response.json()["job_id"])["status"] == "ok"
    points = client.get("/api/recovery/points?identity=documents").json()["points"]
    point = next(item for item in points if item["kind"] == "full")
    assert point["complete"] is True
    (target / "proof.txt").unlink()
    browse = client.get(f"/api/recovery/points/{point['id']}/browse?identity=documents")
    assert (
        browse.status_code == 200 and browse.json()["items"][0]["name"] == "proof.txt"
    )
    restored = client.post(
        f"/api/recovery/points/{point['id']}/restore",
        json={"identity": "documents", "max_total_mb": 1},
    )
    assert restored.status_code == 202
    assert database.job_get(restored.json()["job_id"])["status"] == "ok"
    staging = client.get("/api/recovery/staging").json()["items"][0]
    assert staging["verified"] is True
    assert (
        Path(staging["staging_path"]) / "proof.txt"
    ).read_bytes() == b"full recovery proof"


def test_expansion_mutations_require_auth_csrf_and_exclusive_job(tmp_path, monkeypatch):
    client, database, _config, _target = fixture(tmp_path, monkeypatch)
    client.headers.pop("X-CSRF-Token")
    assert (
        client.post(
            "/api/recovery/snapshots", json={"identity": "documents"}
        ).status_code
        == 403
    )
    client.headers["X-CSRF-Token"] = "test-csrf"
    job = database.job_start("backup", exclusive_scope=True)
    assert (
        client.post(
            "/api/recovery/snapshots", json={"identity": "documents"}
        ).status_code
        == 409
    )
    database.job_finish(job, "ok")

    def denied():
        raise HTTPException(401, "Login required")

    client.app.dependency_overrides[require_auth] = denied
    assert (
        client.post(
            "/api/recovery/snapshots", json={"identity": "documents"}
        ).status_code
        == 401
    )


def test_handover_preview_and_import_reauthentication(tmp_path, monkeypatch):
    client, _database, _config, _target = fixture(tmp_path, monkeypatch)
    package = {
        "rescue_inventory": {
            "schema": "sicherpfad-rescue-inventory-v1",
            "data_paths": [],
            "records": [],
        }
    }
    envelope = api_recovery.encrypted_handover(package, "long-rescue-passphrase")
    body = {"envelope": envelope, "passphrase": "long-rescue-passphrase"}
    preview = client.post("/api/recovery/handover/preview", json=body)
    assert preview.status_code == 200
    assert preview.json()["config_will_change"] is False

    def reauthenticate(request, user, password):
        if password != "owner-password":
            raise HTTPException(403, "Password required")

    monkeypatch.setattr(api_recovery, "require_reauthentication", reauthenticate)
    denied = client.post(
        "/api/recovery/handover/import",
        json={**body, "current_password": "wrong", "mappings": {}},
    )
    assert denied.status_code == 403
    imported = client.post(
        "/api/recovery/handover/import",
        json={**body, "current_password": "owner-password", "mappings": {}},
    )
    assert imported.status_code == 200
    assert imported.json()["config_changed"] is False
    assert "owner-password" not in json.dumps(imported.json())
