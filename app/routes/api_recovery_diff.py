"""Recovery Center: Textvergleich Staging-Datei gegen Live-Datei des Datenwegs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import require_auth
from ..config_store import get_config
from ..db import get_db
from ..jobs.selective_restore import list_staging
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
    pair = _find_pair(config, str(entry.get("pair") or ""))
    live_root = str(pair.get("local") or "")
    if not live_root:
        raise HTTPException(404, "Datenweg hat keinen lokalen Pfad")
    left_path = _resolve(str(entry["staging_path"]), relative)
    right_path = _resolve(live_root, relative)
    if not os.path.isfile(left_path):
        raise HTTPException(404, "Datei im Recovery-Staging nicht gefunden")
    left, left_meta = _read_text(left_path)
    right, right_meta = "", None
    if os.path.isfile(right_path):
        right, right_meta = _read_text(right_path)
    return {
        "recovery_id": recovery_id,
        "path": relative,
        "left": left_meta,
        "right": right_meta,
        **diff_rows(left, right),
    }


__all__ = ["router", "staging_diff"]
