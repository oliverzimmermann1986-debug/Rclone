"""FastAPI-App für rclone-sync-container."""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import secrets
import socket
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .auth import (
    LOGIN_CSRF_COOKIE,
    bump_session_version,
    SESSION_COOKIE,
    clear_login_failures,
    create_session,
    login_key,
    login_retry_after,
    record_login_failure,
    require_auth,
    session_max_age,
    session_user,
    verify_password,
)
from .config_store import get_config
from .db import get_db
from .rclone_args import rclone_subprocess_env
from .security import CSRF_COOKIE, new_csrf_token, require_csrf
from .utils import bounded_number as _bounded_number
from .routes import (
    api_pbs,
    api_browse,
    api_config,
    api_diagnostics,
    api_jobs,
    api_maintenance,
    api_storage,
    api_test,
)
from .maintenance import run_automatic_maintenance
from .jobs import runtime_state
from .jobs.job_lifecycle import (
    BACKUP_KINDS,
    PBS_KINDS,
    reconcile_locked_scope,
)
from .jobs.locks import file_lock_or_none

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

MAX_API_BODY_BYTES = 2 * 1024 * 1024
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _sd_notify(message: str) -> None:
    socket_path = os.getenv("NOTIFY_SOCKET")
    if not socket_path:
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(
                "\0" + socket_path[1:] if socket_path.startswith("@") else socket_path
            )
            sock.sendall(message.encode("utf-8"))
    except Exception as exc:
        logger.warning("sd_notify fehlgeschlagen: %s", exc)


def _cookie_secure(request: Request) -> bool:
    configured = get_config().get("web", "secure_cookie", default=False)
    if configured is True:
        return True
    if str(configured).lower() == "auto":
        return request.url.scheme == "https"
    return False


def _set_csrf_cookie(response, request: Request, token: str) -> None:
    response.set_cookie(
        CSRF_COOKIE,
        token,
        httponly=False,
        secure=_cookie_secure(request),
        samesite="strict",
        max_age=session_max_age(),
        path="/",
    )


def _production_security_warnings() -> list[str]:
    """Erkennt riskante Web-Defaults, ohne lokale HTTP-Installationen zu blockieren."""
    web = get_config().get("web", default={}) or {}
    warnings: list[str] = []
    allowed_hosts = web.get("allowed_hosts", ["*"]) or ["*"]
    if isinstance(allowed_hosts, str):
        allowed_hosts = [allowed_hosts]
    if "*" in {str(item).strip() for item in allowed_hosts}:
        warnings.append("web.allowed_hosts enthält den Wildcard-Eintrag '*'")
    if web.get("secure_cookie", False) is False:
        warnings.append("web.secure_cookie ist deaktiviert")
    try:
        hsts_seconds = int(web.get("hsts_seconds", 0) or 0)
    except (TypeError, ValueError):
        hsts_seconds = 0
    if hsts_seconds <= 0:
        warnings.append("web.hsts_seconds ist deaktiviert")
    return warnings


def _run_startup_maintenance() -> None:
    try:
        maintenance = run_automatic_maintenance()
        if maintenance.get("enabled"):
            logger.info("Automatische Wartung: %s", maintenance)
    except Exception:
        logger.exception("Automatische Wartung fehlgeschlagen")


@asynccontextmanager
async def _lifespan(_app):
    db = get_db()
    recovered = 0
    for scope, kinds in (
        (runtime_state.DEFAULT_CANCEL_SCOPE, BACKUP_KINDS),
        ("pbs", PBS_KINDS),
    ):
        with file_lock_or_none(scope) as got_lock:
            if got_lock is None:
                logger.info(
                    "Startup-Recovery für %s übersprungen: Job-Lock belegt",
                    scope,
                )
                continue
            result = reconcile_locked_scope(db, scope=scope, kinds=kinds)
            recovered += int(result.get("recovered_jobs") or 0)
            if not result.get("safe"):
                logger.error(
                    "Startup-Recovery für %s blockiert: %s aktiver "
                    "registrierter Unterprozess(e)",
                    scope,
                    result.get("active_processes", 0),
                )
    if recovered:
        logger.warning("%d verwaiste laufende Job(s) als stale markiert", recovered)
    security_warnings = _production_security_warnings()
    if security_warnings:
        logger.warning(
            "Unsichere Web-Defaults aktiv: %s. Bei externem Zugriff HTTPS, "
            "Secure-Cookies, HSTS und konkrete allowed_hosts konfigurieren.",
            "; ".join(security_warnings),
        )
    _sd_notify("READY=1")
    logger.info("rclone-sync app ready")
    threading.Thread(
        target=_run_startup_maintenance,
        name="startup-maintenance",
        daemon=True,
    ).start()
    try:
        yield
    finally:
        _sd_notify("STOPPING=1")


