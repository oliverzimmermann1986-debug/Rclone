"""FastAPI-App für rclone-sync-container.
Web-UI mit Pair-Verwaltung, Progress, Cancel, History, Logs."""
from __future__ import annotations

import logging
import os
import secrets
import socket
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .auth import SESSION_COOKIE, SESSION_MAX_AGE, create_session, verify_password
from .config_store import get_config
from .db import get_db
from .routes import api_browse, api_config, api_jobs, api_test

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _sd_notify(msg: str) -> None:
    sock_path = os.getenv("NOTIFY_SOCKET")
    if not sock_path:
        return
    try:
        family = socket.AF_UNIX
        with socket.socket(family, socket.SOCK_DGRAM) as s:
            if sock_path[0] == "@":
                s.connect("\0" + sock_path[1:])
            else:
                s.connect(sock_path)
            s.sendall(msg.encode("utf-8"))
    except Exception as e:
        logger.warning(f"sd_notify failed: {e}")


@asynccontextmanager
async def _lifespan(app):
    # DB initialisieren (legt Tabellen an)
    get_db()
    _sd_notify("READY=1")
    logger.info("rclone-sync app ready")
    try:
        yield
    finally:
        _sd_notify("STOPPING=1")


app = FastAPI(
    title="rclone-sync Container",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_lifespan,
)

# Routes
app.include_router(api_config.router)
app.include_router(api_jobs.router)
app.include_router(api_test.router)
app.include_router(api_browse.router)

# Static + UI
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# Login + Logout
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return (STATIC_DIR / "login.html").read_text(encoding="utf-8")


@app.post("/login")
def login_submit(username: str = Form(...), password: str = Form(...)):
    if not verify_password(username, password):
        return RedirectResponse(url="/login?error=1", status_code=303)
    token = create_session(username)
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(
        SESSION_COOKIE, token,
        httponly=True, samesite="lax",
        max_age=SESSION_MAX_AGE,
        secure=False,  # via Reverse-Proxy oft auf https — Cookie kommt durch
    )
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    # Session-Check
    from .auth import session_user
    token = request.cookies.get(SESSION_COOKIE, "")
    if not session_user(token):
        return RedirectResponse(url="/login", status_code=303)
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


# Health-Endpoints
@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/healthz/deep")
def healthz_deep():
    """Tiefer Check: DB + rclone-Binary."""
    import subprocess
    checks = {}

    # DB
    try:
        with get_db().conn() as c:
            c.execute("SELECT 1").fetchone()
        checks["db"] = {"ok": True}
    except Exception as e:
        checks["db"] = {"ok": False, "error": str(e)[:200]}

    # rclone-Binary + listremotes
    try:
        r = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            remotes = [x.strip(":") for x in r.stdout.split() if x.strip()]
            checks["rclone"] = {"ok": True, "remotes": remotes}
        else:
            checks["rclone"] = {"ok": False, "error": r.stderr.strip()[:200]}
    except FileNotFoundError:
        checks["rclone"] = {"ok": False, "error": "rclone binary nicht gefunden"}
    except Exception as e:
        checks["rclone"] = {"ok": False, "error": str(e)}

    return {"ok": all(v.get("ok") for v in checks.values()), "checks": checks}
