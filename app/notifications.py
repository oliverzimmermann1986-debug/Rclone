"""Notifications via Webhooks: Discord, Telegram, Generic JSON."""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from .config_store import get_config

logger = logging.getLogger(__name__)

EVENTS = ("sync_started", "sync_ok", "sync_error", "conflict", "mount_check_failed", "cancelled")


def _request(url: str, *, data: bytes | None = None, headers: Dict[str, str] | None = None) -> None:
    req = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": "rclone-sync-container/1.0", **(headers or {})},
    )
    urllib.request.urlopen(req, timeout=10).read()


def _post_discord(url: str, title: str, msg: str, color: int) -> None:
    payload = {
        "username": "rclone-sync",
        "embeds": [{"title": title[:256], "description": msg[:2000], "color": color}],
    }
    _request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})


def _post_telegram(url: str, msg: str) -> None:
    # Template-URL: ...&text={message}; alternativ Endpoint, der JSON {text} annimmt.
    if "{message}" in url:
        _request(url.replace("{message}", urllib.parse.quote(msg[:4000])))
    else:
        body = json.dumps({"text": msg[:4000]}).encode("utf-8")
        _request(url, data=body, headers={"Content-Type": "application/json"})


def _post_generic(url: str, event: str, payload: Dict[str, Any]) -> None:
    body = json.dumps({"event": event, **payload}).encode("utf-8")
    _request(url, data=body, headers={"Content-Type": "application/json"})


_COLORS = {
    "sync_started": 0x60A5FA,
    "sync_ok": 0x4ADE80,
    "sync_error": 0xF87171,
    "conflict": 0xFBBF24,
    "mount_check_failed": 0xF87171,
    "cancelled": 0x8E9AAE,
}


def notify_one(hook: Dict[str, Any], event: str, title: str, message: str, **extra) -> None:
    """Sendet genau einen Hook. Für UI-Test ohne alle Webhooks zu triggern."""
    if event not in EVENTS:
        raise ValueError(f"Unbekanntes Event: {event}")
    if not isinstance(hook, dict) or not hook.get("url"):
        raise ValueError("Webhook URL fehlt")
    kind = (hook.get("type") or "generic").lower()
    if kind == "discord":
        _post_discord(hook["url"], title, message, _COLORS.get(event, 0x06B6D4))
    elif kind == "telegram":
        _post_telegram(hook["url"], f"{title}\n\n{message}")
    else:
        _post_generic(hook["url"], event, {"title": title, "message": message, **extra})


def notify(event: str, title: str, message: str, **extra) -> None:
    """Best-effort Notifications; Fehler werden geloggt, nicht geworfen."""
    if event not in EVENTS:
        logger.warning("Unbekanntes Event %r, ignoriere", event)
        return
    hooks: List[Dict] = get_config().get("notifications", "webhooks", default=[]) or []
    for h in hooks:
        if not isinstance(h, dict) or not h.get("url"):
            continue
        if event not in (h.get("events") or []):
            continue
        kind = (h.get("type") or "generic").lower()
        try:
            if kind == "discord":
                _post_discord(h["url"], title, message, _COLORS.get(event, 0x06B6D4))
            elif kind == "telegram":
                _post_telegram(h["url"], f"{title}\n\n{message}")
            else:
                _post_generic(h["url"], event, {"title": title, "message": message, **extra})
            logger.info("notify[%s] %s: ok", kind, event)
        except Exception as e:
            logger.warning("notify[%s] %s fehlgeschlagen: %s", kind, event, e)
