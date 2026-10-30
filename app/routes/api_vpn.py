"""VPN-Tunnel-Status (WireGuard/Tailscale/Erreichbarkeit) für den System-Tab."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..auth import require_auth
from ..security import require_csrf
from ..vpn_sensor import vpn_snapshot

router = APIRouter(
    prefix="/api/diagnostics",
    tags=["diagnostics"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


@router.get("/vpn")
def vpn(refresh: bool = False) -> dict[str, Any]:
    return vpn_snapshot(refresh=refresh)
