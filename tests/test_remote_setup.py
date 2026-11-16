import base64
import json
import os
import shlex
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


def test_providers_prioritizes_iclouddrive_and_lists_existing_icloud_remotes(
    client, monkeypatch
):
    def rc(method, payload=None, config=None):
        if method == "config/providers":
            return {
                "providers": [
                    {"Name": "s3", "Description": "Amazon S3"},
                    {"Name": "pcloud", "Description": "pCloud"},
                    {"Name": "iclouddrive", "Description": "iCloud Drive and Photos"},
                    {"Name": "drive", "Description": "Google Drive"},
                ]
            }
        if method == "config/listremotes":
            return {"remotes": ["gdrive:", "icloud:"]}
        if method == "config/get":
            if payload["name"] == "icloud":
                return {
                    "type": "iclouddrive",
                    "service": "photos",
                    "apple_id": "max@icloud.com",
                    "password": "obscured-secret",
                }
            return {"type": "drive"}
        raise AssertionError(method)

    monkeypatch.setattr(remotes, "_rc", rc)
    res = client.get("/api/remotes/providers")
    assert res.status_code == 200
    body = res.json()
    names = [p["name"] for p in body["providers"]]
    assert names[:3] == ["drive", "iclouddrive", "pcloud"]
    icloud_desc = next(
        p["description"] for p in body["providers"] if p["name"] == "iclouddrive"
    )
    assert "Apple iCloud" in icloud_desc
    assert body["icloud_remotes"] == [
        {"name": "icloud", "service": "photos", "apple_id": "max@icloud.com"}
    ]


