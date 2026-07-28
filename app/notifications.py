"""Best-effort Webhook-Benachrichtigungen mit DNS-Pinning und SSRF-Schutz."""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import socket
import ssl
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import __version__
from .config_store import get_config
from .rclone_args import redact_command_text

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
)
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_REQUEST_BYTES = 512 * 1024
_MAX_REDIRECTS = 4
_SENSITIVE_PAYLOAD_KEYS = {
    "password",
    "password_hash",
    "secret",
    "secret_key",
    "token",
    "credential",
    "credentials",
    "access_key",
    "private_key",
}


def _redact_payload(value: Any) -> Any:
    if isinstance(value, str):
        return redact_command_text(value)
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): (
                "***REDACTED***"
                if str(key).casefold() in _SENSITIVE_PAYLOAD_KEYS
                else _redact_payload(item)
            )
            for key, item in value.items()
        }
    return value


def _notification_policy() -> tuple[bool, bool, float, int]:
    """Liefert auch bei manuell beschädigter Alt-Konfiguration sichere Grenzen."""
    cfg = get_config()
    try:
        timeout = float(cfg.get("notifications", "timeout_seconds", default=10) or 10)
    except (TypeError, ValueError, OverflowError):
        timeout = 10.0
    try:
        workers = int(cfg.get("notifications", "max_parallel", default=4) or 4)
    except (TypeError, ValueError, OverflowError):
        workers = 4
    return (
        bool(cfg.get("notifications", "allow_http", default=False)),
        bool(cfg.get("notifications", "allow_private_targets", default=False)),
        max(1.0, min(timeout, 60.0)),
        max(1, min(workers, 16)),
    )


def _resolved_addresses(hostname: str, port: int, *, allow_private: bool) -> list[str]:
    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Webhook-Hostname nicht auflösbar: {exc}") from exc
    unique: list[str] = []
    for item in addresses:
        raw_ip = item[4][0].split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise ValueError("Webhook lieferte eine ungültige IP-Adresse") from exc
        if not allow_private and not address.is_global:
            raise ValueError(f"Private/lokale Webhook-Adresse blockiert: {address}")
        if raw_ip not in unique:
            unique.append(raw_ip)
    if not unique:
        raise ValueError("Webhook-Hostname liefert keine Adresse")
    return unique


def _validate_url(url: str) -> tuple[urllib.parse.SplitResult, str]:
    allow_http, allow_private, _timeout, _workers = _notification_policy()
    candidate = url.replace("{message}", "message")
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme not in ({"http", "https"} if allow_http else {"https"}):
        raise ValueError("Webhook muss HTTPS verwenden")
    if not parsed.hostname:
        raise ValueError("Webhook-Hostname fehlt")
    if parsed.username or parsed.password:
        raise ValueError("Zugangsdaten im Webhook-Hostteil sind nicht erlaubt")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"Ungültiger Webhook-Host/Port: {exc}") from exc
    addresses = _resolved_addresses(hostname, port, allow_private=allow_private)
    return parsed, addresses[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, ip: str, port: int, *, timeout: float):
        super().__init__(
            hostname, port=port, timeout=timeout, context=ssl.create_default_context()
        )
        self._pinned_ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _host_header(hostname: str, port: int, scheme: str) -> str:
    display = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    return display if port == default_port else f"{display}:{port}"


def _request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
) -> None:
    if data is not None and len(data) > _MAX_REQUEST_BYTES:
        raise ValueError("Webhook-Payload ist zu groß")
    if timeout is None:
        _allow_http, _allow_private, timeout, _workers = _notification_policy()
    timeout = max(1.0, min(float(timeout), 60.0))
    current_url = url
    current_data = data
    current_headers = dict(headers or {})

    for redirect_count in range(_MAX_REDIRECTS + 1):
        parsed, pinned_ip = _validate_url(current_url)
        hostname = parsed.hostname.encode("idna").decode("ascii")  # type: ignore[union-attr]
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        request_headers = {
            "User-Agent": f"rclone-sync-container/{__version__}",
            "Accept": "application/json, text/plain, */*",
            "Host": _host_header(hostname, port, parsed.scheme),
            **current_headers,
        }
        if current_data is not None:
            request_headers.setdefault("Content-Length", str(len(current_data)))

        connection: http.client.HTTPConnection
        if parsed.scheme == "https":
            connection = _PinnedHTTPSConnection(
                hostname, pinned_ip, port, timeout=timeout
            )
        else:
            connection = http.client.HTTPConnection(
                pinned_ip, port=port, timeout=timeout
            )
        try:
            connection.request(
                "POST" if current_data is not None else "GET",
                path,
                body=current_data,
                headers=request_headers,
            )
            response = connection.getresponse()
            response.read(_MAX_RESPONSE_BYTES + 1)
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise RuntimeError(
                        f"Webhook-Redirect {response.status} ohne Location"
                    )
                if redirect_count >= _MAX_REDIRECTS:
                    raise RuntimeError("Zu viele Webhook-Weiterleitungen")
                current_url = urllib.parse.urljoin(current_url, location)
                if response.status == 303 or (
                    response.status in {301, 302} and current_data is not None
                ):
                    current_data = None
                    current_headers.pop("Content-Type", None)
                continue
            if not 200 <= response.status < 300:
                raise RuntimeError(f"Webhook antwortete mit HTTP {response.status}")
            return
        finally:
            connection.close()
    raise RuntimeError("Webhook-Weiterleitung konnte nicht abgeschlossen werden")


