"""API-Endpunkte für den interaktiven Time-Travel-Browser und Versions-Diff."""

from __future__ import annotations

from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import require_auth
from ..config_store import get_config
from ..recovery_points import RecoveryPointError
from ..security import require_csrf
from ..time_travel import (
    browse_virtual_point,
    diff_point_files,
    file_version_history,
)
from .api_recovery import _find_pair

router = APIRouter(
    prefix="/api/recovery/timetravel",
    tags=["recovery"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


def _raise_for_recovery_error(exc: RecoveryPointError) -> NoReturn:
    message = str(exc)
    lowered = message.lower()
    if "nicht gefunden" in lowered or "keine versionsablage" in lowered:
        status = 404
    elif "zu groß" in lowered:
        status = 413
    elif "textdateien" in lowered:
        status = 415
    elif "ungültig" in lowered or "fehlt" in lowered or "unsicher" in lowered:
        status = 400
    else:
        status = 502
    raise HTTPException(status, message) from exc


@router.get("/browse")
def timetravel_browse(
    identity: str = Query(min_length=1, max_length=128),
    point_id: str = Query(default="current", min_length=1, max_length=128),
    path: str = Query(default="", max_length=1024),
) -> dict[str, Any]:
    config = get_config().snapshot()
    pair = _find_pair(config, identity)
    try:
        return browse_virtual_point(config, pair, point_id=point_id, relative_path=path)
    except RecoveryPointError as exc:
        _raise_for_recovery_error(exc)


@router.get("/history")
def timetravel_history(
    identity: str = Query(min_length=1, max_length=128),
    path: str = Query(min_length=1, max_length=1024),
) -> dict[str, Any]:
    config = get_config().snapshot()
    pair = _find_pair(config, identity)
    try:
        return file_version_history(config, pair, path)
    except RecoveryPointError as exc:
        _raise_for_recovery_error(exc)


@router.get("/diff")
def timetravel_diff(
    identity: str = Query(min_length=1, max_length=128),
    path: str = Query(min_length=1, max_length=1024),
    from_point: str = Query(min_length=1, max_length=128),
    to_point: str = Query(default="current", min_length=1, max_length=128),
) -> dict[str, Any]:
    config = get_config().snapshot()
    pair = _find_pair(config, identity)
    try:
        return diff_point_files(
            config, pair, path, from_point=from_point, to_point=to_point
        )
    except RecoveryPointError as exc:
        _raise_for_recovery_error(exc)


__all__ = [
    "router",
    "timetravel_browse",
    "timetravel_diff",
    "timetravel_history",
]