app = FastAPI(
    title="rclone-sync Container",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_lifespan,
)


def _host_allowed(request: Request) -> bool:
    # Lokale Healthchecks bleiben unabhängig von einer engen Host-Allowlist möglich.
    if request.url.path in {"/healthz", "/readyz"} and request.client:
        try:
            client_ip = ipaddress.ip_address(request.client.host.split("%", 1)[0])
            if client_ip.is_loopback and str(request.url.hostname or "").casefold() in {
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                return True
        except ValueError:
            pass
    configured = get_config().get("web", "allowed_hosts", default=["*"]) or ["*"]
    if isinstance(configured, str):
        configured = [configured]
    allowed = {str(item).casefold().strip() for item in configured if str(item).strip()}
    if "*" in allowed:
        return True
    host = str(request.url.hostname or "").casefold()
    return host in allowed or any(
        rule.startswith("*.") and host.endswith(rule[1:]) for rule in allowed
    )


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    # "Origin: null" senden sandboxed iframes und Redirect-Ketten — aber wegen
    # unserer Referrer-Policy "no-referrer" auch manche Browser (v. a. Firefox)
    # bei völlig normalen same-origin Formular-POSTs. Sec-Fetch-Site ist in dem
    # Fall die verlässlichere Angabe.
    if origin == "null":
        # Ablehnen nur bei ausdrücklich cross-origin gemeldeter Herkunft.
        # Fehlt Sec-Fetch-Site (ältere Browser, Webviews, Privacy-Extensions,
        # die Header strippen), bleibt der Double-Submit-CSRF-Schutz mit
        # SameSite=strict-Cookies die maßgebliche Verteidigung — wie vor 1.7.1.
        sec_fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
        if sec_fetch_site in {"cross-site", "same-site"}:
            logger.warning(
                "Origin-Prüfung: Origin=null abgelehnt (Sec-Fetch-Site=%r, path=%s)",
                sec_fetch_site,
                request.url.path,
            )
            return False
        return True
    try:
        from urllib.parse import urlsplit

        parsed = urlsplit(origin)
    except ValueError:
        return False
    # Nur der Host:Port zählt. Das Schema weicht legitim ab, wenn ein
    # TLS-Reverse-Proxy davor liegt, dessen X-Forwarded-Proto uvicorn nicht
    # vertraut (--forwarded-allow-ips) — der Browser sendet dann https, die
    # App sieht http. Ein Angreifer kann den Host im Origin nicht fälschen.
    if parsed.netloc.casefold() == request.url.netloc.casefold():
        return True
    # Proxys, die den Host-Header nicht durchreichen (z. B. auf die
    # Upstream-Adresse umschreiben), erzeugen einen Netloc-Mismatch. Explizit
    # konfigurierte allowed_hosts gelten dann als vertrauenswürdige Origins —
    # der Wildcard-Default "*" bewusst nicht.
    origin_host = str(parsed.hostname or "").casefold()
    configured = get_config().get("web", "allowed_hosts", default=["*"]) or ["*"]
    if isinstance(configured, str):
        configured = [configured]
    explicit = {
        str(item).casefold().strip()
        for item in configured
        if str(item).strip() and str(item).strip() != "*"
    }
    if origin_host and (
        origin_host in explicit
        or any(
            rule.startswith("*.") and origin_host.endswith(rule[1:])
            for rule in explicit
        )
    ):
        return True
    logger.warning(
        "Origin-Prüfung fehlgeschlagen: Origin=%r, erwartet Host=%r, path=%s "
        "(hinter einem Proxy: Host-Header durchreichen oder Host in "
        "web.allowed_hosts eintragen)",
        origin,
        request.url.netloc,
        request.url.path,
    )
    return False


def _apply_security_headers(response, request: Request, request_id: str):
    response.headers.setdefault("X-Request-ID", request_id)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    )
    hsts_seconds = int(
        _bounded_number(
            get_config().get("web", "hsts_seconds", default=0),
            default=0,
            minimum=0,
            maximum=63_072_000,
        )
    )
    if hsts_seconds > 0 and request.url.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", f"max-age={hsts_seconds}"
        )
    if request.url.path.startswith("/api/") or request.url.path in {
        "/",
        "/login",
        "/logout",
    }:
        response.headers.setdefault("Cache-Control", "no-store")
    elif request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "public, max-age=86400")
    if "server" in response.headers:
        del response.headers["server"]
    return response


