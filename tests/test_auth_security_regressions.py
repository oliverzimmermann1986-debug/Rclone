from __future__ import annotations

import base64
import hashlib
import json
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace

import bcrypt
import pytest
import yaml
from fastapi import HTTPException, Request

from app import auth, cli, config_store, db, webauthn_service
from app.config_store import Config
from app.db import Database
from app.routes import api_config, api_webauthn


@pytest.fixture
def account(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.yaml"
    data = {
        "web": {
            "username": "admin",
            "password": "",
            "password_hash": bcrypt.hashpw(
                b"correct-password", bcrypt.gensalt(rounds=4)
            ).decode("ascii"),
            "secret_key": "auth-regression-secret-key-at-least-32-characters",
            "session_version": 1,
            "webauthn_rp_id": "backup.example",
            "webauthn_origin": "https://backup.example",
            "secure_cookie": True,
            "local_browse_roots": [str(tmp_path)],
        },
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "logs_dir": str(tmp_path / "logs"),
            "temp_dir": str(tmp_path / "temp"),
        },
        "backup": {"enabled": True, "pairs": [], "default_schedule": "manual"},
        "notifications": {"webhooks": []},
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    store = Config(path)
    database = Database(tmp_path / "app.db")
    monkeypatch.setattr(config_store, "_config", store)
    monkeypatch.setattr(db, "_db", database)
    monkeypatch.setenv("RCLONE_SYNC_CONFIG", str(path))
    return store, database


def _request() -> Request:
    token = auth.create_session("admin")
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/webauthn/native/registration/ticket",
            "headers": [(b"cookie", f"{auth.SESSION_COOKIE}={token}".encode("ascii"))],
            "client": ("192.0.2.17", 1234),
        }
    )


def _registration_ticket() -> tuple[str, str]:
    verifier = "R" * 43
    issued = api_webauthn.create_native_registration_ticket(
        api_webauthn.NativeRegistrationTicketRequest(
            method="passkey",
            label="Dummy key",
            current_password="correct-password",
            app_challenge=api_webauthn._pkce_challenge(verifier),
        ),
        _request(),
        "admin",
    )
    return issued["registration_token"], verifier


def _registration_challenge() -> str:
    token, verifier = _registration_ticket()
    options = api_webauthn.begin_native_registration(
        api_webauthn.NativeRegistrationOptionsRequest(token=token, verifier=verifier)
    )
    return options["challenge_id"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_version", 2),
        ("username", "new-admin"),
        ("secret_key", "rotated-auth-regression-secret-key-long-enough"),
        ("password_hash", "a-new-credential-generation"),
    ],
)
def test_pending_registration_ticket_is_rejected_after_account_rotation(
    account, field, value
):
    store, database = account
    token, verifier = _registration_ticket()
    store.update(lambda data: data["web"].update({field: value}))

    with pytest.raises(HTTPException) as denied:
        api_webauthn.begin_native_registration(
            api_webauthn.NativeRegistrationOptionsRequest(
                token=token, verifier=verifier
            )
        )
    assert denied.value.status_code == 401
    assert database.webauthn_credentials() == []


def test_successful_logout_rotation_revokes_already_redeemed_registration(account):
    _store, database = account
    challenge_id = _registration_challenge()
    auth.bump_session_version()

    with pytest.raises(ValueError, match="abgelaufen|benutzt"):
        webauthn_service.finish_registration(
            challenge_id, {}, expected_native=True, database=database
        )
    assert database.webauthn_credentials() == []


def test_revocation_during_authenticator_verification_prevents_credential_insert(
    account, monkeypatch
):
    _store, database = account
    challenge_id = _registration_challenge()

    def verified_after_revocation(**_kwargs):
        auth.bump_session_version()
        return SimpleNamespace(
            credential_id=b"dummy-credential",
            credential_public_key=b"dummy-public-key",
            sign_count=0,
            credential_device_type=SimpleNamespace(value="multi_device"),
            credential_backed_up=True,
        )

    monkeypatch.setattr(
        webauthn_service, "verify_registration_response", verified_after_revocation
    )
    with pytest.raises(ValueError, match="widerrufen"):
        webauthn_service.finish_registration(
            challenge_id, {}, expected_native=True, database=database
        )
    assert database.webauthn_credentials() == []