def test_icloud_express_direct_credentials_obscures_password_and_saves_without_reauth(
    client, tmp_path, monkeypatch
):
    live_conf = tmp_path / "rclone.conf"
    live_conf.write_text("[existing]\ntype = local\n", encoding="utf-8")
    monkeypatch.setenv("RCLONE_CONFIG", str(live_conf))
    monkeypatch.setattr(
        remotes,
        "get_db",
        lambda: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )

    recorded_steps: list[tuple[str, str]] = []
    live_create_called = False

    def rc(method, payload=None, config=None):
        nonlocal live_create_called
        payload = payload or {}
        if method == "config/providers":
            return {
                "providers": [
                    {"Name": "iclouddrive", "Description": "iCloud Drive and Photos"}
                ]
            }
        if method == "config/listremotes":
            return {"remotes": ["existing:"]}
        if method == "core/obscure":
            return {
                "obscured": "obscured-apple-pw"
                if payload["clear"] == "MyAppleSecret!"
                else "obscured-other"
            }
        if method == "config/create":
            if config is None:
                live_create_called = True
                raise AssertionError("live config/create must not run for iclouddrive")
            return {
                "State": "*all-set,0,false",
                "Option": {"Name": "service", "Required": True, "Default": "drive"},
            }
        if method == "config/update":
            assert config is not None
            state = payload["opt"]["state"]
            ans = payload["opt"]["result"]
            recorded_steps.append((state, ans))
            if state == "*all-set,0,false":
                return {
                    "State": "*all-set,1,false",
                    "Option": {"Name": "apple_id", "Required": True},
                }
            if state == "*all-set,1,false":
                return {
                    "State": "*all-set,2,false",
                    "Option": {
                        "Name": "password",
                        "Required": True,
                        "IsPassword": True,
                    },
                }
            if state == "*all-set,2,false":
                return {
                    "State": "*all-advanced",
                    "Option": {"Name": "config_fs_advanced", "Default": False},
                }
            if state == "*all-advanced":
                return {
                    "State": "2fa_do",
                    "Option": {
                        "Name": "config_2fa",
                        "Help": "Two-factor authentication: please enter your 2FA code",
                        "Required": True,
                    },
                }
            if state == "2fa_do":
                assert ans == "123456"
                return {"State": ""}
        if method == "config/get":
            assert config is not None
            return {
                "type": "iclouddrive",
                "service": "photos",
                "apple_id": "max@icloud.com",
                "password": "obscured-apple-pw",
                "trust_token": "TRUST-TOKEN-XYZ",
                "cookies": "COOKIES-ABC",
                "_auth_session": "",
            }
        raise AssertionError(method)

    monkeypatch.setattr(remotes, "_rc", rc)

    started = client.post(
        "/api/remotes/setup",
        json={
            "name": "icloud-fotos",
            "provider": "iclouddrive",
            "icloud_service": "photos",
            "apple_id": "max@icloud.com",
            "apple_password": "MyAppleSecret!",
        },
    )
    assert started.status_code == 200, started.text
    view = started.json()
    assert not view["ready"]
    assert view["option"]["name"] == "config_2fa"
    assert view["option"]["input_mode"] == "numeric"
    assert view["option"]["secret"] is False
    assert "6-stelliger Apple-2FA" in (view["option"]["label_de"] or "")
    assert ("*all-set,0,false", "photos") in recorded_steps
    assert ("*all-set,1,false", "max@icloud.com") in recorded_steps
    assert ("*all-set,2,false", "obscured-apple-pw") in recorded_steps
    assert ("*all-advanced", "false") in recorded_steps

    # Submit 6-digit code with whitespace/hyphen formatting as shown on Apple devices
    answered = client.post(
        f"/api/remotes/setup/{view['id']}/answer",
        json={"step": view["step"], "answer": " 123 456 "},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["ready"] is True

    saved = client.post(f"/api/remotes/setup/{view['id']}/save")
    assert saved.status_code == 200, saved.text
    assert saved.json() == {
        "ok": True,
        "name": "icloud-fotos",
        "path": "icloud-fotos:",
        "reconnected": False,
    }
    assert not live_create_called
    rendered = live_conf.read_text(encoding="utf-8")
    assert "[existing]" in rendered
    assert "[icloud-fotos]" in rendered
    assert "type = iclouddrive" in rendered
    assert "service = photos" in rendered
    assert "trust_token = TRUST-TOKEN-XYZ" in rendered
    assert "_auth_session" not in rendered
    assert "MyAppleSecret!" not in rendered


def test_icloud_reconnect_replaces_existing_section_and_blocks_other_types(
    client, tmp_path, monkeypatch
):
    live_conf = tmp_path / "rclone.conf"
    live_conf.write_text(
        "[gdrive]\ntype = drive\n\n[icloud]\ntype = iclouddrive\nservice = drive\napple_id = alt@icloud.com\ntrust_token = EXPIRED\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RCLONE_CONFIG", str(live_conf))
    monkeypatch.setattr(
        remotes,
        "get_db",
        lambda: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )

    seen_apple_id = []

    def rc(method, payload=None, config=None):
        payload = payload or {}
        if method == "config/providers":
            return {
                "providers": [
                    {"Name": "iclouddrive", "Description": "iCloud Drive and Photos"},
                    {"Name": "drive", "Description": "Google Drive"},
                ]
            }
        if method == "config/listremotes":
            return {"remotes": ["gdrive:", "icloud:"]}
        if method == "config/get":
            if config is None:
                if payload["name"] == "gdrive":
                    return {"type": "drive"}
                return {
                    "type": "iclouddrive",
                    "service": "drive",
                    "apple_id": "alt@icloud.com",
                }
            return {
                "type": "iclouddrive",
                "service": "drive",
                "apple_id": "alt@icloud.com",
                "password": "OBS(pw)",
                "trust_token": "FRESH-TOKEN-999",
                "cookies": "FRESH-COOKIES",
            }
        if method == "core/obscure":
            return {"obscured": "OBS(pw)"}
        if method == "config/create":
            return {"State": "*all-set,0,false", "Option": {"Name": "service"}}
        if method == "config/update":
            st = payload["opt"]["state"]
            res = payload["opt"]["result"]
            if st == "*all-set,0,false":
                return {"State": "*all-set,1,false", "Option": {"Name": "apple_id"}}
            if st == "*all-set,1,false":
                seen_apple_id.append(res)
                return {
                    "State": "*all-set,2,false",
                    "Option": {"Name": "password", "IsPassword": True},
                }
            if st == "*all-set,2,false":
                return {
                    "State": "2fa_do",
                    "Option": {"Name": "config_2fa", "Required": True},
                }
            if st == "2fa_do" and res == "sms":
                return {
                    "State": "2fa_sms",
                    "Option": {"Name": "config_2fa_sms", "Required": True},
                }
            if st == "2fa_sms" and res == "654321":
                return {"State": ""}
        raise AssertionError((method, payload))

    monkeypatch.setattr(remotes, "_rc", rc)

    # Reconnect must NEVER overwrite a non-iclouddrive remote
    conflict = client.post(
        "/api/remotes/setup",
        json={"name": "gdrive", "provider": "iclouddrive", "reconnect": True},
    )
    assert conflict.status_code == 409

    # Reconnect on existing icloud remote pre-fills apple_id and replaces section cleanly
    started = client.post(
        "/api/remotes/setup",
        json={
            "name": "icloud",
            "provider": "iclouddrive",
            "icloud_service": "",
            "apple_password": "pw",
            "reconnect": True,
        },
    )
    assert started.status_code == 200, started.text
    assert seen_apple_id == ["alt@icloud.com"]
    s1 = started.json()
    assert s1["option"]["name"] == "config_2fa"

    # Trigger SMS fallback
    s2 = client.post(
        f"/api/remotes/setup/{s1['id']}/answer",
        json={"step": s1["step"], "answer": "sms"},
    ).json()
    assert s2["option"]["name"] == "config_2fa_sms"
    assert s2["option"]["input_mode"] == "numeric"

    s3 = client.post(
        f"/api/remotes/setup/{s2['id']}/answer",
        json={"step": s2["step"], "answer": "654-321"},
    ).json()
    assert s3["ready"] is True

    saved = client.post(f"/api/remotes/setup/{s3['id']}/save")
    assert saved.status_code == 200, saved.text
    assert saved.json()["reconnected"] is True
    content = live_conf.read_text(encoding="utf-8")
    assert content.count("[icloud]") == 1
    assert "[gdrive]" in content
    assert "FRESH-TOKEN-999" in content
    assert "EXPIRED" not in content


def test_apple_wrong_password_returns_helpful_400_without_credential_leak(monkeypatch):
    monkeypatch.setattr(
        remotes.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            a,
            1,
            '{"error":"authSRPComplete: sign in failed: incorrect username or password secret-canary"}',
            "notice secret-canary",
        ),
    )
    with pytest.raises(HTTPException) as err:
        remotes._rc("config/create", {"password": "secret-canary"})
    assert err.value.status_code == 400
    assert "reguläre Apple-Account-Passwort" in str(err.value.detail)
    assert "secret-canary" not in str(err.value.detail)


