"""Kleine, gemeinsam genutzte Hilfsfunktionen ohne Seiteneffekte."""

from __future__ import annotations

from typing import Any


def bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    """Parst einen Wert defensiv als int und begrenzt ihn auf [minimum, maximum]."""
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def bounded_number(
    value: Any, *, default: float, minimum: float, maximum: float
) -> float:
    """Parst einen Wert defensiv als float (NaN-sicher) und begrenzt ihn."""
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    if parsed != parsed:  # NaN
        parsed = default
    return max(minimum, min(parsed, maximum))