def test_browser_registration_challenge_also_obeys_account_rotation(account):
    _store, database = account
    options = webauthn_service.registration_options("passkey", "Browser key")
    auth.bump_session_version()
    with pytest.raises(ValueError, match="abgelaufen|benutzt"):
        webauthn_service.finish_registration(
            options["challenge_id"], {}, expected_native=False, database=database
        )
    assert database.webauthn_credentials() == []


def test_native_login_exchange_cannot_recreate_a_revoked_session(account):
    _store, database = account
    token = "dummy-exchange-token-long-enough-1234567890"
    verifier = "V" * 43
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    database.webauthn_credential_add(
        credential_id="exchange-fixture-key",
        method="passkey",
        public_key=b"fixture-public-key",
        sign_count=0,
        transports=[],
        device_type="multi_device",
        backed_up=True,
        label="Exchange fixture",
    )
    credential = database.webauthn_credential_get("exchange-fixture-key")
    database.native_auth_exchange_create(
        token_hash,
        api_webauthn._pkce_challenge(verifier),
        "admin",
        account_generation=auth.account_generation(),
        credential_id=credential["credential_id"],
        credential_generation=credential["generation"],
    )
    auth.bump_session_version()
    with pytest.raises(HTTPException) as denied:
        api_webauthn.exchange_native_session(
            api_webauthn.ExchangeRequest(token=token, verifier=verifier), _request()
        )
    assert denied.value.status_code == 401


def test_pending_authentication_challenge_is_rejected_before_crypto_after_revocation(
    account,
):
    _store, database = account
    credential_id = base64.urlsafe_b64encode(b"dummy-credential").rstrip(b"=").decode()
    database.webauthn_credential_add(
        credential_id=credential_id,
        method="passkey",
        public_key=b"dummy-public-key",
        sign_count=0,
        transports=[],
        device_type="multi_device",
        backed_up=True,
        label="Dummy",
    )
    options = webauthn_service.authentication_options("passkey")
    auth.bump_session_version()
    with pytest.raises(ValueError, match="abgelaufen|benutzt"):
        webauthn_service.finish_authentication(
            options["challenge_id"], {"id": credential_id}, database=database
        )


def test_old_unbound_pending_records_fail_closed(account):
    _store, database = account
    database.webauthn_challenge_create(
        challenge_id="old-challenge-12345678901234567890",
        challenge=b"old-challenge",
        purpose="register",
        method="passkey",
    )
    with pytest.raises(ValueError, match="abgelaufen|benutzt"):
        webauthn_service.finish_registration(
            "old-challenge-12345678901234567890", {}, database=database
        )
    database.native_auth_exchange_create("old-token", "old-verifier", "admin")
    assert (
        database.native_auth_exchange_consume(
            "old-token", "old-verifier", account_generation=auth.account_generation()
        )
        is None
    )


def test_legacy_migration_does_not_overwrite_a_concurrent_password_rotation(
    account, monkeypatch
):
    store, _database = account
    store.update(
        lambda data: data["web"].update(
            {"password_hash": "", "password": "legacy-password"}
        )
    )
    rotated_hash = bcrypt.hashpw(b"new-password", bcrypt.gensalt(rounds=4)).decode(
        "ascii"
    )
    hashpw = auth.bcrypt.hashpw

    def rotate_after_hash(password, salt):
        hashed = hashpw(password, salt)
        store.update(
            lambda data: data["web"].update(
                {"password_hash": rotated_hash, "password": "", "session_version": 2}
            )
        )
        return hashed

    monkeypatch.setattr(auth.bcrypt, "hashpw", rotate_after_hash)
    assert auth.verify_password("admin", "legacy-password") is False
    assert store.get("web", "password_hash") == rotated_hash
    assert store.get("web", "session_version") == 2
    assert auth.verify_password("admin", "new-password") is True


def test_config_backup_restore_does_not_resurrect_old_session(account):
    store, _database = account
    old_token = auth.create_session("admin")
    backup = store.path.with_suffix(".yaml.bak")
    backup.write_text(yaml.safe_dump(store.snapshot()), encoding="utf-8")
    auth.bump_session_version()
    assert auth.session_user(old_token) is None
    assert cli.cmd_restore_config_backup(SimpleNamespace(yes=True)) == 0
    assert auth.session_user(old_token) is None
    assert auth.session_user(auth.create_session("admin")) == "admin"


