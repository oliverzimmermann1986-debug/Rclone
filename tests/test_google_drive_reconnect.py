"""Google OAuth setup: private drafts, explicit publication and exact preservation."""

import configparser
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import SESSION_COOKIE, require_auth
from app.db import Database
from app.jobs import locks, runtime_state
from app.routes import api_remotes as remotes


OLD_TOKEN = json.dumps(
    {"access_token": "old-account-access", "refresh_token": "old-refresh"},
    separators=(",", ":"),
)
NEW_TOKEN = json.dumps(
    {"access_token": "new-account-access", "refresh_token": "new-refresh"},
    separators=(",", ":"),
)


def read_config(path):
    parser = configparser.RawConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    return {section: dict(parser[section]) for section in parser.sections()}


def write_config(path, sections):
    parser = configparser.RawConfigParser(interpolation=None)
    parser.optionxform = str
    for name, values in sections.items():
        parser[name] = {key: str(value) for key, value in values.items()}
    with Path(path).open("w", encoding="utf-8") as stream:
        parser.write(stream)


@pytest.fixture
def drive_setup(tmp_path, monkeypatch):
    live = tmp_path / "rclone.conf"
    original = {
        "type": "drive",
        "client_id": "existing-own-client.apps.googleusercontent.com",
        "client_secret": "existing-private-client-secret",
        "token": OLD_TOKEN,
        "scope": "drive.readonly",
        "root_folder_id": "original-root",
        "team_drive": "original-team",
        "chunk_size": "128Mi",
        "acknowledge_abuse": "true",
        "description": "Keep all options",
    }
    crypt = {
        "type": "crypt",
        "remote": "Google:encrypted",
        "password": "unrelated-obscured-secret",
    }
    write_config(live, {"Google": original, "Encrypted": crypt})
    monkeypatch.setenv("RCLONE_CONFIG", str(live))
    monkeypatch.setattr(locks, "LOCK_DIR", tmp_path / "locks")
    database = Database(tmp_path / "fixture.db")
    monkeypatch.setattr(remotes, "get_db", lambda: database)
    monkeypatch.setattr(runtime_state, "active_processes", lambda *_a: [])
    monkeypatch.setattr(runtime_state, "load_run_state", lambda: {})
    calls, initial_drafts = [], []

    def begin_auth(setup, _command):
        setup.direct_auth_url = (
            "https://accounts.google.com/o/oauth2/auth?state=fixture"
        )

    monkeypatch.setattr(remotes, "_start_browser_auth_helper", begin_auth)

    def local_question():
        return {"State": "choose-local", "Option": {"Name": "config_is_local"}}

    def rc(method, payload=None, config=None):
        payload = payload or {}
        calls.append((method, payload, config))
        path = Path(config) if config else live
        if method == "config/providers":
            return {
                "providers": [{"Name": "drive"}, {"Name": "webdav"}, {"Name": "crypt"}]
            }
        if method == "config/listremotes":
            return {"remotes": [name + ":" for name in read_config(live)]}
        if method == "config/get":
            return dict(read_config(path)[payload["name"]])
        if method == "config/create":
            assert config, "OAuth setup must not create a live remote"
            parameters = {"type": payload["type"], **payload["parameters"]}
            write_config(path, {payload["name"]: parameters})
            initial_drafts.append(dict(parameters))
            return local_question()
        if method == "config/update":
            assert config, "OAuth setup must not update a live remote"
            sections = read_config(path)
            section = sections[payload["name"]]
            if not payload["opt"].get("continue"):
                assert "token" not in section
                assert not payload["opt"].get("all")
                section.update(payload["parameters"])
                initial_drafts.append(dict(section))
                write_config(path, sections)
                return local_question()
            state, value = payload["opt"]["state"], payload["opt"]["result"]
            if state == "choose-local":
                assert value == "false"
                return {
                    "State": "oauth-token",
                    "Option": {
                        "Name": "config_token",
                        "Required": True,
                        "Help": 'rclone authorize "drive" "private-client-secret-blob"',
                        "Default": "must-not-echo-token",
                    },
                }
            if state == "oauth-token":
                section["token"] = value
                # A backend's post-auth suggestions must not reset old options.
                section["scope"] = "drive"
                section["root_folder_id"] = "suggested-root"
                write_config(path, sections)
                return {
                    "State": "team-confirm",
                    "Option": {"Name": "config_change_team_drive"},
                }
            if state == "team-confirm":
                assert value == "false"
                return {"State": ""}
        raise AssertionError((method, payload, config))

    monkeypatch.setattr(remotes, "_rc", rc)
    app = FastAPI()
    app.include_router(remotes.router)
    app.dependency_overrides[require_auth] = lambda: "admin"
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, "fixture-owner-a")
    client.cookies.set("rclone_sync_csrf", "fixture-csrf")
    client.headers["X-CSRF-Token"] = "fixture-csrf"
    yield client, live, database, calls, initial_drafts
    for key in list(remotes._setups):
        remotes._discard(key)


