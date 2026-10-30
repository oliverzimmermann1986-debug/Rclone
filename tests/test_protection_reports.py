import base64
import json
import re
import ssl
import sys
from types import SimpleNamespace
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from app import protection_reports as reports
from app.db import Database
from app.jobs.scheduler import rclone_history_key
from app.restore_evidence import pair_binding, restore_history_key

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def database(tmp_path):
    return Database(tmp_path / "reports.db")


def config(**weekly):
    return {"backup": {"pairs": []}, "reports": {"weekly": weekly}}


def smtp(**changes):
    return {
        "enabled": True,
        "host": "smtp.example.org",
        "port": 587,
        "encryption": "starttls",
        "username": "operator",
        "password": "secret-canary",
        "from_address": "backup@example.org",
        "to_addresses": ["owner@example.org"],
        "timeout_seconds": 5,
        **changes,
    }


def test_week_boundaries_follow_local_timezone_and_dst():
    start, end = reports.previous_completed_week(NOW)
    assert (start.date().isoformat(), end.date().isoformat()) == (
        "2026-10-19",
        "2026-10-26",
    )
    assert end.timestamp() - start.timestamp() == 169 * 3600
    start, end = reports.previous_completed_week(
        datetime(2026, 4, 2, tzinfo=timezone.utc)
    )
    assert end.timestamp() - start.timestamp() == 167 * 3600
    start, end = reports.previous_completed_week(
        datetime(2026, 10, 26, 0, 30, tzinfo=timezone.utc), "America/New_York"
    )
    assert end.date().isoformat() == "2026-10-19"


def test_generation_uses_completed_week_and_never_sends(database, monkeypatch):
    monkeypatch.setattr(
        reports,
        "_send_email",
        lambda *_: pytest.fail("manual generation must not send"),
    )
    with database.conn() as con:
        con.executemany(
            "INSERT INTO jobs(kind,status,started_at,ended_at,summary_json,dry_run) VALUES(?,?,?,?,?,?)",
            [
                (
                    "backup",
                    "ok",
                    1792455000,
                    1792458000,
                    json.dumps({"password": "CANARY", "path": "/private/secret"}),
                    0,
                ),
                ("backup", "error", 1792455000, 1792458100, "{}", 0),
                ("backup", "ok", NOW - 100, NOW, "{}", 0),
                ("backup", "running", 1792455000, None, "{}", 0),
            ],
        )
    created = reports.generate(config(email=smtp()), database, now=NOW)
    counts = created["report"]["summary"]["jobs"]
    assert counts["total"] == 2 and counts["successful"] == 1 and counts["failed"] == 1
    serialized = json.dumps(created)
    assert "CANARY" not in serialized and "/private/secret" not in serialized
    assert created["delivery"]["state"] == "disabled"
    same = reports.generate(config(email=smtp()), Database(database.path), now=NOW + 60)
    assert same == created


def test_concurrent_generation_has_one_durable_report(database):
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(
            pool.map(
                lambda _: reports.generate(config(), Database(database.path), now=NOW),
                range(3),
            )
        )
    assert len({r["report"]["id"] for r in results}) == 1
    assert reports.history(database)["total"] == 1


def test_protection_evidence_is_bound_to_current_path_and_week_end(database):
    pair = {
        "id": "fixture-path",
        "name": "CANARY-NAME",
        "enabled": True,
        "local": "/private/source",
        "remote": "cloud:/private/target",
        "direction": "push",
        "mode": "copy",
    }
    start, end = reports.previous_completed_week(NOW)
    checked = end.timestamp() - 3600
    proof = {
        "ok": True,
        "evidence_binding": pair_binding(pair),
        "evidence_checked_at": checked,
        "sample_status": "complete",
        "verified": 3,
        "sample_size": 3,
    }
    with database.conn() as connection:
        for identifier, kind, ended in [
            (1, "backup", checked),
            (2, "restoretest", checked),
            (3, "restoretest", NOW),
        ]:
            connection.execute(
                "INSERT INTO jobs(id,kind,status,started_at,ended_at) VALUES(?,?,'ok',?,?)",
                (identifier, kind, ended - 10, ended),
            )
        for identifier, key, result in [
            (1, rclone_history_key(pair), {}),
            (2, restore_history_key(pair), proof),
            (3, restore_history_key(pair), {"error": "future failure"}),
        ]:
            ended = checked if identifier < 3 else NOW
            connection.execute(
                "INSERT INTO pair_runs(job_id,pair_name,history_key,ok,status,started_at,ended_at,result_json) VALUES(?,?,?,1,'ok',?,?,?)",
                (identifier, pair["name"], key, ended - 10, ended, json.dumps(result)),
            )
    cfg = {"backup": {"pairs": [pair]}}
    result = reports.build_summary(cfg, database, start=start, end=end)
    assert result["protection"]["paths_with_success"] == 1
    assert result["protection"]["valid_restore_paths"] == 1
    assert "CANARY" not in json.dumps(result) and "/private/" not in json.dumps(result)
    pair["remote"] = "other:/target"
    assert (
        reports.build_summary(cfg, database, start=start, end=end)["protection"][
            "valid_restore_paths"
        ]
        == 0
    )


