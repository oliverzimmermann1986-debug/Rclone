"""Notifications via Webhooks: Discord, Telegram, Generic JSON.
Wird von rclone_sync.py + main bei Events aufgerufen."""
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any, Dict, List

from .config_store import get_config

logger = logging.getLogger(__name__)

# Bekannte Event-Typen
EVENTS = ("sync_started", "sync_ok", "sync_error", "conflict",
          "mount_check_failed", "cancelled")


def _post_discord(url: str, title: str, msg: str, color: int) -> None:
    payload = {
        "username": "rclone-sync",
        "embeds": [{
            "title": title[:256],
            "description": msg[:2000],
            "color": color,
        }],
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10).read()


def _post_telegram(url: str, msg: str) -> None:
    # url-Format: https://api.telegram.org/botTOKEN/sendMessage?chat_id=XXX
    # Oder Config-Felder. Wir nehmen URL mit Token + Chat-ID erwartet als
    # template-String mit '{message}'.
    if "{message}" in url:
        req_url = url.replace("{message}", urllib.parse.quote(msg[:4000]))
        urllib.request.urlopen(req_url, timeout=10).read()
    else:
        # Standard Telegram-API
        payload = json.dumps({"text": msg[:4000]}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10).read()


def _post_generic(url: str, event: str, payload: Dict[str, Any]) -> None:
    body = json.dumps({"event": event, **payload}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10).read()


_COLORS = {
    "sync_started": 0x60a5fa,
    "sync_ok": 0x4ade80,
    "sync_error": 0xf87171,
    "conflict": 0xfbbf24,
    "mount_check_failed": 0xf87171,
    "cancelled": 0x8e9aae,
}


def notify(event: str, title: str, message: str, **extra) -> None:
    """Schickt Notification an alle konfigurierten Webhooks die das Event
    abonniert haben. Best-effort — Fehler werden geloggt, nicht geworfen."""
    if event not in EVENTS:
        logger.warning(f"Unbekanntes Event '{event}', ignoriere")
        return
    hooks: List[Dict] = get_config().get("notifications", "webhooks", default=[]) or []
    for h in hooks:
        if not h.get("url"):
            continue
        if event not in (h.get("events") or []):
            continue
        kind = h.get("type", "generic")
        try:
            if kind == "discord":
                _post_discord(h["url"], title, message, _COLORS.get(event, 0x06b6d4))
            elif kind == "telegram":
                _post_telegram(h["url"], f"{title}\n\n{message}")
            else:
                _post_generic(h["url"], event, {
                    "title": title, "message": message, **extra,
                })
            logger.info(f"notify[{kind}] {event}: ok")
        except Exception as e:
            logger.warning(f"notify[{kind}] {event} fehlgeschlagen: {e}")
