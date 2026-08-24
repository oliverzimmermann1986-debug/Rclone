"""Eingeschränkte Pfad-Browser für die Pair-Konfiguration."""

from __future__ import annotations

import heapq
import subprocess
import threading
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_auth
from ..config_store import get_config
from ..db import get_db
from ..rclone_args import rclone_subprocess_env
from ..security import (
    DEFAULT_HIDDEN_REMOTE_PATHS,
    ensure_within,
    is_hidden_remote_path as _is_hidden_remote_path,
    is_relative_to,
    normalize_hidden_remote_paths,
    parse_browse_roots,
    require_csrf,
)

router = APIRouter(
    prefix="/api/browse",
    tags=["browse"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)

_MAX_ENTRIES = 1000
_BLOCKED_NAMES = {".snapshot", ".zfs", "__pycache__", "$RECYCLE.BIN"}


class CreateDirectoryPayload(BaseModel):
    kind: Literal["local", "remote"]
    parent: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=255)


def _audit_best_effort(kind: str, path: str) -> None:
    try:
        get_db().audit_add(
            "directory_created",
            actor="web",
            details={"kind": kind, "path": path},
        )
    except Exception:
        # Der Ordner ist bereits erstellt; ein Audit-Problem darf die korrekte
        # API-Antwort nicht in einen scheinbaren Fehler verwandeln.
        pass


def _directory_name(value: str) -> str:
    name = value.strip()
    blocked = {entry.casefold() for entry in _BLOCKED_NAMES}
    if (
        not name
        or name in {".", ".."}
        or name.startswith(".")
        or name.casefold() in blocked
        or any(char in name for char in ("/", "\\", "\x00", "\n", "\r"))
    ):
        raise HTTPException(
            400, "Bitte einen einfachen, sichtbaren Ordnernamen eingeben"
        )
    return name


def _hidden_remote_paths() -> set[str]:
    return normalize_hidden_remote_paths(
        get_config().get(
            "web",
            "hidden_remote_paths",
            default=list(DEFAULT_HIDDEN_REMOTE_PATHS),
        )
    )


def _rclone_remotes() -> list[str]:
    result = subprocess.run(
        ["rclone", "listremotes"],
        capture_output=True,
        text=True,
        timeout=10,
        stdin=subprocess.DEVNULL,
        env=rclone_subprocess_env(),
    )
    if result.returncode != 0:
        raise HTTPException(500, f"listremotes: {result.stderr.strip()[:300]}")
    return sorted(
        {line.strip() for line in result.stdout.splitlines() if line.strip()},
        key=str.casefold,
    )