def _post_discord(url: str, title: str, message: str, color: int) -> None:
    payload = {
        "username": "rclone-sync",
        "embeds": [
            {"title": title[:256], "description": message[:2000], "color": color}
        ],
    }
    _request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _post_telegram(url: str, message: str) -> None:
    if "{message}" in url:
        _request(url.replace("{message}", urllib.parse.quote(message[:4000], safe="")))
    else:
        body = json.dumps({"text": message[:4000]}).encode("utf-8")
        _request(url, data=body, headers={"Content-Type": "application/json"})


def _bounded_payload(payload: dict[str, Any]) -> bytes:
    payload = _redact_payload(payload)
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    if len(body) <= _MAX_REQUEST_BYTES:
        return body
    compact = {key: value for key, value in payload.items() if key != "summary"}
    compact["summary_omitted"] = "Payload überschritt Größenlimit"
    return json.dumps(compact, ensure_ascii=False, default=str).encode("utf-8")


def _post_generic(url: str, event: str, payload: dict[str, Any]) -> None:
    _request(
        url,
        data=_bounded_payload({"event": event, **payload}),
        headers={"Content-Type": "application/json"},
    )


_COLORS = {
    "sync_started": 0x60A5FA,
    "sync_ok": 0x4ADE80,
    "sync_error": 0xF87171,
    "conflict": 0xFBBF24,
    "mount_check_failed": 0xF87171,
    "cancelled": 0x8E9AAE,
    "pair_overdue": 0xFBBF24,
    "restore_test_ok": 0x4ADE80,
    "restore_test_error": 0xF87171,
}


def notify_one(
    hook: dict[str, Any], event: str, title: str, message: str, **extra: Any
) -> None:
    if event not in EVENTS:
        raise ValueError(f"Unbekanntes Event: {event}")
    if not isinstance(hook, dict) or not hook.get("url"):
        raise ValueError("Webhook URL fehlt")
    if hook.get("enabled", True) is False:
        return
    title = redact_command_text(title)
    message = redact_command_text(message)
    extra = _redact_payload(extra)
    kind = str(hook.get("type") or "generic").lower()
    url = str(hook["url"])
    if kind == "discord":
        _post_discord(url, title, message, _COLORS.get(event, 0x06B6D4))
    elif kind == "telegram":
        _post_telegram(url, f"{title}\n\n{message}")
    elif kind == "generic":
        _post_generic(url, event, {"title": title, "message": message, **extra})
    else:
        raise ValueError(f"Unbekannter Webhook-Typ: {kind}")


def notify(event: str, title: str, message: str, **extra: Any) -> None:
    """Benachrichtigt passende Hooks parallel; Fehler stoppen keinen Sync."""
    if event not in EVENTS:
        logger.warning("Unbekanntes Event %r, ignoriere", event)
        return
    hooks = [
        hook
        for hook in (get_config().get("notifications", "webhooks", default=[]) or [])
        if isinstance(hook, dict)
        and hook.get("url")
        and hook.get("enabled", True)
        and event in (hook.get("events") or [])
    ]
    if not hooks:
        return
    _allow_http, _allow_private, _timeout, workers = _notification_policy()
    with ThreadPoolExecutor(
        max_workers=min(workers, len(hooks)), thread_name_prefix="webhook"
    ) as pool:
        futures = {
            pool.submit(notify_one, hook, event, title, message, **extra): hook
            for hook in hooks
        }
        for future in as_completed(futures):
            hook = futures[future]
            kind = str(hook.get("type") or "generic").lower()
            try:
                future.result()
                logger.info("notify[%s] %s: ok", kind, event)
            except Exception as exc:
                logger.warning("notify[%s] %s fehlgeschlagen: %s", kind, event, exc)
