"""GFS-Ausdünnung historischer --backup-dir-Versionsordner."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from typing import Any, Callable

from .jobs.rclone_sync import _is_remote, _resolve_backup_path
from .rclone_args import rclone_subprocess_env
from .utils import bounded_number

STAMP_FORMAT = "%Y-%m-%dT%H-%M-%S"
# ASCII-only: \d würde auch Unicode-Ziffern in Fremdordnern akzeptieren.
_STAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}")
_DATE_LAST_SEGMENT = re.compile(r"(.*[/:])?\{date\}")
DEFAULTS = {"enabled": False, "keep_daily": 7, "keep_weekly": 4, "keep_monthly": 12}
_LIMITS = {"keep_daily": 365, "keep_weekly": 104, "keep_monthly": 120}
LIST_TIMEOUT = 120
PURGE_TIMEOUT = 1800


def select_gfs(
    stamps: list[datetime], *, keep_daily: int, keep_weekly: int, keep_monthly: int
) -> dict[str, Any]:
    """Teilt Versionen in Tages-, dann ISO-Wochen-, dann Monats-Buckets (jüngste zuerst).

    --backup-dir-Ordner sind Deltas (nur die im Lauf verdrängten Dateien): Je Bucket
    bleibt der älteste Ordner, die jüngeren werden in ihn zusammengeführt (merge);
    nur Ordner außerhalb aller Buckets werden gelöscht (purge, neueste zuerst).
    """
    rest = sorted(set(stamps), reverse=True)
    buckets: list[list[datetime]] = []
    for count, key in (
        (keep_daily, lambda s: s.date()),
        (keep_weekly, lambda s: s.isocalendar()[:2]),
        (keep_monthly, lambda s: (s.year, s.month)),
    ):
        groups: dict[Any, list[datetime]] = {}
        for stamp in rest:
            groups.setdefault(key(stamp), []).append(stamp)
        chosen = list(groups.values())[: max(0, count)]
        buckets += chosen
        rest = rest[sum(map(len, chosen)) :]  # Gruppen liegen zeitlich am Stück
    return {
        "keep": {group[-1] for group in buckets},
        "merge": {group[-1]: sorted(group[:-1]) for group in buckets if len(group) > 1},
        "purge": rest,
    }


def version_roots(
    cfg_backup: dict, pair: dict, *, warnings: list[str] | None = None
) -> list[str]:
    """Elternordner der {date}-Versionen je Schreibseite (wie _backup_dir_args)."""
    warnings = [] if warnings is None else warnings

    def spec(key: str) -> str:
        return str(pair.get(key) or cfg_backup.get(key) or "").strip()

    generic = spec("backup_dir")
    remote = str(pair.get("remote") or "").strip()
    local = str(pair.get("local") or "").strip()
    direction = str(pair.get("direction") or "bisync").lower().strip()
    if direction == "bisync":
        shared = "" if _is_remote(generic) or generic.startswith("/") else generic
        sides = [
            (remote, spec("backup_dir1") or shared, "backup_dir1"),
            (local, spec("backup_dir2") or shared, "backup_dir2"),
        ]
    elif direction in ("push", "pull"):
        sides = [(remote if direction == "push" else local, generic, "backup_dir")]
    else:
        warnings.append(f"Unbekannte direction {direction!r}; keine Ausdünnung")
        return []

    roots: list[str] = []
    for target, raw, key in sides:
        raw = raw.rstrip("/")
        if "{date}" not in raw:
            continue  # fester Ordner ohne Versionen
        match = _DATE_LAST_SEGMENT.fullmatch(raw)
        if not match or "{date}" in (match.group(1) or ""):
            warnings.append(
                f"backup_dir {raw!r}: {{date}} muss das letzte Pfadsegment sein; keine Ausdünnung"
            )
            continue
        parent = match.group(1) or ""
        parent = parent.rstrip("/") or parent  # "/" bleibt als absolute Wurzel
        absolute = _is_remote(parent) or parent.startswith("/")
        # Globales absolutes Ziel teilen sich alle Pairs; parallele Läufe schreiben hinein.
        if absolute and not str(pair.get(key) or "").strip():
            warnings.append(
                f"backup_dir {raw!r}: geteilte Versionswurzel wird nicht ausgedünnt (Pair-eigenes backup_dir setzen)"
            )
            continue
        if not parent or not (absolute or target):
            warnings.append(
                f"backup_dir {raw!r}: Versionen lägen direkt im Sync-Ziel; keine Ausdünnung"
            )
            continue
        roots.append(_resolve_backup_path(target, parent, ""))
    return list(dict.fromkeys(roots))


def _rclone(args: list[str], run: Callable[..., Any], timeout: int) -> Any:
    return run(
        ["rclone", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        env=rclone_subprocess_env(),
    )


def list_versions(
    root: str, *, run: Callable[..., Any] = subprocess.run
) -> list[dict[str, Any]]:
    """Nur exakt zeitgestempelte Unterordner; fehlender Root ergibt []."""
    result = _rclone(
        ["lsjson", "--dirs-only", "--no-mimetype", "--no-modtime", "--", root],
        run,
        LIST_TIMEOUT,
    )
    if result.returncode:
        error = (result.stderr or result.stdout or "").strip()
        if result.returncode == 3 or "directory not found" in error.lower():
            return []
        raise RuntimeError(f"rclone lsjson exit={result.returncode}: {error[:500]}")
    versions = []
    for entry in json.loads(result.stdout or "[]"):
        name = str(entry.get("Name") or "")
        if not _STAMP_RE.fullmatch(name):
            continue
        try:
            stamp = datetime.strptime(name, STAMP_FORMAT)
        except ValueError:
            continue
        versions.append({"name": name, "stamp": stamp})
    return versions


def apply_lifecycle(
    cfg: dict,
    pair: dict,
    *,
    dry_run: bool = True,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Wendet GFS auf alle Versionswurzeln eines Pairs an (dry_run: nur Plan).

    Vor jedem move/rmdirs/purge wird der Lockdown geprüft; aktiv -> Abbruch mit
    ``aborted: "lockdown"``.
    """
    backup = cfg.get("backup") or {}
    raw = pair.get("versions_lifecycle") or backup.get("versions_lifecycle") or {}
    settings = {**DEFAULTS, **(raw if isinstance(raw, dict) else {})}
    if settings["enabled"] is not True:
        return {"skipped": True, "reason": "disabled"}
    counts = {
        key: int(
            bounded_number(
                settings[key], default=DEFAULTS[key], minimum=0, maximum=limit
            )
        )
        for key, limit in _LIMITS.items()
    }

    warnings: list[str] = []
    roots: list[dict[str, Any]] = []
    outcome: dict[str, Any] = {"dry_run": dry_run, "roots": roots, "warnings": warnings}

    def locked() -> bool:
        from .lockdown import is_locked_down  # lokal: lockdown importiert db

        if not is_locked_down():
            return False
        outcome["aborted"] = "lockdown"
        warnings.append("Lockdown aktiv; Ausdünnung abgebrochen")
        return True

    def step(*args: str) -> None:
        result = _rclone(list(args), run, PURGE_TIMEOUT)
        if result.returncode:
            detail = (result.stderr or result.stdout or "").strip()[:500]
            raise RuntimeError(f"rclone {args[0]} exit={result.returncode}: {detail}")

    for root in version_roots(backup, pair, warnings=warnings):
        entry: dict[str, Any] = {
            "root": root,
            "total": 0,
            "keep": [],
            "merge": [],
            "purge": [],
            "merged": [],
            "purged": [],
            "failed": [],
        }
        roots.append(entry)
        try:
            names = {v["stamp"]: v["name"] for v in list_versions(root, run=run)}
        except Exception as exc:
            entry["error"] = f"Versionen nicht lesbar: {exc}"[:500]
            continue
        plan = select_gfs(list(names), **counts)
        entry.update(
            total=len(names),
            keep=sorted(names[stamp] for stamp in plan["keep"]),
            merge=[
                {"into": names[into], "from": names[stamp]}
                for into, younger in sorted(plan["merge"].items())
                for stamp in younger  # aufsteigend: ältere Vorversion zuerst
            ],
            purge=[names[stamp] for stamp in plan["purge"]],
        )
        if entry["purge"] and len(entry["purge"]) >= len(names):
            entry["error"] = "Schutz: Lifecycle würde alle Versionen löschen"
            continue
        if dry_run:
            continue
        for item in entry["merge"]:
            if locked():
                return outcome
            done = {**item, "ok": False, "error": None}
            entry["merged"].append(done)
            source = _resolve_backup_path(root, item["from"], "")
            try:
                # Vorhandenes gewinnt: je Pfad bleibt die älteste Vorversion im Bucket.
                step(
                    "move",
                    "--ignore-existing",
                    "--",
                    source,
                    _resolve_backup_path(root, item["into"], ""),
                )
                # move rc 0: Reste sind nur übersprungene Duplikate, deren ältere
                # Version schon im Vertreter liegt; rmdirs entfernt danach nur Leeres.
                for verb in ("delete", "rmdirs"):
                    if locked():
                        done["error"] = "Lockdown"
                        return outcome
                    step(verb, "--", source)
                done["ok"] = True
            except Exception as exc:
                done["error"] = str(exc)[:500]
                entry["failed"].append({"name": item["from"], "error": done["error"]})
        if any(done["ok"] for done in entry["merged"]):
            # rmdirs meldet Erfolg auch, wenn der Ordner nicht leer war.
            try:
                left = {v["name"] for v in list_versions(root, run=run)}
            except Exception as exc:
                left = set()
                entry["error"] = f"Zusammenführung nicht prüfbar: {exc}"[:500]
            for done in entry["merged"]:
                if done["ok"] and done["from"] in left:
                    done.update(ok=False, error="Ordner nach move nicht leer")
                    entry["failed"].append(
                        {"name": done["from"], "error": done["error"]}
                    )
        for name in entry["purge"]:
            if locked():
                return outcome
            try:
                step("purge", "--", _resolve_backup_path(root, name, ""))
                entry["purged"].append(name)
            except Exception as exc:
                entry["failed"].append({"name": name, "error": str(exc)[:500]})
    return outcome