def _rclone_directories(path: str) -> tuple[list[str], bool]:
    """Liest höchstens 1.001 Zeilen und beendet rclone danach kontrolliert."""
    proc = subprocess.Popen(
        [
            "rclone",
            "lsf",
            "--dirs-only",
            "--max-depth",
            "1",
            "--format",
            "p",
            "--",
            path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        stdin=subprocess.DEVNULL,
        env=rclone_subprocess_env(),
    )
    lines: list[str] = []
    read_error: list[str] = []

    def reader() -> None:
        assert proc.stdout is not None
        try:
            while len(lines) <= _MAX_ENTRIES:
                line = proc.stdout.readline(65537)
                if not line:
                    break
                if len(line) > 65536 and not line.endswith("\n"):
                    read_error.append(
                        "rclone lieferte einen überlangen Verzeichnisnamen"
                    )
                    break
                lines.append(line.rstrip("\r\n"))
        except (OSError, UnicodeError) as exc:
            read_error.append(str(exc))

    thread = threading.Thread(target=reader, name="rclone-browser-reader", daemon=True)
    thread.start()
    thread.join(60)
    timed_out = thread.is_alive()
    truncated = len(lines) > _MAX_ENTRIES
    if timed_out or truncated or read_error:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    thread.join(1)
    if timed_out:
        raise subprocess.TimeoutExpired(proc.args, 60)
    if read_error:
        raise HTTPException(502, read_error[0])
    if proc.returncode != 0 and not truncated:
        error = "\n".join(lines[-10:]).strip()[:300]
        raise HTTPException(502, f"lsf: {error or f'exit {proc.returncode}'}")

    names: list[str] = []
    for raw in lines[:_MAX_ENTRIES]:
        name = raw.rstrip("/")
        if not name or any(char in name for char in ("\x00", "\n", "\r")):
            continue
        names.append(name)
    names.sort(key=str.casefold)
    return names, truncated


def _browse_roots() -> list[Path]:
    cfg = get_config()
    configured = cfg.get(
        "web",
        "local_browse_roots",
        default=["/mnt", "/media", "/srv", "/opt/rclone-sync/data"],
    )
    roots = parse_browse_roots(configured)
    # Nur vorhandene Verzeichnisse anzeigen. Die Validierung erzwingt absolute Pfade.
    unique: list[Path] = []
    for root in roots:
        if root.is_dir() and root not in unique:
            unique.append(root)
    return unique


@router.get("/rclone")
def browse_rclone(path: str = "") -> dict[str, Any]:
    """Listet ausschließlich bereits konfigurierte rclone-Remotes."""
    try:
        remotes = _rclone_remotes()
        hidden = _hidden_remote_paths()
        if not path:
            return {
                "path": "",
                "parent": None,
                "is_root": True,
                "entries": [
                    {"name": remote.rstrip(":"), "path": remote, "is_dir": True}
                    for remote in remotes[:_MAX_ENTRIES]
                ],
                "truncated": len(remotes) > _MAX_ENTRIES,
            }

        if (
            len(path) > 4096
            or path.startswith("-")
            or any(c in path for c in ("\n", "\r", "\x00"))
        ):
            raise HTTPException(400, "Pfad enthält ungültige Zeichen")
        if ":" not in path:
            raise HTTPException(400, "Pfad muss 'remote:ordner' sein")
        remote, remote_path = path.split(":", 1)
        if any(segment == ".." for segment in remote_path.split("/")):
            raise HTTPException(400, "Pfad darf keine '..'-Segmente enthalten")
        remote_name = remote + ":"
        if remote_name not in remotes:
            raise HTTPException(403, "Remote ist nicht konfiguriert")
        if _is_hidden_remote_path(path, hidden):
            raise HTTPException(403, "Dieser Remote-Pfad ist im Browser ausgeblendet")

        names, truncated = _rclone_directories(path)
        entries = [
            {"name": name, "path": path.rstrip("/") + "/" + name, "is_dir": True}
            for name in names
            if not _is_hidden_remote_path(path.rstrip("/") + "/" + name, hidden)
        ]

        if path.endswith(":") or path.endswith(":/"):
            parent = ""
        else:
            base, rest = path.split(":", 1)
            rest = rest.rstrip("/")
            parent = base + ":" + rest.rsplit("/", 1)[0] if "/" in rest else base + ":"
        return {
            "path": path,
            "parent": parent,
            "is_root": False,
            "entries": entries,
            "truncated": truncated,
        }
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "rclone Timeout") from exc
    except FileNotFoundError as exc:
        raise HTTPException(500, "rclone nicht installiert") from exc


@router.get("/local")
def browse_local(path: str = "") -> dict[str, Any]:
    """Listet lokale Verzeichnisse nur innerhalb expliziter Browser-Roots."""
    roots = _browse_roots()
    if not roots:
        raise HTTPException(503, "Keine vorhandenen lokalen Browser-Roots konfiguriert")

    if not path or path == "/":
        entries = [
            {"name": str(root), "path": str(root), "is_dir": True, "is_root": True}
            for root in sorted(roots, key=lambda item: str(item).casefold())
        ]
        return {"path": "/", "parent": None, "is_root": True, "entries": entries}

    if len(path) > 4096 or any(c in path for c in ("\x00", "\n", "\r")):
        raise HTTPException(400, "Pfad enthält ungültige Zeichen")
    target = ensure_within(Path(path), roots)
    if not target.exists() or not target.is_dir():
        return {"path": str(target), "entries": [], "error": "Verzeichnis fehlt"}

    entries: list[dict[str, Any]] = []
    try:

        def eligible_children():
            for child in target.iterdir():
                if child.name.startswith(".") or child.name in _BLOCKED_NAMES:
                    continue
                try:
                    resolved = child.resolve()
                    if not child.is_dir() or not any(
                        is_relative_to(resolved, root) for root in roots
                    ):
                        continue
                except (OSError, RuntimeError):
                    continue
                yield child.name, resolved

        selected = heapq.nsmallest(
            _MAX_ENTRIES + 1, eligible_children(), key=lambda item: item[0].casefold()
        )
        truncated = len(selected) > _MAX_ENTRIES
        for name, resolved in selected[:_MAX_ENTRIES]:
            entries.append({"name": name, "path": str(resolved), "is_dir": True})
    except PermissionError as exc:
        raise HTTPException(403, "Keine Leseberechtigung") from exc
    except OSError as exc:
        raise HTTPException(
            500, f"Verzeichnis konnte nicht gelesen werden: {exc}"
        ) from exc

    parent_path = target.parent.resolve()
    parent = (
        str(parent_path)
        if any(is_relative_to(parent_path, root) for root in roots)
        else ""
    )
    return {
        "path": str(target),
        "parent": parent,
        "entries": entries,
        "is_root": False,
        "truncated": truncated,
    }


