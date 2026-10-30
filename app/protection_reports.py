"""Durable, path-free weekly protection reports and opt-in verified SMTP."""

from __future__ import annotations

import hashlib
import html
import io
import json
import math
import re
import secrets
import smtplib
import socket
import ssl
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter

from .heartbeat import resolve_host, snapshot
from .jobs.scheduler import rclone_history_key
from .lockdown import is_locked_down
from .restore_evidence import evaluate_restore_evidence, restore_history_key

_MAX_PDF_BYTES = 1024 * 1024
_MAX_ATTEMPTS = 5
_ADDRESS = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\Z"
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS weekly_protection_reports (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 week_start TEXT NOT NULL, week_end TEXT NOT NULL, timezone TEXT NOT NULL,
 generated_at REAL NOT NULL, summary_json TEXT NOT NULL, pdf BLOB NOT NULL,
 delivery_state TEXT NOT NULL DEFAULT 'disabled', delivery_attempts INTEGER NOT NULL DEFAULT 0,
 delivery_next_attempt_at REAL, delivery_last_attempt_at REAL, delivery_sent_at REAL,
 delivery_error TEXT, delivery_claim TEXT NOT NULL DEFAULT '', delivery_claim_until REAL NOT NULL DEFAULT 0,
 UNIQUE(week_start, timezone)
)
"""
_EMAIL_DEFAULTS = {
    "enabled": False,
    "host": "",
    "port": 587,
    "encryption": "starttls",
    "username": "",
    "password": "",
    "from_address": "",
    "to_addresses": [],
    "timeout_seconds": 10,
}


def settings(cfg: Any) -> dict[str, Any]:
    raw = (snapshot(cfg).get("reports") or {}).get("weekly") or {}
    return {
        "enabled": False,
        "schedule": "0 8 * * 1",
        "timezone": "Europe/Berlin",
        "retention_weeks": 52,
        **raw,
        "email": {**_EMAIL_DEFAULTS, **(raw.get("email") or {})},
    }


def previous_completed_week(
    now: float | datetime | None = None, timezone_name: str = "Europe/Berlin"
) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    current = (
        datetime.now(zone)
        if now is None
        else datetime.fromtimestamp(float(now), zone)
        if not isinstance(now, datetime)
        else now.astimezone(zone)
    )
    end = (current - timedelta(days=current.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return end - timedelta(days=7), end


def _ensure_schema(db: Any) -> None:
    with db.conn() as connection:
        connection.execute("PRAGMA busy_timeout=1000")
        connection.execute(_SCHEMA)


def _json(raw: Any) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _forecast_summary(cfg: Any, db: Any, end: float) -> list[dict[str, Any]]:
    try:
        from .storage_forecast import forecasts

        data = forecasts(cfg, db, now=end)
    except (ImportError, OSError, ValueError):
        return []
    grouped: dict[str, dict[str, Any]] = {}
    for item in data.get("items") or []:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("observed_at"), (float, int))
            or item["observed_at"] > end
        ):
            continue
        kind = item.get("kind") if item.get("kind") in {"local", "remote"} else "other"
        group = grouped.setdefault(
            kind,
            {
                "kind": kind,
                "targets": 0,
                "warning_targets": 0,
                "unknown_targets": 0,
                "minimum_days_to_full": None,
            },
        )
        group["targets"] += 1
        state = item.get("state")
        group["warning_targets"] += int(state in {"warning", "full"})
        group["unknown_targets"] += int(
            state in {"unknown", "insufficient_samples", "uncertain", "stale", "error"}
        )
        days = item.get("days_to_full")
        if (
            isinstance(days, (int, float))
            and not isinstance(days, bool)
            and math.isfinite(days)
            and days >= 0
        ):
            old = group["minimum_days_to_full"]
            group["minimum_days_to_full"] = round(
                min(old, days) if old is not None else days, 1
            )
    return [grouped[key] for key in sorted(grouped)]


def build_summary(
    cfg: Any, db: Any, *, start: datetime, end: datetime
) -> dict[str, Any]:
    """Query only bounded aggregates; no job names, paths, logs or raw errors."""
    root = snapshot(cfg)
    pairs = [
        p
        for p in (root.get("backup") or {}).get("pairs", [])
        if isinstance(p, dict) and p.get("enabled", True)
    ]
    beginning, ending = start.timestamp(), end.timestamp()
    with db.conn() as connection:
        rows = connection.execute(
            "SELECT CASE WHEN historical_restore_warning(kind,status,summary_json)=1 THEN 'warning' ELSE status END AS state, "
            "kind,dry_run,COUNT(*) AS total FROM jobs WHERE ended_at>=? AND ended_at<? GROUP BY state,kind,dry_run",
            (beginning, ending),
        ).fetchall()
        counts = {
            "total": 0,
            "successful": 0,
            "failed": 0,
            "warnings": 0,
            "cancelled": 0,
            "dry_runs": 0,
        }
        restore_tests, verified_restore_tests = 0, 0
        for row in rows:
            amount, state = int(row["total"]), row["state"]
            counts["total"] += amount
            field = {
                "ok": "successful",
                "error": "failed",
                "stale": "failed",
                "warning": "warnings",
                "cancelled": "cancelled",
            }.get(state)
            if field:
                counts[field] += amount
            counts["dry_runs"] += amount if row["dry_run"] else 0
            if row["kind"] == "restoretest":
                restore_tests += amount
                verified_restore_tests += amount if state == "ok" else 0
        protected, valid_proofs = 0, 0
        for pair in pairs[:5000]:
            success = connection.execute(
                "SELECT 1 FROM pair_runs AS p JOIN jobs AS j ON j.id=p.job_id WHERE p.history_key=? "
                "AND p.ok=1 AND p.dry_run=0 AND p.ended_at>=? AND p.ended_at<? "
                "AND j.kind IN ('backup','quicksync') LIMIT 1",
                (rclone_history_key(pair), beginning, ending),
            ).fetchone()
            protected += int(success is not None)
            proof = connection.execute(
                "SELECT * FROM pair_runs WHERE history_key=? AND ended_at<? "
                "ORDER BY ended_at DESC,id DESC LIMIT 1",
                (restore_history_key(pair), ending),
            ).fetchone()
            if proof:
                attempt = {
                    "ok": bool(proof["ok"]),
                    "ended_at": proof["ended_at"],
                    "pair": _json(proof["result_json"]),
                }
                evidence = evaluate_restore_evidence(
                    pair, {"last_result": attempt}, now=ending
                )
                valid_proofs += int(evidence["valid"])
    return {
        "jobs": counts,
        "protection": {
            "configured_paths": len(pairs),
            "paths_with_success": protected,
            "restore_tests": restore_tests,
            "verified_restore_tests": verified_restore_tests,
            "valid_restore_paths": valid_proofs,
        },
        "storage_forecast": _forecast_summary(cfg, db, ending),
        "notes": [
            "Abgeschlossene Läufe werden nach ihrem Abschlusszeitpunkt der Berichtswoche zugeordnet.",
            "Die Datenwegkonfiguration stammt vom Erstellzeitpunkt; Restore-Nachweise werden zum Ende der Berichtswoche bewertet.",
            "Speicherprognosen sind Schätzungen aus vorhandenen Messungen; fehlende Messungen werden nicht als freier Speicher gewertet.",
            "Die Historie folgt der Aufbewahrung des Servers. Der Bericht enthält keine Pfade, Anmeldedaten oder Rohprotokolle.",
        ],
    }


def _date(value: str) -> str:
    return datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m.%Y")


def _period(report: Mapping[str, Any]) -> str:
    last = datetime.strptime(str(report["week_end"]), "%Y-%m-%d") - timedelta(days=1)
    return f"{_date(str(report['week_start']))} bis {last:%d.%m.%Y}"


def render_html(report: Mapping[str, Any]) -> str:
    summary, safe = report["summary"], html.escape
    jobs, protection = summary["jobs"], summary["protection"]
    rows = [
        ("Abgeschlossene Läufe", jobs["total"]),
        ("Erfolgreich", jobs["successful"]),
        ("Fehler", jobs["failed"]),
        ("Hinweise", jobs["warnings"]),
        ("Abgebrochen", jobs["cancelled"]),
        ("Probeläufe", jobs["dry_runs"]),
        ("Aktivierte Datenwege", protection["configured_paths"]),
        (
            "Datenwege mit Sicherungserfolg in der Woche",
            protection["paths_with_success"],
        ),
        ("Restore-Drills", protection["restore_tests"]),
        (
            "Vollständig erfolgreiche Restore-Drills",
            protection["verified_restore_tests"],
        ),
        ("Datenwege mit gültigem Restore-Nachweis", protection["valid_restore_paths"]),
    ]
    table = "".join(
        f"<tr><th scope='row'>{safe(label)}</th><td>{value}</td></tr>"
        for label, value in rows
    )
    forecasts = "".join(
        f"<li>{'Lokale Ziele' if item['kind'] == 'local' else 'Remote-Ziele' if item['kind'] == 'remote' else 'Weitere Ziele'}: "
        f"{item['targets']} Messreihen, {item['warning_targets']} Warnungen, "
        f"{item['unknown_targets']} ohne belastbare Prognose.</li>"
        for item in summary["storage_forecast"]
    )
    forecasts = (
        forecasts
        or "<li>Für diese Berichtswoche liegt keine Speicherprognose vor.</li>"
    )
    notes = "".join(f"<li>{safe(str(note))}</li>" for note in summary["notes"])
    return (
        "<!doctype html><html lang='de'><meta charset='utf-8'><title>Sicherpfad - Wöchentlicher Schutzbericht</title>"
        "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:760px;margin:36px auto;padding:0 20px;color:#16302a}"
        "h1{font-size:28px}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px;border-bottom:1px solid #dbe6e0}"
        "td{text-align:right;font-variant-numeric:tabular-nums}li{margin-bottom:8px}small{color:#50675d}</style>"
        f"<h1>Wöchentlicher Schutzbericht</h1><p>{safe(_period(report))}<br><small>Zeitzone: {safe(str(report['timezone']))}</small></p>"
        f"<h2>Läufe und Nachweise</h2><table>{table}</table><h2>Speicherentwicklung</h2><ul>{forecasts}</ul>"
        f"<h2>Einordnung</h2><ul>{notes}</ul><p><small>Sicherpfad - Bericht ohne betriebliche Pfade und Zugangsdaten.</small></p></html>"
    )


def render_pdf(report: Mapping[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    output = io.BytesIO()
    doc = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title="Sicherpfad - Wöchentlicher Schutzbericht",
        author="Sicherpfad",
        pageCompression=1,
    )
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="ReportTitle",
            fontName="Helvetica-Bold",
            fontSize=25,
            leading=30,
            textColor=colors.HexColor("#133d32"),
            spaceAfter=8 * mm,
        )
    )
    styles.add(
        ParagraphStyle(
            name="ReportBody",
            fontName="Helvetica",
            fontSize=10,
            leading=15,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#253e35"),
            spaceAfter=3 * mm,
        )
    )
    styles.add(
        ParagraphStyle(
            name="ReportNote",
            parent=styles["ReportBody"],
            fontSize=8.5,
            leading=12,
            textColor=colors.HexColor("#54665f"),
        )
    )
    summary, jobs, protection = (
        report["summary"],
        report["summary"]["jobs"],
        report["summary"]["protection"],
    )
    story = [
        Paragraph("Wöchentlicher<br/>Schutzbericht", styles["ReportTitle"]),
        Paragraph(html.escape(_period(report)), styles["ReportBody"]),
        Paragraph(
            f"Zeitzone: {html.escape(str(report['timezone']))}", styles["ReportNote"]
        ),
        Spacer(1, 5 * mm),
    ]
    data = [
        ["Läufe und Nachweise", "Anzahl"],
        ["Abgeschlossene Läufe", jobs["total"]],
        ["Erfolgreich", jobs["successful"]],
        ["Fehler", jobs["failed"]],
        ["Hinweise", jobs["warnings"]],
        ["Abgebrochen", jobs["cancelled"]],
        ["Probeläufe", jobs["dry_runs"]],
        ["Aktivierte Datenwege", protection["configured_paths"]],
        [
            "Datenwege mit Sicherungserfolg in der Woche",
            protection["paths_with_success"],
        ],
        ["Restore-Drills", protection["restore_tests"]],
        [
            "Vollständig erfolgreiche Restore-Drills",
            protection["verified_restore_tests"],
        ],
        ["Datenwege mit gültigem Restore-Nachweis", protection["valid_restore_paths"]],
    ]
    table = Table(data, colWidths=[140 * mm, 30 * mm], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#253e35")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6f0eb")),
                ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#d9e4dd")),
            ]
        )
    )
    story += [
        table,
        Spacer(1, 7 * mm),
        Paragraph("Speicherentwicklung", styles["Heading2"]),
    ]
    for item in summary["storage_forecast"]:
        name = (
            "Lokale Ziele"
            if item["kind"] == "local"
            else "Remote-Ziele"
            if item["kind"] == "remote"
            else "Weitere Ziele"
        )
        days = item["minimum_days_to_full"]
        estimate = (
            "Keine belastbare Füllprognose."
            if days is None
            else f"Früheste geschätzte Füllung: in {str(f'{days:g}').replace('.', ',')} Tagen."
        )
        story.append(
            Paragraph(
                f"<b>{name}:</b> {item['targets']} Messreihen, {item['warning_targets']} Warnungen, "
                f"{item['unknown_targets']} ohne belastbare Prognose. {estimate}",
                styles["ReportBody"],
            )
        )
    if not summary["storage_forecast"]:
        story.append(
            Paragraph(
                "Für diese Berichtswoche liegt keine Speicherprognose vor.",
                styles["ReportBody"],
            )
        )
    story += [Spacer(1, 3 * mm), Paragraph("Einordnung", styles["Heading2"])]
    story += [
        Paragraph(html.escape(str(note)), styles["ReportNote"])
        for note in summary["notes"]
    ]

    def footer(canvas: Any, _doc: Any) -> None:
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#54665f"))
        canvas.drawString(
            20 * mm, 12 * mm, "Sicherpfad | Schutzstatus aus vorhandenen Nachweisen"
        )
        canvas.drawRightString(190 * mm, 12 * mm, f"Seite {canvas.getPageNumber()}")

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    result = output.getvalue()
    if len(result) > _MAX_PDF_BYTES:
        raise ValueError("Bericht überschreitet die PDF-Größengrenze.")
    return result


def _metadata(row: Any) -> dict[str, Any]:
    metadata = {
        key: row[key]
        for key in ("id", "week_start", "week_end", "timezone", "generated_at")
    }
    return metadata | {
        "period_label": _period(metadata),
        "summary": _json(row["summary_json"]),
    }


def _delivery(row: Any) -> dict[str, Any]:
    return {
        "state": row["delivery_state"],
        "attempts": row["delivery_attempts"],
        "next_attempt_at": row["delivery_next_attempt_at"],
        "last_attempt_at": row["delivery_last_attempt_at"],
        "last_success_at": row["delivery_sent_at"],
        "last_error": row["delivery_error"],
    }


def get_report(db: Any, report_id: int, *, pdf: bool = False) -> dict[str, Any] | None:
    _ensure_schema(db)
    with db.conn() as connection:
        row = connection.execute(
            "SELECT * FROM weekly_protection_reports WHERE id=?", (report_id,)
        ).fetchone()
    if row is None:
        return None
    result = {"report": _metadata(row), "delivery": _delivery(row)}
    if pdf:
        result["pdf"] = bytes(row["pdf"])
    return result


def latest(db: Any) -> dict[str, Any]:
    _ensure_schema(db)
    with db.conn() as connection:
        row = connection.execute(
            "SELECT * FROM weekly_protection_reports ORDER BY week_start DESC,id DESC LIMIT 1"
        ).fetchone()
    return {
        "latest": _metadata(row) if row else None,
        "delivery": _delivery(row) if row else None,
    }


def history(db: Any, *, limit: int = 52) -> dict[str, Any]:
    _ensure_schema(db)
    with db.conn() as connection:
        rows = connection.execute(
            "SELECT * FROM weekly_protection_reports ORDER BY week_start DESC,id DESC LIMIT ?",
            (max(1, min(int(limit), 104)),),
        ).fetchall()
        total = connection.execute(
            "SELECT COUNT(*) FROM weekly_protection_reports"
        ).fetchone()[0]
    return {"items": [_metadata(row) for row in rows], "total": total}


def generate(
    cfg: Any, db: Any, *, now: float | None = None, period_at: float | None = None
) -> dict[str, Any]:
    raw = settings(cfg)
    current = time.time() if now is None else float(now)
    start, end = previous_completed_week(
        current if period_at is None else period_at, raw["timezone"]
    )
    _ensure_schema(db)
    with db.conn() as connection:
        row = connection.execute(
            "SELECT * FROM weekly_protection_reports WHERE week_start=? AND timezone=?",
            (start.date().isoformat(), raw["timezone"]),
        ).fetchone()
    if row:
        return {"ok": True, "report": _metadata(row), "delivery": _delivery(row)}
    metadata = {
        "week_start": start.date().isoformat(),
        "week_end": end.date().isoformat(),
        "timezone": raw["timezone"],
        "generated_at": current,
        "summary": build_summary(cfg, db, start=start, end=end),
    }
    pdf = render_pdf(metadata)
    with db.conn() as connection:
        connection.execute("PRAGMA busy_timeout=1000")
        connection.execute(
            "INSERT OR IGNORE INTO weekly_protection_reports(week_start,week_end,timezone,generated_at,summary_json,pdf) VALUES (?,?,?,?,?,?)",
            (
                metadata["week_start"],
                metadata["week_end"],
                metadata["timezone"],
                current,
                json.dumps(metadata["summary"], ensure_ascii=False),
                pdf,
            ),
        )
        row = connection.execute(
            "SELECT * FROM weekly_protection_reports WHERE week_start=? AND timezone=?",
            (metadata["week_start"], raw["timezone"]),
        ).fetchone()
        keep = max(4, min(int(raw["retention_weeks"]), 104))
        connection.execute(
            "DELETE FROM weekly_protection_reports WHERE id NOT IN (SELECT id FROM weekly_protection_reports ORDER BY week_start DESC,id DESC LIMIT ?) AND delivery_claim_until<=?",
            (keep, current),
        )
    return {"ok": True, "report": _metadata(row), "delivery": _delivery(row)}


def validate_email(raw: Mapping[str, Any]) -> None:
    host = str(raw.get("host") or "")
    if (
        not host
        or len(host) > 253
        or any(ord(c) <= 32 or ord(c) == 127 for c in host)
        or any(c in host for c in "/\\@?#")
    ):
        raise ValueError("SMTP-Servername ist ungültig.")
    if raw.get("encryption") not in {"tls", "starttls"}:
        raise ValueError("SMTP benötigt TLS oder STARTTLS mit Zertifikatsprüfung.")
    if not 1 <= int(raw.get("port") or 0) <= 65535:
        raise ValueError("SMTP-Port ist ungültig.")
    recipients = raw.get("to_addresses")
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= 10:
        raise ValueError(
            "Mindestens eine und höchstens zehn Empfängeradressen angeben."
        )
    for address in [raw.get("from_address"), *recipients]:
        if (
            not isinstance(address, str)
            or len(address) > 320
            or not _ADDRESS.fullmatch(address)
            or ".." in address
            or any(ord(c) <= 32 or ord(c) == 127 for c in address)
        ):
            raise ValueError(
                "E-Mail-Adressen müssen einfache Adressen ohne Anzeigenamen oder Steuerzeichen sein."
            )
    username = str(raw.get("username") or "")
    if len(username) > 320 or any(ord(c) < 32 or ord(c) == 127 for c in username):
        raise ValueError("SMTP-Benutzername ist ungültig.")
    if len(str(raw.get("password") or "")) > 4096:
        raise ValueError("SMTP-Passwort ist zu lang.")
    if not 1 <= float(raw.get("timeout_seconds", 10)) <= 30:
        raise ValueError("SMTP-Zeitlimit muss zwischen 1 und 30 Sekunden liegen.")


class _PinnedSMTP(smtplib.SMTP):
    def __init__(self, host: str, address: str, *, timeout: float, implicit_tls: bool):
        super().__init__(timeout=timeout)
        self._host = host
        self.address = address
        self.implicit_tls = implicit_tls

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        raw = socket.create_connection((self.address, port), timeout=timeout)
        if not self.implicit_tls:
            return raw
        try:
            return ssl.create_default_context().wrap_socket(
                raw, server_hostname=self._host
            )
        except BaseException:
            raw.close()
            raise


def _send_email(raw: Mapping[str, Any], report: Mapping[str, Any], pdf: bytes) -> None:
    validate_email(raw)
    timeout = float(raw.get("timeout_seconds", 10))
    deadline = time.monotonic() + timeout
    host = str(raw["host"])
    address = resolve_host(host, int(raw["port"]), timeout, public_only=False)
    remaining = max(0.01, deadline - time.monotonic())
    client = _PinnedSMTP(
        host, address, timeout=remaining, implicit_tls=raw["encryption"] == "tls"
    )

    def abort() -> None:
        sock = client.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        client.close()

    timer = threading.Timer(remaining, abort)
    timer.daemon = True
    timer.start()
    try:
        client.connect(host, int(raw["port"]))
        client.ehlo()
        if raw["encryption"] == "starttls":
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        if raw.get("username"):
            client.login(str(raw["username"]), str(raw.get("password") or ""))
        message = EmailMessage(policy=SMTP)
        message["Subject"] = f"Sicherpfad - Schutzbericht {_period(report)}"
        message["From"] = str(raw["from_address"])
        message["To"] = ", ".join(raw["to_addresses"])
        message["Date"] = formatdate(float(report["generated_at"]), usegmt=True)
        digest = hashlib.sha256(
            f"{report['week_start']}:{report['timezone']}:{report['id']}".encode()
        ).hexdigest()[:32]
        message["Message-ID"] = (
            f"<sicherpfad-{digest}@{str(raw['from_address']).rsplit('@', 1)[1]}>"
        )
        message.set_content(
            f"Wöchentlicher Schutzbericht für {_period(report)}.\n\nDer vollständige Bericht steht im PDF-Anhang.\nEr enthält keine Pfade, Anmeldedaten oder Rohprotokolle."
        )
        message.add_alternative(render_html(report), subtype="html")
        message.add_attachment(
            pdf,
            maintype="application",
            subtype="pdf",
            filename=f"sicherpfad-wochenbericht-{report['week_start']}.pdf",
        )
        client.send_message(
            message,
            from_addr=str(raw["from_address"]),
            to_addrs=list(raw["to_addresses"]),
        )
    finally:
        timer.cancel()
        client.close()


def send(
    cfg: Any, db: Any, report_id: int, *, now: float | None = None, manual: bool = False
) -> dict[str, Any]:
    raw = settings(cfg)["email"]
    current = time.time() if now is None else float(now)
    stored = get_report(db, report_id, pdf=True)
    if not stored:
        raise KeyError("Bericht nicht gefunden.")
    if raw["enabled"] is not True or is_locked_down(db):
        return {
            "ok": False,
            "delivery": stored["delivery"],
            "blocked_by_lockdown": is_locked_down(db),
        }
    validate_email(raw)
    claim = secrets.token_hex(16)
    with db.conn() as connection:
        connection.execute("PRAGMA busy_timeout=1000")
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM weekly_protection_reports WHERE id=?", (report_id,)
        ).fetchone()
        state = row["delivery_state"]
        if (
            not manual
            and state == "sending"
            and row["delivery_claim_until"] <= current
            and row["delivery_attempts"] >= _MAX_ATTEMPTS
        ):
            connection.execute(
                "UPDATE weekly_protection_reports SET delivery_state='failed',delivery_error=?,"
                "delivery_claim='',delivery_claim_until=0,delivery_next_attempt_at=NULL WHERE id=?",
                (
                    "Der letzte Sendeversuch wurde nicht abgeschlossen. Zustellung ist unbekannt.",
                    report_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM weekly_protection_reports WHERE id=?", (report_id,)
            ).fetchone()
            return {"ok": False, "delivery": _delivery(row)}
        if (
            state == "sent"
            or row["delivery_claim_until"] > current
            or (
                not manual
                and (
                    state not in {"pending", "retrying", "sending"}
                    or (row["delivery_next_attempt_at"] or 0) > current
                    or row["delivery_attempts"] >= _MAX_ATTEMPTS
                )
            )
        ):
            return {"ok": state == "sent", "delivery": _delivery(row)}
        attempts = (
            int(row["delivery_attempts"]) if not manual or state != "failed" else 0
        ) + 1
        connection.execute(
            "UPDATE weekly_protection_reports SET delivery_state='sending',delivery_claim=?,delivery_claim_until=?,delivery_attempts=?,delivery_last_attempt_at=? WHERE id=?",
            (claim, current + 60, attempts, current, report_id),
        )
    error = None
    try:
        _send_email(raw, stored["report"], stored["pdf"])
    except ssl.SSLCertVerificationError:
        error = "TLS-Zertifikat des SMTP-Servers ist nicht vertrauenswürdig."
    except smtplib.SMTPAuthenticationError:
        error = "SMTP-Anmeldung wurde abgewiesen."
    except (TimeoutError, socket.timeout):
        error = "Zeitüberschreitung beim E-Mail-Versand."
    except (OSError, smtplib.SMTPException, ssl.SSLError):
        error = "E-Mail konnte nicht über eine sichere SMTP-Verbindung gesendet werden."
    except Exception:
        error = "E-Mail-Versand fehlgeschlagen."
    state = (
        "sent"
        if error is None
        else "failed"
        if attempts >= _MAX_ATTEMPTS
        else "retrying"
    )
    next_attempt = (
        current + min(86400, 300 * 2 ** min(attempts - 1, 8))
        if state == "retrying"
        else None
    )
    with db.conn() as connection:
        connection.execute(
            "UPDATE weekly_protection_reports SET delivery_state=?,delivery_error=?,delivery_sent_at=?,delivery_next_attempt_at=?,delivery_claim='',delivery_claim_until=0 WHERE id=? AND delivery_claim=?",
            (
                state,
                error,
                current if not error else None,
                next_attempt,
                report_id,
                claim,
            ),
        )
    return {"ok": error is None, "delivery": get_report(db, report_id)["delivery"]}


def tick(cfg: Any, db: Any, *, now: float | None = None) -> dict[str, Any]:
    raw = settings(cfg)
    if raw["enabled"] is not True or is_locked_down(db):
        return {"enabled": raw["enabled"] is True, "generated": False, "sent": False}
    current = time.time() if now is None else float(now)
    local = datetime.fromtimestamp(current, ZoneInfo(raw["timezone"]))
    slot = croniter(
        raw["schedule"],
        local.replace(second=0, microsecond=0) + timedelta(microseconds=1),
    ).get_prev(datetime)
    generated = generate(cfg, db, now=current, period_at=slot.timestamp())
    identifier = generated["report"]["id"]
    if raw["email"]["enabled"] is not True:
        return {
            "enabled": True,
            "generated": True,
            "report_id": identifier,
            "sent": False,
        }
    with db.conn() as connection:
        connection.execute(
            "UPDATE weekly_protection_reports SET delivery_state='pending',delivery_next_attempt_at=? WHERE id=? AND delivery_state='disabled'",
            (current, identifier),
        )
        pending = connection.execute(
            "SELECT id FROM weekly_protection_reports WHERE delivery_state IN ('pending','retrying','sending') AND COALESCE(delivery_next_attempt_at,0)<=? AND delivery_claim_until<=? ORDER BY delivery_next_attempt_at,id LIMIT 1",
            (current, current),
        ).fetchone()
    result = (
        send(cfg, db, int(pending["id"]), now=current) if pending else {"ok": False}
    )
    return {
        "enabled": True,
        "generated": True,
        "report_id": identifier,
        "sent": result["ok"],
    }


def run_operational_tick(
    cfg: Any, db: Any, *, now: float | None = None
) -> dict[str, Any]:
    """Hook for a dedicated minutely service; backup jobs must not call it inline."""
    from . import heartbeat

    result = {}
    for name, callback in (("heartbeat", heartbeat.tick), ("weekly_report", tick)):
        try:
            result[name] = callback(cfg, db, now=now)
        except Exception:
            result[name] = {
                "ok": False,
                "error": "Betriebsprüfung konnte nicht abgeschlossen werden.",
            }
    return result
