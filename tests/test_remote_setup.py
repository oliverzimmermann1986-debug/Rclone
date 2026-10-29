import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.auth import SESSION_COOKIE, require_auth
from app.routes import api_remotes as remotes


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(remotes.router)
    app.dependency_overrides[require_auth] = lambda: "admin"
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, "session-a")
    client.cookies.set("rclone_sync_csrf", "csrf")
    client.headers["X-CSRF-Token"] = "csrf"
    yield client
    for key in list(remotes._setups):
        remotes._discard(key)


@pytest.fixture
def fake_rclone(monkeypatch):
    calls = []
    live = {}

    def rc(method, payload=None, config=None):
        payload = payload or {}
        calls.append((method, payload, config))
        if method == "config/providers":
            return {"providers": [{"Name": "webdav", "Description": "WebDAV"}]}
        if method == "config/listremotes":
            return {"remotes": list(live)}
        if method == "config/create":
            if config:
                Path(config).write_text("draft", encoding="utf-8")
                return {
                    "State": "question",
                    "Option": {
                        "Name": "pass",
                        "IsPassword": True,
                        "Default": "never-echo",
                    },
                }
            live[payload["name"]] = payload["parameters"]
            return {}
        if method == "config/update":
            return {"State": ""}
        if method == "config/get":
            return {"type": "webdav", "pass": "obscured", "url": "https://example.org"}
        raise AssertionError(method)

    monkeypatch.setattr(remotes, "_rc", rc)
    monkeypatch.setattr(
        remotes,
        "get_db",
        lambda: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )
    return calls, live


def begin(client):
    response = client.post(
        "/api/remotes/setup", json={"name": "cloud", "provider": "webdav"}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_complete_flow_stays_private_until_save(client, fake_rclone):
    calls, live = fake_rclone
    setup = begin(client)
    assert setup["option"]["secret"] and setup["option"]["default"] == ""
    assert "never-echo" not in json.dumps(setup)
    assert not live
    assert client.post(f"/api/remotes/setup/{setup['id']}/save").status_code == 409
    response = client.post(
        f"/api/remotes/setup/{setup['id']}/answer",
        json={"answer": "private-password", "step": setup["step"]},
    )
    assert response.json()["ready"]
    assert not live
    saved = client.post(f"/api/remotes/setup/{setup['id']}/save")
    assert saved.status_code == 200
    assert saved.json()["path"] == "cloud:"
    assert live["cloud"]["pass"] == "obscured"
    assert calls[-1][1]["opt"]["noObscure"] is True
    assert setup["id"] not in remotes._setups


def test_cancel_removes_draft_and_cannot_access_other_session(client, fake_rclone):
    setup = begin(client)
    draft = Path(remotes._setups[setup["id"]].config)
    client.cookies.set(SESSION_COOKIE, "session-b")
    assert client.delete(f"/api/remotes/setup/{setup['id']}").status_code == 404
    assert draft.exists()
    client.cookies.set(SESSION_COOKIE, "session-a")
    assert client.delete(f"/api/remotes/setup/{setup['id']}").status_code == 200
    assert not draft.exists()


def test_expired_setup_is_cleaned(client, fake_rclone, monkeypatch):
    setup = begin(client)
    draft = Path(remotes._setups[setup["id"]].config)
    monkeypatch.setattr(remotes, "_TTL", -1)
    assert client.post(f"/api/remotes/setup/{setup['id']}/save").status_code == 404
    assert not draft.exists()


@pytest.mark.parametrize("name", ["-flag", "C:", "../cloud", "a\nb", "a b", "x" * 65])
def test_invalid_names_rejected(client, fake_rclone, name):
    assert (
        client.post(
            "/api/remotes/setup", json={"name": name, "provider": "webdav"}
        ).status_code
        == 400
    )


def test_duplicate_and_stale_steps_rejected(client, fake_rclone):
    _, live = fake_rclone
    setup = begin(client)
    assert (
        client.post(
            f"/api/remotes/setup/{setup['id']}/answer",
            json={"answer": "secret", "step": 0},
        ).status_code
        == 409
    )
    live["CLOUD"] = {}
    assert (
        client.post(
            "/api/remotes/setup", json={"name": "cloud", "provider": "webdav"}
        ).status_code
        == 409
    )
    client.post(
        f"/api/remotes/setup/{setup['id']}/answer",
        json={"answer": "secret", "step": setup["step"]},
    )
    assert client.post(f"/api/remotes/setup/{setup['id']}/save").status_code == 409
    assert live == {"CLOUD": {}}


def test_csrf_and_auth_required(client, fake_rclone):
    client.headers.pop("X-CSRF-Token")
    assert (
        client.post(
            "/api/remotes/setup", json={"name": "cloud", "provider": "webdav"}
        ).status_code
        == 403
    )
    client.app.dependency_overrides[require_auth] = lambda: (_ for _ in ()).throw(
        HTTPException(401)
    )
    assert client.get("/api/remotes/providers").status_code == 401


def test_errors_do_not_echo_credentials(monkeypatch):
    monkeypatch.setattr(
        remotes.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            a, 1, "token-canary", "password-canary"
        ),
    )
    with pytest.raises(HTTPException) as error:
        remotes._rc("config/create", {"pass": "password-canary"})
    assert "canary" not in str(error.value)


