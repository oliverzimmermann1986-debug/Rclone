"""Regressions for committed authentication and owner-bound APNs operations."""

import json
import threading
from types import SimpleNamespace

import pytest
import yaml
from fastapi import HTTPException, Request

from app import auth, config_store, db, push_notifications as push
from app.config_store import Config
from app.config_validation import DEFAULT_APNS_ERROR_EVENTS, validate_config
from app.db import Database
from app.jobs import scheduler_cli
from app.routes import api_push, api_webauthn


TOKEN = "ab" * 32
CREDENTIAL_ID = "ZHVtbXkta2V5"


@pytest.fixture
def account(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "web": {
                    "username": "admin",
                    "secret_key": "auth-push-regression-0123456789-ABCDEFGHIJKLMN",
                    "session_version": 1,
                    "secure_cookie": True,
                    "webauthn_rp_id": "backup.example",
                    "webauthn_origin": "https://backup.example",
                },
                "paths": {
                    "data_dir": str(tmp_path),
                    "logs_dir": str(tmp_path / "logs"),
                    "temp_dir": str(tmp_path / "temp"),
                },
                "backup": {"pairs": [], "jobs": []},
            }
        ),
        encoding="utf-8",
    )
    store = Config(path)
    database = Database(tmp_path / "fixture.db")
    monkeypatch.setattr(config_store, "_config", store)
    monkeypatch.setattr(db, "_db", database)
    monkeypatch.setattr(auth, "_webauthn_reauth_memory", {})
    return store, database


def request():
    cookie = auth.create_session("admin")
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/webauthn/authentication/verify",
            "root_path": "",
            "query_string": b"",
            "http_version": "1.1",
            "headers": [(b"cookie", f"{auth.SESSION_COOKIE}={cookie}".encode())],
            "client": ("192.0.2.10", 12345),
            "server": ("backup.example", 443),
            "scheme": "https",
        }
    )


def credential(database):
    database.webauthn_credential_add(
        credential_id=CREDENTIAL_ID,
        method="passkey",
        public_key=b"dummy-key-not-used-for-cryptographic-verification",
        sign_count=0,
        transports=[],
        device_type="single_device",
        backed_up=False,
        label="Fixture",
    )
    stored = database.webauthn_credential_get(CREDENTIAL_ID)
    return {
        "username": "admin",
        "account_generation": auth.account_generation(),
        "credential_id": CREDENTIAL_ID,
        "credential_generation": stored["generation"],
        "native": False,
        "method": "passkey",
        "app_binding": "",
    }


def test_stepup_requires_committed_consumption(account):
    _store, database = account
    req = request()
    token = auth.issue_webauthn_reauth_token(req, "admin")
    with database.conn() as connection:
        connection.execute(
            "CREATE TRIGGER reject_stepup_write BEFORE UPDATE ON runtime_settings "
            "WHEN NEW.key='webauthn_reauth_tokens' "
            "BEGIN SELECT RAISE(FAIL, 'fixture write failure'); END"
        )
    assert auth.consume_webauthn_reauth_token(req, "admin", token) is None
    assert database.runtime_get("webauthn_reauth_tokens")["tokens"]
    with database.conn() as connection:
        connection.execute("DROP TRIGGER reject_stepup_write")
    assert auth.consume_webauthn_reauth_token(req, "admin", token)
    assert auth.consume_webauthn_reauth_token(req, "admin", token) is None


@pytest.mark.parametrize("revocation", ["account", "credential"])
def test_stepup_finally_rechecks_verified_grant(account, monkeypatch, revocation):
    store, database = account
    req = request()
    proof = credential(database)

    def verify_then_revoke(*_args):
        if revocation == "account":
            store.update(lambda data: data["web"].update(session_version=2))
        else:
            database.webauthn_credential_delete(CREDENTIAL_ID)
        return proof

    monkeypatch.setattr(api_webauthn, "finish_authentication", verify_then_revoke)
    with pytest.raises(HTTPException) as denied:
        api_webauthn.complete_reauth(
            api_webauthn.CeremonyResponse(challenge_id="c" * 24, credential={}),
            req,
            user="admin",
        )
    assert denied.value.status_code == 400
    assert not database.runtime_get("webauthn_reauth_tokens", {}).get("tokens")


