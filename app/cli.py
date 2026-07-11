"""Admin-CLI: Passwort/Secret, Doctor, Plan, Check und Log-Wartung."""

from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
import secrets
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import bcrypt
import yaml

from .config_store import get_config
from .config_validation import ConfigValidationError, validate_config
from .db import get_db
from .maintenance import prune_logs


def _rotate_sessions(data: dict) -> dict:
    web = data.setdefault("web", {})
    try:
        version = int(web.get("session_version", 1) or 1)
    except (TypeError, ValueError):
        version = 1
    web["session_version"] = max(1, version) + 1
    return web


def cmd_set_password(args) -> int:
    password = args.password
    if not password:
        password = getpass.getpass("Neues Passwort: ")
        confirmation = getpass.getpass("Wiederholen: ")
        if password != confirmation:
            print("✗ Passwörter unterschiedlich")
            return 1
    if password != password.strip():
        print("✗ Passwort darf nicht mit Leerzeichen beginnen oder enden")
        return 1
    if len(password) < 12:
        print("✗ Passwort muss mindestens 12 Zeichen haben")
        return 1
    password_hash = bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=12)
    ).decode("ascii")

    def updater(data: dict) -> None:
        web = _rotate_sessions(data)
        web["password_hash"] = password_hash
        web["password"] = ""

    store = get_config()
    store.update(updater)
    data_dir = Path(store.get("paths", "data_dir", default="/opt/rclone-sync/data"))
    try:
        (data_dir / ".initial-password").unlink(missing_ok=True)
    except OSError:
        pass
    print("✓ Passwort gespeichert; bestehende Sessions wurden abgemeldet")
    return 0


def cmd_gen_secret(_args) -> int:
    secret = secrets.token_urlsafe(48)

    def updater(data: dict) -> None:
        web = _rotate_sessions(data)
        web["secret_key"] = secret

    get_config().update(updater)
    print(
        f"✓ Neuer secret_key gesetzt ({len(secret)} Zeichen); bestehende Sessions wurden abgemeldet"
    )
    return 0


def cmd_plan(args) -> int:
    from .jobs.rclone_sync import build_job_plan

    pairs = (
        [part.strip() for part in args.pairs.split(",") if part.strip()]
        if args.pairs
        else None
    )
    result = build_job_plan(dry_run=not args.no_dry_run, pairs_filter=pairs)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


def cmd_doctor(_args) -> int:
    from .routes.api_diagnostics import doctor

    result = doctor()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


