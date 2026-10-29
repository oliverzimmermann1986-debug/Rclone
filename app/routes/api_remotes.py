"""Guided rclone setup; unfinished credentials stay outside the live config."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, SecretStr

from ..auth import SESSION_COOKIE, require_auth
from ..db import get_db
from ..rclone_args import rclone_subprocess_env
from ..security import require_csrf

router = APIRouter(
    prefix="/api/remotes",
    tags=["remotes"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)
_lock = threading.RLock()
_TTL = 1800
_MAX_SESSIONS = 8


class StartRemote(BaseModel):
    name: str
    provider: str


class RemoteAnswer(BaseModel):
    answer: SecretStr
    step: int


@dataclass
class Setup:
    owner: str
    name: str
    provider: str
    directory: Any
    updated: float = field(default_factory=time.monotonic)
    state: str = ""
    option: dict = field(default_factory=dict)
    step: int = 0
    error: bool = False
    timer: threading.Timer | None = None

    @property
    def config(self) -> str:
        return str(Path(self.directory.name) / "rclone.conf")


_setups: dict[str, Setup] = {}


def _owner(request: Request) -> str:
    return hashlib.sha256(request.cookies.get(SESSION_COOKIE, "").encode()).hexdigest()


def _run(args: list[str], payload: dict | None = None) -> Any:
    env = rclone_subprocess_env()
    env["RCLONE_LOG_LEVEL"] = "ERROR"
    try:
        result = subprocess.run(
            ["rclone", *args, "--json", json.dumps(payload or {})],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        if result.returncode:
            raise HTTPException(
                502,
                "rclone konnte den Einrichtungsschritt nicht abschließen. Bitte Angaben prüfen.",
            )
        return json.loads(result.stdout or "{}")
    except FileNotFoundError:
        raise HTTPException(
            503, "rclone ist auf dem Server nicht installiert"
        ) from None
    except subprocess.TimeoutExpired:
        raise HTTPException(
            504, "Zeitlimit bei der Remote-Einrichtung erreicht"
        ) from None
    except (OSError, ValueError):
        raise HTTPException(
            502, "Die rclone-Antwort konnte nicht verarbeitet werden"
        ) from None


def _rc(method: str, payload: dict | None = None, config: str | None = None) -> dict:
    args = ["rc", "--loopback", method]
    if config:
        args += ["--config", config]
    return _run(args, payload)


def _expire() -> None:
    for key, setup in list(_setups.items()):
        if time.monotonic() - setup.updated > _TTL:
            _discard(key)


def _discard(key: str) -> None:
    setup = _setups.pop(key, None)
    if setup:
        if setup.timer:
            setup.timer.cancel()
        setup.directory.cleanup()


def _get(key: str, request: Request) -> Setup:
    _expire()
    setup = _setups.get(key)
    if setup is None or setup.owner != _owner(request):
        raise HTTPException(404, "Einrichtung abgelaufen. Bitte neu beginnen.")
    return setup


def _view(key: str, setup: Setup) -> dict:
    option = setup.option
    # Do not echo secret defaults or backend error text containing submitted values.
    default = option.get("Default")
    return {
        "id": key,
        "name": setup.name,
        "step": setup.step,
        "ready": not bool(setup.state) and not setup.error,
        "error": "Die Angabe wurde nicht akzeptiert. Bitte prüfen und erneut eingeben."
        if setup.error
        else "",
        "option": {
            "name": option.get("Name", ""),
            "help": option.get("Help", ""),
            "secret": bool(option.get("IsPassword"))
            or "token" in option.get("Name", "").lower(),
            "required": bool(option.get("Required")),
            "default": ""
            if option.get("IsPassword") or "token" in option.get("Name", "").lower()
            else (
                ""
                if default is None
                else str(default).lower()
                if isinstance(default, bool)
                else str(default)
            ),
            "examples": [
                {"value": str(e["Value"]), "help": e.get("Help", "")}
                for e in option.get("Examples") or []
            ],
            "exclusive": bool(option.get("Exclusive")),
        },
    }


def _advance(setup: Setup, result: dict) -> None:
    # Always use remote authorization: a browser on the server is not the client.
    for _ in range(5):
        option = result.get("Option") or {}
        if not result.get("State") or option.get("Name") != "config_is_local":
            break
        result = _rc(
            "config/update",
            {
                "name": setup.name,
                "parameters": {},
                "opt": {
                    "nonInteractive": True,
                    "all": True,
                    "obscure": True,
                    "continue": True,
                    "state": result["State"],
                    "result": "false",
                },
            },
            setup.config,
        )
    setup.state = result.get("State", "")
    setup.option = result.get("Option") or {}
    setup.step += 1
    setup.error = bool(result.get("Error"))


@router.get("/providers")
def providers() -> dict:
    data = _rc("config/providers")
    preferred = ["pcloud", "drive", "webdav", "onedrive"]
    return {
        "providers": sorted(
            [
                {"name": p["Name"], "description": p.get("Description", p["Name"])}
                for p in data.get("providers", [])
            ],
            key=lambda p: (
                preferred.index(p["name"])
                if p["name"] in preferred
                else len(preferred),
                p["description"].casefold(),
            ),
        )
    }


@router.post("/setup")
def start(payload: StartRemote, request: Request) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}", payload.name):
        raise HTTPException(
            400, "Remote-Name: 2–64 Buchstaben, Ziffern, Bindestriche oder Unterstriche"
        )
    if payload.provider not in {p["name"] for p in providers()["providers"]}:
        raise HTTPException(400, "Unbekannter Remote-Anbieter")
    with _lock:
        _expire()
        existing = _rc("config/listremotes").get("remotes", [])
        if payload.name.casefold() in {n.rstrip(":").casefold() for n in existing}:
            raise HTTPException(409, "Ein Remote mit diesem Namen existiert bereits")
        if len(_setups) >= _MAX_SESSIONS:
            raise HTTPException(
                429, "Zu viele offene Einrichtungen. Bitte eine Einrichtung schließen."
            )
        setup = Setup(
            _owner(request),
            payload.name,
            payload.provider,
            tempfile.TemporaryDirectory(prefix="sicherpfad-remote-"),
        )
        key = secrets.token_urlsafe(32)
        _setups[key] = setup

        def expire_setup() -> None:
            with _lock:
                _discard(key)

        timer = threading.Timer(_TTL, expire_setup)
        timer.daemon = True
        setup.timer = timer
        timer.start()
        try:
            _advance(
                setup,
                _rc(
                    "config/create",
                    {
                        "name": setup.name,
                        "type": setup.provider,
                        "parameters": {},
                        "opt": {"nonInteractive": True, "all": True, "obscure": True},
                    },
                    setup.config,
                ),
            )
        except Exception:
            _discard(key)
            raise
        return _view(key, setup)


@router.post("/setup/{key}/answer")
def answer(key: str, payload: RemoteAnswer, request: Request) -> dict:
    with _lock:
        setup = _get(key, request)
        if payload.step != setup.step or not setup.state:
            raise HTTPException(
                409, "Dieser Einrichtungsschritt ist nicht mehr aktuell"
            )
        value = payload.answer.get_secret_value()
        if len(value) > 65536 or "\x00" in value:
            raise HTTPException(
                400, "Die Angabe ist zu lang oder enthält ungültige Zeichen"
            )
        if not value and setup.option.get("Required"):
            raise HTTPException(
                400, "Bitte eine Angabe für dieses Pflichtfeld eingeben"
            )
        if setup.option.get("Exclusive") and setup.option.get("Examples"):
            if value not in {str(e["Value"]) for e in setup.option["Examples"]}:
                raise HTTPException(400, "Bitte einen der angebotenen Werte auswählen")
        # The generic *all-set state stores its answer verbatim. Post-config
        # password questions, in contrast, expect clear text and handle it themselves.
        if (
            value
            and setup.option.get("IsPassword")
            and setup.state.startswith("*all-set,")
        ):
            value = _rc("core/obscure", {"clear": value})["obscured"]
        _advance(
            setup,
            _rc(
                "config/update",
                {
                    "name": setup.name,
                    "parameters": {},
                    "opt": {
                        "nonInteractive": True,
                        "all": True,
                        "obscure": True,
                        "continue": True,
                        "state": setup.state,
                        "result": value,
                    },
                },
                setup.config,
            ),
        )
        return _view(key, setup)


@router.post("/setup/{key}/save")
def save(key: str, request: Request) -> dict:
    with _lock:
        setup = _get(key, request)
        if setup.state or setup.error:
            raise HTTPException(
                409, "Bitte zuerst alle Einrichtungsschritte abschließen"
            )
        existing = _rc("config/listremotes").get("remotes", [])
        if setup.name.casefold() in {n.rstrip(":").casefold() for n in existing}:
            raise HTTPException(
                409, "Ein Remote mit diesem Namen existiert inzwischen bereits"
            )
        parameters = _rc("config/get", {"name": setup.name}, setup.config)
        parameters.pop("type", None)
        # The staged OAuth flow is already complete. Recreating the section must
        # not prompt for another login or open a browser on the server.
        parameters["config_refresh_token"] = "false"
        parameters["config_is_local"] = "false"
        result = _rc(
            "config/create",
            {
                "name": setup.name,
                "type": setup.provider,
                "parameters": parameters,
                "opt": {"nonInteractive": True, "noObscure": True},
            },
        )
        if result.get("State") or result.get("Error"):
            # Only this newly created remote can be removed; existing ones are rejected above.
            _rc("config/delete", {"name": setup.name})
            raise HTTPException(
                502,
                "Die Anmeldung ist noch nicht vollständig. Bitte Einrichtung wiederholen.",
            )
        name = setup.name
        try:
            get_db().audit_add(
                "remote_created",
                actor="web",
                details={"name": name, "provider": setup.provider},
            )
        except Exception:
            pass
        _discard(key)
        return {"ok": True, "name": name, "path": name + ":"}


@router.delete("/setup/{key}")
def cancel(key: str, request: Request) -> dict:
    with _lock:
        _get(key, request)
        _discard(key)
        return {"ok": True}
