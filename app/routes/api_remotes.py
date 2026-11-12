"""Guided rclone setup; unfinished credentials stay outside the live config."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr

from ..auth import SESSION_COOKIE, require_auth
from ..config_store import get_config
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

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_ALLOWED_DRIVE_SCOPES = {
    "drive",
    "drive.file",
    "drive.readonly",
    "drive.metadata.readonly",
    "drive.appfolder",
}

_OPTION_HINTS_DE: dict[str, tuple[str, str]] = {
    "client_id": (
        "Eigene OAuth Client-ID (optional)",
        "Leer lassen, um die integrierte Standard-Anmeldung von rclone zu verwenden.",
    ),
    "client_secret": (
        "Eigenes OAuth Client-Secret (optional)",
        "Leer lassen, sofern keine eigene Client-ID eingetragen wurde.",
    ),
    "scope": (
        "Zugriffsumfang (Scope)",
        "„drive“ erlaubt vollen Lese-/Schreibzugriff für Backups; „drive.file“ beschränkt rclone auf selbst angelegte Dateien.",
    ),
    "root_folder_id": (
        "Google-Drive-Startordner-ID (optional)",
        "Leer lassen für das gesamte Drive oder die Ordner-ID aus der Browser-URL (nach /folders/…) eintragen.",
    ),
    "service_account_file": (
        "Service-Account-JSON-Pfad (optional)",
        "Für reguläre Anmeldung per Google-Konto leer lassen.",
    ),
    "config_fs_advanced": (
        "Erweiterte Optionen bearbeiten?",
        "Für die normale Einrichtung „false“ (Nein) wählen.",
    ),
    "config_token": (
        "Google-Anmeldung abschließen (URL aus Adresszeile oder Token)",
        "Klicke oben auf „1. Bei Google im Browser anmelden“, bestätige den Zugriff und füge danach einfach die Adresse aus der Browser-Adresszeile (127.0.0.1:53682/?code=…) hier ein.",
    ),
    "config_change_team_drive": (
        "Als Geteilte Ablage (Shared Drive / Team Drive) einrichten?",
        "„false“ für dein persönliches Google Drive, „true“ für eine Google-Workspace-Geteilte-Ablage.",
    ),
    "config_team_drive": (
        "Geteilte Ablage auswählen",
        "Wähle die gewünschte Geteilte Ablage aus der Liste.",
    ),
    "url": (
        "Server-URL",
        "Vollständige HTTPS-Adresse des Speichers (z. B. WebDAV- oder Nextcloud-URL).",
    ),
    "user": (
        "Benutzername",
        "Anmeldename beim Speicheranbieter.",
    ),
    "pass": (
        "Passwort / App-Passwort",
        "Bei Konten mit 2-Faktor-Anmeldung ein App-Passwort verwenden.",
    ),
}


class StartRemote(BaseModel):
    name: str
    provider: str
    express: bool | None = None
    scope: str = "drive"
    client_id: str = ""
    client_secret: SecretStr | None = None
    root_folder_id: str = ""
    team_drive: bool = False


class RemoteAnswer(BaseModel):
    answer: SecretStr
    step: int


class GoogleDriveServiceAccountPayload(BaseModel):
    name: str
    service_account_json: SecretStr
    scope: str = "drive"
    root_folder_id: str = Field(default="", max_length=256)
    team_drive: str = Field(default="", max_length=256)
    impersonate: str = Field(default="", max_length=320)


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
    express: bool = False
    auto_answers: dict[str, str] = field(default_factory=dict)
    auth_proc: subprocess.Popen[str] | None = None
    auth_output: list[str] = field(default_factory=list)
    auth_local_base: str = "http://127.0.0.1:53682"
    auth_state: str = ""
    direct_auth_url: str = ""

    @property
    def config(self) -> str:
        return str(Path(self.directory.name) / "rclone.conf")


_setups: dict[str, Setup] = {}
_AUTH_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_AUTH_CMD_RE = re.compile(r"rclone\s+authorize\s+[^\r\n]+")
_PASTE_ARROW_RE = re.compile(r"--->\s*(.+?)\s*<---", re.DOTALL)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _stop_browser_auth_helper(setup: Setup) -> None:
    proc = setup.auth_proc
    setup.auth_proc = None
    if proc is not None:
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except OSError:
            pass


def _resolve_redirect_location(local_auth_url: str) -> str:
    """Liest die OAuth-Provider-Redirect-URL (z. B. accounts.google.com) vom lokalen rclone-authorize-Port."""
    parsed = urllib.parse.urlparse(local_auth_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        return ""
    opener = urllib.request.build_opener(_NoRedirectHandler())
    for _ in range(15):
        loc = ""
        try:
            resp = opener.open(local_auth_url, timeout=2.0)
            loc = resp.headers.get("Location") or ""
        except urllib.error.HTTPError as err:
            loc = err.headers.get("Location") if err.headers else ""
        except OSError:
            loc = ""
        if loc and loc.startswith("https://"):
            return loc
        time.sleep(0.05)
    return ""


def _start_browser_auth_helper(setup: Setup, authorize_cmd: str) -> None:
    """Startet `rclone authorize --auth-no-open-browser` auf dem Server, um die direkte Google-Login-URL bereitzustellen."""
    for other in list(_setups.values()):
        _stop_browser_auth_helper(other)
    _stop_browser_auth_helper(setup)
    setup.auth_output.clear()
    setup.direct_auth_url = ""
    setup.auth_state = ""
    if not authorize_cmd or not shutil.which("rclone"):
        return
    try:
        parts = shlex.split(authorize_cmd)
    except ValueError:
        return
    if len(parts) < 2 or parts[0] != "rclone" or parts[1] != "authorize":
        return
    tail = [p for p in parts[2:] if not p.startswith("-")][:2]
    if not tail:
        return
    env = rclone_subprocess_env()
    try:
        proc = subprocess.Popen(
            ["rclone", "authorize", *tail, "--auth-no-open-browser"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return
    setup.auth_proc = proc

    def _reader() -> None:
        stream = proc.stdout
        if stream is None:
            return
        for line in stream:
            setup.auth_output.append(line)
            if not setup.direct_auth_url:
                match = _AUTH_URL_RE.search(line)
                if match:
                    candidate = match.group(0).rstrip(".,)\"'")
                    parsed = urllib.parse.urlparse(candidate)
                    if (
                        parsed.hostname in {"127.0.0.1", "localhost"}
                        and parsed.port
                        and ("/auth" in parsed.path or "state=" in parsed.query)
                    ):
                        setup.auth_local_base = f"http://127.0.0.1:{parsed.port}"
                        qs = urllib.parse.parse_qs(parsed.query)
                        if qs.get("state"):
                            setup.auth_state = qs["state"][0]
                        redirect_url = _resolve_redirect_location(candidate)
                        setup.direct_auth_url = redirect_url or candidate

    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not setup.direct_auth_url:
        if proc.poll() is not None:
            break
        time.sleep(0.05)


def _extract_callback_query(raw_value: str, fallback_state: str = "") -> str | None:
    """Erkennt eingefügte Browser-Redirect-URLs (http://127.0.0.1:53682/?state=...&code=...) oder reine OAuth-Codes."""
    text = str(raw_value or "").strip()
    if not text:
        return None
    if text.startswith(
        (
            "http://127.0.0.1",
            "http://localhost",
            "https://127.0.0.1",
            "https://localhost",
        )
    ):
        parsed = urllib.parse.urlparse(text)
        qs = urllib.parse.parse_qs(parsed.query)
        if "code" in qs:
            if "state" not in qs and fallback_state:
                qs["state"] = [fallback_state]
            return urllib.parse.urlencode({k: v[0] for k, v in qs.items()})
    if "code=" in text and "{" not in text:
        qtext = text.lstrip("?/")
        qs = urllib.parse.parse_qs(qtext)
        if "code" in qs:
            if "state" not in qs and fallback_state:
                qs["state"] = [fallback_state]
            return urllib.parse.urlencode({k: v[0] for k, v in qs.items()})
    if text.startswith("4/") and " " not in text and "{" not in text and len(text) > 16:
        params = {"code": text}
        if fallback_state:
            params["state"] = fallback_state
        return urllib.parse.urlencode(params)
    return None


def _complete_browser_auth_if_needed(setup: Setup, raw_value: str) -> str:
    """Übergibt eine eingefügte Browser-Redirect-URL an den lokalen rclone-authorize-Listener und liest das OAuth-Token."""
    query = _extract_callback_query(raw_value, setup.auth_state)
    if query and setup.auth_proc is not None and setup.auth_proc.poll() is None:
        callback_url = f"{setup.auth_local_base}/?{query}"
        try:
            urllib.request.urlopen(callback_url, timeout=10.0).read()
        except OSError:
            pass
    if (query or not raw_value.strip()) and setup.auth_proc is not None:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            combined = "".join(setup.auth_output)
            if "--->" in combined and "<---" in combined:
                _stop_browser_auth_helper(setup)
                return _normalize_oauth_token_input(combined)
            if setup.auth_proc.poll() is not None:
                break
            time.sleep(0.05)
        combined = "".join(setup.auth_output)
        if "access_token" in combined or "--->" in combined:
            _stop_browser_auth_helper(setup)
            return _normalize_oauth_token_input(combined)
    return _normalize_oauth_token_input(raw_value)


def _normalize_oauth_token_input(raw_value: str) -> str:
    """Extrahiert Token-Blobs oder JSON auch dann sauber, wenn Terminal-Rahmen (---> ... <---) oder Zeilenumbrüche mitkopiert wurden."""
    text = str(raw_value or "").strip()
    if not text:
        return ""
    arrow_match = _PASTE_ARROW_RE.search(text)
    if arrow_match:
        text = arrow_match.group(1).strip()
    if "{" in text and "}" in text and "access_token" in text:
        start_idx = text.find("{")
        end_idx = text.rfind("}")
        if 0 <= start_idx < end_idx:
            candidate = text[start_idx : end_idx + 1]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict) and "access_token" in parsed:
                    return json.dumps(parsed, separators=(",", ":"))
            except ValueError:
                pass
    # Wenn rclone einen mehrzeilig umgebrochenen Base64-Token-Block ausgegeben hat:
    if "\n" in text or "\r" in text:
        lines = [
            ln.strip()
            for ln in text.splitlines()
            if ln.strip()
            and not ln.strip().startswith("Paste the following")
            and not ln.strip().startswith("End paste")
        ]
        joined = "".join(lines)
        if joined:
            return joined
    return text


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
        _stop_browser_auth_helper(setup)
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
    opt_name = str(option.get("Name", "") or "")
    help_text = str(option.get("Help", "") or "")
    url_match = _AUTH_URL_RE.search(help_text)
    cmd_match = _AUTH_CMD_RE.search(help_text)
    label_de, hint_de = _OPTION_HINTS_DE.get(opt_name, ("", ""))
    raw_auth_url = url_match.group(0).rstrip(".,)") if url_match else ""
    if "rclone.org" in raw_auth_url:
        raw_auth_url = ""
    return {
        "id": key,
        "name": setup.name,
        "provider": setup.provider,
        "step": setup.step,
        "ready": not bool(setup.state) and not setup.error,
        "error": "Die Angabe wurde nicht akzeptiert. Bitte prüfen und erneut eingeben."
        if setup.error
        else "",
        "option": {
            "name": opt_name,
            "label_de": label_de or None,
            "hint_de": hint_de or None,
            "help": help_text,
            "auth_url": (setup.direct_auth_url or raw_auth_url) or None,
            "direct_auth_url": setup.direct_auth_url or None,
            "authorize_command": cmd_match.group(0).strip() if cmd_match else None,
            "secret": bool(option.get("IsPassword")) or "token" in opt_name.lower(),
            "required": bool(option.get("Required")),
            "default": ""
            if option.get("IsPassword") or "token" in opt_name.lower()
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
    # In express mode, pre-filled standard options are answered automatically so the
    # user jumps directly to the OAuth token step.
    for _ in range(15):
        option = result.get("Option") or {}
        opt_name = str(option.get("Name") or "")
        if not result.get("State") or result.get("Error"):
            break
        if opt_name == "config_is_local":
            auto_val: str | None = "false"
        elif setup.express and opt_name in setup.auto_answers:
            auto_val = setup.auto_answers.pop(opt_name)
        else:
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
                    "result": auto_val,
                },
            },
            setup.config,
        )
    setup.state = result.get("State", "")
    setup.option = result.get("Option") or {}
    setup.step += 1
    setup.error = bool(result.get("Error"))
    if str(setup.option.get("Name") or "") == "config_token":
        help_text = str(setup.option.get("Help", "") or "")
        cmd_match = _AUTH_CMD_RE.search(help_text)
        if cmd_match:
            _start_browser_auth_helper(setup, cmd_match.group(0).strip())
    elif not setup.state:
        _stop_browser_auth_helper(setup)