def test_bcrypt_verification_cannot_bind_old_password_to_a_new_generation(
    account, monkeypatch
):
    store, _database = account
    checkpw = auth.bcrypt.checkpw

    def verify_then_rotate(password, password_hash):
        matched = checkpw(password, password_hash)
        store.update(lambda data: data["web"].update({"session_version": 2}))
        return matched

    monkeypatch.setattr(auth.bcrypt, "checkpw", verify_then_rotate)
    assert auth.verify_password_with_generation("admin", "correct-password") is None


def _password_login_request(path: str) -> tuple[Request, str]:
    nonce = "dummy-login-csrf-12345678901234567890"
    request = Request(
        {
            "type": "http",
            "scheme": "https",
            "server": ("backup.example", 443),
            "method": "POST",
            "path": path,
            "headers": [
                (b"cookie", f"{auth.LOGIN_CSRF_COOKIE}={nonce}".encode("ascii"))
            ],
            "client": ("192.0.2.17", 1234),
        }
    )
    return request, nonce


@pytest.mark.parametrize("native", [False, True])
def test_rotation_after_password_verification_prevents_both_login_cookie_paths(
    account, monkeypatch, native
):
    from app import main
    from app.auth_contract import NATIVE_LOGIN_PATH, NativeLoginRequest

    store, _database = account
    authenticate = main._authenticate_login

    def authenticate_then_rotate(request, username, password):
        result = authenticate(request, username, password)
        assert result == ("success", 0)
        store.update(lambda data: data["web"].update({"session_version": 2}))
        return result

    monkeypatch.setattr(main, "_authenticate_login", authenticate_then_rotate)
    request, nonce = _password_login_request(NATIVE_LOGIN_PATH if native else "/login")
    if native:
        response = main.native_login_submit(
            request,
            NativeLoginRequest(
                username="admin", password="correct-password", login_csrf=nonce
            ),
        )
        assert response.status_code == 401
        assert json.loads(response.body) == {"status": "invalid_credentials"}
    else:
        response = main.login_submit(
            request,
            username="admin",
            password="correct-password",
            login_csrf=nonce,
            next="",
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login?error=1"
    assert response.headers.getlist("set-cookie") == []


@pytest.mark.parametrize("native", [False, True])
def test_legitimate_legacy_migration_still_issues_a_generation_bound_session(
    account, native
):
    from app import main
    from app.auth_contract import NATIVE_LOGIN_PATH, NativeLoginRequest

    store, _database = account
    store.update(
        lambda data: data["web"].update(
            {"password_hash": "", "password": "legacy-password"}
        )
    )
    request, nonce = _password_login_request(NATIVE_LOGIN_PATH if native else "/login")
    if native:
        response = main.native_login_submit(
            request,
            NativeLoginRequest(
                username="admin", password="legacy-password", login_csrf=nonce
            ),
        )
        assert response.status_code == 200
        assert json.loads(response.body) == {"status": "success"}
    else:
        response = main.login_submit(
            request,
            username="admin",
            password="legacy-password",
            login_csrf=nonce,
            next="",
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"
    cookies = SimpleCookie()
    for header in response.headers.getlist("set-cookie"):
        cookies.load(header)
    assert auth.session_user(cookies[auth.SESSION_COOKIE].value) == "admin"
    assert store.get("web", "password") == ""
    assert store.get("web", "password_hash").startswith("$2")


def test_rotation_immediately_after_legacy_migration_does_not_rebind_its_proof(
    account, monkeypatch
):
    store, _database = account
    store.update(
        lambda data: data["web"].update(
            {"password_hash": "", "password": "legacy-password"}
        )
    )
    update = store.update

    def migrate_then_rotate(updater, **kwargs):
        result = update(updater, **kwargs)
        update(lambda data: data["web"].update({"session_version": 2}))
        return result

    monkeypatch.setattr(store, "update", migrate_then_rotate)
    assert auth.verify_password_with_generation("admin", "legacy-password") is None


def _authentication_key(database, *, raw_id=b"revocation-fixture-key", sign_count=0):
    credential_id = base64.urlsafe_b64encode(raw_id).rstrip(b"=").decode("ascii")
    database.webauthn_credential_add(
        credential_id=credential_id,
        method="passkey",
        public_key=b"dummy-cose-public-key",
        sign_count=sign_count,
        transports=["internal"],
        device_type="multi_device",
        backed_up=True,
        label="Dummy authentication key",
    )
    return database.webauthn_credential_get(credential_id)


def _verified_assertion(*, count=1):
    return SimpleNamespace(
        new_sign_count=count,
        credential_device_type=SimpleNamespace(value="multi_device"),
        credential_backed_up=True,
    )


def _delete_authentication_key(credential_id):
    return api_webauthn.delete_credential(
        credential_id,
        api_webauthn.DeleteCredentialRequest(current_password="correct-password"),
        _request(),
        "admin",
    )


def test_deleted_key_cannot_finish_an_assertion_and_other_sessions_remain_valid(
    account, monkeypatch
):
    _store, database = account
    credential = _authentication_key(database)
    other = _authentication_key(database, raw_id=b"other-valid-key")
    established_session = auth.create_session("admin")
    generation = auth.account_generation()
    options = webauthn_service.authentication_options("passkey")

    def delete_during_verification(**_kwargs):
        _delete_authentication_key(credential["credential_id"])
        return _verified_assertion()

    monkeypatch.setattr(
        webauthn_service, "verify_authentication_response", delete_during_verification
    )
    with pytest.raises(ValueError, match="widerrufen"):
        webauthn_service.finish_authentication(
            options["challenge_id"], {"id": credential["credential_id"]}
        )
    assert auth.account_generation() == generation
    assert auth.session_user(established_session) == "admin"
    assert database.webauthn_credential_get(other["credential_id"]) is not None
    monkeypatch.setattr(
        webauthn_service,
        "verify_authentication_response",
        lambda **_kwargs: _verified_assertion(),
    )
    other_options = webauthn_service.authentication_options("passkey")
    authenticated = webauthn_service.finish_authentication(
        other_options["challenge_id"], {"id": other["credential_id"]}
    )
    assert authenticated["credential_id"] == other["credential_id"]


@pytest.mark.parametrize("native", [False, True])
def test_key_deletion_after_verified_assertion_prevents_actual_login_issuance(
    account, monkeypatch, native
):
    _store, database = account
    credential = _authentication_key(database)
    options = webauthn_service.authentication_options(
        "passkey", native=native, app_binding=api_webauthn._pkce_challenge("R" * 43)
    )
    monkeypatch.setattr(
        webauthn_service,
        "verify_authentication_response",
        lambda **_kwargs: _verified_assertion(),
    )
    finish = api_webauthn.finish_authentication

    def verify_then_delete(*args, **kwargs):
        result = finish(*args, **kwargs)
        _delete_authentication_key(credential["credential_id"])
        return result

    monkeypatch.setattr(api_webauthn, "finish_authentication", verify_then_delete)
    with pytest.raises(HTTPException) as denied:
        api_webauthn.complete_authentication(
            api_webauthn.CeremonyResponse(
                challenge_id=options["challenge_id"],
                credential={"id": credential["credential_id"]},
            ),
            _request(),
        )
    assert denied.value.status_code == 400
    with database.conn() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM native_auth_exchanges").fetchone()[
                0
            ]
            == 0
        )


