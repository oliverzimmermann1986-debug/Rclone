"""S3 Object Lock / Versionierung je Bucket erkennen (SigV4 ohne boto3)."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from .rclone_args import rclone_subprocess_env

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
CACHE_TTL_SECONDS = 3600
_LABELS = {
    "immutable": "Ransomware-immun",
    "lock_no_default": "Object Lock ohne Standardregel",
    "versioned": "versioniert",
    "unprotected": "ohne Schutz",
    "unknown": "unbekannt",
}
_lock = threading.Lock()
_cache: tuple[tuple[tuple[str, str], ...], float, dict[str, Any]] | None = None

Fetch = Callable[[str, dict[str, str]], tuple[int, str]]


def _canonical_query(query: str) -> str:
    pairs = []
    for part in query.split("&"):
        if part:
            key, _, value = part.partition("=")
            pairs.append(
                (quote(unquote(key), safe="~"), quote(unquote(value), safe="~"))
            )
    return "&".join(f"{key}={value}" for key, value in sorted(pairs))


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def sign_request(
    method: str,
    url: str,
    *,
    access_key: str,
    secret_key: str,
    region: str,
    service: str,
    amz_date: str,
    payload_hash: str,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """AWS Signature Version 4; liefert die zu sendenden Header."""
    parts = urlsplit(url)
    headers = {"Host": parts.netloc.lower(), "x-amz-date": amz_date}
    if service == "s3":
        # S3 verlangt den Header und weist unsignierte x-amz-* ab; die AWS-Testsuite signiert ihn nicht.
        headers["x-amz-content-sha256"] = payload_hash
    headers.update(extra_headers or {})
    canonical = {
        key.lower(): " ".join(str(value).split()) for key, value in headers.items()
    }
    signed = ";".join(sorted(canonical))
    # shortcut: Pfad nur einfach URI-kodiert (S3-Regel); andere Dienste bräuchten Doppelkodierung.
    request = "\n".join(
        [
            method.upper(),
            quote(unquote(parts.path or "/"), safe="/~"),
            _canonical_query(parts.query),
            *(f"{key}:{canonical[key]}" for key in sorted(canonical)),
            "",
            signed,
            payload_hash,
        ]
    )
    scope = f"{amz_date[:8]}/{region}/{service}/aws4_request"
    to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(request.encode("utf-8")).hexdigest(),
        ]
    )
    key = ("AWS4" + secret_key).encode("utf-8")
    for part in (amz_date[:8], region, service, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    return headers


def _xml_value(xml: str | None, name: str) -> str | None:
    try:
        root = ET.fromstring(xml or "")
    except ET.ParseError:
        return None
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == name:
            return (element.text or "").strip()
    return None


def _result(
    level: str,
    hint: str,
    versioning: bool | None = None,
    retention: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "level": level,
        "label": _LABELS[level],
        "hint": hint,
        "versioning": versioning,
        "retention": retention,
    }


def evaluate(
    object_lock_xml: str | None, lock_status: int, versioning_xml: str | None
) -> dict[str, Any]:
    """Bewertet die Antworten von ?object-lock und ?versioning (None = nicht lesbar)."""
    versioning = (
        None
        if versioning_xml is None
        else _xml_value(versioning_xml, "Status") == "Enabled"
    )
    if lock_status == 403:
        return _result(
            "unknown",
            "keine Berechtigung (s3:GetBucketObjectLockConfiguration)",
            versioning,
        )
    if lock_status == 404 or "ObjectLockConfigurationNotFoundError" in (
        object_lock_xml or ""
    ):
        lock = False
    elif lock_status == 200:
        lock = _xml_value(object_lock_xml, "ObjectLockEnabled") == "Enabled"
    else:
        return _result(
            "unknown",
            f"Object-Lock-Status nicht lesbar (HTTP {lock_status})",
            versioning,
        )
    if lock:
        mode = _xml_value(object_lock_xml, "Mode")
        days = _xml_value(object_lock_xml, "Days") or ""
        years = _xml_value(object_lock_xml, "Years") or ""
        period = (
            ("days", int(days))
            if days.isdigit()
            else ("years", int(years))
            if years.isdigit()
            else None
        )
        if mode in ("COMPLIANCE", "GOVERNANCE") and period:
            unit = {"days": ("Tag", "Tage"), "years": ("Jahr", "Jahre")}[period[0]]
            hint = (
                f"Object Lock aktiv: neue Objekte bleiben {period[1]} "
                f"{unit[period[1] != 1]} unveränderlich ({mode})."
            )
            if mode == "GOVERNANCE":
                hint += " Konten mit s3:BypassGovernanceRetention können die Sperre aufheben."
            return _result(
                "immutable", hint, versioning, {"mode": mode, period[0]: period[1]}
            )
        return _result(
            "lock_no_default",
            "Object Lock aktiv, aber ohne Standard-Aufbewahrung: nur Objekte mit "
            "explizit gesetzter Retention sind geschützt.",
            versioning,
        )
    if versioning:
        return _result(
            "versioned",
            "Versionierung aktiv: ältere Stände bleiben erhalten, lassen sich mit "
            "denselben Zugangsdaten aber löschen.",
            True,
        )
    if versioning is False:
        return _result(
            "unprotected",
            "Weder Object Lock noch Versionierung: Löschen oder Überschreiben ist endgültig.",
            False,
        )
    return _result("unknown", "Versionierung nicht lesbar")


def _fetch(url: str, headers: dict[str, str]) -> tuple[int, str]:
    parts = urlsplit(url)
    connection_class = (
        http.client.HTTPSConnection
        if parts.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_class(parts.netloc, timeout=10)
    try:
        connection.request("GET", f"{parts.path or '/'}?{parts.query}", headers=headers)
        response = connection.getresponse()
        return response.status, response.read(1_000_000).decode("utf-8", "replace")
    finally:
        connection.close()


def _targets(cfg: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """(remote, bucket) aus aktiven Datenwegen, z. B. ``remote:/bucket/prefix``."""
    found = set()
    for pair in (cfg.get("backup") or {}).get("pairs") or []:
        if not isinstance(pair, dict) or not pair.get("enabled", True):
            continue
        name, sep, path = str(pair.get("remote") or "").partition(":")
        bucket = path.lstrip("/").split("/", 1)[0]
        if sep and name and bucket:
            found.add((name.split(",", 1)[0], bucket))
    return tuple(sorted(found))


def _check(conf: dict[str, Any], bucket: str, fetch: Fetch) -> dict[str, Any]:
    access_key = str(conf.get("access_key_id") or "")
    secret_key = str(conf.get("secret_access_key") or "")
    if not (access_key and secret_key):
        return {
            **_result("unknown", "Zugangsdaten nicht aus rclone-Config lesbar"),
            "error": None,
        }
    region = str(conf.get("region") or "") or "us-east-1"
    endpoint = str(conf.get("endpoint") or "") or f"s3.{region}.amazonaws.com"
    if "://" not in endpoint:
        endpoint = f"https://{endpoint}"
    token = str(conf.get("session_token") or "")
    answers = {}
    for query in ("object-lock", "versioning"):
        url = f"{endpoint.rstrip('/')}/{quote(bucket)}?{query}"
        headers = sign_request(
            "GET",
            url,
            access_key=access_key,
            secret_key=secret_key,
            region=region,
            service="s3",
            amz_date=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
            payload_hash=EMPTY_SHA256,
            extra_headers={"x-amz-security-token": token} if token else None,
        )
        try:
            answers[query] = fetch(url, headers)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # Nur der Fehlertyp: Meldungen könnten Header oder Zugangsdaten spiegeln.
            return {
                **_result("unknown", "S3-Endpunkt nicht erreichbar"),
                "error": f"Verbindung fehlgeschlagen ({type(exc).__name__})",
            }
    lock_status, lock_body = answers["object-lock"]
    versioning_status, versioning_body = answers["versioning"]
    return {
        **evaluate(
            lock_body,
            lock_status,
            versioning_body if versioning_status == 200 else None,
        ),
        "error": None,
    }


def _build(
    targets: tuple[tuple[str, str], ...], run: Callable[..., Any], fetch: Fetch
) -> dict[str, Any]:
    report: dict[str, Any] = {"generated_at": time.time(), "buckets": [], "error": None}
    try:
        result = run(
            ["rclone", "config", "dump"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            stdin=subprocess.DEVNULL,
            env=rclone_subprocess_env(),
        )
        remotes = json.loads(result.stdout or "{}") if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        remotes = None
    if not isinstance(remotes, dict):
        report["error"] = "rclone-Konfiguration nicht lesbar"
        return report
    for remote, bucket in targets:
        conf = remotes.get(remote)
        if isinstance(conf, dict) and conf.get("type") == "s3":
            report["buckets"].append(
                {"remote": remote, "bucket": bucket, **_check(conf, bucket, fetch)}
            )
    return report


def object_lock_report(
    cfg: dict[str, Any],
    *,
    run: Callable[..., Any] = subprocess.run,
    fetch: Fetch | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Siegel je S3-Bucket der aktiven Datenwege; 1 h Cache je Ziel-Menge."""
    global _cache
    targets = _targets(cfg)
    # shortcut: Buckets nacheinander geprüft (je bis 2x10 s); bei vielen Zielen parallelisieren.
    with _lock:
        if (
            not refresh
            and _cache
            and _cache[0] == targets
            and time.time() - _cache[1] < CACHE_TTL_SECONDS
        ):
            return _cache[2]
        report = _build(targets, run, fetch or _fetch)
        if report["error"] is None:
            _cache = (targets, time.time(), report)
        return report