def test_rclone_error_can_be_answered_again(client, fake_rclone, monkeypatch):
    setup = begin(client)
    original = remotes._rc

    def invalid_answer(method, payload=None, config=None):
        if method == "config/update":
            return {
                "State": "retry",
                "Error": "secret-canary",
                "Option": {"Name": "pass", "IsPassword": True},
            }
        return original(method, payload, config)

    monkeypatch.setattr(remotes, "_rc", invalid_answer)
    result = client.post(
        f"/api/remotes/setup/{setup['id']}/answer",
        json={"answer": "bad", "step": setup["step"]},
    ).json()
    assert result["step"] == setup["step"] + 1
    assert result["error"] and "secret-canary" not in json.dumps(result)
    assert not result["ready"]


@pytest.mark.skipif(not shutil.which("rclone"), reason="rclone binary unavailable")
@pytest.mark.parametrize("provider", ["pcloud", "drive", "onedrive"])
def test_real_cloud_setup_uses_remote_authorization(
    client, tmp_path, monkeypatch, provider
):
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "live.conf"))
    response = client.post(
        "/api/remotes/setup", json={"name": "cloud", "provider": provider}
    )
    assert response.status_code == 200, response.text
    setup = response.json()
    for _ in range(30):
        assert not setup["ready"]
        option = setup["option"]
        assert option["name"] != "config_is_local"
        if option["name"] == "config_token":
            assert "rclone authorize" in option["help"]
            assert option["secret"]
            return
        value = option["default"]
        if option["name"] in {"client_id", "client_secret"} and option["required"]:
            value = "fixture-client-credential"
        if option["name"] == "scope" and provider == "drive":
            value = "drive"
        response = client.post(
            f"/api/remotes/setup/{setup['id']}/answer",
            json={"answer": value, "step": setup["step"]},
        )
        assert response.status_code == 200, (option, response.text)
        setup = response.json()
    pytest.fail("Remote authorization question was never returned")


@pytest.mark.skipif(not shutil.which("rclone"), reason="rclone binary unavailable")
def test_real_rclone_webdav_roundtrip(client, tmp_path, monkeypatch):
    live = tmp_path / "live.conf"
    live.write_text("[existing]\ntype = local\n", encoding="utf-8")
    monkeypatch.setenv("RCLONE_CONFIG", str(live))
    monkeypatch.setattr(
        remotes,
        "get_db",
        lambda: (_ for _ in ()).throw(RuntimeError("no audit in integration")),
    )
    setup = begin(client)
    values = {
        "url": "https://example.invalid/dav",
        "vendor": "other",
        "user": "test",
        "pass": "test-secret",
        "config_advanced": "false",
    }
    for _ in range(100):
        if setup["ready"]:
            break
        option = setup["option"]
        value = values.get(option["name"], option["default"])
        response = client.post(
            f"/api/remotes/setup/{setup['id']}/answer",
            json={"answer": value, "step": setup["step"]},
        )
        assert response.status_code == 200, response.text
        setup = response.json()
        assert not setup["error"], option["name"]
    assert setup["ready"]
    assert "test-secret" not in Path(remotes._setups[setup["id"]].config).read_text(
        encoding="utf-8"
    )
    assert "[cloud]" not in live.read_text(encoding="utf-8")
    response = client.post(f"/api/remotes/setup/{setup['id']}/save")
    assert response.status_code == 200, response.text
    contents = live.read_text(encoding="utf-8")
    assert "[existing]" in contents and "[cloud]" in contents
    assert "test-secret" not in contents
    assert remotes._rc("config/get", {"name": "cloud"})["url"] == values["url"]