def start_reconnect(client, **extra):
    response = client.post(
        "/api/remotes/setup",
        json={
            "name": "google",
            "provider": "drive",
            "reconnect": True,
            "require_own_client": True,
            "express": True,
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def finish_auth(client, view, token=NEW_TOKEN):
    response = client.post(
        f"/api/remotes/setup/{view['id']}/answer",
        json={"step": view["step"], "answer": token},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ready"] is True
    return response.json()


def test_provider_metadata_lists_drive_without_exposing_credentials(drive_setup):
    client, _live, _database, _calls, _drafts = drive_setup
    response = client.get("/api/remotes/providers")
    assert response.status_code == 200
    assert response.json()["drive_remotes"] == [
        {"name": "Google", "has_own_client": True}
    ]
    assert response.json()["drive_reconnect_supported"] is True
    assert (
        "client-secret" not in response.text and "account-access" not in response.text
    )


def test_sensitive_drive_client_secret_question_is_a_secure_response_field(drive_setup):
    client, _live, _database, _calls, _drafts = drive_setup
    view = start_reconnect(client)
    setup = remotes._setups[view["id"]]
    setup.option = {
        "Name": "client_secret",
        "Sensitive": True,
        "IsPassword": False,
        "Default": "private-secret-default",
        "Examples": [{"Value": "private-secret-example"}],
    }
    response = remotes._view(view["id"], setup)
    assert response["option"]["secret"] is True
    assert response["option"]["default"] == "" and response["option"]["examples"] == []
    assert "private-secret" not in json.dumps(response)


def test_reconnect_preserves_options_and_other_remotes_until_explicit_save(drive_setup):
    client, live, _database, calls, drafts = drive_setup
    before_bytes, original = live.read_bytes(), read_config(live)
    view = start_reconnect(client)
    assert live.read_bytes() == before_bytes
    assert "token" not in drafts[0]
    assert {key: value for key, value in drafts[0].items()} == {
        key: value for key, value in original["Google"].items() if key != "token"
    }
    for forbidden in [
        "existing-private-client-secret",
        "old-account-access",
        "private-client-secret-blob",
        "must-not-echo-token",
    ]:
        assert forbidden not in json.dumps(view)
    assert view["option"]["authorize_command"] is None
    assert view["option"]["auth_url"].startswith("https://accounts.google.com/")
    assert client.post(f"/api/remotes/setup/{view['id']}/save").status_code == 409
    ready = finish_auth(client, view)
    assert live.read_bytes() == before_bytes
    saved = client.post(f"/api/remotes/setup/{ready['id']}/save")
    assert saved.status_code == 200, saved.text
    assert saved.json() == {
        "ok": True,
        "name": "Google",
        "path": "Google:",
        "reconnected": True,
    }
    final = read_config(live)
    assert final["Google"] == {**original["Google"], "token": NEW_TOKEN}
    assert final["Encrypted"] == original["Encrypted"]
    assert not any(
        method in {"config/create", "config/update"} and config is None
        for method, _payload, config in calls
    )


def test_reconnect_can_replace_client_pair_without_resetting_scope_or_team(drive_setup):
    client, live, _database, _calls, drafts = drive_setup
    original = read_config(live)["Google"]
    view = start_reconnect(
        client, client_id="new-own-client", client_secret="new-own-secret"
    )
    assert (
        drafts[0]["client_id"] == "new-own-client"
        and drafts[0]["client_secret"] == "new-own-secret"
    )
    ready = finish_auth(client, view)
    assert client.post(f"/api/remotes/setup/{ready['id']}/save").status_code == 200
    assert read_config(live)["Google"] == {
        **original,
        "client_id": "new-own-client",
        "client_secret": "new-own-secret",
        "token": NEW_TOKEN,
    }


@pytest.mark.parametrize(
    "fields", [{"client_id": "only-id"}, {"client_secret": "only-secret"}]
)
def test_partial_client_replacement_is_rejected_without_creating_draft(
    drive_setup, fields
):
    client, live, _database, _calls, drafts = drive_setup
    original = live.read_bytes()
    response = client.post(
        "/api/remotes/setup",
        json={
            "name": "Google",
            "provider": "drive",
            "reconnect": True,
            "require_own_client": True,
            **fields,
        },
    )
    assert response.status_code == 400
    assert not drafts and live.read_bytes() == original


def test_new_native_drive_requires_client_pair_but_legacy_start_is_compatible(
    drive_setup,
):
    client, _live, _database, _calls, _drafts = drive_setup
    native = client.post(
        "/api/remotes/setup",
        json={"name": "NewDrive", "provider": "drive", "require_own_client": True},
    )
    assert native.status_code == 400
    legacy = client.post(
        "/api/remotes/setup", json={"name": "NewDrive", "provider": "drive"}
    )
    assert legacy.status_code == 200, legacy.text


def test_new_native_drive_creates_live_section_only_after_auth_and_save(drive_setup):
    client, live, _database, _calls, _drafts = drive_setup
    original = read_config(live)
    response = client.post(
        "/api/remotes/setup",
        json={
            "name": "NewDrive",
            "provider": "drive",
            "require_own_client": True,
            "client_id": "new-id",
            "client_secret": "new-secret",
        },
    )
    assert response.status_code == 200, response.text
    ready = finish_auth(client, response.json())
    assert read_config(live) == original
    assert client.post(f"/api/remotes/setup/{ready['id']}/save").status_code == 200
    assert read_config(live)["NewDrive"]["token"] == NEW_TOKEN
    assert read_config(live)["Encrypted"] == original["Encrypted"]


def test_same_name_other_provider_and_missing_reconnect_are_rejected(drive_setup):
    client, _live, _database, _calls, _drafts = drive_setup
    for name, expected in [("Encrypted", 409), ("MissingDrive", 404)]:
        response = client.post(
            "/api/remotes/setup",
            json={"name": name, "provider": "drive", "reconnect": True},
        )
        assert response.status_code == expected


def test_owner_and_step_guards_still_protect_private_draft(drive_setup):
    client, live, _database, _calls, _drafts = drive_setup
    before = live.read_bytes()
    view = start_reconnect(client)
    client.cookies.set(SESSION_COOKIE, "fixture-owner-b")
    assert client.post(f"/api/remotes/setup/{view['id']}/save").status_code == 404
    client.cookies.set(SESSION_COOKIE, "fixture-owner-a")
    assert (
        client.post(
            f"/api/remotes/setup/{view['id']}/answer",
            json={"step": 0, "answer": NEW_TOKEN},
        ).status_code
        == 409
    )
    client.headers.pop("X-CSRF-Token")
    assert client.post(f"/api/remotes/setup/{view['id']}/save").status_code == 403
    assert live.read_bytes() == before


def test_remote_change_conflicts_without_overwriting_new_configuration(drive_setup):
    client, live, _database, _calls, _drafts = drive_setup
    ready = finish_auth(client, start_reconnect(client))
    changed = read_config(live)
    changed["Google"]["root_folder_id"] = "concurrent-new-root"
    write_config(live, changed)
    before = live.read_bytes()
    response = client.post(f"/api/remotes/setup/{ready['id']}/save")
    assert response.status_code == 409
    assert live.read_bytes() == before


def test_changes_to_another_remote_are_preserved_at_save(drive_setup):
    client, live, _database, _calls, _drafts = drive_setup
    ready = finish_auth(client, start_reconnect(client))
    changed = read_config(live)
    changed["Encrypted"]["filename_encryption"] = "off"
    write_config(live, changed)
    assert client.post(f"/api/remotes/setup/{ready['id']}/save").status_code == 200
    assert read_config(live)["Encrypted"] == changed["Encrypted"]


@pytest.mark.parametrize("kind", ["backup", "check", "recovery"])
def test_active_job_blocks_save_without_stopping_or_mutating_job(drive_setup, kind):
    client, live, database, _calls, _drafts = drive_setup
    ready = finish_auth(client, start_reconnect(client))
    before = live.read_bytes()
    job = database.job_start(kind)
    response = client.post(f"/api/remotes/setup/{ready['id']}/save")
    assert response.status_code == 409
    assert database.job_get(job)["status"] == "running" and live.read_bytes() == before


@pytest.mark.parametrize("reason", ["lock", "process", "run-state"])
def test_runtime_and_file_lock_gate_publication(drive_setup, monkeypatch, reason):
    client, live, _database, _calls, _drafts = drive_setup
    ready = finish_auth(client, start_reconnect(client))
    before = live.read_bytes()
    lease = (
        locks.try_file_lock(runtime_state.DEFAULT_CANCEL_SCOPE)
        if reason == "lock"
        else None
    )
    if reason == "process":
        monkeypatch.setattr(
            runtime_state, "active_processes", lambda *_a: [{"pid": 12345}]
        )
    if reason == "run-state":
        monkeypatch.setattr(
            runtime_state, "load_run_state", lambda: {"status": "running"}
        )
    try:
        assert client.post(f"/api/remotes/setup/{ready['id']}/save").status_code == 409
        assert live.read_bytes() == before
    finally:
        if lease:
            lease.release()


def test_original_account_token_is_never_accepted_as_fresh_login(drive_setup):
    client, live, _database, _calls, _drafts = drive_setup
    before = live.read_bytes()
    ready = finish_auth(
        client,
        start_reconnect(client),
        token=json.dumps(json.loads(OLD_TOKEN), indent=4, sort_keys=True),
    )
    assert client.post(f"/api/remotes/setup/{ready['id']}/save").status_code == 409
    assert live.read_bytes() == before


def test_same_access_with_new_refresh_is_a_distinct_complete_oauth_result(drive_setup):
    client, live, _database, _calls, _drafts = drive_setup
    token = json.dumps(
        {
            **json.loads(OLD_TOKEN),
            "refresh_token": "new-consent-refresh",
            "expiry": "2030-01-01T00:00:00Z",
        }
    )
    ready = finish_auth(client, start_reconnect(client), token=token)
    assert client.post(f"/api/remotes/setup/{ready['id']}/save").status_code == 200
    assert json.loads(read_config(live)["Google"]["token"]) == json.loads(token)


def test_access_token_without_refresh_is_not_a_complete_drive_login(drive_setup):
    client, live, _database, _calls, _drafts = drive_setup
    before = live.read_bytes()
    ready = finish_auth(
        client,
        start_reconnect(client),
        token=json.dumps({"access_token": "new-access-without-refresh"}),
    )
    assert client.post(f"/api/remotes/setup/{ready['id']}/save").status_code == 409
    assert live.read_bytes() == before


def test_oauth_result_cannot_silently_replace_the_selected_client(drive_setup):
    client, live, _database, _calls, _drafts = drive_setup
    before = live.read_bytes()
    ready = finish_auth(client, start_reconnect(client))
    path = Path(remotes._setups[ready["id"]].config)
    changed = read_config(path)
    changed["Google"]["client_id"] = "different-nonempty-client"
    write_config(path, changed)
    assert client.post(f"/api/remotes/setup/{ready['id']}/save").status_code == 409
    assert live.read_bytes() == before


def test_reconnect_without_existing_own_client_does_not_use_shared_fallback(
    drive_setup,
):
    client, live, _database, _calls, drafts = drive_setup
    sections = read_config(live)
    sections["Google"].pop("client_id")
    sections["Google"].pop("client_secret")
    write_config(live, sections)
    response = client.post(
        "/api/remotes/setup",
        json={
            "name": "Google",
            "provider": "drive",
            "reconnect": True,
            "require_own_client": True,
        },
    )
    assert response.status_code == 400 and not drafts


def test_encrypted_live_config_is_explicitly_rejected_without_modification(drive_setup):
    client, live, _database, _calls, _drafts = drive_setup
    ready = finish_auth(client, start_reconnect(client))
    encrypted = b"RCLONE_ENCRYPT_V0:\nfixture-encrypted-content\n"
    live.write_bytes(encrypted)
    response = client.post(f"/api/remotes/setup/{ready['id']}/save")
    assert response.status_code == 409
    assert live.read_bytes() == encrypted


@pytest.mark.parametrize(
    "auth", [{"service_account_file": "/fixture/account.json"}, {"env_auth": "true"}]
)
def test_browser_reconnect_does_not_convert_service_account_or_env_auth(
    drive_setup, auth
):
    client, live, _database, _calls, drafts = drive_setup
    original = read_config(live)
    original["Google"].update(auth)
    write_config(live, original)
    response = client.post(
        "/api/remotes/setup",
        json={"name": "Google", "provider": "drive", "reconnect": True},
    )
    assert response.status_code == 409 and not drafts
