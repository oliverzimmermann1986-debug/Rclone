from __future__ import annotations

import hashlib
import base64
from pathlib import Path
from types import SimpleNamespace

import bcrypt
import pytest
import yaml
from fastapi.testclient import TestClient

from app import config_store, db, webauthn_service
from app.config_store import Config
from app.db import Database


def test_webauthn_schema_and_one_time_challenges(tmp_path: Path):
    database = Database(tmp_path / "webauthn.db")
    database.webauthn_challenge_create(
        challenge_id="challenge-12345678901234567890",
        challenge=b"random-challenge",
        purpose="authenticate",
        method="passkey",
        native=True,
    )

    consumed = database.webauthn_challenge_consume(
        "challenge-12345678901234567890", purpose="authenticate"
    )
    assert consumed is not None
    assert bytes(consumed["challenge"]) == b"random-challenge"
    assert consumed["native"] is True
    assert (
        database.webauthn_challenge_consume(
            "challenge-12345678901234567890", purpose="authenticate"
        )
        is None
    )


def test_native_exchange_is_hashed_and_single_use(tmp_path: Path):
    database = Database(tmp_path / "exchange.db")
    token = "one-time-native-token"
    verifier = "A" * 43
    verifier_hash = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    database.native_auth_exchange_create(token_hash, verifier_hash, "admin")

    assert database.native_auth_exchange_consume(token_hash, "wrong") is None
    assert database.native_auth_exchange_consume(token_hash, verifier_hash) == "admin"
    assert database.native_auth_exchange_consume(token_hash, verifier_hash) is None


def test_option_generation_uses_rp_and_separates_authenticator_types(
    tmp_path: Path, monkeypatch
):
    database = Database(tmp_path / "options.db")
    monkeypatch.setattr(
        webauthn_service,
        "settings",
        lambda: ("backup.example.de", "https://backup.example.de", "admin"),
    )
    monkeypatch.setattr(webauthn_service, "_user_handle", lambda: b"stable-user-handle")

    passkey = webauthn_service.registration_options(
        "passkey", "iPhone", database=database
    )
    security_key = webauthn_service.registration_options(
        "security_key", "YubiKey", database=database
    )

    assert passkey["publicKey"]["rp"]["id"] == "backup.example.de"
    assert (
        passkey["publicKey"]["authenticatorSelection"]["authenticatorAttachment"]
        == "platform"
    )
    assert (
        security_key["publicKey"]["authenticatorSelection"]["authenticatorAttachment"]
        == "cross-platform"
    )
    assert passkey["challenge_id"] != security_key["challenge_id"]


def test_authentication_is_bound_to_selected_method(tmp_path: Path, monkeypatch):
    database = Database(tmp_path / "method.db")
    database.webauthn_credential_add(
        credential_id="credential-id",
        method="security_key",
        public_key=b"public-key",
        sign_count=0,
        transports=["usb"],
        device_type="single_device",
        backed_up=False,
        label="USB key",
    )
    database.webauthn_challenge_create(
        challenge_id="challenge-12345678901234567890",
        challenge=b"challenge",
        purpose="authenticate",
        method="passkey",
    )
    monkeypatch.setattr(
        webauthn_service,
        "settings",
        lambda: ("backup.example.de", "https://backup.example.de", "admin"),
    )

    with pytest.raises(ValueError, match="gewählte Anmeldeart"):
        webauthn_service.finish_authentication(
            "challenge-12345678901234567890",
            {"id": "credential-id"},
            database=database,
        )


def test_verified_registration_persists_only_public_credential_data(
    tmp_path: Path, monkeypatch
):
    database = Database(tmp_path / "registration.db")
    database.webauthn_challenge_create(
        challenge_id="challenge-12345678901234567890",
        challenge=b"challenge",
        purpose="register",
        method="passkey",
        label="Mein iPhone",
    )
    monkeypatch.setattr(
        webauthn_service,
        "settings",
        lambda: ("backup.example.de", "https://backup.example.de", "admin"),
    )
    monkeypatch.setattr(
        webauthn_service,
        "verify_registration_response",
        lambda **_kwargs: SimpleNamespace(
            credential_id=b"credential-id",
            credential_public_key=b"cose-public-key",
            sign_count=0,
            credential_device_type=SimpleNamespace(value="multi_device"),
            credential_backed_up=True,
        ),
    )

    result = webauthn_service.finish_registration(
        "challenge-12345678901234567890",
        {"id": "ignored", "response": {"transports": ["internal"]}},
        database=database,
    )

    stored = database.webauthn_credential_get(result["credential_id"])
    assert stored is not None
    assert stored["label"] == "Mein iPhone"
    assert stored["method"] == "passkey"
    assert bytes(stored["public_key"]) == b"cose-public-key"
    assert stored["transports"] == ["internal"]
    assert stored["backed_up"] is True


def test_native_exchange_endpoint_sets_session_once_and_security_page_redirects(
    tmp_path: Path, monkeypatch
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "web": {
                    "username": "admin",
                    "password_hash": bcrypt.hashpw(
                        b"very-secure-password", bcrypt.gensalt(rounds=4)
                    ).decode(),
                    "secret_key": "test-secret-which-is-long-enough-123456789",
                    "session_version": 1,
                    "secure_cookie": True,
                    "allowed_hosts": ["backup.example.de"],
                    "webauthn_rp_id": "backup.example.de",
                    "webauthn_origin": "https://backup.example.de",
                },
                "paths": {
                    "data_dir": str(tmp_path),
                    "logs_dir": str(tmp_path),
                    "temp_dir": str(tmp_path),
                },
                "backup": {"enabled": True, "pairs": []},
            }
        ),
        encoding="utf-8",
    )
    database = Database(tmp_path / "api.db")
    monkeypatch.setattr(config_store, "_config", Config(config_path))
    monkeypatch.setattr(db, "_db", database)
    from app import main

    token = "native-exchange-token-with-enough-random-looking-characters"
    verifier = "A" * 43
    verifier_hash = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    database.native_auth_exchange_create(
        hashlib.sha256(token.encode()).hexdigest(), verifier_hash, "admin"
    )
    with TestClient(main.app, base_url="https://backup.example.de") as client:
        redirect = client.get("/security", follow_redirects=False)
        assert redirect.status_code == 303
        assert redirect.headers["location"] == "/login?next=/security"

        exchanged = client.post(
            "/api/webauthn/native/exchange",
            json={"token": token, "verifier": verifier},
            headers={"Origin": "https://backup.example.de"},
        )
        assert exchanged.status_code == 200
        assert exchanged.json() == {"status": "success"}
        assert client.cookies.get("rclone_sync_session")
        assert client.cookies.get("rclone_sync_csrf")

        replay = client.post(
            "/api/webauthn/native/exchange",
            json={"token": token, "verifier": verifier},
            headers={"Origin": "https://backup.example.de"},
        )
        assert replay.status_code == 401
