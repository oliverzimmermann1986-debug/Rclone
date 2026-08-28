"""Nicht blockierende Benachrichtigungen über die dauerhafte APNs-Outbox."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

EVENTS = (
    "sync_started",
    "sync_ok",
    "sync_error",
    "conflict",
    "mount_check_failed",
    "cancelled",
    "pair_overdue",
    "restore_test_ok",
    "restore_test_error",
    "anomaly_blocked",
    "recovery_ready",
    "recovery_error",
)


def notify(event: str, title: str, message: str, **extra: Any) -> None:
    """Schreibt APNs-Ereignisse dauerhaft vor und blockiert keinen Job am Netz."""

    if event not in EVENTS:
        logger.warning("Unbekanntes Event %r, ignoriere", event)
        return
    from .push_notifications import queue_push_notification

    try:
        result = queue_push_notification(
            event,
            title,
            message,
            extra=extra,
        )
        if result.get("queued"):
            logger.info("notify[apns] %s: dauerhaft vorgemerkt", event)
    except Exception as exc:
        # Ein SQLite-/Konfigurationsfehler darf den eigentlichen Backup-Lauf
        # weiterhin nicht in einen falschen Fehlerstatus versetzen.
        logger.warning("notify[apns] %s fehlgeschlagen: %s", event, exc)


__all__ = ["EVENTS", "notify"]