def _error_response(request: Request, request_id: str, status_code: int, detail: str):
    response = JSONResponse(
        status_code=status_code, content={"detail": detail, "request_id": request_id}
    )
    return _apply_security_headers(response, request, request_id)


class _RequestTooLarge(Exception):
    pass


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    request_id_header = request.headers.get("x-request-id", "")
    request_id = (
        request_id_header
        if _REQUEST_ID_RE.match(request_id_header)
        else uuid.uuid4().hex
    )
    request.state.request_id = request_id
    if not _host_allowed(request):
        return _error_response(request, request_id, 400, "Host nicht erlaubt")

    method = request.method.upper()
    state_changing = method in {"POST", "PUT", "PATCH", "DELETE"}
    body_limit_exceeded = False
    if state_changing and not _same_origin(request):
        return _error_response(
            request, request_id, 403, "Origin-Prüfung fehlgeschlagen"
        )

    if state_changing:
        length = request.headers.get("content-length")
        if length:
            try:
                parsed_length = int(length)
            except ValueError:
                return _error_response(
                    request, request_id, 400, "Ungültiger Content-Length-Header"
                )
            if parsed_length < 0:
                return _error_response(
                    request, request_id, 400, "Ungültiger Content-Length-Header"
                )
            if parsed_length > MAX_API_BODY_BYTES:
                return _error_response(request, request_id, 413, "Anfrage ist zu groß")

        # Content-Length ist bei chunked Requests optional und daher keine
        # ausreichende Begrenzung. Der Receive-Wrapper zählt die echten Bytes.
        original_receive = request._receive
        received = 0

        async def limited_receive():
            nonlocal received, body_limit_exceeded
            message = await original_receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_API_BODY_BYTES:
                    body_limit_exceeded = True
                    raise _RequestTooLarge
            return message

        request._receive = limited_receive

    try:
        response = await call_next(request)
    except _RequestTooLarge:
        return _error_response(request, request_id, 413, "Anfrage ist zu groß")
    # Starlette/FastAPI kann Receive-Fehler beim Body-Parsing in eine eigene
    # 400-Antwort umwandeln. Der Zähler bleibt die maßgebliche Quelle und
    # ersetzt eine solche Antwort zuverlässig durch HTTP 413.
    if body_limit_exceeded:
        return _error_response(request, request_id, 413, "Anfrage ist zu groß")
    return _apply_security_headers(response, request, request_id)


app.include_router(api_config.router)
app.include_router(api_jobs.router)
app.include_router(api_test.router)
app.include_router(api_storage.router)
app.include_router(api_diagnostics.router)
app.include_router(api_maintenance.router)
app.include_router(api_browse.router)
app.include_router(api_pbs.router)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    html = (STATIC_DIR / "login.html").read_text(encoding="utf-8")
    error = (
        '<div class="err">Falsches Login oder vorübergehend gesperrt</div>'
        if request.query_params.get("error")
        else ""
    )
    nonce = secrets.token_urlsafe(32)
    html = html.replace("<!--LOGIN_ERROR-->", error).replace("<!--LOGIN_CSRF-->", nonce)
    response = HTMLResponse(html)
    response.set_cookie(
        LOGIN_CSRF_COOKIE,
        nonce,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="strict",
        max_age=600,
        path="/login",
    )
    return response


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(..., min_length=1, max_length=128),
    password: str = Form(..., min_length=1, max_length=1024),
    login_csrf: str = Form(..., min_length=20, max_length=256),
):
    cookie_nonce = request.cookies.get(LOGIN_CSRF_COOKIE, "")
    if not cookie_nonce or not secrets.compare_digest(cookie_nonce, login_csrf):
        return RedirectResponse(url="/login?error=csrf", status_code=303)
    key = login_key(request, username)
    retry_after = login_retry_after(key)
    if retry_after:
        response = RedirectResponse(url="/login?error=rate", status_code=303)
        response.headers["Retry-After"] = str(retry_after)
        return response
    if not verify_password(username, password):
        record_login_failure(key)
        return RedirectResponse(url="/login?error=1", status_code=303)

    clear_login_failures(key)
    try:
        data_dir = Path(
            get_config().get("paths", "data_dir", default="/opt/rclone-sync/data")
        )
        (data_dir / ".initial-password").unlink(missing_ok=True)
    except OSError:
        logger.exception(
            "Initialpasswort-Datei konnte nach dem Login nicht entfernt werden"
        )
    token = create_session(username)
    csrf = new_csrf_token()
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=session_max_age(),
        secure=_cookie_secure(request),
        path="/",
    )
    _set_csrf_cookie(response, request, csrf)
    response.delete_cookie(LOGIN_CSRF_COOKIE, path="/login")
    return response