@pytest.mark.skipif(
    not shutil.which("rclone"), reason="Optional offline integration needs rclone"
)
@pytest.mark.parametrize("reconnect", [False, True])
def test_real_own_drive_oauth_reaches_fresh_token_question_offline(
    tmp_path, monkeypatch, reconnect
):
    """Real RC state/storage only: stop before submitting any OAuth result."""
    config = tmp_path / "rclone.conf"
    # Isolate from inherited backend/account environment overrides as well as
    # from the live config. All credentials below are deliberately fake.
    for key in list(os.environ):
        if key.startswith(("RCLONE_CONFIG_", "RCLONE_DRIVE_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("RCLONE_CONFIG", str(config))
    helper_calls = []
    monkeypatch.setattr(
        remotes,
        "_start_browser_auth_helper",
        lambda _setup, command: helper_calls.append(command),
    )
    parameters = {
        "client_id": "fixture.apps.googleusercontent.com",
        "client_secret": "fixture-private-client-secret",
        "scope": "drive.readonly",
        "root_folder_id": "fixture-root",
        "team_drive": "fixture-team",
        "chunk_size": "128Mi",
    }
    if reconnect:
        config.write_text(
            "[Google]\ntype = drive\n"
            + "".join(f"{key} = {value}\n" for key, value in parameters.items()),
            encoding="utf-8",
        )
        result = remotes._rc(
            "config/update",
            {
                "name": "Google",
                "parameters": {},
                "opt": {"nonInteractive": True, "obscure": True},
            },
            str(config),
        )
    else:
        result = remotes._rc(
            "config/create",
            {
                "name": "Google",
                "type": "drive",
                "parameters": parameters,
                "opt": {"nonInteractive": True, "all": False, "obscure": True},
            },
            str(config),
        )
    directory = type("FixtureDirectory", (), {"name": str(tmp_path)})()
    setup = remotes.Setup(
        "owner", "Google", "drive", directory, express=True, require_own_client=True
    )
    remotes._advance(setup, result)
    assert setup.option["Name"] == "config_token" and setup.state
    assert setup.error is False and len(helper_calls) == 1
    command = shlex.split(helper_calls[0])
    assert command[:3] == ["rclone", "authorize", "drive"] and len(command) == 4
    encoded = command[3]
    authorization = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4)))
    assert authorization["client_id"] == parameters["client_id"]
    assert authorization["client_secret"] == parameters["client_secret"]
    assert "token" not in authorization
    stored = remotes._rc("config/get", {"name": "Google"}, setup.config)
    # OAuth client_secret is Sensitive but is not an IsPassword option in
    # rclone; retain its own storage representation without an extra encoding.
    assert {key: stored[key] for key in parameters} == parameters
    assert "token" not in stored