def test_real_pdf_and_html_are_readable_path_free_and_bounded(database):
    created = reports.generate(config(), database, now=NOW)
    stored = reports.get_report(database, created["report"]["id"], pdf=True)
    pdf = stored["pdf"]
    assert pdf.startswith(b"%PDF-") and len(pdf) < 1024 * 1024
    content = []
    for stream in re.findall(rb"stream\r?\n(.*?)endstream", pdf, re.S):
        try:
            content.append(
                zlib.decompress(base64.a85decode(stream.strip(), adobe=True))
            )
        except (ValueError, zlib.error):
            pass
    text = b"\n".join(content)
    assert b"Schutzbericht" in text and b"19.10.2026" in text
    assert 1 <= len(re.findall(rb"/Type\s*/Page\b", pdf)) <= 2
    html = reports.render_html(created["report"])
    assert "lang='de'" in html and "Abgeschlossene Läufe" in html
    assert "19.10.2026 bis 25.10.2026" in html


def test_retention_is_bounded_and_pdf_remains_available(database):
    identifiers = []
    for n in range(7):
        made = reports.generate(
            config(retention_weeks=4), database, now=NOW + n * 7 * 86400
        )
        identifiers.append(made["report"]["id"])
    assert reports.history(database)["total"] == 4
    assert reports.get_report(database, identifiers[0]) is None
    assert reports.get_report(database, identifiers[-1], pdf=True)["pdf"].startswith(
        b"%PDF"
    )


def test_tick_disabled_and_lockdown_make_no_network_calls(database, monkeypatch):
    monkeypatch.setattr(
        reports, "_send_email", lambda *_: pytest.fail("disabled integration")
    )
    assert not reports.tick(config(), database, now=NOW)["generated"]
    database.runtime_set("lockdown", {"active": True})
    assert not reports.tick(config(enabled=True, email=smtp()), database, now=NOW)[
        "generated"
    ]


def test_weekly_tick_deduplicates_and_retries_smtp_durably(database, monkeypatch):
    calls = []

    def send(_settings, _report, _pdf):
        calls.append(True)
        if len(calls) == 1:
            raise OSError("secret-canary")

    monkeypatch.setattr(reports, "_send_email", send)
    cfg = config(enabled=True, email=smtp())
    reports.tick(cfg, database, now=NOW)
    first = reports.latest(database)
    assert first["delivery"]["state"] == "retrying"
    assert "secret-canary" not in json.dumps(first)
    reports.tick(cfg, Database(database.path), now=NOW + 60)
    assert len(calls) == 1
    reports.tick(cfg, database, now=NOW + 300)
    assert len(calls) == 2 and reports.latest(database)["delivery"]["state"] == "sent"
    reports.tick(cfg, database, now=NOW + 3600)
    assert len(calls) == 2


def test_concurrent_email_dispatch_has_one_lease(database, monkeypatch):
    cfg = config(email=smtp())
    made = reports.generate(cfg, database, now=NOW)
    calls = []
    monkeypatch.setattr(reports, "_send_email", lambda *_: calls.append(True))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda _: reports.send(
                    cfg,
                    Database(database.path),
                    made["report"]["id"],
                    now=NOW,
                    manual=True,
                ),
                range(4),
            )
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"from_address": "safe@example.org\r\nBcc: attacker@example.org"},
        {"to_addresses": ["a@example.org\nBcc:b@example.org"]},
        {"host": "smtp.example.org/evil"},
        {"encryption": "none"},
        {"username": "name\r\nAUTH"},
    ],
)
def test_smtp_settings_reject_injection_and_plaintext(changes):
    with pytest.raises(ValueError):
        reports.validate_email(smtp(**changes))


