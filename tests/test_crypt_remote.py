import json
import logging
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import SESSION_COOKIE, require_auth
from app.crypt_remote import build_create_payload, strip_secrets, validate_base_path
from app.routes import api_crypt

OWN = {"password": "own-key-1234567890", "password2": "own-salt-0987654321"}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(api_crypt.router)
    app.dependency_overrides[require_auth] = lambda: "admin"
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, "session-a")
    client.cookies.set("rclone_sync_csrf", "csrf")
    client.headers["X-CSRF-Token"] = "csrf"
    return client


@pytest.fixture
def rclone(monkeypatch):
    """Fake rclone binary below the real _rc/_run path."""
    state = {
        "live": {"cloud": {"type": "pcloud", "token": "tok"}, "old": {"type": "crypt"}},
        "calls": [],
        "fail": set(),
        "lsd": 0,
        "audit": [],
    }

    def run(args, **kwargs):
        if args[1] == "lsd":
            state["calls"].append(("lsd", args))
            if state["lsd"] is None:
                raise subprocess.TimeoutExpired(args, 60)
            return subprocess.CompletedProcess(args, state["lsd"], "", "boom")
        method, payload = args[3], json.loads(args[-1])
        state["calls"].append((method, payload))
        if method in state["fail"]:
            return subprocess.CompletedProcess(args, 1, "", "failed: " + args[-1])
        out = {}
        if method == "config/dump":
            out = state["live"]
        elif method == "config/listremotes":
            out = {"remotes": list(state["live"])}
        elif method == "config/create":
            state["live"][payload["name"]] = {"type": "crypt", **payload["parameters"]}
        elif method == "config/delete":
            state["live"].pop(payload["name"], None)
        return subprocess.CompletedProcess(args, 0, json.dumps(out), "")

    class Db:
        def audit_add(self, event, *, actor, details):
            state["audit"].append((event, actor, details))

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(api_crypt, "get_db", Db)
    return state


def methods(state):
    return [c[0] for c in state["calls"]]


def test_candidates_expose_only_name_and_type(client, rclone):
    assert client.get("/api/remotes/crypt/candidates").json() == {
        "candidates": [{"name": "cloud", "type": "pcloud"}]
    }


def test_create_with_generated_keys(client, rclone):
    response = client.post(
        "/api/remotes/crypt",
        json={"name": "safe", "base_remote": "cloud", "base_path": "Sicherpfad/enc"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    keys = body["keys"]
    assert len(keys["password"]) >= 32 and len(keys["password2"]) >= 32
    assert keys["password"] != keys["password2"]
    assert body["remote"] == "cloud:Sicherpfad/enc"
    assert body["filename_encryption"] == "standard"
    assert body["directory_name_encryption"] is True
    assert body["warning"] == api_crypt.WARNING
    create = next(p for m, p in rclone["calls"] if m == "config/create")
    assert create == {
        "name": "safe",
        "type": "crypt",
        "parameters": {
            "remote": "cloud:Sicherpfad/enc",
            "filename_encryption": "standard",
            "directory_name_encryption": "true",
            "password": keys["password"],
            "password2": keys["password2"],
        },
        "opt": {"obscure": True, "nonInteractive": True},
    }
    assert rclone["audit"] == [
        (
            "crypt_remote_created",
            "web",
            {
                "name": "safe",
                "base_remote": "cloud",
                "filename_encryption": "standard",
                "generated": True,
            },
        )
    ]
    audit = json.dumps(rclone["audit"])
    assert keys["password"] not in audit and keys["password2"] not in audit


def test_create_with_own_keys_and_options(client, rclone):
    response = client.post(
        "/api/remotes/crypt",
        json={
            "name": "safe",
            "base_remote": "cloud",
            "filename_encryption": "off",
            "directory_name_encryption": False,
            **OWN,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["keys"] == OWN
    create = next(p for m, p in rclone["calls"] if m == "config/create")
    assert create == build_create_payload(
        "safe", "cloud", "", "off", False, *OWN.values()
    )
    assert rclone["audit"][0][2]["generated"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"name": "-flag"},
        {"name": "a b"},
        {"name": "x" * 65},
        {"base_path": "/abs"},
        {"base_path": "a/../b"},
        {"base_path": "a\\..\\b"},
        {"base_path": "a\nb"},
        {"base_path": "x" * 513},
        {"filename_encryption": "weird"},
        {"password": "short"},
        {"password2": "x" * 1025},
        {"base_remote": "old"},
        {"base_remote": "missing"},
    ],
)
def test_invalid_payload_rejected(client, rclone, change):
    body = {"name": "safe", "base_remote": "cloud", **change}
    assert client.post("/api/remotes/crypt", json=body).status_code == 400
    assert "config/create" not in methods(rclone)


def test_duplicate_name_case_insensitive(client, rclone):
    rclone["live"]["SAFE"] = {"type": "drive"}
    response = client.post(
        "/api/remotes/crypt", json={"name": "safe", "base_remote": "cloud"}
    )
    assert response.status_code == 409
    assert "config/create" not in methods(rclone)


def test_rclone_failure_leaks_no_keys_and_removes_remote(client, rclone, caplog):
    rclone["fail"].add("config/create")
    with caplog.at_level(logging.DEBUG):
        response = client.post(
            "/api/remotes/crypt", json={"name": "safe", "base_remote": "cloud", **OWN}
        )
    assert response.status_code == 502
    for secret in OWN.values():
        assert secret not in response.text and secret not in caplog.text
    assert "cloud:" not in response.text
    assert methods(rclone)[-1] == "config/delete" and "safe" not in rclone["live"]
    assert rclone["audit"] == []


@pytest.mark.parametrize("code,status", [(0, 200), (3, 200), (1, 502), (None, 502)])
def test_verify(client, rclone, code, status):
    rclone["lsd"] = code
    response = client.post("/api/remotes/crypt/safe/verify")
    assert response.status_code == status
    assert rclone["calls"][-1] == ("lsd", ["rclone", "lsd", "safe:"])
    assert "boom" not in response.text


def test_verify_rejects_bad_name(client, rclone):
    assert client.post("/api/remotes/crypt/-x/verify").status_code == 400
    assert rclone["calls"] == []


def test_helpers():
    assert validate_base_path("") == "" and validate_base_path("a/b c") == "a/b c"
    payload = build_create_payload("n", "b", "p", "standard", True, "k1", "k2")
    assert "k1" not in json.dumps(strip_secrets(payload))
    assert payload["parameters"]["password"] == "k1"