@pytest.mark.parametrize("readd_same_id", [False, True])
def test_native_exchange_cannot_use_deleted_or_recreated_credential(
    account, monkeypatch, readd_same_id
):
    _store, database = account
    credential = _authentication_key(database)
    verifier = "R" * 43
    options = webauthn_service.authentication_options(
        "passkey", native=True, app_binding=api_webauthn._pkce_challenge(verifier)
    )
    monkeypatch.setattr(
        webauthn_service,
        "verify_authentication_response",
        lambda **_kwargs: _verified_assertion(),
    )
    result = api_webauthn.complete_authentication(
        api_webauthn.CeremonyResponse(
            challenge_id=options["challenge_id"],
            credential={"id": credential["credential_id"]},
        ),
        _request(),
    )
    token = result["native_exchange_token"]
    _delete_authentication_key(credential["credential_id"])
    if readd_same_id:
        new = _authentication_key(database)
        assert new["credential_id"] == credential["credential_id"]
        assert new["generation"] != credential["generation"]
    with pytest.raises(HTTPException) as denied:
        api_webauthn.exchange_native_session(
            api_webauthn.ExchangeRequest(token=token, verifier=verifier), _request()
        )
    assert denied.value.status_code == 401


def test_recreated_credential_during_verification_does_not_inherit_old_assertion(
    account, monkeypatch
):
    _store, database = account
    credential = _authentication_key(database)
    options = webauthn_service.authentication_options("passkey")

    def recreate_during_verification(**_kwargs):
        _delete_authentication_key(credential["credential_id"])
        _authentication_key(database)
        return _verified_assertion()

    monkeypatch.setattr(
        webauthn_service, "verify_authentication_response", recreate_during_verification
    )
    with pytest.raises(ValueError, match="widerrufen"):
        webauthn_service.finish_authentication(
            options["challenge_id"], {"id": credential["credential_id"]}
        )
    assert (
        database.webauthn_credential_get(credential["credential_id"])["sign_count"] == 0
    )