def test_starttls_precedes_credentials_and_tls_failure_never_sends(
    database, monkeypatch
):
    order = []

    class SMTP:
        sock = None

        def __init__(self, *a, **kw):
            pass

        def connect(self, *a):
            order.append("connect")

        def ehlo(self):
            order.append("ehlo")

        def starttls(self, context):
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            order.append("tls")
            raise ssl.SSLCertVerificationError("private SMTP detail")

        def login(self, *a):
            pytest.fail("no authentication before trusted TLS")

        def send_message(self, *a, **kw):
            pytest.fail("TLS must succeed first")

        def close(self):
            pass

    monkeypatch.setattr(reports, "resolve_host", lambda *_a, **_k: "203.0.113.1")
    monkeypatch.setattr(reports, "_PinnedSMTP", SMTP)
    cfg = config(email=smtp())
    made = reports.generate(cfg, database, now=NOW)
    result = reports.send(cfg, database, made["report"]["id"], now=NOW, manual=True)
    assert order == ["connect", "ehlo", "tls"]
    assert result["delivery"]["state"] == "retrying"
    assert "private SMTP detail" not in json.dumps(result)


def test_successful_smtp_message_contains_real_pdf_and_no_credentials(
    database, monkeypatch
):
    order = []
    sent = []

    class SMTP:
        sock = None

        def __init__(self, *a, **kw):
            pass

        def connect(self, *a):
            order.append("connect")

        def ehlo(self):
            order.append("ehlo")

        def starttls(self, context):
            order.append("tls")

        def login(self, *a):
            order.append("login")

        def send_message(self, message, **kwargs):
            order.append("send")
            sent.append((message, kwargs))

        def close(self):
            pass

    monkeypatch.setattr(reports, "resolve_host", lambda *_a, **_k: "203.0.113.1")
    monkeypatch.setattr(reports, "_PinnedSMTP", SMTP)
    cfg = config(email=smtp())
    made = reports.generate(cfg, database, now=NOW)
    assert reports.send(cfg, database, made["report"]["id"], now=NOW, manual=True)["ok"]
    assert order == ["connect", "ehlo", "tls", "ehlo", "login", "send"]
    message, envelope = sent[0]
    attachment = next(message.iter_attachments())
    assert (
        attachment.get_content_type() == "application/pdf"
        and attachment.get_payload(decode=True).startswith(b"%PDF-")
    )
    assert envelope["to_addrs"] == ["owner@example.org"]
    assert "secret-canary" not in message.as_string()


def test_expired_last_delivery_lease_becomes_explicit_failure(database, monkeypatch):
    cfg = config(email=smtp())
    made = reports.generate(cfg, database, now=NOW)
    with database.conn() as con:
        con.execute(
            "UPDATE weekly_protection_reports SET delivery_state='sending',delivery_attempts=5,delivery_claim='crashed',delivery_claim_until=?",
            (NOW - 1,),
        )
    monkeypatch.setattr(
        reports, "_send_email", lambda *_: pytest.fail("retry budget exhausted")
    )
    result = reports.send(cfg, database, made["report"]["id"], now=NOW)
    assert result["delivery"]["state"] == "failed"
    assert "unbekannt" in result["delivery"]["last_error"]


def test_forecast_summary_excludes_future_measurements_and_target_labels(
    database, monkeypatch
):
    sample = {
        "items": [
            {
                "kind": "local",
                "label": "/private/CANARY",
                "observed_at": 100,
                "state": "warning",
                "days_to_full": 12.25,
            },
            {
                "kind": "remote",
                "label": "private bucket",
                "observed_at": 300,
                "state": "full",
                "days_to_full": 0,
            },
        ]
    }
    monkeypatch.setitem(
        sys.modules,
        "app.storage_forecast",
        SimpleNamespace(forecasts=lambda *_a, **_k: sample),
    )
    result = reports._forecast_summary(config(), database, 200)
    assert result == [
        {
            "kind": "local",
            "targets": 1,
            "warning_targets": 1,
            "unknown_targets": 0,
            "minimum_days_to_full": 12.2,
        }
    ]
    assert "CANARY" not in json.dumps(result)
