"""API: Bandbreiten-Heatmap und Zeitplan-Vorschläge (nur lesend)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from .. import bandwidth
from ..auth import require_auth
from ..config_store import get_config
from ..db import get_db
from ..security import require_csrf

router = APIRouter(
    prefix="/api/jobs/bandwidth",
    tags=["bandwidth"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


def _timezone_name() -> str:
    return str(get_config().get("backup", "timezone", default="") or "Europe/Berlin")


@router.get("/heatmap")
def bandwidth_heatmap(days: int = Query(90, ge=7, le=365)) -> dict[str, Any]:
    return bandwidth.heatmap(get_db(), days=days, timezone_name=_timezone_name())


@router.get("/suggestions")
def bandwidth_suggestions() -> dict[str, Any]:
    cfg, db, timezone_name = get_config(), get_db(), _timezone_name()
    hm = bandwidth.heatmap(db, timezone_name=timezone_name)
    busy = bandwidth.scheduled_slots(cfg, timezone_name=timezone_name)
    return {
        "timezone": hm["timezone"],
        "windows": bandwidth.suggest_windows(hm, busy),
        "schedules": bandwidth.propose_schedules(cfg, db, timezone_name=timezone_name),
    }
