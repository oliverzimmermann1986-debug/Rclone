"""Bisync-Konfliktkopien finden und per Entscheidung auflösen.

Seite 1 ist ``pair.remote`` (bisync path1), Seite 2 ist ``pair.local`` (path2).
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import date
from typing import Any, Callable

from .rclone_args import rclone_subprocess_env, redact_command_text

# a.txt.conflict-path1-3 trägt die Herkunft (siehe rclone_sync._global_verb_args);
# alte Kopien heißen a.txt.conflict3, mit --suffix-keep-extension a.conflict3.txt.
CONFLICT_RE = re.compile(
    r"^(?P<base>.+?)\.conflict(?:-path(?P<side>[12])-)?(?P<n>\d+)(?P<ext>\.[^./]*)?$"
)
CHOICES = ("keep_side1", "keep_side2", "keep_both")
MAX_GROUPS = 500
MAX_LIVE_GROUPS = 100
LIST_TIMEOUT = 120
# shortcut: Schritte laufen synchron im Request; große moveto ohne serverseitiges
# Move können das Limit reißen, dann als Hintergrundjob umsetzen.
STEP_TIMEOUT = 600
_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$")
_SIDES = ("side1", "side2")

Runner = Callable[..., Any]


class OriginUnclear(LookupError):
    """Keine eindeutige Kopie der gewählten Seite; die Route antwortet mit 409."""


def valid_rel_path(path: str) -> bool:
    """Relativer Pfad ohne ``..``-Segment, führenden Slash und Steuerzeichen."""
    return (
        bool(path)
        and not path.startswith("/")
        and ".." not in path.split("/")
        and not any(ord(ch) < 32 or ord(ch) == 127 for ch in path)
    )


def _roots(pair: dict[str, Any]) -> dict[str, str]:
    return {"side1": str(pair["remote"]), "side2": str(pair["local"])}


def _join(root: str, rel: str) -> str:
    if not rel:
        return root
    return root + ("" if root.endswith((":", "/")) else "/") + rel


def _rclone(args: list[str], run: Runner, timeout: int) -> Any:
    return run(
        ["rclone", *args],
        env=rclone_subprocess_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def _error(result: Any) -> str:
    text = redact_command_text(str(result.stderr or "").strip())[-300:]
    return text or f"rclone Exit-Code {result.returncode}"


def _lsjson(args: list[str], run: Runner) -> list[dict[str, Any]]:
    try:
        result = _rclone(["lsjson", *args], run, LIST_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Zeitlimit beim Auflisten mit rclone erreicht") from None
    except OSError:
        raise RuntimeError("rclone konnte nicht gestartet werden") from None
    if result.returncode == 3:  # rclone: Verzeichnis nicht gefunden
        return []
    if result.returncode:
        raise RuntimeError(f"rclone lsjson fehlgeschlagen: {_error(result)}")
    try:
        items = json.loads(result.stdout or "[]")
    except ValueError:
        raise RuntimeError(
            "Die rclone-Antwort konnte nicht verarbeitet werden"
        ) from None
    return (
        [item for item in items if isinstance(item, dict)]
        if isinstance(items, list)
        else []
    )


def _list_dir(root: str, directory: str, run: Runner) -> list[dict[str, Any]]:
    return _lsjson(["--files-only", "--no-mimetype", "--", _join(root, directory)], run)


def _entry(item: dict[str, Any], name: str, n: int) -> dict[str, Any]:
    return {
        "name": name,
        "size": item.get("Size"),
        "modtime": item.get("ModTime"),
        "conflict_n": n,
    }


def _base_name(match: re.Match[str]) -> str:
    return match["base"] + (match["ext"] or "")


def _origin(match: re.Match[str], base_present: bool) -> str | None:
    if match["side"]:
        return f"side{match['side']}"
    # Alte Nummern: 1=path1, 2=path2 gilt nur ohne Gewinner, also wenn bisync
    # das Original auf beiden Seiten umbenannt hat.
    if not base_present and match["n"] in ("1", "2"):
        return f"side{match['n']}"
    return None


def _side_winners(
    copies: dict[str, Any], base_present: bool
) -> dict[str, tuple[str, dict[str, Any]] | None]:
    """Setzt ``origin`` je Kopie; je Seite (Fundseite, Kopie) ihrer Fassung oder None."""
    located = [(side, entry) for side in _SIDES for entry in copies[side]]
    for _, entry in located:
        entry["origin"] = _origin(CONFLICT_RE.match(entry["name"]), base_present)
    known = all(entry["origin"] for _, entry in located)
    winners: dict[str, tuple[str, dict[str, Any]] | None] = {}
    for side in _SIDES:
        mine = [(at, entry) for at, entry in located if entry["origin"] == side]
        unique = known and len({entry["name"] for _, entry in mine}) == 1
        # bisync legt jede Kopie auf beide Seiten; die auf der gewählten bevorzugen.
        winners[side] = min(mine, key=lambda hit: hit[0] != side) if unique else None
    return winners


def list_conflicts(
    pair: dict[str, Any], *, run: Runner = subprocess.run
) -> dict[str, Any]:
    """Konfliktkopien beider Seiten, gruppiert nach dem ursprünglichen Pfad."""
    roots = _roots(pair)
    groups: dict[str, dict[str, Any]] = {}
    truncated = False
    skipped = 0
    # shortcut: listet beide Seiten rekursiv und synchron im Request (je bis
    # LIST_TIMEOUT, dazu Live-Abfragen); bei großen Remotes als Hintergrundjob.
    for side in _SIDES:
        listing = _lsjson(
            [
                "-R",
                "--files-only",
                "--no-mimetype",
                "--include",
                "*.conflict*",
                "--",
                roots[side],
            ],
            run,
        )
        for item in listing:
            path = str(item.get("Path") or "")
            directory, _, name = path.rpartition("/")
            match = CONFLICT_RE.match(name)
            if not match:
                continue
            if not valid_rel_path(path):
                skipped += 1
                continue
            base_name = _base_name(match)
            base_path = f"{directory}/{base_name}" if directory else base_name
            group = groups.get(base_path)
            if group is None:
                if len(groups) >= MAX_GROUPS:
                    truncated = True
                    continue
                group = groups[base_path] = {
                    "base_path": base_path,
                    "side1": [],
                    "side2": [],
                    "live": None,
                }
            group[side].append(_entry(item, name, int(match["n"])))

    if len(groups) <= MAX_LIVE_GROUPS:
        listings: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        for group in groups.values():
            directory, _, base_name = group["base_path"].rpartition("/")
            live: dict[str, Any] = {}
            for side in _SIDES:
                if (side, directory) not in listings:
                    listings[(side, directory)] = {
                        str(item.get("Name")): item
                        for item in _list_dir(roots[side], directory, run)
                    }
                item = listings[(side, directory)].get(base_name)
                live[side] = (
                    {"size": item.get("Size"), "modtime": item.get("ModTime")}
                    if item
                    else None
                )
            group["live"] = live

    for group in groups.values():
        for side in _SIDES:
            group[side].sort(key=lambda entry: entry["conflict_n"])
        live = group["live"]
        # Ohne Live-Abfrage (zu viele Gruppen) gilt das Original als vorhanden.
        winners = _side_winners(group, live is None or any(live.values()))
        group["side_resolvable"] = all(winners.values())
    return {
        "pair": str(pair.get("name") or ""),
        "side1": roots["side1"],
        "side2": roots["side2"],
        "groups": [groups[key] for key in sorted(groups)],
        "truncated": truncated,
        "skipped": skipped,
    }


def _keep_both_name(
    match: re.Match[str], side_no: int, modtime: Any, taken: set[str]
) -> str:
    stamp = _TS_RE.match(str(modtime or ""))
    day = stamp.group(1)[:10] if stamp else date.today().isoformat()
    stem, ext = f"{match['base']} (Konflikt Seite{side_no} {day}", match["ext"] or ""
    name, counter = f"{stem}){ext}", 2
    while name in taken:  # nie eine vorhandene oder geplante Datei überschreiben
        name, counter = f"{stem} {counter}){ext}", counter + 1
    taken.add(name)
    return name


def resolve_conflict(
    pair: dict[str, Any],
    base_path: str,
    choice: str,
    *,
    run: Runner = subprocess.run,
) -> dict[str, Any]:
    """Führt die Entscheidung als rclone-Schritte aus; bricht beim ersten Fehler ab."""
    if choice not in CHOICES:
        raise ValueError("Unbekannte Entscheidung")
    if not valid_rel_path(base_path):
        raise ValueError("Ungültiger Pfad")
    roots = _roots(pair)
    directory, _, base_name = base_path.rpartition("/")

    def rel(name: str) -> str:
        return f"{directory}/{name}" if directory else name

    names: dict[str, set[str]] = {}
    copies: dict[str, list[dict[str, Any]]] = {}
    for side in _SIDES:
        listing = _list_dir(roots[side], directory, run)
        names[side] = {str(item.get("Name")) for item in listing}
        copies[side] = []
        for item in listing:
            name = str(item.get("Name") or "")
            match = CONFLICT_RE.match(name)
            if match and _base_name(match) == base_name and valid_rel_path(name):
                copies[side].append(_entry(item, name, int(match["n"])))
    if not copies["side1"] and not copies["side2"]:
        raise LookupError("Für diesen Pfad gibt es keine Konfliktkopien")
    winners = _side_winners(copies, base_name in names["side1"] | names["side2"])

    plan: list[list[str]] = []
    if choice == "keep_both":
        for side_no, side in enumerate(_SIDES, start=1):
            for copy in sorted(copies[side], key=lambda entry: entry["conflict_n"]):
                match = CONFLICT_RE.match(copy["name"])
                target = _keep_both_name(match, side_no, copy["modtime"], names[side])
                plan.append(
                    [
                        "moveto",
                        "--",
                        _join(roots[side], rel(copy["name"])),
                        _join(roots[side], rel(target)),
                    ]
                )
    else:
        keep = "side1" if choice == "keep_side1" else "side2"
        found = winners[keep]
        if found is None:
            raise OriginUnclear(
                "Herkunft der Konfliktkopien nicht eindeutig; "
                "nur ‚Beide behalten‘ möglich"
            )
        source, winner = found
        plan.append(
            [
                "moveto",
                "--",
                _join(roots[source], rel(winner["name"])),
                _join(roots[keep], base_path),
            ]
        )
        for side in _SIDES:
            for copy in copies[side]:
                if copy is not winner:
                    plan.append(
                        ["deletefile", "--", _join(roots[side], rel(copy["name"]))]
                    )

    # Lokal importiert: hält dieses Modul ohne DB/Config importierbar.
    from .lockdown import is_locked_down

    steps: list[dict[str, Any]] = []
    for args in plan:
        if is_locked_down():
            return {"steps": steps, "completed": False, "aborted": "lockdown"}
        try:
            result = _rclone(args, run, STEP_TIMEOUT)
            error = _error(result) if result.returncode else None
        except subprocess.TimeoutExpired:
            error = "Zeitlimit erreicht"
        except OSError:
            error = "rclone konnte nicht gestartet werden"
        steps.append({"cmd": ["rclone", *args], "ok": error is None, "error": error})
        if error:
            break
    return {
        "steps": steps,
        "completed": len(steps) == len(plan) and all(step["ok"] for step in steps),
    }
