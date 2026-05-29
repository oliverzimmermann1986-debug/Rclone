"""Minimaler Auth-Layer: Username + bcrypt-hashed Passwort + Session-Cookie.
URLSafeTimedSerializer für signed Session-Tokens."""
from __future__ import annotations

import logging
import secrets
from typing import Optional

import bcrypt
from fastapi import Cookie, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .config_store import get_config

logger = logging.getLogger(__name__)

SESSION_COOKIE = "rclone_sync_session"
SESSION_MAX_AGE = 7 * 24 * 3600  # 7 Tage


def _serializer() -> URLSafeTimedSerializer:
    secret = get_config().get("web", "secret_key", default="") or ""
    if not secret or secret == "change-this-to-random-string-32chars-min":
        secret = secrets.token_urlsafe(48)
        get_config().set("web", "secret_key", secret)
        get_config().save()
        logger.warning("web.secret_key war leer/default — neuer wurde generiert.")
    return URLSafeTimedSerializer(secret, salt="rclone-sync-session-v1")


def verify_password(username: str, password: str) -> bool:
    cfg = get_config()
    cfg_user = cfg.get("web", "username", default="admin") or "admin"
    if username.lower() != cfg_user.lower():
        return False
    hashed = cfg.get("web", "password_hash", default="") or ""
    if not hashed:
        # Plaintext-Fallback + Hashen
        plain = cfg.get("web", "password", default="") or ""
        if not plain or plain == "changeme":
            return False
        if password != plain:
            return False
        # Migrate
        new_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")
        cfg.set("web", "password_hash", new_hash)
        cfg.set("web", "password", "")
        cfg.save()
        logger.info("Klartext-Passwort migriert zu bcrypt-Hash")
        return True
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))
    except Exception:
        return False


def create_session(username: str) -> str:
    return _serializer().dumps({"u": username})


def session_user(token: str) -> Optional[str]:
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE)
        return data.get("u")
    except BadSignature:
        return None
    except Exception:
        return None


def require_auth(
    request: Request,
    session_cookie: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
):
    user = session_user(session_cookie or "")
    if not user:
        raise HTTPException(401, "Nicht eingeloggt")
    return user
