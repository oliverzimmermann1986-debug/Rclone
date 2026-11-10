"""S3 Object Lock / WORM-Siegel der Datenweg-Ziele."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..auth import require_auth
from ..config_store import get_config
from ..s3_object_lock import object_lock_report
from ..security import require_csrf

router = APIRouter(
    prefix="/api/storage",
    tags=["storage"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


@router.get("/object-lock")
def object_lock(refresh: bool = False) -> dict[str, Any]:
    return object_lock_report(get_config().snapshot(), refresh=refresh)
