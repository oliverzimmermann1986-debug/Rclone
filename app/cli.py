"""Admin-CLI: Passwort/Secret, Doctor, Plan, Check und Log-Wartung."""
from __future__ import annotations

import argparse
import getpass
import json
import secrets
import sys
from pathlib import Path

import bcrypt

from .config_store import get_config


def cmd_set_password(args):
    pw = args.password
    if not pw:
        pw = getpass.getpass("Neues Passwort: ")
        pw2 = getpass.getpass("Wiederholen: ")
        if pw != pw2:
            print("✗ Passwörter unterschiedlich")
            return 1
    if len(pw) < 8:
        print("⚠ Mindestens 8 Zeichen empfohlen")
    h = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")
    cfg = get_config()
    cfg.set("web", "password_hash", h)
    cfg.set("web", "password", "")
    cfg.save()
    print("✓ Passwort gespeichert")
    return 0


def cmd_gen_secret(_args):
    s = secrets.token_urlsafe(48)
    cfg = get_config()
    cfg.set("web", "secret_key", s)
    cfg.save()
    print(f"✓ Neuer secret_key gesetzt ({len(s)} Zeichen)")
    return 0


def cmd_plan(args):
    from .jobs.rclone_sync import build_job_plan
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()] if args.pairs else None
    print(json.dumps(build_job_plan(dry_run=not args.no_dry_run, pairs_filter=pairs), ensure_ascii=False, indent=2))
    return 0


def cmd_doctor(_args):
    # Nutzt dieselbe Logik wie der Backend-Doctor, aber ohne HTTP/Auth.
    from .routes.api_diagnostics import doctor
    result = doctor()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


def cmd_check_pair(args):
    from .jobs.rclone_sync import run_pair_check
    result = run_pair_check(args.name, one_way=args.one_way, download=args.download)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def cmd_prune_logs(args):
    import time
    root = Path(get_config().get("paths", "logs_dir", default="/opt/rclone-sync/logs"))
    cutoff = time.time() - args.days * 86400
    matched = deleted = bytes_deleted = 0
    if root.exists():
        for p in root.rglob("*.log"):
            if p.is_file() and p.stat().st_mtime < cutoff:
                matched += 1
                size = p.stat().st_size
                if not args.dry_run:
                    try:
                        p.unlink()
                        deleted += 1
                        bytes_deleted += size
                    except OSError as e:
                        print(f"⚠ konnte nicht löschen: {p}: {e}")
    print(json.dumps({"dry_run": args.dry_run, "matched": matched, "deleted": deleted, "bytes_deleted": bytes_deleted}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("set-password", help="Web-Passwort setzen")
    p.add_argument("password", nargs="?")
    p.set_defaults(func=cmd_set_password)

    p = sub.add_parser("gen-secret", help="Session-Secret neu erzeugen")
    p.set_defaults(func=cmd_gen_secret)

    p = sub.add_parser("plan", help="rclone-Ausführungsplan anzeigen")
    p.add_argument("--pairs", help="Kommagetrennte Pair-Namen")
    p.add_argument("--no-dry-run", action="store_true", help="Plan ohne --dry-run zeigen")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("doctor", help="Diagnose ausführen")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("check-pair", help="Read-only rclone check für Pair")
    p.add_argument("name")
    p.add_argument("--one-way", action="store_true")
    p.add_argument("--download", action="store_true")
    p.set_defaults(func=cmd_check_pair)

    p = sub.add_parser("prune-logs", help="alte Logs löschen")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--delete", dest="dry_run", action="store_false")
    p.set_defaults(func=cmd_prune_logs)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
