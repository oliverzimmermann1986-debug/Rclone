"""Gemeinsame Web- und Pfad-Sicherheitshelfer."""

from __future__ import annotations

import posixpath
import secrets
from pathlib import Path
from typing import Iterable, Optional

from fastapi import Cookie, Header, HTTPException, Request

CSRF_COOKIE = "rclone_sync_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Nativ verschlüsselte pCloud-Ordner sind über die normale API nicht sinnvoll
# synchronisierbar. Zusätzliche Ziele über web.hidden_remote_paths.
DEFAULT_HIDDEN_REMOTE_PATHS = ("pcloud:/Crypto Folder",)


def normalize_remote_path(path: str) -> str:
    """Kanonisiert ``remote:pfad`` inklusive ``.``, ``..`` und Doppel-Slashes."""
    text = str(path or "")
    if ":" not in text:
        return text.rstrip("/")
    remote, rest = text.split(":", 1)
    stripped = rest.strip("/")
    if not stripped:
        return f"{remote}:"
    cleaned = posixpath.normpath("/" + stripped)
    return f"{remote}:" if cleaned == "/" else f"{remote}:{cleaned}"


def normalize_hidden_remote_paths(values: object) -> set[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        values = list(DEFAULT_HIDDEN_REMOTE_PATHS)
    return {
        normalize_remote_path(str(item)).casefold()
        for item in values
        if str(item).strip() and ":" in str(item)
    }


def is_hidden_remote_path(path: str, hidden: Iterable[str]) -> bool:
    normalized = normalize_remote_path(path).casefold()
    return any(
        normalized == blocked or normalized.startswith(blocked.rstrip("/") + "/")
        for blocked in hidden
    )


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def require_csrf(
    request: Request,
    csrf_cookie: Optional[str] = Cookie(default=None, alias=CSRF_COOKIE),
    csrf_header: Optional[str] = Header(default=None, alias="X-CSRF-Token"),
) -> None:
    """Double-submit-CSRF-Prüfung für alle schreibenden API-Aufrufe."""
    if request.method.upper() in SAFE_METHODS:
        return
    if (
        not csrf_cookie
        or not csrf_header
        or not secrets.compare_digest(csrf_cookie, csrf_header)
    ):
        raise HTTPException(403, "CSRF-Prüfung fehlgeschlagen")


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def ensure_within(path: Path, roots: Iterable[Path]) -> Path:
    resolved = path.expanduser().resolve()
    if not any(is_relative_to(resolved, root) for root in roots):
        raise HTTPException(403, f"Pfad außerhalb der erlaubten Bereiche: {resolved}")
    return resolved


def parse_browse_roots(values: object) -> list[Path]:
    roots: list[Path] = []
    if isinstance(values, str):
        values = [values]
    if isinstance(values, list):
        for value in values:
            if not isinstance(value, str) or not value.strip():
                continue
            try:
                roots.append(Path(value).expanduser().resolve())
            except (OSError, RuntimeError):
                continue
    return roots
