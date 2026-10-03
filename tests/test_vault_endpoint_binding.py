import copy
import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import device_vault
from app.auth import require_auth
from app.db import Database
from app.routes import api_vault
from app.security import CSRF_COOKIE


class Config:
    def __init__(self, data):
        self.data = data

    def snapshot(self):
        return copy.deepcopy(self.data)


@pytest.fixture
def env(tmp_path, monkeypatch):
    pair = {
        "id": "photos",
        "name": "Fotos",
        "local": str(tmp_path / "source"),
        "remote": str(tmp_path / "target"),
        "direction": "push",
        "schedule": "0 1 * * *",
    }
    store = Config(
        {
            "paths": {"device_vault_dir": str(tmp_path / "vault")},
            "backup": {"pairs": [pair], "timeout_hours": 0.1},
        }
    )
    database = Database(tmp_path / "state.db")
    monkeypatch.setattr(api_vault, "get_config", lambda: store)
    monkeypatch.setattr(api_vault, "get_db", lambda: database)
    monkeypatch.setattr(device_vault, "notify", lambda *args, **kwargs: None)
    app = FastAPI()
    app.include_router(api_vault.router)
    app.dependency_overrides[require_auth] = lambda: "owner"
    client = TestClient(app)
    client.cookies.set(CSRF_COOKIE, "test-csrf")
    client.headers["X-CSRF-Token"] = "test-csrf"
    payload = b"private upload bytes"
    body = {
        "identity": "photos",
        "filename": "photo.jpg",
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "source_type": "photo",
        "expected_endpoints": device_vault.endpoint_binding(pair),
    }
    return client, store, database, body, payload


def change_destination(store, mutation):
    pair = store.data["backup"]["pairs"][0]
    if mutation == "deleted":
        store.data["backup"]["pairs"] = []
    elif mutation == "direction":
        pair["direction"] = "pull"
    else:
        pair[mutation] += "-changed"


@pytest.mark.parametrize("mutation", ["local", "remote", "direction", "deleted"])
def test_stale_create_rejects_before_receipt_payload_or_target(env, mutation):
    client, store, _database, body, _payload = env
    change_destination(store, mutation)
    response = client.post("/api/vault/uploads", json=body)
    assert response.status_code == 409
    assert "Konfiguration neu laden" in response.json()["detail"]
    assert not Path(store.data["paths"]["device_vault_dir"]).exists()
    assert not Path(body["expected_endpoints"]["remote"]).exists()


def uploaded(env, *, legacy=False):
    client, store, _database, body, payload = env
    request = (
        {key: value for key, value in body.items() if key != "expected_endpoints"}
        if legacy
        else body
    )
    response = client.post("/api/vault/uploads", json=request)
    assert response.status_code == 201
    record = response.json()
    if legacy:
        root = device_vault.vault_root(store.snapshot())
        saved = device_vault._load_record(root, record["id"])
        saved.pop("endpoint_binding")
        device_vault._save_record(root, saved)
    assert (
        client.put(
            f"/api/vault/uploads/{record['id']}?offset=0", content=payload
        ).status_code
        == 200
    )
    return record


@pytest.mark.parametrize("mutation", ["local", "remote", "direction", "deleted"])
def test_stale_completion_post_rejects_and_keeps_uploaded_bytes(env, mutation):
    client, store, _database, body, payload = env
    record = uploaded(env)
    change_destination(store, mutation)
    response = client.post(f"/api/vault/uploads/{record['id']}/complete")
    assert response.status_code == 409
    root = device_vault.vault_root(store.snapshot())
    assert device_vault._part_path(root, record["id"]).read_bytes() == payload
    assert device_vault._load_record(root, record["id"])["status"] == "uploaded"
    assert not Path(body["expected_endpoints"]["remote"]).exists()