@router.get("/providers")
def providers() -> dict:
    data = _rc("config/providers")
    preferred = ["drive", "pcloud", "webdav", "onedrive"]
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
    if not _NAME_RE.fullmatch(payload.name):
        raise HTTPException(
            400, "Remote-Name: 2–64 Buchstaben, Ziffern, Bindestriche oder Unterstriche"
        )
    if payload.provider not in {p["name"] for p in providers()["providers"]}:
        raise HTTPException(400, "Unbekannter Remote-Anbieter")
    drive_scope = payload.scope.strip() or "drive"
    if payload.provider == "drive" and drive_scope not in _ALLOWED_DRIVE_SCOPES:
        raise HTTPException(400, "Ungültiger Google-Drive-Zugriffsumfang (Scope)")
    with _lock:
        _expire()
        existing = _rc("config/listremotes").get("remotes", [])
        if payload.name.casefold() in {n.rstrip(":").casefold() for n in existing}:
            raise HTTPException(409, "Ein Remote mit diesem Namen existiert bereits")
        if len(_setups) >= _MAX_SESSIONS:
            raise HTTPException(
                429, "Zu viele offene Einrichtungen. Bitte eine Einrichtung schließen."
            )
        auto_answers: dict[str, str] = {}
        initial_params: dict[str, str] = {}
        use_express = (
            payload.express
            if payload.express is not None
            else (payload.provider == "drive")
        )
        if use_express:
            auto_answers["config_fs_advanced"] = "false"
            auto_answers["config_advanced"] = "false"
            if payload.provider == "drive":
                client_id = payload.client_id.strip()
                client_secret = (
                    payload.client_secret.get_secret_value().strip()
                    if payload.client_secret
                    else ""
                )
                root_folder_id = payload.root_folder_id.strip()
                auto_answers.update(
                    {
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "scope": drive_scope,
                        "root_folder_id": root_folder_id,
                        "service_account_file": "",
                        "config_change_team_drive": "true"
                        if payload.team_drive
                        else "false",
                    }
                )
                initial_params["scope"] = drive_scope
                if client_id:
                    initial_params["client_id"] = client_id
                if client_secret:
                    initial_params["client_secret"] = client_secret
                if root_folder_id:
                    initial_params["root_folder_id"] = root_folder_id

        setup = Setup(
            _owner(request),
            payload.name,
            payload.provider,
            tempfile.TemporaryDirectory(prefix="sicherpfad-remote-"),
            express=bool(use_express),
            auto_answers=auto_answers,
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
                        "parameters": initial_params,
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
        opt_name = str(setup.option.get("Name") or "")
        if "token" in opt_name.lower():
            value = _complete_browser_auth_if_needed(setup, value)
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


@router.post("/google-drive/service-account")
def create_google_drive_service_account(
    payload: GoogleDriveServiceAccountPayload,
) -> dict:
    """Richtet ein Google-Drive-Remote direkt über ein Google-Service-Account-JSON ein."""
    name = payload.name.strip()
    if not _NAME_RE.fullmatch(name):
        raise HTTPException(
            400, "Remote-Name: 2–64 Buchstaben, Ziffern, Bindestriche oder Unterstriche"
        )
    scope = payload.scope.strip() or "drive"
    if scope not in _ALLOWED_DRIVE_SCOPES:
        raise HTTPException(400, "Ungültiger Google-Drive-Zugriffsumfang (Scope)")
    raw_json = payload.service_account_json.get_secret_value().strip()
    if len(raw_json) > 131072 or "\x00" in raw_json:
        raise HTTPException(400, "Service-Account-JSON ist zu groß oder ungültig")
    try:
        parsed_sa = json.loads(raw_json)
    except ValueError as exc:
        raise HTTPException(
            400, "Service-Account-Datei ist kein gültiges JSON"
        ) from exc
    if (
        not isinstance(parsed_sa, dict)
        or parsed_sa.get("type") != "service_account"
        or not str(parsed_sa.get("client_email") or "").strip()
        or "BEGIN PRIVATE KEY" not in str(parsed_sa.get("private_key") or "")
    ):
        raise HTTPException(
            400,
            "Ungültige Google-Service-Account-Datei (erwartet type='service_account', client_email und private_key)",
        )
    client_email = str(parsed_sa["client_email"]).strip()

    with _lock:
        existing = _rc("config/listremotes").get("remotes", [])
        if name.casefold() in {n.rstrip(":").casefold() for n in existing}:
            raise HTTPException(409, "Ein Remote mit diesem Namen existiert bereits")

        try:
            snap = get_config().snapshot()
            paths = snap.get("paths") if isinstance(snap, dict) else {}
            data_dir = Path(
                str((paths or {}).get("data_dir") or "/opt/rclone-sync/data")
            )
        except Exception:
            data_dir = Path("/opt/rclone-sync/data")

        sa_dir = data_dir / "service-accounts"
        sa_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if sa_dir.is_symlink():
            raise HTTPException(
                400, "Service-Account-Verzeichnis darf kein Symlink sein"
            )
        try:
            os.chmod(sa_dir, stat.S_IRWXU)
        except OSError:
            pass

        sa_file = sa_dir / f"{name}.json"
        if sa_file.is_symlink():
            raise HTTPException(400, "Service-Account-Zieldatei darf kein Symlink sein")
        flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(sa_file, flags, stat.S_IRUSR | stat.S_IWUSR)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(
                    json.dumps(parsed_sa, ensure_ascii=False, indent=2).encode("utf-8")
                )
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            sa_file.unlink(missing_ok=True)
            raise

        parameters: dict[str, str] = {
            "scope": scope,
            "service_account_file": str(sa_file),
            "config_is_local": "false",
            "config_refresh_token": "false",
        }
        if payload.root_folder_id.strip():
            parameters["root_folder_id"] = payload.root_folder_id.strip()
        if payload.team_drive.strip():
            parameters["team_drive"] = payload.team_drive.strip()
        if payload.impersonate.strip():
            parameters["impersonate"] = payload.impersonate.strip()

        try:
            result = _rc(
                "config/create",
                {
                    "name": name,
                    "type": "drive",
                    "parameters": parameters,
                    "opt": {"nonInteractive": True, "noObscure": True},
                },
            )
            if result.get("State") or result.get("Error"):
                _rc("config/delete", {"name": name})
                sa_file.unlink(missing_ok=True)
                raise HTTPException(
                    502,
                    "Google-Drive-Service-Account konnte nicht in rclone angelegt werden.",
                )
        except Exception:
            sa_file.unlink(missing_ok=True)
            raise

        try:
            get_db().audit_add(
                "remote_created",
                actor="web",
                details={
                    "name": name,
                    "provider": "drive",
                    "auth_mode": "service_account",
                    "client_email": client_email,
                },
            )
        except Exception:
            pass
        return {
            "ok": True,
            "name": name,
            "path": f"{name}:",
            "client_email": client_email,
        }