@pytest.mark.parametrize("explicit_method", [None, "passkey", "security_key"])
def test_hardware_only_reauth_default_uses_registered_method(
    account, monkeypatch, explicit_method
):
    _store, database = account
    credential(database)
    with database.conn() as connection:
        connection.execute(
            "UPDATE webauthn_credentials SET method='security_key' WHERE credential_id=?",
            (CREDENTIAL_ID,),
        )
    assert api_webauthn.status() == {
        "enabled": True,
        "passkey": False,
        "security_key": True,
    }
    calls = []

    def options(method, **_kwargs):
        calls.append(method)
        return {"fixture": True}

    monkeypatch.setattr(api_webauthn, "authentication_options", options)
    body = (
        api_webauthn.ReauthOptionsRequest(method=explicit_method)
        if explicit_method
        else None
    )
    assert api_webauthn.begin_reauth(request(), body, user="admin") == {"fixture": True}
    assert calls == [explicit_method or "security_key"]


def test_revoked_inflight_session_cannot_register_push(account):
    _store, database = account
    req = request()
    # The dependency authenticated this request before the account rotated.
    assert auth.session_user(req.cookies[auth.SESSION_COOKIE]) == "admin"
    auth.bump_session_version()
    with pytest.raises(HTTPException) as denied:
        api_push.register_push_device(
            api_push.PushDeviceRegistration(token=TOKEN), req, user="admin"
        )
    assert denied.value.status_code == 401
    assert database.push_devices() == []
    assert api_push.register_push_device(
        api_push.PushDeviceRegistration(token=TOKEN), request(), user="admin"
    )["registered"]


@pytest.mark.parametrize("length", [65, 127, 511])
def test_push_tokens_require_complete_hex_bytes(length):
    with pytest.raises(HTTPException) as denied:
        api_push._token("a" * length)
    assert denied.value.status_code == 422
    assert api_push._token("ab" * 32) == "ab" * 32


def test_persisted_push_registration_survives_audit_failure(account, monkeypatch):
    _store, database = account
    monkeypatch.setattr(
        database, "audit_add", lambda *_a, **_k: (_ for _ in ()).throw(OSError())
    )
    result = api_push.register_push_device(
        api_push.PushDeviceRegistration(token=TOKEN), request(), user="admin"
    )
    assert result["registered"] and database.push_device_exists(TOKEN)


def test_persisted_key_deletion_survives_audit_failure(account, monkeypatch):
    _store, database = account
    credential(database)
    generation = auth.account_generation()
    monkeypatch.setattr(
        api_webauthn, "require_reauthentication", lambda *_a: generation
    )
    monkeypatch.setattr(
        database, "audit_add", lambda *_a, **_k: (_ for _ in ()).throw(OSError())
    )
    assert api_webauthn.delete_credential(
        CREDENTIAL_ID,
        api_webauthn.DeleteCredentialRequest(current_password="fixture-confirmed"),
        request(),
        user="admin",
    ) == {"ok": True}
    assert database.webauthn_credential_get(CREDENTIAL_ID) is None


def test_verified_login_still_issues_cookie_if_audit_fails(account, monkeypatch):
    _store, database = account
    proof = credential(database)
    monkeypatch.setattr(api_webauthn, "finish_authentication", lambda *_a: proof)
    monkeypatch.setattr(
        database, "audit_add", lambda *_a, **_k: (_ for _ in ()).throw(OSError())
    )
    response = api_webauthn.complete_authentication(
        api_webauthn.CeremonyResponse(challenge_id="c" * 24, credential={}), request()
    )
    assert response.status_code == 200
    assert any(
        name == b"set-cookie" and value.startswith(auth.SESSION_COOKIE.encode() + b"=")
        for name, value in response.raw_headers
    )


def test_apns_validation_and_sender_share_all_default_errors(account):
    store, _database = account
    config = store.snapshot()
    config["notifications"] = {
        "apns": {
            "enabled": True,
            "team_id": "ABCDEFGHIJ",
            "key_id": "KLMNOPQRST",
            "key_file": str(store.path.parent / "dummy.p8"),
            "topic": "de.oliverzimmermann.rclonesync",
        }
    }
    normalized, _warnings = validate_config(config)
    assert normalized["notifications"]["apns"]["events"] == list(
        DEFAULT_APNS_ERROR_EVENTS
    )
    assert push.DEFAULT_ERROR_EVENTS == DEFAULT_APNS_ERROR_EVENTS
    assert len(DEFAULT_APNS_ERROR_EVENTS) == 9


