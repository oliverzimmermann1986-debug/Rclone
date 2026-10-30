"""Minimaler Lese-Client für die PBS-REST-API (nur API-Token, nur Stdlib).

Liefert Speicher-/Dedup-Kennzahlen und erkennt laufende Wartung (GC/Verify).
Alle Abfragen sind fehlertolerant: Fehler kommen als ``{"error": ...}`` zurück.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import ssl
import time
from typing import Any, Mapping, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8007
TIMEOUT_SECONDS = 10
MAINTENANCE_TYPES = frozenset(
    {
        "garbage_collection",
        "verify",
        "verificationjob",
        "verify_group",
        "verify_snapshot",
    }
)
_MAX_BODY = 4 * 1024 * 1024
_FORMAT_ERROR = (
    "pbs.repository hat kein gültiges Format "
    "(erwartet user@realm!token@host[:port]:datastore)"
)
_maintenance_cache: dict[str, dict[str, Any]] = {}


class PbsApiError(ValueError):
    """Fehler mit kurzer deutscher Meldung ohne Secrets."""


def parse_repository(repository: Any) -> dict[str, Any]:
    """``[auth_id@]host[:port]:datastore`` → {auth_id, host, port, datastore}."""
    auth_id, _, location = str(repository or "").strip().rpartition("@")
    if location.startswith("["):
        host, bracket, rest = location[1:].partition("]")
        if not bracket or not rest.startswith(":"):
            raise PbsApiError(_FORMAT_ERROR)
        parts = [host, *rest[1:].split(":")]
    else:
        parts = location.split(":")
    if len(parts) == 2:
        parts.insert(1, str(DEFAULT_PORT))
    if len(parts) != 3:
        raise PbsApiError(_FORMAT_ERROR)
    host, port, datastore = parts
    if not host or not datastore or not port.isdigit() or not 0 < int(port) < 65536:
        raise PbsApiError(_FORMAT_ERROR)
    return {
        "auth_id": auth_id or None,
        "host": host,
        "port": int(port),
        "datastore": datastore,
    }


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS ohne CA-Prüfung, dafür mit festem SHA-256-Fingerprint des Server-Zertifikats."""

    def __init__(self, host: str, port: int, fingerprint: str):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        super().__init__(host, port, timeout=TIMEOUT_SECONDS, context=context)
        self.fingerprint = fingerprint

    def connect(self) -> None:
        super().connect()
        der = self.sock.getpeercert(binary_form=True) or b""
        if hashlib.sha256(der).hexdigest() != self.fingerprint:
            self.close()
            raise PbsApiError("TLS-Fingerprint des PBS stimmt nicht überein")


def _get(settings: Mapping[str, Any], path: str) -> Any:
    repo = parse_repository(settings.get("repository"))
    auth_id = repo["auth_id"] or ""
    if "!" not in auth_id:
        raise PbsApiError("Nur API-Token werden unterstützt")
    fingerprint = (
        str(settings.get("fingerprint") or "").replace(":", "").strip().lower()
    )
    if fingerprint:
        conn = _PinnedHTTPSConnection(repo["host"], repo["port"], fingerprint)
    else:
        conn = http.client.HTTPSConnection(
            repo["host"],
            repo["port"],
            timeout=TIMEOUT_SECONDS,
            context=ssl.create_default_context(),
        )
    token = f"PBSAPIToken={auth_id}:{settings.get('password') or ''}"
    try:
        conn.request(
            "GET",
            "/api2/json" + path.format(store=quote(repo["datastore"], safe="")),
            headers={"Authorization": token, "Accept": "application/json"},
        )
        response = conn.getresponse()
        body = response.read(_MAX_BODY + 1)
    finally:
        conn.close()
    if response.status in (401, 403):
        raise PbsApiError(f"PBS-API verweigert den Zugriff (HTTP {response.status})")
    if response.status != 200:
        raise PbsApiError(f"PBS-API antwortet mit HTTP {response.status}")
    if len(body) > _MAX_BODY:
        raise PbsApiError("Antwort der PBS-API ist zu groß")
    try:
        return json.loads(body)["data"]
    except (ValueError, KeyError, TypeError):
        raise PbsApiError("Antwort der PBS-API ist nicht lesbar") from None


