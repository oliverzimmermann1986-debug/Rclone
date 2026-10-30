import json
import secrets

import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import account_generation, require_auth
from app.config_store import Config
from app.db import Database
from app.routes import api_operations as operations, api_reports as reports


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cfg = Config(tmp_path / "config.yaml")
    cfg.replace(
        {
            "web": {"username": "admin", "secret_key": secrets.token_urlsafe(48)},
            "paths": {"data_dir": str(tmp_path)},
            "backup": {"pairs": []},
            "notifications": {
                "heartbeat": {"secret_url": "https://receiver.example/secret-canary"}
            },
            "reports": {"weekly": {"email": {"password": "smtp-canary"}}},
        }
    )
    db = Database(tmp_path / "operations.db")
    for module in (operations, reports):
        monkeypatch.setattr(module, "get_config", lambda: cfg)
        monkeypatch.setattr(module, "get_db", lambda: db)
        monkeypatch.setattr(module, "_audit_best_effort", lambda *_a, **_k: True)
        monkeypatch.setattr(
            module,
            "require_reauthentication",
            lambda *_a, **_k: account_generation(cfg.snapshot()),
        )
    app = FastAPI()
    app.include_router(operations.router)
    app.include_router(reports.router)
    app.dependency_overrides[require_auth] = lambda: "admin"
    with TestClient(app) as client:
        client.cookies.set("rclone_sync_csrf", "csrf")
        client.headers["X-CSRF-Token"] = "csrf"
        yield client, cfg, db


def test_safe_settings_hide_receiver_and_smtp_password(setup):
    client, cfg, _db = setup
    response = client.get("/api/operations/settings")
    assert response.status_code == 200
    data = response.json()
    assert data["_revision"] == cfg.revision
    assert (
        data["heartbeat"]["url_configured"]
        and data["weekly_report"]["email"]["password_set"]
    )
    assert "secret-canary" not in response.text and "smtp-canary" not in response.text


def test_partial_settings_update_preserves_secrets_and_revision_guard(setup):
    client, cfg, _db = setup
    original = cfg.revision
    data = {
        "expected_revision": original,
        "current_password": "confirmed",
        "heartbeat": {"period_seconds": 120},
        "weekly_report": {"email": {"enabled": False}},
    }
    response = client.put("/api/operations/settings", json=data)
    assert response.status_code == 200, response.text
    assert response.json()["_revision"] != original
    assert cfg.get("notifications", "heartbeat", "secret_url").endswith("secret-canary")
    assert cfg.get("reports", "weekly", "email", "password") == "smtp-canary"
    assert client.put("/api/operations/settings", json=data).status_code == 409
    assert "canary" not in response.text


def test_csrf_and_password_confirmation_are_required(setup):
    client, cfg, _db = setup
    assert client.post("/api/operations/heartbeat/test", json={}).status_code == 422
    client.headers.pop("X-CSRF-Token")
    assert (
        client.put(
            "/api/operations/settings",
            json={"expected_revision": cfg.revision, "current_password": "yes"},
        ).status_code
        == 403
    )


def test_generation_download_and_disabled_email_are_separate_actions(
    setup, monkeypatch
):
    client, _cfg, _db = setup
    monkeypatch.setattr(
        operations.heartbeat, "_send", lambda *_: pytest.fail("no live heartbeat")
    )
    monkeypatch.setattr(
        reports.protection_reports,
        "_send_email",
        lambda *_: pytest.fail("no live email"),
    )
    response = client.post("/api/reports/weekly/generate", json={})
    assert response.status_code == 200, response.text
    identifier = response.json()["report"]["id"]
    pdf = client.get(f"/api/reports/weekly/{identifier}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
    assert pdf.headers["cache-control"] == "no-store"
    assert client.get("/api/reports/weekly/history").json()["total"] == 1
    assert (
        client.post(
            f"/api/reports/weekly/{identifier}/send",
            json={"current_password": "confirmed"},
        ).status_code
        == 400
    )
    assert client.get("/api/reports/weekly/999999/pdf").status_code == 404
    assert "canary" not in json.dumps(client.get("/api/reports/weekly").json())


def test_reauthentication_failure_or_rotation_cannot_send_heartbeat(setup, monkeypatch):
    client, _cfg, _db = setup
    monkeypatch.setattr(
        operations.heartbeat,
        "_send",
        lambda *_: pytest.fail("no network after revoked confirmation"),
    )

    def reject(*_args):
        raise HTTPException(403, "Aktuelles Passwort falsch")

    monkeypatch.setattr(operations, "require_reauthentication", reject)
    assert (
        client.post(
            "/api/operations/heartbeat/test", json={"current_password": "bad"}
        ).status_code
        == 403
    )
    monkeypatch.setattr(
        operations, "require_reauthentication", lambda *_args: "obsolete-generation"
    )
    assert (
        client.post(
            "/api/operations/heartbeat/test", json={"current_password": "old"}
        ).status_code
        == 409
    )


def test_explicit_secret_clearing_and_unsafe_receiver_errors_are_redacted(setup):
    client, cfg, _db = setup
    response = client.put(
        "/api/operations/settings",
        json={
            "expected_revision": cfg.revision,
            "current_password": "yes",
            "heartbeat": {"secret_url": "http://127.0.0.1/private-canary"},
        },
    )
    assert response.status_code == 422 and "private-canary" not in response.text
    response = client.put(
        "/api/operations/settings",
        json={
            "expected_revision": cfg.revision,
            "current_password": "yes",
            "heartbeat": {"secret_url": ""},
        },
    )
    assert (
        response.status_code == 200
        and response.json()["heartbeat"]["url_configured"] is False
    )
