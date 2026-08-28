"""Passkey and physical security-key registration and authentication."""

from __future__ import annotations

import hashlib
import base64
import re
import secrets
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from webauthn.helpers.exceptions import WebAuthnException

from ..auth import (
    SESSION_COOKIE,
    clear_login_failures,
    create_session,
    login_key,
    login_retry_after,
    record_login_failure,
    require_auth,
    require_reauthentication,
    session_user,
    session_max_age,
)
from ..config_store import get_config
from ..db import get_db
from ..security import CSRF_COOKIE, new_csrf_token, require_csrf
from ..webauthn_service import (
    WebAuthnUnavailable,
    authentication_options,
    finish_authentication,
    finish_registration,
    registration_options,
    status,
)

router = APIRouter(tags=["webauthn"])
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
Method = Literal["passkey", "security_key"]


class AuthenticationOptionsRequest(BaseModel):
    method: Method
    native: bool = False
    native_challenge: str = Field(default="", max_length=128)


class CeremonyResponse(BaseModel):
    challenge_id: str = Field(min_length=20, max_length=128)
    credential: dict[str, Any]


class RegistrationOptionsRequest(BaseModel):
    method: Method
    label: str = Field(default="", max_length=80)
    current_password: str = Field(min_length=1, max_length=1024)


class DeleteCredentialRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)


class ExchangeRequest(BaseModel):
    token: str = Field(min_length=32, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")
    verifier: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")


def _cookie_secure(request: Request) -> bool:
    configured = get_config().get("web", "secure_cookie", default=False)
    if configured is True:
        return True
    return str(configured).lower() == "auto" and request.url.scheme == "https"


def _set_authenticated_session(
    response: JSONResponse, request: Request, username: str
) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        create_session(username),
        httponly=True,
        samesite="lax",
        max_age=session_max_age(),
        secure=_cookie_secure(request),
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        new_csrf_token(),
        httponly=False,
        samesite="strict",
        max_age=session_max_age(),
        secure=_cookie_secure(request),
        path="/",
    )


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii", errors="ignore")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _ceremony_error(exc: Exception) -> HTTPException:
    if isinstance(exc, WebAuthnUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (ValueError, WebAuthnException)):
        return HTTPException(
            status_code=400,
            detail="Der Passkey oder Sicherheitsschlüssel konnte nicht geprüft werden.",
        )
    return HTTPException(
        status_code=500, detail="WebAuthn konnte nicht verarbeitet werden."
    )


@router.get("/api/webauthn/status")
def webauthn_status() -> dict[str, Any]:
    return status()


@router.post("/api/webauthn/authentication/options")
def begin_authentication(body: AuthenticationOptionsRequest, request: Request):
    configured_username = str(
        get_config().get("web", "username", default="admin") or "admin"
    )
    retry_after = login_retry_after(login_key(request, configured_username))
    if retry_after:
        response = JSONResponse(
            status_code=429,
            content={
                "detail": "Zu viele Anmeldeversuche.",
                "retry_after_seconds": retry_after,
            },
        )
        response.headers["Retry-After"] = str(retry_after)
        return response
    if body.native and not re.fullmatch(r"[A-Za-z0-9_-]{43}", body.native_challenge):
        raise HTTPException(status_code=422, detail="Ungültige App-Bindung")
    if not body.native and body.native_challenge:
        raise HTTPException(status_code=422, detail="App-Bindung ohne native Anmeldung")
    try:
        return authentication_options(
            body.method,
            native=body.native,
            app_binding=body.native_challenge,
        )
    except Exception as exc:
        raise _ceremony_error(exc) from exc


