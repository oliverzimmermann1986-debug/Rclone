"""Preflight-Matrix: geplante Änderungen eines Dry-Run-Laufs und Ausnahmefilter."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import maintenance
from .db import get_db
from .security import is_relative_to

PREFLIGHT_KINDS = {"backup", "quicksync"}
_MAX_LOG_BYTES = 20 * 1024 * 1024
_MAX_EXCLUDES = 200
_MAX_PATH_LEN = 1024
# Gieriger Pfad: das zuletzt vorkommende ": Skipped " trennt, Pfade dürfen ": " enthalten.
_SKIPPED_RE = re.compile(
    r"(?:\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)? )?"
    r"(?:(?:DEBUG|INFO|NOTICE|WARNING|ERROR|CRITICAL) *: )?"
    r"(?P<path>.*): Skipped (?P<action>.+?) as --dry-run is set"
    r"(?: \(size (?P<size>[^)]*)\))?"
)
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?) *([KMGTP]?)i?B?", re.IGNORECASE)
_SIZE_UNITS = ("", "K", "M", "G", "T", "P")
_GLOB_SPECIAL_RE = re.compile(r"([*?\[\]{}\\])")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _size_bytes(raw: str | None) -> int | None:
    match = _SIZE_RE.fullmatch((raw or "").strip())
    if not match:
        return None
    return round(
        float(match.group(1)) * 1024 ** _SIZE_UNITS.index(match.group(2).upper())
    )


def parse_dry_run_log(text: str, *, limit: int = 5000) -> dict[str, Any]:
    """Zerlegt rclone-Dry-Run-Zeilen; counts zählen alles, Listen enden bei limit."""
    lists: dict[str, list[dict[str, Any]]] = {
        "copies": [],
        "deletes": [],
        "moves": [],
        "other": [],
    }
    counts = dict.fromkeys(lists, 0)
    total = 0
    for raw in text.split("\n"):
        line = raw.replace("\r", "")
        match = None if line.startswith("# ") else _SKIPPED_RE.fullmatch(line)
        if not match:
            continue
        path, action = match.group("path"), match.group("action")
        if action in ("copy", "delete"):
            key = "copies" if action == "copy" else "deletes"
            item: dict[str, Any] = {
                "path": path,
                "size_bytes": _size_bytes(match.group("size")),
            }
        elif action == "move" or action.startswith("move to "):
            key, item = "moves", {"path": path, "to": action[8:] or None}
        else:
            key, item = "other", {"path": path, "action": action}
        counts[key] += 1
        total += 1
        if total <= limit:
            lists[key].append(item)
    return {**lists, "counts": counts, "truncated": total > limit}


def _pair_matrix(pair: dict[str, Any], root: Path) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": pair.get("name"),
        "direction": pair.get("direction"),
        "mode": pair.get("mode"),
        "log_file": pair.get("log_file") or None,
        "matrix": None,
    }
    if not entry["log_file"]:
        return entry
    path = Path(str(entry["log_file"])).resolve()
    if not is_relative_to(path, root):
        entry["error"] = "Logdatei liegt außerhalb des Logverzeichnisses"
        return entry
    try:
        with path.open("rb") as handle:
            data = handle.read(_MAX_LOG_BYTES + 1)
    except OSError:
        entry["error"] = "Logdatei ist nicht lesbar"
        return entry
    matrix = parse_dry_run_log(data[:_MAX_LOG_BYTES].decode("utf-8", errors="replace"))
    matrix["truncated"] = matrix["truncated"] or len(data) > _MAX_LOG_BYTES
    entry["matrix"] = matrix
    return entry


def job_matrix(
    job_id: int, *, db: Any = None, logs_root: Path | str | None = None
) -> dict[str, Any]:
    """Matrix je Datenweg; LookupError ohne Job, ValueError ohne Dry-Run."""
    job = (db or get_db()).job_get(job_id)
    if not job:
        raise LookupError("Job nicht gefunden")
    if job.get("kind") not in PREFLIGHT_KINDS or not job.get("dry_run"):
        raise ValueError(
            "Preflight-Matrix gibt es nur für Dry-Run-Läufe von Backup oder Quick-Sync"
        )
    root = Path(logs_root).resolve() if logs_root else maintenance.logs_root()
    summary = job.get("summary") or {}
    # Quick-Sync speichert einen flachen Summary ohne "pairs".
    pairs = summary["pairs"] if "pairs" in summary else [summary]
    return {
        "job_id": job_id,
        "dry_run": True,
        "pairs": [_pair_matrix(pair, root) for pair in pairs],
    }


def exclude_filter_args(paths: list[str]) -> list[str]:
    """rclone-Filter, die die Pfade (relativ zum Datenweg-Root) ausnehmen."""
    unique = list(dict.fromkeys(paths))
    if len(unique) > _MAX_EXCLUDES:
        raise ValueError(f"Höchstens {_MAX_EXCLUDES} Pfade können ausgenommen werden")
    args: list[str] = []
    for path in unique:
        if not isinstance(path, str) or not path:
            raise ValueError("Leerer Pfad kann nicht ausgenommen werden")
        if len(path) > _MAX_PATH_LEN:
            raise ValueError(f"Pfad ist länger als {_MAX_PATH_LEN} Zeichen")
        if path.startswith("/"):
            raise ValueError("Pfad muss relativ sein (ohne führendes /)")
        if ".." in path.split("/"):
            raise ValueError("Pfad darf kein ..-Segment enthalten")
        if _CONTROL_RE.search(path):
            raise ValueError("Pfad enthält Steuerzeichen")
        # _protected_filter_args strippt Filterwerte; ein Ausschluss träfe sonst die falsche Datei.
        if path != path.rstrip():
            raise ValueError("Pfad darf nicht mit Leerzeichen enden")
        args += ["--filter", "- /" + _GLOB_SPECIAL_RE.sub(r"\\\1", path)]
    return args