@router.post("/directory")
def create_directory(payload: CreateDirectoryPayload) -> dict[str, Any]:
    """Legt genau einen sichtbaren Unterordner im aktuellen Browser-Pfad an."""
    name = _directory_name(payload.name)
    if payload.kind == "local":
        roots = _browse_roots()
        if not roots:
            raise HTTPException(
                503, "Keine vorhandenen lokalen Browser-Roots konfiguriert"
            )
        parent = ensure_within(Path(payload.parent), roots)
        if not parent.is_dir():
            raise HTTPException(404, "Übergeordneter Ordner wurde nicht gefunden")
        target = ensure_within(parent / name, roots)
        try:
            target.mkdir(mode=0o750)
        except FileExistsError as exc:
            raise HTTPException(
                409, "Ein Ordner mit diesem Namen existiert bereits"
            ) from exc
        except PermissionError as exc:
            raise HTTPException(
                403, "Keine Schreibberechtigung in diesem Ordner"
            ) from exc
        except OSError as exc:
            raise HTTPException(
                500, f"Ordner konnte nicht angelegt werden: {exc}"
            ) from exc
        created_path = str(target)
    else:
        parent_text = payload.parent.strip()
        if (
            len(parent_text) > 4096
            or parent_text.startswith("-")
            or any(char in parent_text for char in ("\x00", "\n", "\r"))
            or ":" not in parent_text
        ):
            raise HTTPException(400, "Remote-Pfad ist ungültig")
        remote, remote_path = parent_text.split(":", 1)
        if any(segment == ".." for segment in remote_path.split("/")):
            raise HTTPException(400, "Pfad darf keine '..'-Segmente enthalten")
        if remote + ":" not in _rclone_remotes():
            raise HTTPException(403, "Remote ist nicht konfiguriert")
        hidden = _hidden_remote_paths()
        if _is_hidden_remote_path(parent_text, hidden):
            raise HTTPException(403, "Dieser Remote-Pfad ist im Browser ausgeblendet")
        created_path = parent_text.rstrip("/") + "/" + name
        if len(created_path) > 4096:
            raise HTTPException(400, "Der vollständige Remote-Pfad ist zu lang")
        if _is_hidden_remote_path(created_path, hidden):
            raise HTTPException(403, "Dieser Remote-Pfad ist im Browser ausgeblendet")
        try:
            names, _truncated = _rclone_directories(parent_text)
            if any(existing.casefold() == name.casefold() for existing in names):
                raise HTTPException(
                    409, "Ein Ordner mit diesem Namen existiert bereits"
                )
            result = subprocess.run(
                ["rclone", "mkdir", "--", created_path],
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL,
                env=rclone_subprocess_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(504, "rclone Timeout beim Anlegen des Ordners") from exc
        except FileNotFoundError as exc:
            raise HTTPException(500, "rclone nicht installiert") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:300]
            raise HTTPException(
                502,
                f"Remote-Ordner konnte nicht angelegt werden: {detail or f'exit {result.returncode}'}",
            )

    _audit_best_effort(payload.kind, created_path)
    return {"ok": True, "kind": payload.kind, "path": created_path}
