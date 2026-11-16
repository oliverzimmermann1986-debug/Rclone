"""Recovery Center: Textvergleich Staging-Datei gegen Live-Datei des Datenwegs."""

from __future__ import annotations

import os
import stat
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import require_auth
from ..config_store import get_config
from ..db import get_db
from ..jobs.selective_restore import list_staging
from ..recovery_points import _is_remote
from ..security import require_csrf
from ..text_diff import SAMPLE_BYTES, diff_rows, is_probably_text
from .api_recovery import _find_pair, _relative_path

router = APIRouter(
    prefix="/api/recovery",
    tags=["recovery"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)

MAX_FILE_BYTES = 1024 * 1024


def _resolve(root: str, relative: str) -> str:
    """Löst root/relative auf und verlangt, dass das Ergebnis unter root bleibt."""
    real_root = os.path.realpath(root)
    real = os.path.realpath(os.path.join(real_root, relative))
    if not Path(real).is_relative_to(real_root):
        raise HTTPException(400, "Ungültiger relativer Pfad")
    return real


def _read_text(real: str) -> tuple[str, dict[str, Any]]:
    with open(real, "rb") as handle:
        stat = os.fstat(handle.fileno())
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "Datei ist für den Vergleich zu groß (max. 1 MiB)")
    if not is_probably_text(data[:SAMPLE_BYTES]):
        raise HTTPException(415, "Nur Textdateien können verglichen werden")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(415, "Nur Textdateien können verglichen werden") from None
    return text, {"size": stat.st_size, "mtime": stat.st_mtime}


def _read_text_at(root: str, relative: str) -> tuple[str, dict[str, Any]]:
    """Keep every directory and the file anchored during the bounded read."""
    base = Path(root).expanduser().absolute()
    try:
        with ExitStack() as stack:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directory = os.open(base.anchor, flags)
            stack.callback(os.close, directory)
            parts = (*base.parts[1:], *Path(relative).parts)
            for part in parts[:-1]:
                directory = os.open(part, flags, dir_fd=directory)
                stack.callback(os.close, directory)
            descriptor = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory,
            )
            stream = stack.enter_context(os.fdopen(descriptor, "rb"))
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise HTTPException(
                    400, "Nur reguläre Dateien können verglichen werden"
                )
            data = stream.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        raise
    except (OSError, AttributeError, NotImplementedError) as exc:
        raise HTTPException(
            400, "Dateipfad kann nicht sicher für den Vergleich geöffnet werden"
        ) from exc
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "Datei ist für den Vergleich zu groß (max. 1 MiB)")
    if not is_probably_text(data[:SAMPLE_BYTES]):
        raise HTTPException(415, "Nur Textdateien können verglichen werden")
    try:
        return data.decode("utf-8"), {"size": info.st_size, "mtime": info.st_mtime}
    except UnicodeDecodeError as exc:
        raise HTTPException(415, "Nur Textdateien können verglichen werden") from exc


@router.get("/staging/{recovery_id}/diff")
def staging_diff(
    recovery_id: str, path: str = Query(min_length=1, max_length=1024)
) -> dict[str, Any]:
    relative = _relative_path(path)
    if not relative:
        raise HTTPException(400, "Ungültiger relativer Pfad")
    config = get_config().snapshot()
    try:
        items = list_staging(get_db(), config)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    entry = next((item for item in items if item.get("id") == recovery_id), None)
    if entry is None or not entry.get("staging_path"):
        raise HTTPException(404, "Recovery-Staging nicht gefunden")
    binding = entry.get("evidence_binding") or {}
    pair = _find_pair(
        config,
        str(binding.get("pair_id") or entry.get("identity") or entry.get("pair") or ""),
    )
    live_root = str(pair.get("local") or "")
    if not live_root:
        raise HTTPException(404, "Datenweg hat keinen lokalen Pfad")
    if _is_remote(live_root):
        raise HTTPException(
            400, "Textvergleich mit einer Cloud-Live-Quelle wird nicht unterstützt"
        )
    try:
        left, left_meta = _read_text_at(str(entry["staging_path"]), relative)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Datei im Recovery-Staging nicht gefunden") from exc
    right, right_meta = "", None
    try:
        right, right_meta = _read_text_at(live_root, relative)
    except FileNotFoundError:
        pass
    return {
        "recovery_id": recovery_id,
        "path": relative,
        "left": left_meta,
        "right": right_meta,
        **diff_rows(left, right),
    }


__all__ = ["router", "staging_diff"]
