"""Session-Authentifizierung mit bcrypt, persistenter Sperrlogik und Rotation."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
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
from .config_validation import SESSION_SECRET_PLACEHOLDER, session_secret_strength_error
from .db import get_db
from .utils import bounded_int as _bounded_int

logger = logging.getLogger(__name__)

SESSION_COOKIE = "rclone_sync_session"
LOGIN_CSRF_COOKIE = "rclone_sync_login_csrf"
DEFAULT_SESSION_MAX_AGE = 7 * 24 * 3600

# DB ist die primäre Sperre. Der In-Memory-Fallback schützt weiterhin, falls die
# DB gerade nicht erreichbar ist.
_login_lock = threading.Lock()
_login_failures: dict[str, deque[float]] = defaultdict(deque)
_login_blocked_until: dict[str, float] = {}


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
    if not secret or secret == SESSION_SECRET_PLACEHOLDER:
        generated = secrets.token_urlsafe(48)

        def updater(data: dict) -> None:
            web = data.setdefault("web", {})
            if not isinstance(web, dict):
                raise ValueError("web muss ein Mapping sein")
            current = str(web.get("secret_key") or "")
            if not current or current == SESSION_SECRET_PLACEHOLDER:
                web["secret_key"] = generated

        cfg.update(updater)
        secret = str(cfg.get("web", "secret_key", default=generated) or generated)
        logger.warning("web.secret_key war leer/default — neuer wurde generiert.")
    strength_error = session_secret_strength_error(secret)
    if strength_error:
        logger.critical("Unsicheres Session-Secret abgelehnt: %s", strength_error)
        raise RuntimeError(strength_error)
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


def _trusted_proxy_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    configured = get_config().get("web", "trusted_proxy_ips", default=[]) or []
    if isinstance(configured, str):
        configured = [configured]
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for value in configured:
        try:
            networks.append(ipaddress.ip_network(str(value).strip(), strict=False))
        except ValueError:
            logger.error("Ungültiger vertrauenswürdiger Proxy wird ignoriert: %r", value)
    return tuple(networks)


def client_host(request: Request) -> str:
    """Return the socket peer or the first untrusted hop from the right.

    The web service deliberately disables Uvicorn's implicit proxy-header
    rewriting so ``request.client`` remains the actual socket peer. Forwarded
    chains are considered only when that peer and every traversed proxy hop are
    explicitly trusted by CIDR.
    """

    peer = request.client.host if request.client else "unknown"
    try:
        peer_ip = ipaddress.ip_address(peer.split("%", 1)[0])
    except ValueError:
        return peer[:255]
    networks = _trusted_proxy_networks()

    def trusted(value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return any(value.version == network.version and value in network for network in networks)

    if not networks or not trusted(peer_ip):
        return str(peer_ip)
    raw_chain = request.headers.get("x-forwarded-for", "")
    values = [item.strip() for item in raw_chain.split(",") if item.strip()]
    if not values or len(values) > 32:
        return str(peer_ip)
    try:
        forwarded = [ipaddress.ip_address(value.split("%", 1)[0]) for value in values]
    except ValueError:
        logger.warning("Ungültige X-Forwarded-For-Kette verworfen")
        return str(peer_ip)

    selected = peer_ip
    current = peer_ip
    for hop in reversed(forwarded):
        if not trusted(current):
            break
        selected = hop
        current = hop
    return str(selected)[:255]


def login_key(request: Request, username: str) -> str:
    del username  # Ein Administratorkonto: Fantasie-Nutzernamen dürfen die IP-Bremse nicht umgehen.
    return client_host(request)


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


def reauth_keys(request: Request) -> tuple[str, str]:
    """Anonyme, getrennte Sperrschlüssel für Sitzung und Client-IP.

    Weder die signierte Sitzung noch die IP-Adresse landen im Klartext in der
    Datenbank. Das serverseitige Session-Geheimnis verhindert zudem, dass der
    IP-Schlüssel durch simples Durchprobieren zurückgerechnet werden kann.
    """
    session_token = str(request.cookies.get(SESSION_COOKIE, "") or "")
    client_identity = client_host(request)
    secret = str(get_config().get("web", "secret_key", default="") or "")
    secret_bytes = secret.encode("utf-8") or b"rclone-sync-reauth-v1"

    def digest(scope: str, value: str) -> str:
        payload = f"{scope}\0{value}".encode("utf-8", errors="surrogatepass")
        return hmac.new(secret_bytes, payload, hashlib.sha256).hexdigest()

    return (
        f"reauth:session:{digest('session', session_token)}",
        f"reauth:ip:{digest('ip', client_identity)}",
    )


def _fallback_reauth_retry_after(keys: tuple[str, ...], window_sec: int) -> int:
    return max((_fallback_retry_after(key, window_sec) for key in keys), default=0)


def reauth_retry_after(request: Request) -> int:
    keys = reauth_keys(request)
    window_sec, _max_failures, _lock_sec = _login_policy()
    try:
        return get_db().auth_retry_after_many(keys)
    except Exception:
        logger.exception("Persistente Reauth-Sperre nicht verfügbar; nutze Fallback")
        return _fallback_reauth_retry_after(keys, window_sec)


def record_reauth_failure(request: Request) -> int:
    keys = reauth_keys(request)
    window_sec, max_failures, lock_sec = _login_policy()
    try:
        return get_db().auth_record_failure_many(
            keys,
            window_sec=window_sec,
            max_failures=max_failures,
            lock_sec=lock_sec,
        )
    except Exception:
        logger.exception(
            "Reauth-Fehler konnte nicht persistent gespeichert werden; nutze Fallback"
        )
        now = time.monotonic()
        longest = 0
        with _login_lock:
            for key in keys:
                failures = _login_failures[key]
                while failures and now - failures[0] > window_sec:
                    failures.popleft()
                failures.append(now)
                if len(failures) >= max_failures:
                    _login_blocked_until[key] = now + lock_sec
                    failures.clear()
                    longest = max(longest, lock_sec)
        return longest


def clear_reauth_failures(request: Request) -> None:
    keys = reauth_keys(request)
    try:
        get_db().auth_clear_many(keys)
    except Exception:
        logger.exception("Persistente Reauth-Sperre konnte nicht gelöscht werden")
    with _login_lock:
        for key in keys:
            _login_failures.pop(key, None)
            _login_blocked_until.pop(key, None)


def require_reauthentication(request: Request, username: str, password: str) -> None:
    """Prüft ein aktuelles Passwort mit persistenter Sitzung/IP-Sperre."""

    def rate_limited(retry_after: int) -> HTTPException:
        retry = max(1, int(retry_after))
        return HTTPException(
            429,
            {
                "message": "Zu viele falsche Passwortbestätigungen",
                "reauth_required": True,
                "retry_after_seconds": retry,
            },
            headers={"Retry-After": str(retry)},
        )

    retry_after = reauth_retry_after(request)
    if retry_after:
        raise rate_limited(retry_after)
    if not verify_password(username, password):
        retry_after = record_reauth_failure(request)
        if retry_after:
            raise rate_limited(retry_after)
        raise HTTPException(
            403,
            {
                "message": "Aktuelles Passwort falsch",
                "reauth_required": True,
            },
        )
    clear_reauth_failures(request)


def _constant_time_text_equal(first: str, second: str) -> bool:
    """Vergleicht beliebigen Unicode-Text ohne den ASCII-only-Stringpfad."""
    try:
        return secrets.compare_digest(first.encode("utf-8"), second.encode("utf-8"))
    except UnicodeError:
        return False


def verify_password(username: str, password: str) -> bool:
    cfg = get_config()
    cfg_user = str(cfg.get("web", "username", default="admin") or "admin")
    user_ok = _constant_time_text_equal(username.casefold(), cfg_user.casefold())
    hashed = str(cfg.get("web", "password_hash", default="") or "")
    encoded_password = password.encode("utf-8")
    if len(encoded_password) > 72:
        return False

    if not hashed:
        plain = str(cfg.get("web", "password", default="") or "")
        if not plain or plain == "changeme":
            return False
        password_ok = _constant_time_text_equal(password, plain)
        if user_ok and password_ok:
            try:
                new_hash = bcrypt.hashpw(
                    encoded_password, bcrypt.gensalt(rounds=12)
                ).decode("ascii")
            except ValueError:
                logger.exception("Klartext-Passwort konnte nicht migriert werden")
                return False

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
        password_ok = bcrypt.checkpw(encoded_password, hashed.encode("ascii"))
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
        if not user or not _constant_time_text_equal(
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
