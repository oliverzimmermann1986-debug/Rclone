"""API-Endpunkt für Cloud-Quota, Wachstumsprognose und Transfer-Budget."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..auth import require_auth
from ..cloud_quota import quota_report
from ..config_store import get_config
from ..db import get_db
from ..security import require_csrf

router = APIRouter(
    prefix="/api/storage",
    tags=["storage"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


@router.get("/quota")
def get_quota(refresh: bool = False) -> dict[str, Any]:
    cfg = get_config()
    snapshot = cfg.snapshot() if hasattr(cfg, "snapshot") else cfg
    return quota_report(snapshot, get_db(), refresh=refresh)


__all__ = ["get_quota", "router"]
