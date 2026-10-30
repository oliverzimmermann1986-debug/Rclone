"""Pure normalization shared by persisted operational tool settings."""

from __future__ import annotations

import re
from email.utils import parseaddr
from typing import Any, Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter


def normalize_operations_config(
    cfg: dict[str, Any], errors: list[str], *, number: Callable, boolean: Callable
) -> None:
    def section(parent: dict, key: str, label: str) -> dict:
        value = parent.get(key)
        if value is None:
            value = {}
        if not isinstance(value, dict):
            errors.append(f"{label} muss ein Mapping sein")
            value = {}
        parent[key] = value
        return value

    def bounded(data: dict, key: str, default: int, low: int, high: int) -> int:
        return int(
            number(
                data.get(key, default),
                default=default,
                minimum=low,
                maximum=high,
                integer=True,
            )
        )

    heartbeat = section(cfg["notifications"], "heartbeat", "notifications.heartbeat")
    heartbeat["enabled"] = boolean(heartbeat.get("enabled", False))
    heartbeat["secret_url"] = str(heartbeat.get("secret_url") or "").strip()
    heartbeat["allow_private_hosts"] = boolean(
        heartbeat.get("allow_private_hosts", False)
    )
    heartbeat["period_seconds"] = bounded(heartbeat, "period_seconds", 60, 60, 3600)
    heartbeat["timeout_seconds"] = bounded(heartbeat, "timeout_seconds", 5, 1, 10)
    url = heartbeat["secret_url"]
    if url:
        try:
            parsed = urlsplit(url)
            valid = (
                len(url) <= 2048
                and parsed.scheme == "https"
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.fragment
                and not any(ord(c) < 33 for c in url)
            )
            _ = parsed.port
        except ValueError:
            valid = False
        if not valid:
            errors.append(
                "notifications.heartbeat.secret_url muss eine HTTPS-Adresse ohne Zugangsdaten oder Fragment sein"
            )
    elif heartbeat["enabled"]:
        errors.append("Für den Ausfallwächter fehlt die geheime HTTPS-Ping-Adresse")

    reports = section(cfg, "reports", "reports")
    weekly = section(reports, "weekly", "reports.weekly")
    weekly["enabled"] = boolean(weekly.get("enabled", False))
    weekly["schedule"] = str(weekly.get("schedule") or "0 8 * * 1").strip()
    if (
        not croniter.is_valid(weekly["schedule"])
        or len(weekly["schedule"].split()) != 5
    ):
        errors.append(
            "reports.weekly.schedule muss ein gültiger fünfteiliger Cron-Zeitplan sein"
        )
    weekly["timezone"] = str(
        weekly.get("timezone") or cfg["backup"].get("timezone") or "Europe/Berlin"
    ).strip()
    try:
        ZoneInfo(weekly["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        errors.append("reports.weekly.timezone ist keine gültige Zeitzone")
    weekly["retention_weeks"] = bounded(weekly, "retention_weeks", 52, 4, 104)
    email = section(weekly, "email", "reports.weekly.email")
    email["enabled"] = boolean(email.get("enabled", False))
    for key in ("host", "username", "password", "from_address"):
        if key != "password" and any(c in str(email.get(key) or "") for c in "\r\n"):
            errors.append(f"reports.weekly.email.{key} enthält ungültige Zeichen")
        email[key] = (
            str(email.get(key) or "").strip()
            if key != "password"
            else str(email.get(key) or "")
        )
    email["port"] = bounded(email, "port", 587, 1, 65535)
    email["timeout_seconds"] = bounded(email, "timeout_seconds", 10, 1, 30)
    email["encryption"] = str(email.get("encryption") or "starttls").lower().strip()
    if email["encryption"] not in {"starttls", "tls"}:
        errors.append("reports.weekly.email.encryption muss starttls oder tls sein")
    recipients = email.get("to_addresses") or []
    if not isinstance(recipients, list) or len(recipients) > 10:
        errors.append(
            "reports.weekly.email.to_addresses muss eine Liste mit höchstens zehn Adressen sein"
        )
        recipients = []
    email["to_addresses"] = list(
        dict.fromkeys(str(item).strip() for item in recipients)
    )

    def address_valid(value: str) -> bool:
        return (
            len(value) <= 254
            and parseaddr(value)[1] == value
            and bool(re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", value))
            and value.isascii()
        )

    if any(c in email["host"] for c in "\r\n/@?#") or len(email["host"]) > 253:
        errors.append("reports.weekly.email.host ist ungültig")
    if any(c in email["username"] for c in "\r\n"):
        errors.append("reports.weekly.email.username enthält ungültige Zeichen")
    if email["from_address"] and not address_valid(email["from_address"]):
        errors.append("reports.weekly.email.from_address ist ungültig")
    if any(not address_valid(address) for address in email["to_addresses"]):
        errors.append(
            "reports.weekly.email.to_addresses enthält eine ungültige Adresse"
        )
    if email["enabled"] and not (
        email["host"] and email["from_address"] and email["to_addresses"]
    ):
        errors.append(
            "Für den Berichtversand fehlen SMTP-Host, Absender oder Empfänger"
        )

    forecast = section(cfg["backup"], "storage_forecast", "backup.storage_forecast")
    forecast["enabled"] = boolean(forecast.get("enabled", True), default=True)
    forecast["sample_interval_hours"] = bounded(
        forecast, "sample_interval_hours", 24, 1, 168
    )
    forecast["warning_days"] = bounded(forecast, "warning_days", 30, 1, 365)
    forecast["history_days"] = bounded(forecast, "history_days", 180, 7, 365)
    capacities = forecast.get("capacities") or {}
    if not isinstance(capacities, dict) or len(capacities) > 512:
        errors.append(
            "backup.storage_forecast.capacities muss eine begrenzte Ziel-Zuordnung sein"
        )
        capacities = {}
    clean = {}
    for identity, value in capacities.items():
        try:
            if not isinstance(identity, str) or not re.fullmatch(
                r"[A-Za-z0-9:_.-]{1,160}", identity
            ):
                raise ValueError
            if (
                isinstance(value, bool)
                or int(value) != float(value)
                or not 0 < int(value) <= 2**60
            ):
                raise ValueError
            clean[identity] = int(value)
        except (ValueError, TypeError, OverflowError):
            errors.append(
                "backup.storage_forecast.capacities enthält eine ungültige Kapazität"
            )
    forecast["capacities"] = clean