def cmd_check_pair(args) -> int:
    from .jobs.rclone_sync import run_pair_check

    result = run_pair_check(args.name, one_way=args.one_way, download=args.download)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def cmd_prune_logs(args) -> int:
    if args.days < 1:
        print("✗ --days muss mindestens 1 sein")
        return 1
    result = prune_logs(days=args.days, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_db_maintenance(args) -> int:
    if args.days < 1:
        print("✗ --days muss mindestens 1 sein")
        return 1
    if args.keep_latest < 0:
        print("✗ --keep-latest darf nicht negativ sein")
        return 1
    db = get_db()
    integrity = db.integrity_check()
    deleted = 0
    if args.delete:
        deleted = db.jobs_prune(args.days, args.keep_latest)
        db.auth_prune(7)
        db.checkpoint()
    print(
        json.dumps(
            {
                "integrity": integrity,
                "deleted_jobs": deleted,
                "stats": db.stats(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if integrity.get("ok") else 2


def _configured_path() -> Path:
    return Path(os.getenv("RCLONE_SYNC_CONFIG", "/opt/rclone-sync/data/config.yaml"))


@contextmanager
def _config_file_lock(path: Path) -> Iterator[None]:
    """Verwendet denselben Lockpfad wie Config, auch wenn die Hauptdatei defekt ist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("Config-Lock ist keine reguläre Datei")
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def cmd_validate_config(_args) -> int:
    try:
        normalized, warnings = validate_config(get_config().snapshot())
    except (OSError, UnicodeError, ValueError, ConfigValidationError) as exc:
        print(f"✗ Konfiguration ungültig: {exc}")
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "warnings": warnings,
                "pairs": len((normalized.get("backup") or {}).get("pairs") or []),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_restore_config_backup(args) -> int:
    primary = _configured_path()
    backup = primary.with_suffix(primary.suffix + ".bak")
    if not backup.exists() or backup.is_symlink() or not backup.is_file():
        print(f"✗ Keine sichere reguläre Sicherung gefunden: {backup}")
        return 1
    if not args.yes:
        print("Abbruch: --yes ist für die Wiederherstellung erforderlich.")
        print(f"Quelle: {backup}")
        print(f"Ziel:   {primary}")
        return 1

    try:
        with _config_file_lock(primary):
            raw = backup.read_bytes()
            parsed = yaml.safe_load(raw.decode("utf-8")) or {}
            validate_config(parsed)

            primary.parent.mkdir(parents=True, exist_ok=True)
            invalid = None
            if primary.exists() or primary.is_symlink():
                invalid = primary.with_name(
                    f"{primary.name}.invalid-{time.strftime('%Y%m%d-%H%M%S')}"
                )
                os.replace(primary, invalid)
                os.chmod(invalid, stat.S_IRUSR | stat.S_IWUSR)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{primary.name}.",
                suffix=".restore.tmp",
                dir=str(primary.parent),
            )
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
                os.replace(tmp, primary)
                try:
                    directory_fd = os.open(primary.parent, os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
            finally:
                tmp.unlink(missing_ok=True)
    except (
        OSError,
        UnicodeError,
        yaml.YAMLError,
        ConfigValidationError,
        ValueError,
    ) as exc:
        print(f"✗ Sicherung konnte nicht wiederhergestellt werden: {exc}")
        return 2

    print(f"✓ Konfiguration aus {backup} wiederhergestellt")
    if invalid:
        print(f"  Vorherige Datei gesichert als {invalid}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    command = sub.add_parser("set-password", help="Web-Passwort setzen")
    command.add_argument("password", nargs="?")
    command.set_defaults(func=cmd_set_password)

    command = sub.add_parser("gen-secret", help="Session-Secret neu erzeugen")
    command.set_defaults(func=cmd_gen_secret)

    command = sub.add_parser("plan", help="rclone-Ausführungsplan anzeigen")
    command.add_argument("--pairs", help="Kommagetrennte Pair-Namen")
    command.add_argument(
        "--no-dry-run", action="store_true", help="Plan ohne --dry-run zeigen"
    )
    command.set_defaults(func=cmd_plan)

    command = sub.add_parser("doctor", help="Diagnose ausführen")
    command.set_defaults(func=cmd_doctor)

    command = sub.add_parser("check-pair", help="Read-only rclone check für Pair")
    command.add_argument("name")
    command.add_argument("--one-way", action="store_true")
    command.add_argument("--download", action="store_true")
    command.set_defaults(func=cmd_check_pair)

    command = sub.add_parser("prune-logs", help="alte Logs löschen")
    command.add_argument("--days", type=int, default=30)
    command.add_argument("--dry-run", action="store_true", default=True)
    command.add_argument("--delete", dest="dry_run", action="store_false")
    command.set_defaults(func=cmd_prune_logs)

    command = sub.add_parser(
        "db-maintenance", help="DB prüfen und optional alte Jobs löschen"
    )
    command.add_argument("--days", type=int, default=180)
    command.add_argument("--keep-latest", type=int, default=500)
    command.add_argument("--delete", action="store_true")
    command.set_defaults(func=cmd_db_maintenance)

    command = sub.add_parser(
        "validate-config", help="Konfiguration vollständig validieren"
    )
    command.set_defaults(func=cmd_validate_config)

    command = sub.add_parser(
        "restore-config-backup",
        help="letzte automatisch erzeugte config.yaml.bak wiederherstellen",
    )
    command.add_argument(
        "--yes", action="store_true", help="Wiederherstellung verbindlich bestätigen"
    )
    command.set_defaults(func=cmd_restore_config_backup)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
