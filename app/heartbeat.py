"""Opt-in external dead-man heartbeat, independent of backup execution.

The receiver must alert on missing requests. No URL, response body or exception
text is persisted: a receiver URL frequently contains its authentication token.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import math
import secrets
import socket
import ssl
import threading
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import SplitResult, urlsplit

from .lockdown import is_locked_down

_STATE_KEY = "operations_heartbeat_v1"
_DEFAULTS = {
    "enabled": False,
    "secret_url": "",
    "period_seconds": 60,
    "timeout_seconds": 5,
    "allow_private_hosts": False,
}


class HeartbeatRateLimitError(ValueError):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, retry_after)
        super().__init__(
            "Eine Heartbeat-Probe läuft bereits oder wurde gerade gesendet."
        )


def snapshot(cfg: Any) -> dict[str, Any]:
    return cfg.snapshot() if hasattr(cfg, "snapshot") else dict(cfg)


def settings(cfg: Any) -> dict[str, Any]:
    root = snapshot(cfg)
    raw = (root.get("notifications") or {}).get("heartbeat") or {}
    return {**_DEFAULTS, **raw}


def parse_receiver(value: Any, *, allow_private_hosts: bool = False) -> SplitResult:
    """Validate syntax without DNS or any other network side effect."""
    url = str(value or "")
    if len(url) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise ValueError("Heartbeat-URL enthält ungültige Zeichen.")
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise ValueError("Heartbeat-URL ist ungültig.") from None
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or "%" in host
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ValueError(
            "Heartbeat benötigt eine HTTPS-URL ohne eingebettete Anmeldung."
        )
    try:
        host.encode("idna")
    except UnicodeError:
        raise ValueError("Heartbeat-Empfängername ist ungültig.") from None
    if not allow_private_hosts:
        if host.casefold() == "localhost" or host.casefold().endswith(
            (".localhost", ".local")
        ):
            raise ValueError(
                "Private Heartbeat-Empfänger benötigen eine ausdrückliche Freigabe."
            )
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError(
                    "Private Heartbeat-Empfänger benötigen eine ausdrückliche Freigabe."
                )
    return parsed


def resolve_host(host: str, port: int, timeout: float, *, public_only: bool) -> str:
    """Bound DNS lookup time, then pin the actual connection to a checked IP."""
    result: list[Any] = []
    done = threading.Event()

    def resolve() -> None:
        try:
            result.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError:
            result.append(None)
        finally:
            done.set()

    threading.Thread(target=resolve, daemon=True, name="operations-dns").start()
    if not done.wait(max(0.01, timeout)):
        raise TimeoutError
    rows = result[0]
    if not rows:
        raise OSError("DNS")
    addresses = list(dict.fromkeys(row[4][0] for row in rows))
    if public_only and any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError("private_receiver")
    return addresses[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(
            host, port, timeout=timeout, context=ssl.create_default_context()
        )
        self.address = address

    def connect(self) -> None:
        raw = socket.create_connection((self.address, self.port), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _send(raw: dict[str, Any]) -> int:
    receiver = parse_receiver(
        raw["secret_url"], allow_private_hosts=raw["allow_private_hosts"] is True
    )
    timeout = max(1.0, min(float(raw["timeout_seconds"]), 10.0))
    deadline = time.monotonic() + timeout
    address = resolve_host(
        receiver.hostname or "",
        receiver.port or 443,
        timeout,
        public_only=raw["allow_private_hosts"] is not True,
    )
    remaining = max(0.01, deadline - time.monotonic())
    connection = _PinnedHTTPSConnection(
        receiver.hostname or "", receiver.port or 443, address, remaining
    )

    def abort() -> None:
        sock = connection.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        connection.close()

    timer = threading.Timer(remaining, abort)
    timer.daemon = True
    timer.start()
    try:
        path = receiver.path or "/"
        if receiver.query:
            path += "?" + receiver.query
        connection.request(
            "GET", path, headers={"User-Agent": "Sicherpfad-Heartbeat/1"}
        )
        # No redirect handling, request body, environment proxy or response log.
        return connection.getresponse().status
    finally:
        timer.cancel()
        connection.close()


def _number(value: Any, fallback: float = 0.0) -> float:
    if (
        isinstance(value, (float, int))
        and not isinstance(value, bool)
        and math.isfinite(value)
    ):
        return float(value)
    return fallback


def _fingerprint(raw: dict[str, Any]) -> str:
    return hashlib.sha256(str(raw.get("secret_url") or "").encode()).hexdigest()


def status(cfg: Any, db: Any, *, now: float | None = None) -> dict[str, Any]:
    raw = settings(cfg)
    state = db.runtime_get(_STATE_KEY) or {}
    if not isinstance(state, dict) or state.get("receiver") != _fingerprint(raw):
        state = {}
    configured = bool(raw.get("secret_url"))
    blocked = is_locked_down(db)
    name = (
        "disabled"
        if raw["enabled"] is not True
        else "unconfigured"
        if not configured
        else "pending"
    )
    if raw["enabled"] is True and configured and state.get("last_attempt_at"):
        name = "error" if state.get("last_error") else "ok"
    last_error = state.get("last_error")
    current = time.time() if now is None else float(now)
    if name not in {"disabled", "unconfigured"} and state.get("claim"):
        name = "pending" if _number(state.get("claim_until")) > current else "error"
        if name == "error":
            last_error = "Der letzte Heartbeat-Versuch wurde nicht abgeschlossen."
    if (
        name == "ok"
        and current - _number(state.get("last_success_at"))
        > int(raw["period_seconds"]) * 3
    ):
        name = "error"
        last_error = "Heartbeat ist überfällig. Den unabhängigen Betriebsdienst prüfen."
    return {
        "enabled": raw["enabled"] is True,
        "configured": configured,
        "state": name,
        "blocked_by_lockdown": blocked,
        "period_seconds": int(raw["period_seconds"]),
        "last_attempt_at": state.get("last_attempt_at"),
        "last_success_at": state.get("last_success_at"),
        "next_attempt_at": state.get("next_attempt_at"),
        "consecutive_failures": int(state.get("consecutive_failures") or 0),
        "last_error": last_error,
        "status_code": state.get("status_code"),
    }


def tick(
    cfg: Any, db: Any, *, now: float | None = None, test: bool = False
) -> dict[str, Any]:
    raw = settings(cfg)
    current = time.time() if now is None else float(now)
    if not raw.get("secret_url") or raw["enabled"] is not True and not test:
        return {"attempted": False, "ok": False, "status": status(cfg, db, now=current)}
    if is_locked_down(db):
        return {
            "attempted": False,
            "ok": False,
            "blocked_by_lockdown": True,
            "status": status(cfg, db, now=current),
        }
    parse_receiver(
        raw["secret_url"], allow_private_hosts=raw["allow_private_hosts"] is True
    )
    claim = secrets.token_hex(16)
    receiver = _fingerprint(raw)
    period = max(60, min(int(raw["period_seconds"]), 3600))
    owned = False
    retry_after = 60

    def reserve(value: Any) -> dict[str, Any]:
        nonlocal owned, retry_after
        value = (
            dict(value)
            if isinstance(value, Mapping) and value.get("receiver") == receiver
            else {}
        )
        due = _number(value.get("next_attempt_at"))
        leased = _number(value.get("claim_until"))
        if leased > current or due > current:
            retry_after = max(1, math.ceil(max(leased, due) - current))
            return value
        owned = True
        value.update(
            receiver=receiver,
            claim=claim,
            claim_until=current + 30,
            last_attempt_at=current,
            next_attempt_at=current + period,
        )
        return value

    db.runtime_update(_STATE_KEY, reserve, default={})
    if not owned:
        if test:
            raise HeartbeatRateLimitError(retry_after)
        return {"attempted": False, "ok": False, "status": status(cfg, db, now=current)}
    code: int | None = None
    error: str | None = None
    try:
        code = _send(raw)
        if not 200 <= code < 300:
            error = f"Heartbeat-Empfänger antwortet mit HTTP {code}."
    except ssl.SSLCertVerificationError:
        error = "TLS-Zertifikat des Heartbeat-Empfängers ist nicht vertrauenswürdig."
    except (TimeoutError, socket.timeout):
        error = "Zeitüberschreitung beim Heartbeat."
    except ValueError:
        error = "Heartbeat-Empfängeradresse wurde aus Sicherheitsgründen abgewiesen."
    except (OSError, http.client.HTTPException, ssl.SSLError):
        error = "Heartbeat-Empfänger konnte nicht sicher erreicht werden."
    except Exception:
        error = "Heartbeat konnte nicht gesendet werden."

    def finish(value: Any) -> dict[str, Any]:
        value = dict(value) if isinstance(value, Mapping) else {}
        if value.get("claim") != claim:
            return value
        value.update(claim="", claim_until=0, status_code=code, last_error=error)
        value["consecutive_failures"] = (
            min(100000, int(value.get("consecutive_failures") or 0) + 1) if error else 0
        )
        if not error:
            value["last_success_at"] = current
        return value

    db.runtime_update(_STATE_KEY, finish, default={})
    return {
        "attempted": True,
        "ok": error is None,
        "status": status(cfg, db, now=current),
    }