def _fetch(
    settings: Mapping[str, Any], path: str, expect: type
) -> tuple[Any, Optional[str]]:
    """(data, None) oder (None, Fehlertext); Fehlertexte enthalten nie das Secret."""
    try:
        data = _get(settings, path)
    except PbsApiError as exc:
        return None, str(exc)
    except ssl.SSLCertVerificationError:
        return (
            None,
            "TLS-Zertifikat der PBS-API nicht vertrauenswürdig (pbs.fingerprint setzen)",
        )
    except ssl.SSLError:
        return None, "TLS-Verbindung zur PBS-API fehlgeschlagen"
    except (OSError, http.client.HTTPException):
        return None, "PBS-API nicht erreichbar"
    except Exception as exc:  # Meldung kann Header samt Token enthalten: nur Typ loggen
        logger.warning("PBS-API-Abfrage fehlgeschlagen: %s", type(exc).__name__)
        return None, "PBS-API-Abfrage fehlgeschlagen"
    if not isinstance(data, expect):
        return None, "Antwort der PBS-API ist unvollständig"
    return data, None


def _num(value: Any) -> Optional[float]:
    return (
        value
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def datastore_status(settings: Mapping[str, Any]) -> dict[str, Any]:
    data, error = _fetch(settings, "/admin/datastore/{store}/status", dict)
    if error:
        return {"error": error}
    return {key: _num(data.get(key)) for key in ("total", "used", "avail")}


def gc_status(settings: Mapping[str, Any]) -> dict[str, Any]:
    data, error = _fetch(settings, "/admin/datastore/{store}/gc", dict)
    if error:
        return {"error": error}
    index, disk = _num(data.get("index-data-bytes")), _num(data.get("disk-bytes"))
    upid = data.get("upid")
    return {
        "index_data_bytes": index,
        "disk_bytes": disk,
        "dedup_factor": index / disk if index is not None and disk else None,
        "saved_bytes": index - disk if index is not None and disk is not None else None,
        "removed_bytes": _num(data.get("removed-bytes")),
        "pending_bytes": _num(data.get("pending-bytes")),
        "upid": upid if isinstance(upid, str) else None,
        "last_run": _num(data.get("last-run-endtime")),
    }


def _on_datastore(task: dict[str, Any], datastore: str) -> bool:
    """GC: worker_id == Datastore; Verify: ``<ds>[:…]``. Ohne worker_id: nicht zählen."""
    worker_id = task.get("worker_id")
    return isinstance(worker_id, str) and (
        worker_id == datastore or worker_id.startswith(datastore + ":")
    )


def active_maintenance(settings: Mapping[str, Any]) -> dict[str, Any]:
    data, error = _fetch(settings, "/nodes/localhost/tasks?running=1", list)
    if error:
        return {"error": error}
    # Nach erfolgreichem _fetch ist die Repository-Angabe gültig.
    datastore = parse_repository(settings.get("repository"))["datastore"]
    tasks = [
        {
            "type": task["worker_type"],
            "upid": task.get("upid"),
            "starttime": _num(task.get("starttime")),
        }
        for task in data
        if isinstance(task, dict)
        and task.get("worker_type") in MAINTENANCE_TYPES
        and _on_datastore(task, datastore)
    ]
    return {"running": bool(tasks), "tasks": tasks, "datastore": datastore}


def throttle_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Wirksame pbs.gc_throttle-Werte: enabled (Default True), poll_seconds 15..900."""
    throttle = settings.get("gc_throttle")
    throttle = throttle if isinstance(throttle, Mapping) else {}
    try:
        poll = int(throttle.get("poll_seconds", 60))
    except (TypeError, ValueError):
        poll = 60
    return {
        "enabled": bool(throttle.get("enabled", True)),
        "poll_seconds": min(900, max(15, poll)),
    }


def maintenance_active(
    settings: Mapping[str, Any], *, now: Optional[float] = None
) -> dict[str, Any]:
    """Läuft gerade GC/Verify auf dem PBS? Ergebnis wird poll_seconds lang gecacht."""
    now = time.time() if now is None else now
    throttle = throttle_settings(settings)
    if not settings.get("enabled") or not throttle["enabled"]:
        return {"active": False, "tasks": [], "checked_at": now, "error": None}
    key = str(settings.get("repository") or "")
    cached = _maintenance_cache.get(key)
    if cached and 0 <= now - cached["checked_at"] < throttle["poll_seconds"]:
        return dict(cached)
    status = active_maintenance(settings)
    # Fail-open: eine unerreichbare PBS-API darf Backups nicht dauerhaft blockieren.
    result = {
        "active": status.get("running") is True,
        "tasks": status.get("tasks", []),
        "checked_at": now,
        "error": status.get("error"),
    }
    _maintenance_cache[key] = result
    return dict(result)
