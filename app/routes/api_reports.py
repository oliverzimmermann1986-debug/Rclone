"""Authenticated report summaries/PDFs; explicit confirmed email transmission."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from .. import protection_reports
from ..auth import account_generation, require_auth, require_reauthentication
from ..config_store import get_config
from ..db import get_db
from ..lockdown import is_locked_down
from ..security import require_csrf
from .api_config import _audit_best_effort
from .api_operations import PasswordConfirmation

router = APIRouter(
    prefix="/api/reports/weekly",
    tags=["reports"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


@router.get("")
def latest_weekly_report() -> dict[str, Any]:
    return protection_reports.latest(get_db())


@router.get("/history")
def weekly_report_history(limit: int = Query(52, ge=1, le=104)) -> dict[str, Any]:
    return protection_reports.history(get_db(), limit=limit)


@router.post("/generate")
def generate_weekly_report(user: str = Depends(require_auth)) -> dict[str, Any]:
    result = protection_reports.generate(get_config(), get_db())
    _audit_best_effort(
        "weekly_report_generated",
        actor=user,
        details={"report_id": result["report"]["id"]},
    )
    return result


@router.get("/{report_id}/pdf")
def weekly_report_pdf(report_id: int) -> Response:
    stored = protection_reports.get_report(get_db(), report_id, pdf=True)
    if stored is None:
        raise HTTPException(404, "Wochenbericht nicht gefunden.")
    start = stored["report"]["week_start"]
    return Response(
        stored["pdf"],
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="sicherpfad-wochenbericht-{start}.pdf"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{report_id}/send")
def send_weekly_report(
    report_id: int,
    body: PasswordConfirmation,
    request: Request,
    user: str = Depends(require_auth),
) -> dict[str, Any]:
    generation = require_reauthentication(request, user, body.current_password)
    cfg, db = get_config(), get_db()
    if is_locked_down(db):
        raise HTTPException(423, "Notfall-Lockdown sperrt den Berichtversand.")
    if protection_reports.settings(cfg)["email"]["enabled"] is not True:
        raise HTTPException(400, "E-Mail-Versand ist nicht aktiviert.")
    try:
        with cfg.locked_snapshot() as data:
            if not secrets.compare_digest(account_generation(data), generation):
                raise HTTPException(409, "Anmeldung wurde zwischenzeitlich geändert.")
            result = protection_reports.send(data, db, report_id, manual=True)
    except KeyError:
        raise HTTPException(404, "Wochenbericht nicht gefunden.") from None
    except ValueError:
        raise HTTPException(422, "SMTP-Einstellungen sind ungültig.") from None
    _audit_best_effort(
        "weekly_report_send",
        actor=user,
        details={"report_id": report_id, "ok": result["ok"]},
    )
    return result
