"""CLI: set-password, generate-secret, status."""
from __future__ import annotations

import getpass
import secrets
import sys

import bcrypt

from .config_store import get_config


def cmd_set_password(args):
    if len(args) < 1:
        pw = getpass.getpass("Neues Passwort: ")
        pw2 = getpass.getpass("Wiederholen: ")
        if pw != pw2:
            print("✗ Passwörter unterschiedlich")
            return 1
    else:
        pw = args[0]
    if len(pw) < 8:
        print("⚠ Mindestens 8 Zeichen empfohlen")
    h = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")
    cfg = get_config()
    cfg.set("web", "password_hash", h)
    cfg.set("web", "password", "")
    cfg.save()
    print("✓ Passwort gespeichert")
    return 0


def cmd_gen_secret(args):
    s = secrets.token_urlsafe(48)
    cfg = get_config()
    cfg.set("web", "secret_key", s)
    cfg.save()
    print(f"✓ Neuer secret_key gesetzt ({len(s)} Zeichen)")
    return 0


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m app.cli {set-password|gen-secret}")
        return 1
    cmd = sys.argv[1]
    args = sys.argv[2:]
    cmds = {
        "set-password": cmd_set_password,
        "gen-secret": cmd_gen_secret,
    }
    if cmd not in cmds:
        print(f"Unbekannt: {cmd}")
        return 1
    return cmds[cmd](args)


if __name__ == "__main__":
    sys.exit(main())