def test_stale_parallel_assertion_cannot_overwrite_a_newer_counter(
    account, monkeypatch
):
    _store, database = account
    credential = _authentication_key(database, sign_count=5)
    options = webauthn_service.authentication_options("passkey")

    def another_assertion_finishes_first(**_kwargs):
        assert database.webauthn_credential_used(
            credential["credential_id"],
            expected_generation=credential["generation"],
            expected_sign_count=5,
            sign_count=6,
            device_type="multi_device",
            backed_up=True,
        )
        return _verified_assertion(count=7)

    monkeypatch.setattr(
        webauthn_service,
        "verify_authentication_response",
        another_assertion_finishes_first,
    )
    with pytest.raises(ValueError, match="geändert|widerrufen"):
        webauthn_service.finish_authentication(
            options["challenge_id"], {"id": credential["credential_id"]}
        )
    assert (
        database.webauthn_credential_get(credential["credential_id"])["sign_count"] == 6
    )


def test_key_deletion_requires_the_same_password_generation_at_its_effect(
    account, monkeypatch
):
    store, database = account
    credential = _authentication_key(database)
    reauthenticate = api_webauthn.require_reauthentication

    def verify_then_rotate(*args, **kwargs):
        proof = reauthenticate(*args, **kwargs)
        store.update(lambda data: data["web"].update({"session_version": 2}))
        return proof

    monkeypatch.setattr(api_webauthn, "require_reauthentication", verify_then_rotate)
    with pytest.raises(HTTPException) as denied:
        _delete_authentication_key(credential["credential_id"])
    assert denied.value.status_code == 401
    assert database.webauthn_credential_get(credential["credential_id"]) is not None


def test_key_deletion_waits_for_final_cookie_issuance_across_config_instances(
    account, monkeypatch
):
    import threading

    store, database = account
    writer = Config(store.path)
    credential = _authentication_key(database)
    login_request = _request()
    delete_request = _request()
    generation = auth.account_generation()
    options = webauthn_service.authentication_options("passkey")
    blocked = threading.Event()
    deleted = threading.Event()
    errors = []

    def delete_key():
        try:
            api_webauthn.delete_credential(
                credential["credential_id"],
                api_webauthn.DeleteCredentialRequest(
                    current_password="correct-password"
                ),
                delete_request,
                "admin",
            )
            deleted.set()
        except BaseException as exc:
            errors.append(exc)

    deletion = threading.Thread(target=delete_key, daemon=True)
    monkeypatch.setattr(
        auth,
        "get_config",
        lambda: writer if threading.current_thread() is deletion else store,
    )
    monkeypatch.setattr(
        api_webauthn, "require_reauthentication", lambda *_args: generation
    )
    acquire = config_store.acquire_file_lock

    def observe_lock(descriptor, *, exclusive, blocking):
        try:
            return acquire(descriptor, exclusive=exclusive, blocking=blocking)
        except BlockingIOError:
            if threading.current_thread() is deletion and exclusive:
                blocked.set()
            raise

    monkeypatch.setattr(config_store, "acquire_file_lock", observe_lock)
    monkeypatch.setattr(
        webauthn_service,
        "verify_authentication_response",
        lambda **_kwargs: _verified_assertion(),
    )
    issue_session = api_webauthn._set_authenticated_session

    def issue_while_deletion_waits(response, request, username):
        deletion.start()
        assert blocked.wait(timeout=3), "Credential deletion did not wait for issuance"
        assert not deleted.is_set()
        assert database.webauthn_credential_get(credential["credential_id"]) is not None
        issue_session(response, request, username)

    monkeypatch.setattr(
        api_webauthn, "_set_authenticated_session", issue_while_deletion_waits
    )
    try:
        response = api_webauthn.complete_authentication(
            api_webauthn.CeremonyResponse(
                challenge_id=options["challenge_id"],
                credential={"id": credential["credential_id"]},
            ),
            login_request,
        )
    finally:
        if deletion.ident is not None:
            deletion.join(timeout=5)
    assert not deletion.is_alive()
    assert errors == []
    assert deleted.is_set()
    assert database.webauthn_credential_get(credential["credential_id"]) is None
    cookies = SimpleCookie()
    for header in response.headers.getlist("set-cookie"):
        cookies.load(header)
    assert auth.session_user(cookies[auth.SESSION_COOKIE].value) == "admin"


