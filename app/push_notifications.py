"""APNs provider for authenticated native iPhone error notifications."""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import jwt

from .config_store import get_config
from .db import Database, get_db

logger = logging.getLogger(__name__)

DEFAULT_ERROR_EVENTS = (
    "sync_error",
    "mount_check_failed",
    "pair_overdue",
    "restore_test_error",
)
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: tuple[tuple[str, str, str, int], str, float] | None = None
_INVALID_TOKEN_REASONS = {
    "BadDeviceToken",
    "DeviceTokenNotForTopic",
    "Unregistered",
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
    except (TypeError, ValueError):
        logger.warning("APNs timeout_seconds ist ungültig")
        return None
    settings = {
        "team_id": str(raw.get("team_id") or "").strip(),
        "key_id": str(raw.get("key_id") or "").strip(),
        "key_file": str(raw.get("key_file") or "").strip(),
        "topic": str(raw.get("topic") or "de.oliverzimmermann.rclonesync").strip(),
        "timeout": timeout,
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


def send_push_notifications(
    event: str,
    title: str,
    message: str,
    *,
    db: Database | None = None,
    client_factory: Callable[..., httpx.Client] = httpx.Client,
) -> dict[str, int]:
    """Sends one alert per registered device and prunes rejected tokens."""

    settings = _settings(event)
    database = db or get_db()
    devices = database.push_devices(limit=32) if settings else []
    result = {"sent": 0, "failed": 0, "removed": 0}
    if not settings or not devices:
        return result

    provider_token = _provider_token(settings)
    payload = {
        "aps": {
            "alert": {"title": str(title)[:120], "body": str(message)[:900]},
            "sound": "default",
            "thread-id": "rclone-errors",
        },
        "event": event,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    headers = {
        "authorization": f"bearer {provider_token}",
        "apns-topic": str(settings["topic"]),
        "apns-push-type": "alert",
        "apns-priority": "10",
        "apns-expiration": "0",
        "content-type": "application/json",
    }
    with client_factory(http2=True, timeout=float(settings["timeout"])) as client:
        for device in devices:
            token = str(device["token"])
            host = (
                "https://api.sandbox.push.apple.com"
                if device["environment"] == "sandbox"
                else "https://api.push.apple.com"
            )
            try:
                response = client.post(
                    f"{host}/3/device/{token}", content=body, headers=headers
                )
                if response.status_code == 200:
                    result["sent"] += 1
                    continue
                result["failed"] += 1
                try:
                    reason = str(response.json().get("reason") or "")
                except (ValueError, AttributeError):
                    reason = ""
                if (
                    response.status_code in {400, 410}
                    and reason in _INVALID_TOKEN_REASONS
                ):
                    if database.push_device_delete(token):
                        result["removed"] += 1
                logger.warning(
                    "APNs %s für Gerät abgelehnt: HTTP %s %s",
                    event,
                    response.status_code,
                    reason,
                )
            except httpx.HTTPError as exc:
                result["failed"] += 1
                logger.warning("APNs %s fehlgeschlagen: %s", event, exc)
    return result


__all__ = ["DEFAULT_ERROR_EVENTS", "send_push_notifications"]
