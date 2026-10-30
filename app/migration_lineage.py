"""Signed, durable historical snapshot bindings created only by verified cutovers."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from typing import Mapping

import yaml

from .device_vault import _atomic_json
from .jobs.restore_test import _endpoints

SCHEMA = "sicherpfad-migration-lineage-v1"
MAX_LEDGER_BYTES = 2 * 1024 * 1024
MAX_BINDINGS = 4096
MAX_ANCESTORS = 128


def _path(config):
    from .recovery_snapshots import snapshot_root

    return snapshot_root(config) / ".migration-lineage.json"


def _raw(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _key_path(config):
    return _path(config).with_name(".migration-lineage.key")


def _key(config, *, create=False):
    path = _key_path(config)
    if path.is_symlink():
        raise ValueError("Unsicherer Schlüssel der Migrationshistorie")
    if not path.exists():
        if not create or _path(config).exists() or _path(config).is_symlink():
            raise ValueError(
                "Schlüssel der Migrationshistorie fehlt; er darf nicht neu erzeugt werden"
            )
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        value = secrets.token_bytes(32)
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o600)
        return value
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size != 32
            or (os.name == "posix" and info.st_uid != os.geteuid())
            or (os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077)
        ):
            raise ValueError("Unsicherer Schlüssel der Migrationshistorie")
        value = handle.read(33)
        if len(value) != 32:
            raise ValueError("Schlüssel der Migrationshistorie ist beschädigt")
        return value


def _read(config):
    path = _path(config)
    if path.is_symlink():
        raise ValueError("Unsicherer Nachweis der Migrationshistorie")
    if not path.exists() and not path.is_symlink():
        return []
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > MAX_LEDGER_BYTES
            or (os.name == "posix" and info.st_uid != os.geteuid())
            or (os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077)
        ):
            raise ValueError("Unsicherer Nachweis der Migrationshistorie")
        try:
            value = json.loads(handle.read(MAX_LEDGER_BYTES + 1))
        except ValueError as exc:
            raise ValueError("Migrationshistorie ist beschädigt") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError("Migrationshistorie ist beschädigt")
    body = {"schema": SCHEMA, "bindings": value.get("bindings")}
    signature = hmac.new(_key(config), _raw(body), hashlib.sha256).hexdigest()
    if not isinstance(value.get("signature"), str) or not hmac.compare_digest(
        signature, value["signature"]
    ):
        raise ValueError("Signatur der Migrationshistorie ist ungültig")
    bindings = body["bindings"]
    if not isinstance(bindings, list) or len(bindings) > MAX_BINDINGS:
        raise ValueError("Migrationshistorie überschreitet das Prüflimit")
    for row in bindings:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("ancestors"), list)
            or len(row["ancestors"]) > MAX_ANCESTORS
            or row.get("phase") not in {"prepared", "committed"}
        ):
            raise ValueError("Migrationshistorie ist beschädigt")
    return bindings


def _write(config, bindings):
    body = {"schema": SCHEMA, "bindings": bindings}
    body["signature"] = hmac.new(
        _key(config, create=True), _raw(body), hashlib.sha256
    ).hexdigest()
    if (
        len(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True).encode())
        > MAX_LEDGER_BYTES
    ):
        raise ValueError("Migrationshistorie überschreitet das Größenlimit")
    path = _path(config)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Unsicherer Nachweis der Migrationshistorie")
    _atomic_json(path, body)


def _current(config, pair):
    from .recovery_snapshots import pair_fingerprint

    identity = str(pair.get("id") or "")
    if (
        not identity
        or len(identity) != 32
        or any(c not in "0123456789abcdef" for c in identity)
    ):
        raise ValueError("Migration benötigt eine stabile Datenweg-ID")
    return identity, _endpoints(pair)[0], pair_fingerprint(pair)


def accepts(config: Mapping, pair: Mapping, manifest: Mapping) -> bool:
    identity, source, current = _current(config, pair)
    for row in reversed(_read(config)):
        if (
            row.get("phase") == "committed"
            and row.get("identity") == identity
            and row.get("source") == source
            and row.get("target_fingerprint") == current
        ):
            return (
                str(manifest.get("identity") or "")
                in [identity, *row.get("legacy_identities", [])]
                and str(manifest.get("fingerprint") or "") in row["ancestors"]
            )
    return False


def prepare(config, old_pair, new_pair, *, migration_id: str, committed_revision: str):
    """Called inside the config updater while backup/config locks are held."""
    from .recovery_snapshots import pair_fingerprint

    identity, source, following = _current(config, new_pair)
    previous = pair_fingerprint(old_pair)
    if (old_pair.get("id") and old_pair["id"] != identity) or _endpoints(old_pair)[
        0
    ] != source:
        raise ValueError("Migration darf Quelle und Datenweg-ID nicht ändern")
    rows = _read(config)
    rows = [
        row
        for row in rows
        if not (row["phase"] == "prepared" and row.get("migration_id") == migration_id)
    ]
    ancestors = [previous]
    legacy_identities = [] if old_pair.get("id") else [str(old_pair.get("name") or "")]
    for row in reversed(rows):
        if (
            row.get("phase") == "committed"
            and row.get("identity") == identity
            and row.get("source") == source
            and row.get("target_fingerprint") == previous
        ):
            ancestors = list(dict.fromkeys([*row["ancestors"], previous]))
            legacy_identities = list(row.get("legacy_identities") or [])
            break
    if len(ancestors) > MAX_ANCESTORS or len(rows) >= MAX_BINDINGS:
        raise ValueError("Transitive Migrationshistorie überschreitet das Prüflimit")
    rows.append(
        {
            "migration_id": migration_id,
            "identity": identity,
            "source": source,
            "target_fingerprint": following,
            "ancestors": ancestors,
            "legacy_identities": legacy_identities,
            "phase": "prepared",
            "committed_revision": committed_revision,
            "verified_at": time.time(),
        }
    )
    _write(config, rows)


def commit(config, revision, migration_id=None):
    """Replay only the exact YAML revision prepared by a verified config commit."""
    rows, changed = _read(config), False
    pairs = (config.get("backup") or {}).get("pairs") or []
    for row in rows:
        if (
            row["phase"] != "prepared"
            or row["committed_revision"] != revision
            or (migration_id and row["migration_id"] != migration_id)
        ):
            continue
        for pair in pairs:
            try:
                identity, source, current = _current(config, pair)
            except ValueError:
                continue
            if (identity, source, current) == (
                row["identity"],
                row["source"],
                row["target_fingerprint"],
            ):
                row["phase"] = "committed"
                changed = True
                break
    if changed:
        _write(config, rows)


def config_revision(config):
    raw = yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def committed_ids(config):
    current = set()
    for pair in (config.get("backup") or {}).get("pairs") or []:
        try:
            current.add(_current(config, pair))
        except ValueError:
            continue
    return {
        row["migration_id"]
        for row in _read(config)
        if row["phase"] == "committed"
        and (row["identity"], row["source"], row["target_fingerprint"]) in current
    }