@pytest.mark.parametrize("mutation", ["local", "remote", "direction", "deleted"])
def test_queued_completion_checks_current_config_before_copy(env, mutation):
    _client, store, database, body, payload = env
    record = uploaded(env)
    queued_config = store.snapshot()
    device_vault.queue_completion(queued_config, record["id"])
    change_destination(store, mutation)
    result = device_vault.complete_upload(
        database, queued_config, record["id"], config_provider=store.snapshot
    )
    assert result["status"] == "error"
    assert not result["verified"]
    assert "Konfiguration neu laden" in result["error"]
    root = device_vault.vault_root(queued_config)
    assert device_vault._part_path(root, record["id"]).read_bytes() == payload
    assert not Path(body["expected_endpoints"]["remote"]).exists()


def test_config_change_during_blob_preparation_stops_copy_and_keeps_blob(
    env, monkeypatch
):
    _client, store, database, body, payload = env
    record = uploaded(env)
    queued_config = store.snapshot()
    device_vault.queue_completion(queued_config, record["id"])
    prepare_blob = device_vault._prepare_blob

    def prepare_then_change(root, receipt):
        blob = prepare_blob(root, receipt)
        change_destination(store, "remote")
        return blob

    monkeypatch.setattr(device_vault, "_prepare_blob", prepare_then_change)
    result = device_vault.complete_upload(
        database, queued_config, record["id"], config_provider=store.snapshot
    )
    assert result["status"] == "error"
    root = device_vault.vault_root(queued_config)
    assert device_vault._blob_path(root, body["sha256"]).read_bytes() == payload
    assert not Path(body["expected_endpoints"]["remote"]).exists()


def test_complete_api_background_uses_fresh_config_after_acceptance(env, monkeypatch):
    client, store, _database, body, payload = env
    record = uploaded(env)
    original_snapshot = store.snapshot
    snapshots = 0

    def snapshot():
        nonlocal snapshots
        snapshots += 1
        if snapshots == 2:
            change_destination(store, "remote")
        return original_snapshot()

    monkeypatch.setattr(store, "snapshot", snapshot)
    response = client.post(f"/api/vault/uploads/{record['id']}/complete")
    assert response.status_code == 202 and response.json()["status"] == "queued"
    root = device_vault.vault_root(original_snapshot())
    status = device_vault._load_record(root, record["id"])
    assert status["status"] == "error" and not status["verified"]
    assert "Konfiguration neu laden" in status["error"]
    assert device_vault._part_path(root, record["id"]).read_bytes() == payload
    assert not Path(body["expected_endpoints"]["remote"]).exists()


@pytest.mark.parametrize("legacy", [False, True])
def test_scheduler_change_keeps_endpoints_and_legacy_request_compatible(env, legacy):
    client, store, _database, body, payload = env
    record = uploaded(env, legacy=legacy)
    pair = store.data["backup"]["pairs"][0]
    pair["schedule"] = "0 5 * * *"
    pair["name"] = "Urlaubsfotos"
    pair["direction"] = " PUSH "
    store.data["backup"]["timeout_hours"] = 1
    response = client.post(f"/api/vault/uploads/{record['id']}/complete")
    assert response.status_code == 202
    status = client.get(f"/api/vault/uploads/{record['id']}").json()
    assert status["status"] == "ready" and status["verified"]
    assert (
        Path(body["expected_endpoints"]["remote"]) / status["target_relative"]
    ).read_bytes() == payload


@pytest.mark.parametrize("mutation", ["remote", "deleted"])
def test_legacy_receipt_still_checks_current_identity_and_saved_target(env, mutation):
    client, store, _database, body, payload = env
    record = uploaded(env, legacy=True)
    change_destination(store, mutation)
    assert client.post(f"/api/vault/uploads/{record['id']}/complete").status_code == 409
    root = device_vault.vault_root(store.snapshot())
    assert device_vault._part_path(root, record["id"]).read_bytes() == payload
    assert not Path(body["expected_endpoints"]["remote"]).exists()