def test_credential_generation_migration_preserves_keys_and_invalidates_old_exchanges(
    account,
):
    _store, database = account
    credential = _authentication_key(database, sign_count=3)
    with database.conn() as connection:
        connection.execute("UPDATE webauthn_credentials SET generation=''")
        connection.execute("PRAGMA user_version=15")
        connection.execute(
            "INSERT INTO native_auth_exchanges("
            "token_hash, verifier_hash, username, account_generation, expires_at, created_at"
            ") VALUES ('old-pending-token', 'verifier', 'admin', ?, 9999999999, 1)",
            (auth.account_generation(),),
        )
    upgraded = Database(database.path)
    migrated = upgraded.webauthn_credential_get(credential["credential_id"])
    assert len(migrated["generation"]) == 64
    assert migrated["public_key"] == credential["public_key"]
    assert migrated["sign_count"] == 3
    assert (
        Database(database.path).webauthn_credential_get(credential["credential_id"])[
            "generation"
        ]
        == migrated["generation"]
    )
    assert (
        upgraded.native_auth_exchange_consume(
            "old-pending-token",
            "verifier",
            account_generation=auth.account_generation(),
        )
        is None
    )


@pytest.mark.parametrize("rotation_point", ["after_password_proof", "during_hashing"])
def test_password_change_cannot_overwrite_a_concurrent_password_rotation(
    account, monkeypatch, rotation_point
):
    store, database = account
    writer = Config(store.path)
    request = _request()
    database.push_device_upsert("ab" * 32, "production")
    newer_hash = bcrypt.hashpw(
        b"independently-rotated-password", bcrypt.gensalt(rounds=4)
    ).decode("ascii")

    def rotate():
        writer.update(
            lambda data: data["web"].update(password_hash=newer_hash, session_version=2)
        )

    if rotation_point == "after_password_proof":
        reauthenticate = api_config.require_reauthentication

        def verify_then_rotate(*args):
            proof = reauthenticate(*args)
            rotate()
            return proof

        monkeypatch.setattr(api_config, "require_reauthentication", verify_then_rotate)
    else:
        hashpw = bcrypt.hashpw

        def hash_then_rotate(password, salt):
            result = hashpw(password, salt)
            rotate()
            return result

        monkeypatch.setattr(api_config.bcrypt, "hashpw", hash_then_rotate)

    with pytest.raises(HTTPException) as denied:
        api_config.change_password(
            request,
            api_config.PasswordChange(
                current_password="correct-password",
                new_password="must-not-replace-new-password",
            ),
            user="admin",
        )
    assert denied.value.status_code == 401
    assert store.get("web", "password_hash") == newer_hash
    assert store.get("web", "session_version") == 2
    assert len(database.push_devices()) == 1


def test_password_change_requires_a_current_session_even_after_valid_password_proof(
    account,
):
    store, _database = account
    revoked_request = _request()
    store.update(lambda data: data["web"].update(session_version=2))
    current_hash = store.get("web", "password_hash")

    with pytest.raises(HTTPException) as denied:
        api_config.change_password(
            revoked_request,
            api_config.PasswordChange(
                current_password="correct-password",
                new_password="must-not-replace-new-password",
            ),
            user="admin",
        )
    assert denied.value.status_code == 401
    assert store.get("web", "password_hash") == current_hash
    assert store.get("web", "session_version") == 2