@router.post("/api/webauthn/authentication/verify")
def complete_authentication(body: CeremonyResponse, request: Request):
    configured_username = str(
        get_config().get("web", "username", default="admin") or "admin"
    )
    key = login_key(request, configured_username)
    retry_after = login_retry_after(key)
    if retry_after:
        response = JSONResponse(
            status_code=429,
            content={
                "detail": "Zu viele Anmeldeversuche.",
                "retry_after_seconds": retry_after,
            },
        )
        response.headers["Retry-After"] = str(retry_after)
        return response
    try:
        result = finish_authentication(body.challenge_id, body.credential)
    except Exception as exc:
        retry_after = record_login_failure(key)
        error = _ceremony_error(exc)
        if retry_after:
            error.headers = {"Retry-After": str(retry_after)}
        raise error from exc
    clear_login_failures(key)
    database = get_db()
    database.audit_add(
        "webauthn_login",
        actor=result["username"],
        details={"method": result["method"], "native": result["native"]},
    )
    if result["native"]:
        token = secrets.token_urlsafe(48)
        database.native_auth_exchange_create(
            hashlib.sha256(token.encode("ascii")).hexdigest(),
            result["app_binding"],
            result["username"],
        )
        return {"ok": True, "native_exchange_token": token}
    response = JSONResponse(content={"ok": True})
    _set_authenticated_session(response, request, result["username"])
    return response


@router.post("/api/webauthn/native/exchange")
def exchange_native_session(body: ExchangeRequest, request: Request):
    token_hash = hashlib.sha256(body.token.encode("ascii", errors="ignore")).hexdigest()
    username = get_db().native_auth_exchange_consume(
        token_hash, _pkce_challenge(body.verifier)
    )
    if not username:
        raise HTTPException(
            status_code=401,
            detail="Der einmalige Anmeldecode ist abgelaufen oder ungültig.",
        )
    response = JSONResponse(content={"status": "success"})
    _set_authenticated_session(response, request, username)
    return response


@router.get("/webauthn/native", response_class=HTMLResponse)
def native_authentication_page(method: Method, app_challenge: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", app_challenge):
        raise HTTPException(status_code=422, detail="Ungültige App-Bindung")
    html = (STATIC_DIR / "webauthn-native.html").read_text(encoding="utf-8")
    return HTMLResponse(
        html.replace("<!--WEBAUTHN_METHOD-->", method).replace(
            "<!--WEBAUTHN_APP_CHALLENGE-->", app_challenge
        )
    )


@router.get("/security", response_class=HTMLResponse)
def security_page(request: Request):
    if not session_user(request.cookies.get(SESSION_COOKIE, "")):
        return RedirectResponse("/login?next=/security", status_code=303)
    return HTMLResponse((STATIC_DIR / "security.html").read_text(encoding="utf-8"))


@router.get(
    "/api/webauthn/credentials",
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)
def list_credentials() -> dict[str, Any]:
    credentials = get_db().webauthn_credentials()
    return {
        "credentials": [
            {
                "id": item["credential_id"],
                "method": item["method"],
                "label": item["label"],
                "created_at": item["created_at"],
                "last_used_at": item["last_used_at"],
                "backed_up": item["backed_up"],
            }
            for item in credentials
        ]
    }


@router.post(
    "/api/webauthn/registration/options",
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)
def begin_registration(
    body: RegistrationOptionsRequest,
    request: Request,
    user: str = Depends(require_auth),
):
    require_reauthentication(request, user, body.current_password)
    try:
        return registration_options(body.method, body.label.strip())
    except Exception as exc:
        raise _ceremony_error(exc) from exc


@router.post(
    "/api/webauthn/registration/verify",
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)
def complete_registration(
    body: CeremonyResponse, user: str = Depends(require_auth)
) -> dict[str, Any]:
    try:
        result = finish_registration(body.challenge_id, body.credential)
    except Exception as exc:
        raise _ceremony_error(exc) from exc
    get_db().audit_add(
        "webauthn_credential_registered",
        actor=user,
        details={"method": result["method"], "credential_id": result["credential_id"]},
    )
    return {"ok": True, **result}


@router.delete(
    "/api/webauthn/credentials/{credential_id}",
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)
def delete_credential(
    credential_id: str,
    body: DeleteCredentialRequest,
    request: Request,
    user: str = Depends(require_auth),
) -> dict[str, bool]:
    require_reauthentication(request, user, body.current_password)
    existing = get_db().webauthn_credential_get(credential_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Schlüssel nicht gefunden")
    get_db().webauthn_credential_delete(credential_id)
    get_db().audit_add(
        "webauthn_credential_deleted",
        actor=user,
        details={"method": existing["method"], "credential_id": credential_id},
    )
    return {"ok": True}
