"""WebAuthn ceremonies for the single-admin Sicherpfad installation."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any, Literal

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialHint,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .config_store import get_config
from .db import Database, get_db

WebAuthnMethod = Literal["passkey", "security_key"]


class WebAuthnUnavailable(RuntimeError):
    pass


def settings() -> tuple[str, str, str]:
    web = get_config().get("web", default={}) or {}
    rp_id = str(web.get("webauthn_rp_id") or "").strip().casefold()
    origin = str(web.get("webauthn_origin") or "").strip().rstrip("/")
    username = str(web.get("username") or "admin").strip() or "admin"
    if not rp_id or not origin:
        raise WebAuthnUnavailable(
            "Passkeys sind auf diesem Server noch nicht konfiguriert."
        )
    return rp_id, origin, username


def status(database: Database | None = None) -> dict[str, Any]:
    try:
        settings()
        enabled = True
    except WebAuthnUnavailable:
        enabled = False
    credentials = (database or get_db()).webauthn_credentials() if enabled else []
    return {
        "enabled": enabled,
        "passkey": any(item["method"] == "passkey" for item in credentials),
        "security_key": any(item["method"] == "security_key" for item in credentials),
    }


def _user_handle() -> bytes:
    secret = str(get_config().get("web", "secret_key", default="") or "")
    return hmac.new(
        secret.encode("utf-8"), b"rclone-sync-webauthn-user-v1", hashlib.sha256
    ).digest()


def _descriptors(rows: list[dict[str, Any]]) -> list[PublicKeyCredentialDescriptor]:
    descriptors: list[PublicKeyCredentialDescriptor] = []
    for row in rows:
        transports: list[AuthenticatorTransport] = []
        for raw in row.get("transports") or []:
            try:
                transports.append(AuthenticatorTransport(str(raw)))
            except ValueError:
                continue
        from webauthn.helpers import base64url_to_bytes

        descriptors.append(
            PublicKeyCredentialDescriptor(
                id=base64url_to_bytes(str(row["credential_id"])),
                transports=transports or None,
            )
        )
    return descriptors


def registration_options(
    method: WebAuthnMethod, label: str, *, database: Database | None = None
) -> dict[str, Any]:
    db = database or get_db()
    rp_id, _origin, username = settings()
    is_passkey = method == "passkey"
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name="Sicherpfad",
        user_id=_user_handle(),
        user_name=username,
        user_display_name=username,
        timeout=120_000,
        exclude_credentials=_descriptors(db.webauthn_credentials()),
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=(
                AuthenticatorAttachment.PLATFORM
                if is_passkey
                else AuthenticatorAttachment.CROSS_PLATFORM
            ),
            resident_key=(
                ResidentKeyRequirement.REQUIRED
                if is_passkey
                else ResidentKeyRequirement.PREFERRED
            ),
            require_resident_key=is_passkey,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        hints=[
            PublicKeyCredentialHint.CLIENT_DEVICE
            if is_passkey
            else PublicKeyCredentialHint.SECURITY_KEY
        ],
    )
    challenge_id = secrets.token_urlsafe(24)
    db.webauthn_challenge_create(
        challenge_id=challenge_id,
        challenge=options.challenge,
        purpose="register",
        method=method,
        label=label,
    )
    return {
        "challenge_id": challenge_id,
        "publicKey": json.loads(options_to_json(options)),
    }


def finish_registration(
    challenge_id: str,
    credential: dict[str, Any],
    *,
    database: Database | None = None,
) -> dict[str, Any]:
    db = database or get_db()
    challenge = db.webauthn_challenge_consume(challenge_id, purpose="register")
    if not challenge:
        raise ValueError(
            "Die Registrierungsanfrage ist abgelaufen oder wurde bereits benutzt."
        )
    rp_id, origin, _username = settings()
    verified = verify_registration_response(
        credential=credential,
        expected_challenge=bytes(challenge["challenge"]),
        expected_rp_id=rp_id,
        expected_origin=origin,
        require_user_verification=True,
    )
    credential_id = bytes_to_base64url(verified.credential_id)
    if db.webauthn_credential_get(credential_id):
        raise ValueError("Dieser Schlüssel ist bereits registriert.")
    raw_transports = (
        credential.get("response", {}).get("transports", [])
        if isinstance(credential.get("response"), dict)
        else []
    )
    db.webauthn_credential_add(
        credential_id=credential_id,
        method=str(challenge["method"]),
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=[str(value) for value in raw_transports if isinstance(value, str)],
        device_type=verified.credential_device_type.value,
        backed_up=verified.credential_backed_up,
        label=str(challenge["label"] or ""),
    )
    return {"credential_id": credential_id, "method": str(challenge["method"])}


def authentication_options(
    method: WebAuthnMethod,
    *,
    native: bool = False,
    app_binding: str = "",
    database: Database | None = None,
) -> dict[str, Any]:
    db = database or get_db()
    rp_id, _origin, _username = settings()
    credentials = db.webauthn_credentials(method)
    if not credentials:
        raise LookupError("Für diese Anmeldeart ist noch kein Schlüssel registriert.")
    options = generate_authentication_options(
        rp_id=rp_id,
        timeout=120_000,
        allow_credentials=_descriptors(credentials),
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    challenge_id = secrets.token_urlsafe(24)
    db.webauthn_challenge_create(
        challenge_id=challenge_id,
        challenge=options.challenge,
        purpose="authenticate",
        method=method,
        native=native,
        app_binding=app_binding,
    )
    return {
        "challenge_id": challenge_id,
        "publicKey": json.loads(options_to_json(options)),
    }


def finish_authentication(
    challenge_id: str,
    credential: dict[str, Any],
    *,
    database: Database | None = None,
) -> dict[str, Any]:
    db = database or get_db()
    challenge = db.webauthn_challenge_consume(challenge_id, purpose="authenticate")
    if not challenge:
        raise ValueError(
            "Die Anmeldeanfrage ist abgelaufen oder wurde bereits benutzt."
        )
    credential_id = str(credential.get("id") or "")
    stored = db.webauthn_credential_get(credential_id)
    if not stored or stored["method"] != challenge["method"]:
        raise ValueError(
            "Dieser Schlüssel ist für die gewählte Anmeldeart nicht registriert."
        )
    rp_id, origin, username = settings()
    verified = verify_authentication_response(
        credential=credential,
        expected_challenge=bytes(challenge["challenge"]),
        expected_rp_id=rp_id,
        expected_origin=origin,
        credential_public_key=bytes(stored["public_key"]),
        credential_current_sign_count=int(stored["sign_count"]),
        require_user_verification=True,
    )
    old_count = int(stored["sign_count"])
    if old_count > 0 and verified.new_sign_count <= old_count:
        raise ValueError("Der Signaturzähler des Schlüssels ist ungültig.")
    db.webauthn_credential_used(
        credential_id,
        sign_count=verified.new_sign_count,
        device_type=verified.credential_device_type.value,
        backed_up=verified.credential_backed_up,
    )
    return {
        "username": username,
        "native": bool(challenge["native"]),
        "method": str(challenge["method"]),
        "app_binding": str(challenge["app_binding"] or ""),
    }
