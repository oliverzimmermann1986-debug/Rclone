from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.auth import require_auth
from app.routes import api_vault
from app.security import CSRF_COOKIE


def test_local_copy_deletion_requires_csrf_password_and_records_user(monkeypatch):
    application = FastAPI()
    application.include_router(api_vault.router)
    application.dependency_overrides[require_auth] = lambda: "owner"
    client = TestClient(application)
    calls = []
    monkeypatch.setattr(
        api_vault, "get_config", lambda: SimpleNamespace(snapshot=lambda: {})
    )
    monkeypatch.setattr(
        api_vault,
        "get_db",
        lambda: SimpleNamespace(
            audit_add=lambda *args, **kwargs: calls.append((args, kwargs))
        ),
    )
    monkeypatch.setattr(
        api_vault,
        "remove_local_copy",
        lambda config, upload_id: {"ok": True, "target_untouched": True},
    )

    def authenticate(request, user, password):
        if password != "fixture-password":
            raise HTTPException(403, "Passwort erforderlich")

    monkeypatch.setattr(api_vault, "require_reauthentication", authenticate)
    url = "/api/vault/library/upload-id"
    assert (
        client.request(
            "DELETE", url, json={"current_password": "fixture-password"}
        ).status_code
        == 403
    )
    client.cookies.set(CSRF_COOKIE, "fixture")
    client.headers["X-CSRF-Token"] = "fixture"
    assert (
        client.request("DELETE", url, json={"current_password": "wrong"}).status_code
        == 403
    )
    assert not calls
    response = client.request(
        "DELETE", url, json={"current_password": "fixture-password"}
    )
    assert response.status_code == 200 and response.json()["target_untouched"]
    assert calls[0][1]["actor"] == "owner"
    assert "filename" not in calls[0][1]["details"]
