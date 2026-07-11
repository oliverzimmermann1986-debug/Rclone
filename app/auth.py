"""Session-Authentifizierung mit bcrypt, persistenter Sperrlogik und Rotation."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Optional

import bcrypt
from fastapi import Cookie, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config_store import get_config
from .db import get_db

logger = logging.getLogger(__name__)

SESSION_COOKIE = "rclone_sync_session"
LOGIN_CSRF_COOKIE = "rclone_sync_login_csrf"
DEFAULT_SESSION_MAX_AGE = 7 * 24 * 3600

# DB ist die primäre Sperre. Der In-Memory-Fallback schützt weiterhin, falls die
# DB gerade nicht erreichbar ist.
_login_lock = threading.Lock()
_login_failures: dict[str, deque[float]] = defaultdict(deque)
_login_blocked_until: dict[str, float] = {}


def _bounded_int(value, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _login_policy() -> tuple[int, int, int]:
    web = get_config().get("web", default={}) or {}
    return (
        _bounded_int(
            web.get("login_window_seconds", 300), default=300, minimum=60, maximum=86400
        ),
        _bounded_int(
            web.get("login_max_failures", 10), default=10, minimum=3, maximum=100
        ),
        _bounded_int(
            web.get("login_lock_seconds", 900), default=900, minimum=60, maximum=86400
        ),
    )


def session_max_age() -> int:
    return _bounded_int(
        get_config().get(
            "web", "session_max_age_seconds", default=DEFAULT_SESSION_MAX_AGE
        ),
        default=DEFAULT_SESSION_MAX_AGE,
        minimum=300,
        maximum=30 * 24 * 3600,
    )


def _serializer() -> URLSafeTimedSerializer:
    cfg = get_config()
    secret = str(cfg.get("web", "secret_key", default="") or "")
    if not secret or secret == "change-this-to-random-string-32chars-min":
        generated = secrets.token_urlsafe(48)

        def updater(data: dict) -> None:
            web = data.setdefault("web", {})
            if not isinstance(web, dict):
                raise ValueError("web muss ein Mapping sein")
            current = str(web.get("secret_key") or "")
            if not current or current == "change-this-to-random-string-32chars-min":
                web["secret_key"] = generated

        cfg.update(updater)
        secret = str(cfg.get("web", "secret_key", default=generated) or generated)
        logger.warning("web.secret_key war leer/default — neuer wurde generiert.")
    return URLSafeTimedSerializer(secret, salt="rclone-sync-session-v3")


def _session_version() -> int:
    try:
        return max(1, int(get_config().get("web", "session_version", default=1) or 1))
    except (TypeError, ValueError):
        return 1


def bump_session_version() -> int:
    result: dict[str, int] = {}

    def updater(data: dict) -> None:
        web = data.setdefault("web", {})
        if not isinstance(web, dict):
            raise ValueError("web muss ein Mapping sein")
        try:
            version = int(web.get("session_version", 1) or 1)
        except (TypeError, ValueError):
            version = 1
        web["session_version"] = max(1, version) + 1
        result["version"] = web["session_version"]

    get_config().update(updater)
    return result["version"]


def login_key(request: Request, username: str) -> str:
    del username  # Ein Administratorkonto: Fantasie-Nutzernamen dürfen die IP-Bremse nicht umgehen.
    host = request.client.host if request.client else "unknown"
    return host[:255]


def _fallback_retry_after(key: str, window_sec: int) -> int:
    now = time.monotonic()
    with _login_lock:
        blocked = _login_blocked_until.get(key, 0.0)
        if blocked > now:
            return max(1, int(blocked - now))
        _login_blocked_until.pop(key, None)
        failures = _login_failures.get(key)
        if failures:
            while failures and now - failures[0] > window_sec:
                failures.popleft()
            if not failures:
                _login_failures.pop(key, None)
    return 0


def login_retry_after(key: str) -> int:
    window_sec, _max_failures, _lock_sec = _login_policy()
    try:
        return get_db().auth_retry_after(key)
    except Exception:
        logger.exception("Persistente Login-Sperre nicht verfügbar; nutze Fallback")
        return _fallback_retry_after(key, window_sec)


def record_login_failure(key: str) -> int:
    window_sec, max_failures, lock_sec = _login_policy()
    try:
        return get_db().auth_record_failure(
            key,
            window_sec=window_sec,
            max_failures=max_failures,
            lock_sec=lock_sec,
        )
    except Exception:
        logger.exception(
            "Login-Fehler konnte nicht persistent gespeichert werden; nutze Fallback"
        )
        now = time.monotonic()
        with _login_lock:
            failures = _login_failures[key]
            while failures and now - failures[0] > window_sec:
                failures.popleft()
            failures.append(now)
            if len(failures) >= max_failures:
                _login_blocked_until[key] = now + lock_sec
                failures.clear()
                return lock_sec
        return 0


def clear_login_failures(key: str) -> None:
    try:
        get_db().auth_clear(key)
    except Exception:
        logger.exception("Persistente Login-Sperre konnte nicht gelöscht werden")
    with _login_lock:
        _login_failures.pop(key, None)
        _login_blocked_until.pop(key, None)


def verify_password(username: str, password: str) -> bool:
    cfg = get_config()
    cfg_user = str(cfg.get("web", "username", default="admin") or "admin")
    user_ok = secrets.compare_digest(username.casefold(), cfg_user.casefold())
    hashed = str(cfg.get("web", "password_hash", default="") or "")

    if not hashed:
        plain = str(cfg.get("web", "password", default="") or "")
        if not plain or plain == "changeme":
            return False
        password_ok = secrets.compare_digest(password, plain)
        if user_ok and password_ok:
            new_hash = bcrypt.hashpw(
                password.encode("utf-8"), bcrypt.gensalt(rounds=12)
            ).decode("ascii")

            def updater(data: dict) -> None:
                web = data.setdefault("web", {})
                if not isinstance(web, dict):
                    raise ValueError("web muss ein Mapping sein")
                web["password_hash"] = new_hash
                web["password"] = ""

            cfg.update(updater)
            logger.info("Klartext-Passwort migriert zu bcrypt-Hash")
            return True
        return False

    try:
        password_ok = bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))
        return bool(user_ok and password_ok)
    except (ValueError, TypeError, UnicodeError):
        logger.warning("Ungültiger bcrypt-Hash in der Konfiguration")
        return False


def create_session(username: str) -> str:
    return _serializer().dumps(
        {"u": username, "v": _session_version(), "n": secrets.token_hex(8)}
    )


def session_user(token: str) -> Optional[str]:
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=session_max_age())
        if int(data.get("v", 0)) != _session_version():
            return None
        user = str(data.get("u") or "")
        configured = str(
            get_config().get("web", "username", default="admin") or "admin"
        )
        if not user or not secrets.compare_digest(
            user.casefold(), configured.casefold()
        ):
            return None
        return user
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        return None
    except Exception:
        logger.exception("Session konnte nicht geprüft werden")
        return None


def require_auth(
    request: Request,
    session_cookie: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
) -> str:
    user = session_user(session_cookie or "")
    if not user:
        raise HTTPException(401, "Nicht eingeloggt")
    return user