def test_heartbeat_write_failure_stops_post_before_return(monkeypatch):
    started, stopped, completed = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    class BlockingClient:
        closed = False

        def post(self, *_args, **_kwargs):
            started.set()
            try:
                assert stopped.wait(2)
                raise OSError("fixture connection closed")
            finally:
                completed.set()

        def close(self):
            self.closed = True
            stopped.set()

    def renewal_failure(*_args, **_kwargs):
        assert started.wait(1)
        raise OSError("fixture heartbeat persistence failed")

    client = BlockingClient()
    monkeypatch.setattr(push, "_CLAIM_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    try:
        with pytest.raises(OSError, match="heartbeat persistence failed"):
            push._post_with_claim_heartbeat(
                client,
                "https://example.invalid/fixture",
                content=b"{}",
                headers={},
                database=SimpleNamespace(push_outbox_renew_claims=renewal_failure),
                row_id=1,
                claim_owner="fixture",
                lease_seconds=10,
            )
        closed_on_return, completed_on_return = client.closed, completed.is_set()
    finally:
        client.close()
        assert completed.wait(1)
    assert closed_on_return and completed_on_return


class Reply:
    headers = {}

    def __init__(self, code=200, reason=""):
        self.status_code, self.reason = code, reason

    def json(self):
        return {"reason": self.reason}


class Client:
    def __init__(self, callback, **_kwargs):
        self.callback = callback

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def close(self):
        pass

    def post(self, url, *, content, headers):
        return self.callback(url, content, headers)


def sender_settings(monkeypatch):
    monkeypatch.setattr(
        push,
        "_settings",
        lambda _event: {"timeout": 1, "topic": "dummy", "retention_seconds": 3600},
    )
    monkeypatch.setattr(
        push, "_provider_token", lambda _settings: "dummy-provider-token"
    )


def enqueue(database, key, **payload):
    return database.push_outbox_enqueue(
        event="sync_error",
        title="Dummy",
        message="Dummy",
        payload=payload,
        dedupe_key=key,
        retention_seconds=3600,
    )


def test_old_environment_does_not_send_or_remove_new_registration(account, monkeypatch):
    _store, database = account
    sender_settings(monkeypatch)
    database.push_device_upsert(TOKEN, "sandbox")
    enqueue(database, "old-environment")
    database.push_device_upsert(TOKEN, "production")
    calls = []
    result = push.dispatch_pending_pushes(
        db=database,
        client_factory=lambda **kw: Client(
            lambda *args: calls.append(args) or Reply(), **kw
        ),
    )
    assert calls == [] and result["sent"] == 0
    assert database.push_devices()[0]["environment"] == "production"


def test_rejected_old_delivery_keeps_refreshed_registration(account, monkeypatch):
    _store, database = account
    sender_settings(monkeypatch)
    database.push_device_upsert(TOKEN, "production", now=100)
    # Keep the fixture registration active at the wall clock used by dispatch.
    with database.conn() as connection:
        connection.execute(
            "UPDATE push_devices SET expires_at=1e12 WHERE token=?", (TOKEN,)
        )
    enqueue(database, "refresh-during-post")

    def refresh_then_reject(*_args):
        database.push_device_upsert(TOKEN, "production")
        return Reply(400, "BadDeviceToken")

    result = push.dispatch_pending_pushes(
        db=database, client_factory=lambda **kw: Client(refresh_then_reject, **kw)
    )
    assert result["removed"] == 0 and database.push_device_exists(TOKEN)


def test_synchronous_push_counts_only_requested_message(account, monkeypatch):
    _store, database = account
    sender_settings(monkeypatch)
    database.push_device_upsert(TOKEN, "production")
    enqueue(database, "unrelated", job_id=1)
    calls = []

    def reply_to_message(_url, content, _headers):
        job_id = json.loads(content)["job_id"]
        calls.append(job_id)
        return Reply(503, "ServiceUnavailable") if job_id == 42 else Reply()

    result = push.send_push_notifications(
        "sync_error",
        "Dummy",
        "Dummy",
        extra={"job_id": 42},
        dedupe_key="requested-test",
        db=database,
        client_factory=lambda **kw: Client(reply_to_message, **kw),
    )
    assert result["sent"] == 0 and result["retrying"] == 1
    assert calls == [42]
    assert database.push_outbox_sent_count("unrelated") == 0


def test_scheduler_starts_work_before_draining_pushes(monkeypatch):
    calls = []

    def tick():
        calls.append("backup-started")
        return 7

    def dispatch(**_kwargs):
        assert calls == ["backup-started"]
        calls.append("push-dispatched")
        return {"sent": 0, "retrying": 0}

    monkeypatch.setattr(scheduler_cli, "_run_scheduler_tick", tick)
    monkeypatch.setattr(scheduler_cli, "get_db", lambda: object())
    monkeypatch.setattr(push, "dispatch_pending_pushes", dispatch)
    assert scheduler_cli.main() == 7
    assert calls == ["backup-started", "push-dispatched"]
