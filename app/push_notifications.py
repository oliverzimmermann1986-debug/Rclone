"""Durable APNs delivery for authenticated native iPhone notifications."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx
import jwt

from .config_store import get_config
from .db import Database, get_db
from .rclone_args import redact_command_text

logger = logging.getLogger(__name__)

DEFAULT_ERROR_EVENTS = (
    "sync_error",
    "mount_check_failed",
    "pair_overdue",
    "restore_test_error",
)
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: tuple[tuple[str, str, str, int], str, float] | None = None
_BACKGROUND_DISPATCH_LOCK = threading.Lock()
_DELIVERY_FENCE = threading.RLock()
_CLAIM_HEARTBEAT_INTERVAL_SECONDS = 5.0
_INVALID_TOKEN_REASONS = {
    "BadDeviceToken",
    "DeviceTokenNotForTopic",
    "Unregistered",
}


def revoke_all_push_devices(*, db: Database | None = None) -> int:
    """Fence APNs I/O and revoke all registrations for a session-version change."""

    database = db or get_db()
    with _DELIVERY_FENCE:
        try:
            return database.push_devices_revoke_all()
        except Exception as exc:
            raise OSError("APNs-Geräteregistrierungen konnten nicht widerrufen werden") from exc


def _post_with_claim_heartbeat(
    client: httpx.Client,
    url: str,
    *,
    content: bytes,
    headers: Mapping[str, str],
    database: Database,
    row_id: int,
    claim_owner: str,
    lease_seconds: int,
) -> httpx.Response:
    """Keep an owner-bound outbox claim alive for the full blocking POST."""

    done = threading.Event()
    result: dict[str, Any] = {}

    def post() -> None:
        try:
            result["response"] = client.post(url, content=content, headers=headers)
        except BaseException as exc:  # re-raised unchanged on the dispatcher thread
            result["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=post, name=f"apns-post-{row_id}", daemon=True)
    worker.start()
    interval = max(
        0.01,
        min(float(_CLAIM_HEARTBEAT_INTERVAL_SECONDS), float(lease_seconds) / 3.0),
    )
    while not done.wait(interval):
        renewed = database.push_outbox_renew_claims(
            [row_id],
            claim_owner=claim_owner,
            lease_seconds=lease_seconds,
        )
        if row_id not in renewed:
            logger.error(
                "APNs-Outbox-Lease %s von Dispatcher %s ging während POST verloren",
                row_id,
                claim_owner,
            )
    worker.join()
    error = result.get("error")
    if error is not None:
        raise error
    return result["response"]
_RETRYABLE_STATUS_CODES = {429, 500, 503}
_RETRYABLE_REASONS = {"ExpiredProviderToken", "TooManyProviderTokenUpdates"}
_SENSITIVE_CONTEXT_KEYS = {
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "password",
    "private_key",
    "secret",
    "token",
}


def _settings(event: str) -> dict[str, Any] | None:
    raw = get_config().get("notifications", "apns", default={}) or {}
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return None
    events = raw.get("events") or DEFAULT_ERROR_EVENTS
    if event not in events:
        return None
    try:
        timeout = max(1.0, min(float(raw.get("timeout_seconds") or 10), 30.0))
        retention_hours = max(1.0, min(float(raw.get("retention_hours") or 24), 168.0))
        max_attempts = max(1, min(int(raw.get("max_attempts") or 8), 20))
    except (TypeError, ValueError, OverflowError):
        logger.warning("APNs Timeout-/Retry-Konfiguration ist ungültig")
        return None
    settings = {
        "team_id": str(raw.get("team_id") or "").strip(),
        "key_id": str(raw.get("key_id") or "").strip(),
        "key_file": str(raw.get("key_file") or "").strip(),
        "topic": str(raw.get("topic") or "de.oliverzimmermann.rclonesync").strip(),
        "timeout": timeout,
        "retention_seconds": int(retention_hours * 3600),
        "max_attempts": max_attempts,
    }
    if not all(settings[key] for key in ("team_id", "key_id", "key_file", "topic")):
        logger.warning(
            "APNs ist aktiviert, aber Team-ID, Key-ID, Key-Datei oder Topic fehlt"
        )
        return None
    return settings


def _provider_token(settings: dict[str, Any], *, now: float | None = None) -> str:
    global _TOKEN_CACHE
    now_value = float(time.time() if now is None else now)
    key_path = Path(str(settings["key_file"])).expanduser()
    key_stat = key_path.stat()
    if key_stat.st_size > 16 * 1024:
        raise ValueError("APNs-Schlüsseldatei ist unerwartet groß")
    if os.name != "nt" and stat.S_IMODE(key_stat.st_mode) & 0o077:
        raise PermissionError(
            "APNs-Schlüsseldatei darf nur für den Besitzer lesbar sein"
        )
    cache_key = (
        str(settings["team_id"]),
        str(settings["key_id"]),
        str(key_path.resolve()),
        int(key_stat.st_mtime_ns),
    )
    with _TOKEN_LOCK:
        if (
            _TOKEN_CACHE
            and _TOKEN_CACHE[0] == cache_key
            and now_value - _TOKEN_CACHE[2] < 3_000
        ):
            return _TOKEN_CACHE[1]
        private_key = key_path.read_text(encoding="utf-8")
        token = jwt.encode(
            {"iss": settings["team_id"], "iat": int(now_value)},
            private_key,
            algorithm="ES256",
            headers={"kid": settings["key_id"]},
        )
        _TOKEN_CACHE = (cache_key, token, now_value)
        return token


def _reset_provider_token() -> None:
    global _TOKEN_CACHE
    with _TOKEN_LOCK:
        _TOKEN_CACHE = None


def _safe_context(value: Any, *, depth: int = 0) -> Any:
    if depth > 3:
        return None
    if isinstance(value, str):
        return redact_command_text(value)[:500]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        return [_safe_context(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:30]:
            key = str(raw_key)[:80]
            if key.casefold() in _SENSITIVE_CONTEXT_KEYS:
                continue
            result[key] = _safe_context(item, depth=depth + 1)
        return result
    return str(value)[:500]


def _push_context(extra: Mapping[str, Any]) -> dict[str, Any]:
    """Reduziert große Job-Summaries auf navigierbare, unkritische Felder."""

    summary = extra.get("summary")
    source: Mapping[str, Any] = summary if isinstance(summary, Mapping) else extra
    context: dict[str, Any] = {}
    for key in (
        "job_id",
        "run_id",
        "definition_id",
        "definition_name",
        "scheduled_slot",
        "trigger",
        "pair",
        "pairs",
    ):
        value = source.get(key, extra.get(key))
        if value not in (None, "", []):
            context[key] = _safe_context(value)
    return context


def notification_dedupe_key(
    event: str,
    title: str,
    message: str,
    extra: Mapping[str, Any],
    *,
    now: float | None = None,
) -> str:
    context = _push_context(extra)
    identity = {
        key: context[key]
        for key in (
            "job_id",
            "run_id",
            "definition_id",
            "scheduled_slot",
            "pair",
            "pairs",
        )
        if key in context
    }
    if not identity:
        # Identische statuslose Alarme werden in einem Fünf-Minuten-Fenster
        # zusammengefasst, bleiben in späteren Störfällen aber erneut sichtbar.
        now_value = float(time.time() if now is None else now)
        identity = {
            "content": hashlib.sha256(
                f"{title}\0{message}".encode("utf-8", errors="replace")
            ).hexdigest()[:24],
            "bucket": int(now_value // 300),
        }
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{event}:{digest}"


def _payload(row: Mapping[str, Any]) -> bytes:
    context = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    payload = {
        "aps": {
            "alert": {
                "title": str(row.get("title") or "")[:120],
                "body": str(row.get("message") or "")[:900],
            },
            "sound": "default",
            "thread-id": "rclone-errors",
        },
        "event": str(row.get("event") or ""),
        **dict(context),
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(body) <= 4_096:
        return body
    payload = {"aps": payload["aps"], "event": payload["event"]}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _response_reason(response: Any) -> str:
    try:
        value = response.json()
    except (ValueError, AttributeError):
        return ""
    return str(value.get("reason") or "") if isinstance(value, Mapping) else ""


def _retry_delay(row: Mapping[str, Any], response: Any | None = None) -> int:
    attempts = max(1, int(row.get("attempts") or 1))
    delay = min(3600, 30 * (2 ** min(attempts - 1, 7)))
    if response is not None:
        headers = getattr(response, "headers", {}) or {}
        try:
            delay = max(delay, int(headers.get("retry-after") or 0))
        except (TypeError, ValueError, AttributeError):
            pass
    return delay


def dispatch_pending_pushes(
    *,
    db: Database | None = None,
    client_factory: Callable[..., httpx.Client] = httpx.Client,
    limit: int = 32,
    claim_owner: str | None = None,
    lease_seconds: int | None = None,
) -> dict[str, int]:
    """Claimt fällige Outbox-Zeilen atomar und stellt sie best-effort zu."""

    database = db or get_db()
    owner = str(claim_owner or uuid.uuid4().hex)
    database.push_device_prune_expired()
    requested_lease = (
        max(10, min(int(lease_seconds), 300)) if lease_seconds is not None else 60
    )
    rows = database.push_outbox_claim_due(
        claim_owner=owner,
        limit=limit,
        lease_seconds=requested_lease,
    )
    result = {"sent": 0, "failed": 0, "removed": 0, "retrying": 0}
    if not rows:
        return result

    row_settings = [_settings(str(row.get("event") or "")) for row in rows]
    timeouts = [float(item["timeout"]) for item in row_settings if item]
    client_timeout = max(timeouts, default=10.0)
    active_lease_seconds = (
        requested_lease
        if lease_seconds is not None
        else max(60, min(300, int(client_timeout * 2) + 10))
    )
    with client_factory(http2=True, timeout=client_timeout) as client:
        for index, (row, settings) in enumerate(zip(rows, row_settings)):
            row_id = int(row["id"])
            remaining_ids = [int(item["id"]) for item in rows[index:]]
            renewed_ids = set(
                database.push_outbox_renew_claims(
                    remaining_ids,
                    claim_owner=owner,
                    lease_seconds=active_lease_seconds,
                )
            )
            if row_id not in renewed_ids:
                logger.info(
                    "APNs-Outbox-Zeile %s gehört nicht mehr Dispatcher %s",
                    row_id,
                    owner,
                )
                continue
            if settings is None:
                status = database.push_outbox_finish(
                    row_id,
                    claim_owner=owner,
                    sent=False,
                    error="APNs ist für dieses Event nicht mehr konfiguriert",
                )
                if status == "failed":
                    result["failed"] += 1
                continue
            token = str(row["token"])
            host = (
                "https://api.sandbox.push.apple.com"
                if row["environment"] == "sandbox"
                else "https://api.push.apple.com"
            )
            collapse_id = hashlib.sha256(
                str(row["dedupe_key"]).encode("utf-8")
            ).hexdigest()[:64]
            try:
                headers = {
                    "authorization": f"bearer {_provider_token(settings)}",
                    "apns-topic": str(settings["topic"]),
                    "apns-push-type": "alert",
                    "apns-priority": "10",
                    "apns-expiration": str(int(float(row["expires_at"]))),
                    "apns-collapse-id": collapse_id,
                    "apns-id": str(row["apns_id"]),
                    "content-type": "application/json",
                }
            except (OSError, ValueError, PermissionError, jwt.PyJWTError) as exc:
                status = database.push_outbox_finish(
                    row_id,
                    claim_owner=owner,
                    sent=False,
                    retry=True,
                    retry_delay_seconds=_retry_delay(row),
                    error=str(exc),
                    max_attempts=int(settings.get("max_attempts") or 8),
                )
                if status == "pending":
                    result["retrying"] += 1
                elif status == "failed":
                    result["failed"] += 1
                logger.warning("APNs Provider-Token fehlgeschlagen: %s", exc)
                continue
            try:
                with _DELIVERY_FENCE:
                    if not database.push_device_exists(token):
                        logger.info(
                            "APNs-Gerät %s wurde vor Zustellung widerrufen", token[-8:]
                        )
                        database.push_outbox_finish(
                            row_id,
                            claim_owner=owner,
                            sent=False,
                            error="APNs-Gerät wurde widerrufen",
                        )
                        continue
                    response = _post_with_claim_heartbeat(
                        client,
                        f"{host}/3/device/{token}",
                        content=_payload(row),
                        headers=headers,
                        database=database,
                        row_id=row_id,
                        claim_owner=owner,
                        lease_seconds=active_lease_seconds,
                    )
                reason = _response_reason(response)
                if response.status_code == 200:
                    status = database.push_outbox_finish(
                        row_id, claim_owner=owner, sent=True
                    )
                    if status == "sent":
                        result["sent"] += 1
                    continue
                if (
                    response.status_code in {400, 410}
                    and reason in _INVALID_TOKEN_REASONS
                ):
                    status = database.push_outbox_finish(
                        row_id,
                        claim_owner=owner,
                        sent=False,
                        error=f"HTTP {response.status_code} {reason}".strip(),
                    )
                    if status == "failed":
                        if database.push_device_delete(token, claim_owner=owner):
                            result["removed"] += 1
                        result["failed"] += 1
                    continue
                retryable = (
                    response.status_code in _RETRYABLE_STATUS_CODES
                    or reason in _RETRYABLE_REASONS
                )
                if reason == "ExpiredProviderToken":
                    _reset_provider_token()
                status = database.push_outbox_finish(
                    row_id,
                    claim_owner=owner,
                    sent=False,
                    retry=retryable,
                    retry_delay_seconds=_retry_delay(row, response),
                    error=f"HTTP {response.status_code} {reason}".strip(),
                    max_attempts=int(settings.get("max_attempts") or 8),
                )
                if status == "pending":
                    result["retrying"] += 1
                elif status == "failed":
                    result["failed"] += 1
                logger.warning(
                    "APNs %s für Gerät abgelehnt: HTTP %s %s",
                    row["event"],
                    response.status_code,
                    reason,
                )
            except (httpx.HTTPError, OSError) as exc:
                status = database.push_outbox_finish(
                    row_id,
                    claim_owner=owner,
                    sent=False,
                    retry=True,
                    retry_delay_seconds=_retry_delay(row),
                    error=str(exc),
                    max_attempts=int(settings.get("max_attempts") or 8),
                )
                if status == "pending":
                    result["retrying"] += 1
                elif status == "failed":
                    result["failed"] += 1
                logger.warning("APNs %s fehlgeschlagen: %s", row["event"], exc)
    return result


def send_push_notifications(
    event: str,
    title: str,
    message: str,
    *,
    extra: Mapping[str, Any] | None = None,
    dedupe_key: str | None = None,
    db: Database | None = None,
    client_factory: Callable[..., httpx.Client] = httpx.Client,
) -> dict[str, int]:
    """Persistiert eine Meldung vor dem ersten APNs-Versuch und drainiert sofort."""

    settings = _settings(event)
    database = db or get_db()
    result = {
        "queued": 0,
        "sent": 0,
        "failed": 0,
        "removed": 0,
        "retrying": 0,
    }
    if settings is None:
        return result
    context = _push_context(extra or {})
    key = dedupe_key or notification_dedupe_key(event, title, message, extra or {})
    result["queued"] = database.push_outbox_enqueue(
        event=event,
        title=redact_command_text(str(title)),
        message=redact_command_text(str(message)),
        payload=context,
        dedupe_key=key,
        retention_seconds=int(settings.get("retention_seconds") or 86400),
    )
    result.update(dispatch_pending_pushes(db=database, client_factory=client_factory))
    return result


def queue_push_notification(
    event: str,
    title: str,
    message: str,
    *,
    extra: Mapping[str, Any] | None = None,
    dedupe_key: str | None = None,
    db: Database | None = None,
) -> dict[str, int]:
    """Persistiert sofort; ein Daemon-Worker übernimmt den ersten Netzversuch."""

    settings = _settings(event)
    database = db or get_db()
    if settings is None:
        return {"queued": 0}
    key = dedupe_key or notification_dedupe_key(event, title, message, extra or {})
    queued = database.push_outbox_enqueue(
        event=event,
        title=redact_command_text(str(title)),
        message=redact_command_text(str(message)),
        payload=_push_context(extra or {}),
        dedupe_key=key,
        retention_seconds=int(settings.get("retention_seconds") or 86400),
    )

    def dispatch() -> None:
        if not _BACKGROUND_DISPATCH_LOCK.acquire(blocking=False):
            return
        try:
            dispatch_pending_pushes(db=database)
        except Exception:
            logger.exception("APNs-Hintergrundzustellung fehlgeschlagen")
        finally:
            _BACKGROUND_DISPATCH_LOCK.release()

    if queued:
        threading.Thread(
            target=dispatch,
            name="push-dispatch",
            daemon=True,
        ).start()
    return {"queued": queued}


__all__ = [
    "DEFAULT_ERROR_EVENTS",
    "dispatch_pending_pushes",
    "notification_dedupe_key",
    "queue_push_notification",
    "send_push_notifications",
]
