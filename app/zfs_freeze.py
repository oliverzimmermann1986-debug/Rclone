"""ZFS-Snapshot als eingefrorene Quelle für push-Läufe.

rclone liest dann aus ``<mountpoint>/.zfs/snapshot/<name>/…`` statt aus dem
laufenden Dateisystem; der Snapshot wird danach wieder zerstört.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Callable, Iterator, Mapping

logger = logging.getLogger(__name__)

# Erstes Zeichen alphanumerisch, damit kein Name als zfs-Option gelesen wird;
# ohne "@", "%" und "," kann destroy nie mehrere Snapshots treffen.
_DATASET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:-]*$")
_SNAPSHOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_OWN_RE = re.compile(r"sicherpfad-([0-9]{8}-[0-9]{6})-[0-9a-f]{6}")
_STALE_AFTER = timedelta(hours=24)

Run = Callable[..., Any]


def _zfs(args: list[str], run: Run, timeout: int) -> str:
    try:
        result = run(
            ["zfs", *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "zfs-Werkzeuge nicht installiert (Paket zfsutils-linux)"
        ) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"zfs {args[0]}: Timeout nach {timeout}s") from None
    except OSError as exc:
        raise RuntimeError(f"zfs {args[0]} nicht ausführbar: {exc}") from None
    if result.returncode != 0:
        detail = result.stderr or result.stdout or f"exit {result.returncode}"
        raise RuntimeError(f"zfs {args[0]} fehlgeschlagen: {detail.strip()[:300]}")
    return result.stdout or ""


def _under(path: str, mountpoint: str) -> bool:
    # Segmentgenau: /tank/data2 liegt nicht unter /tank/data.
    return path == mountpoint or path.startswith(mountpoint.rstrip("/") + "/")


def _check_names(dataset: str, name: str) -> None:
    if not _DATASET_RE.match(dataset) or not _SNAPSHOT_RE.match(name):
        raise ValueError(f"Ungültiger ZFS-Name: {dataset}@{name}")


def dataset_for_path(path: str, *, run: Run = subprocess.run) -> tuple[str, str] | None:
    """(Dataset, Mountpoint) des eingehängten Datasets, das ``path`` enthält.

    Liegt unter ``path`` ein weiteres eingehängtes Dataset, wäre dessen Inhalt
    im Snapshot leer; das wird mit RuntimeError abgelehnt.
    """
    real = os.path.realpath(path)
    mounted: list[tuple[str, str]] = []
    output = _zfs(
        ["list", "-H", "-o", "name,mountpoint,mounted", "-t", "filesystem"], run, 30
    )
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 3 or fields[2] != "yes" or not fields[1].startswith("/"):
            continue  # none/legacy/-, nicht eingehängt
        mounted.append((fields[0], fields[1]))
    best = max(
        (item for item in mounted if _under(real, item[1])),
        key=lambda item: len(item[1]),
        default=None,
    )
    if best is not None:
        nested = [n for n, mp in mounted if mp != best[1] and _under(mp, real)]
        if nested:
            raise RuntimeError(
                f"Unter {real} liegt ein weiteres ZFS-Dataset ({nested[0]}); "
                "der Snapshot würde dessen Inhalt nicht enthalten"
            )
    return best


def create_snapshot(dataset: str, name: str, *, run: Run = subprocess.run) -> None:
    _check_names(dataset, name)
    _zfs(["snapshot", f"{dataset}@{name}"], run, 120)


def destroy_snapshot(dataset: str, name: str, *, run: Run = subprocess.run) -> None:
    _check_names(dataset, name)
    _zfs(["destroy", f"{dataset}@{name}"], run, 120)


def _destroy_stale(dataset: str, *, run: Run = subprocess.run) -> dict:
    """Zerstört eigene Snapshots > 24 h, die ein hart beendeter Lauf hinterließ.

    Fehler werden nur geloggt und gezählt; jüngere gehören evtl. einem parallelen Lauf.
    """
    counts = {"destroyed": 0, "failed": 0}
    try:
        if not _DATASET_RE.match(dataset):
            raise ValueError(f"Ungültiger ZFS-Name: {dataset}")
        names = _zfs(["list", "-H", "-t", "snapshot", "-o", "name", dataset], run, 60)
    except (RuntimeError, ValueError) as exc:
        logger.error("Alte ZFS-Snapshots von %s nicht lesbar: %s", dataset, exc)
        counts["failed"] += 1
        return counts
    cutoff = datetime.now() - _STALE_AFTER
    for line in names.splitlines():
        owner, _, name = line.strip().partition("@")
        match = _OWN_RE.fullmatch(name)
        if owner != dataset or not match:
            continue
        try:
            if datetime.strptime(match.group(1), "%Y%m%d-%H%M%S") >= cutoff:
                continue
            destroy_snapshot(dataset, name, run=run)
            counts["destroyed"] += 1
        except (RuntimeError, ValueError) as exc:
            logger.error(
                "Alter ZFS-Snapshot %s@%s nicht entfernt: %s", dataset, name, exc
            )
            counts["failed"] += 1
    return counts


def snapshot_source(mountpoint: str, snapshot: str, path: str) -> str:
    if not _under(path, mountpoint):
        raise ValueError(f"{path} liegt nicht unter {mountpoint}")
    base = mountpoint.rstrip("/")
    rest = path[len(base) :].strip("/")
    root = f"{base}/.zfs/snapshot/{snapshot}"
    return f"{root}/{rest}" if rest else root


@contextmanager
def frozen_source(
    pair: Mapping[str, Any], *, run: Run = subprocess.run
) -> Iterator[dict[str, Any]]:
    """Liefert die Quelle für rclone; mit ``zfs_snapshot`` aus einem Snapshot."""
    if pair.get("zfs_snapshot") is not True:
        yield {"active": False, "source": pair.get("local")}
        return
    if str(pair.get("direction") or "bisync").lower().strip() != "push":
        raise RuntimeError("zfs_snapshot ist nur für direction: push möglich")
    local = str(pair.get("local") or "")
    if not local.startswith("/"):
        raise RuntimeError("zfs_snapshot braucht einen absoluten lokalen Pfad")
    real = os.path.realpath(local)
    found = dataset_for_path(real, run=run)
    if found is None:
        raise RuntimeError(f"Pfad liegt auf keinem ZFS-Dataset: {local}")
    dataset, mountpoint = found
    name = f"sicherpfad-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    result: dict[str, Any] = {
        "active": True,
        "source": snapshot_source(mountpoint, name, real),
        "dataset": dataset,
        "snapshot": name,
        "stale_cleanup": _destroy_stale(dataset, run=run),
    }
    create_snapshot(dataset, name, run=run)
    try:
        yield result
    finally:
        try:
            destroy_snapshot(dataset, name, run=run)
        except (RuntimeError, ValueError) as exc:
            logger.error("ZFS-Snapshot %s@%s nicht entfernt: %s", dataset, name, exc)
            result["cleanup_error"] = str(exc)