@app.get("/logout")
def logout_get():
    return RedirectResponse(url="/", status_code=303)


@app.post("/logout", dependencies=[Depends(require_auth), Depends(require_csrf)])
def logout():
    # Stateless Tokens lassen sich nur über die Session-Version widerrufen.
    # Beim Single-Admin-Konto ist "alle Sessions beenden" das erwartete Verhalten.
    try:
        bump_session_version()
    except Exception:
        logger.exception("Session-Version konnte beim Logout nicht erhöht werden")
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    token = request.cookies.get(SESSION_COOKIE, "")
    if not session_user(token):
        return RedirectResponse(url="/login", status_code=303)
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # Cache-Busting: Query-Version an app.js/style.css folgt der App-Version,
    # sonst hängen Browser nach Updates auf altem Frontend (fehlende GUI-Features).
    html = html.replace("?v=__APP_VERSION__", f"?v={__version__}")
    response = HTMLResponse(html)
    if not request.cookies.get(CSRF_COOKIE):
        _set_csrf_cookie(response, request, new_csrf_token())
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
    logger.exception(
        "Unbehandelter Fehler request_id=%s path=%s", request_id, request.url.path
    )
    response = JSONResponse(
        status_code=500,
        content={"detail": "Interner Serverfehler", "request_id": request_id},
    )
    # Der generische Exception-Handler läuft in Starlettes äußerster
    # ServerErrorMiddleware und damit außerhalb der security_middleware.
    # Fehlerantworten dürfen unabhängig vom Pfad nie gecacht werden.
    response.headers["Cache-Control"] = "no-store"
    return _apply_security_headers(response, request, request_id)


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__}


@app.get("/readyz")
def readyz():
    """Kompakter Readiness-Check für systemd/Uptime Kuma ohne sensible Details."""
    try:
        cfg = get_config()
        paths = cfg.get("paths", default={}) or {}
        with get_db().conn() as connection:
            connection.execute("SELECT 1").fetchone()
        data_dir = Path(str(paths.get("data_dir") or "/opt/rclone-sync/data"))
        ready = data_dir.exists() and os.access(data_dir, os.R_OK | os.W_OK)
    except Exception:
        ready = False
    warnings = _production_security_warnings() if ready else []
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "ok": ready,
            "version": __version__,
            "warnings": warnings,
            "secure_configuration": not warnings,
        },
    )


@app.get("/healthz/deep")
def healthz_deep(_user: str = Depends(require_auth)):
    """Authentifizierter Check von DB und rclone, ohne öffentliche Remote-Leaks."""
    import subprocess

    checks = {}
    try:
        with get_db().conn() as connection:
            connection.execute("SELECT 1").fetchone()
        checks["db"] = {"ok": True}
    except Exception as exc:
        checks["db"] = {"ok": False, "error": str(exc)[:200]}
    try:
        result = subprocess.run(
            ["rclone", "listremotes"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
            env=rclone_subprocess_env(),
        )
        checks["rclone"] = {
            "ok": result.returncode == 0,
            "remote_count": len(result.stdout.splitlines())
            if result.returncode == 0
            else 0,
        }
        if result.returncode != 0:
            checks["rclone"]["error"] = result.stderr.strip()[:200]
    except FileNotFoundError:
        checks["rclone"] = {"ok": False, "error": "rclone binary nicht gefunden"}
    except Exception as exc:
        checks["rclone"] = {"ok": False, "error": str(exc)[:200]}
    ok = all(item.get("ok") for item in checks.values())
    return JSONResponse(
        status_code=200 if ok else 503, content={"ok": ok, "checks": checks}
    )
