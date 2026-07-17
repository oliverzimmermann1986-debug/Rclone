#!/usr/bin/env bash
# Idempotenter Installer für Debian/Ubuntu-LXC.
set -Eeuo pipefail

APP_USER="rclone-sync"
APP_GROUP="rclone-sync"
APP_DIR="/opt/rclone-sync"
REPO_URL="${REPO_URL:-https://github.com/appear7240/rclone-sync-container.git}"
BRANCH="${BRANCH:-main}"
BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/rclone-sync}"
BACKUP_KEEP="${BACKUP_KEEP:-10}"
WEB_WAS_ACTIVE=0
SCHEDULER_WAS_ACTIVE=0
PREVIOUS_GIT_HEAD=""
SOURCE_UPDATE_STARTED=0
VENV_ROLLBACK=""
SYSTEM_FILES_ROLLBACK=""
SYSTEM_FILES_STARTED=0
SYSTEM_TARGETS=(
  /etc/systemd/system/rclone-sync-web.service
  /etc/systemd/system/rclone-sync.service
  /etc/systemd/system/rclone-sync.timer
  /etc/systemd/system/sync-scheduler.service
  /etc/systemd/system/sync-scheduler.timer
  /etc/sudoers.d/rclone-sync
)

on_error() {
  local line="$1"
  trap - ERR
  set +e
  echo "Installation fehlgeschlagen (Zeile $line). Rollback wird versucht." >&2
  if (( SOURCE_UPDATE_STARTED )) && [[ -n "$PREVIOUS_GIT_HEAD" && -d "$APP_DIR/.git" ]]; then
    git -c safe.directory="$APP_DIR" -C "$APP_DIR" reset --hard "$PREVIOUS_GIT_HEAD" >/dev/null 2>&1 || true
  fi
  if [[ -n "$VENV_ROLLBACK" && -d "$VENV_ROLLBACK" ]]; then
    rm -rf "$APP_DIR/venv"
    mv "$VENV_ROLLBACK" "$APP_DIR/venv" || true
  fi
  if (( SYSTEM_FILES_STARTED )) && [[ -d "$SYSTEM_FILES_ROLLBACK" ]]; then
    for target in "${SYSTEM_TARGETS[@]}"; do
      name=$(basename "$target")
      if [[ -f "$SYSTEM_FILES_ROLLBACK/$name" ]]; then
        cp -a "$SYSTEM_FILES_ROLLBACK/$name" "$target" || true
      elif [[ -f "$SYSTEM_FILES_ROLLBACK/$name.missing" ]]; then
        rm -f "$target"
      fi
    done
  fi
  [[ -z "$SYSTEM_FILES_ROLLBACK" ]] || rm -rf "$SYSTEM_FILES_ROLLBACK"
  systemctl daemon-reload >/dev/null 2>&1 || true
  if (( WEB_WAS_ACTIVE )); then systemctl start rclone-sync-web.service >/dev/null 2>&1 || true; fi
  if (( SCHEDULER_WAS_ACTIVE )); then systemctl start sync-scheduler.timer >/dev/null 2>&1 || true; fi
}
trap 'on_error "$LINENO"' ERR

if [[ ${EUID} -ne 0 ]]; then
  echo "Bitte als root oder mit sudo ausführen" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
printf '%s\n' "📦 Pakete installieren …"
apt-get update -qq
apt-get install -y --no-install-recommends \
  ca-certificates curl wget gnupg git openssl \
  python3 python3-venv python3-pip python3-dev build-essential \
  rclone sqlite3 sudo tzdata

ln -sf /usr/share/zoneinfo/Europe/Berlin /etc/localtime
printf '%s\n' "Europe/Berlin" > /etc/timezone

printf '%s\n' "👤 Systemkonto $APP_USER …"
if ! getent group "$APP_GROUP" >/dev/null; then
  groupadd --system "$APP_GROUP"
fi
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --gid "$APP_GROUP" --create-home --home-dir "/home/$APP_USER" --shell /bin/bash "$APP_USER"
fi

if systemctl is-active --quiet rclone-sync-web.service 2>/dev/null; then WEB_WAS_ACTIVE=1; fi
if systemctl is-active --quiet sync-scheduler.timer 2>/dev/null; then SCHEDULER_WAS_ACTIVE=1; fi

if [[ -d "$APP_DIR/.git" ]] && [[ "${ALLOW_DIRTY_UPGRADE:-0}" != "1" ]]; then
  if [[ -n "$(git -c safe.directory="$APP_DIR" -C "$APP_DIR" status --porcelain --untracked-files=no)" ]]; then
    echo "Abbruch: Das bestehende Repository enthält lokale Änderungen." >&2
    echo "Änderungen committen/sichern oder ALLOW_DIRTY_UPGRADE=1 bewusst setzen." >&2
    exit 1
  fi
fi

printf '%s\n' "🛑 Dienste für konsistentes Upgrade stoppen …"
systemctl stop sync-scheduler.timer rclone-sync-web.service rclone-sync.service >/dev/null 2>&1 || true

printf '%s\n' "💾 Laufzeitdaten sichern …"
if [[ -d "$APP_DIR/data" || -d "/home/$APP_USER/.config/rclone" ]]; then
  stamp=$(date +%Y%m%d-%H%M%S)
  backup_dir="$BACKUP_ROOT/$stamp"
  install -d -m 0700 "$backup_dir"
  [[ ! -d "$APP_DIR/data" ]] || cp -a "$APP_DIR/data" "$backup_dir/data"
  [[ ! -d "/home/$APP_USER/.config/rclone" ]] || cp -a "/home/$APP_USER/.config/rclone" "$backup_dir/rclone-config"
  printf '%s\n' "$APP_DIR" > "$backup_dir/source-path.txt"
  chmod -R go-rwx "$backup_dir"
  if [[ "$BACKUP_KEEP" =~ ^[0-9]+$ ]] && (( BACKUP_KEEP > 0 )); then
    mapfile -t old_backups < <(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -r | tail -n +$((BACKUP_KEEP + 1)))
    for old in "${old_backups[@]}"; do rm -rf -- "$BACKUP_ROOT/$old"; done
  fi
fi

printf '%s\n' "📥 Repository aktualisieren …"
if [[ -d "$APP_DIR/.git" ]]; then
  PREVIOUS_GIT_HEAD=$(git -c safe.directory="$APP_DIR" -C "$APP_DIR" rev-parse HEAD)
  SOURCE_UPDATE_STARTED=1
fi
if [[ ! -d "$APP_DIR/.git" ]]; then
  rm -rf "$APP_DIR"
  git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$APP_DIR"
else
  git -c safe.directory="$APP_DIR" -C "$APP_DIR" fetch --prune origin "$BRANCH"
  git -c safe.directory="$APP_DIR" -C "$APP_DIR" checkout "$BRANCH"
  git -c safe.directory="$APP_DIR" -C "$APP_DIR" pull --ff-only origin "$BRANCH"
fi

cd "$APP_DIR"
printf '%s\n' "🐍 Python-Umgebung …"
VENV_ROLLBACK="$APP_DIR/venv.rollback"
rm -rf "$VENV_ROLLBACK"
if [[ -d "$APP_DIR/venv" ]]; then
  mv "$APP_DIR/venv" "$VENV_ROLLBACK"
fi
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --disable-pip-version-check --upgrade pip wheel
"$APP_DIR/venv/bin/pip" install --disable-pip-version-check -r "$APP_DIR/requirements.txt"

printf '%s\n' "📁 Laufzeitverzeichnisse …"
install -d -m 0700 -o "$APP_USER" -g "$APP_GROUP" \
  "$APP_DIR/data" "$APP_DIR/data/.rclone-cache" "$APP_DIR/data/runtime" \
  "$APP_DIR/logs" "$APP_DIR/temp"

INITIAL_PASSWORD=""
if [[ ! -f "$APP_DIR/data/config.yaml" ]]; then
  printf '%s\n' "📝 Sichere Erstkonfiguration erzeugen …"
  cp "$APP_DIR/config/config.example.yaml" "$APP_DIR/data/config.yaml"
  INITIAL_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)
  GEN_SECRET=$(openssl rand -base64 64 | tr -d '/+=' | cut -c1-64)
  INITIAL_PASSWORD="$INITIAL_PASSWORD" GEN_SECRET="$GEN_SECRET" CONFIG_PATH="$APP_DIR/data/config.yaml" \
    "$APP_DIR/venv/bin/python" - <<'PY'
import os
from pathlib import Path

import bcrypt
import yaml

path = Path(os.environ["CONFIG_PATH"])
data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
web = data.setdefault("web", {})
password = os.environ["INITIAL_PASSWORD"]
web["password"] = ""
web["password_hash"] = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode("ascii")
web["secret_key"] = os.environ["GEN_SECRET"]
web["session_version"] = 1
path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
PY
  printf '%s\n' "$INITIAL_PASSWORD" > "$APP_DIR/data/.initial-password"
fi

if [[ ! -f "$APP_DIR/data/rclone-filters.txt" ]]; then
  cp "$APP_DIR/config/rclone-filters.example.txt" "$APP_DIR/data/rclone-filters.txt"
fi

chown -R "$APP_USER:$APP_GROUP" "$APP_DIR/data" "$APP_DIR/logs" "$APP_DIR/temp"
chmod 0600 "$APP_DIR/data/config.yaml"
[[ ! -f "$APP_DIR/data/config.yaml.bak" ]] || chmod 0600 "$APP_DIR/data/config.yaml.bak"
[[ ! -f "$APP_DIR/data/.initial-password" ]] || chmod 0600 "$APP_DIR/data/.initial-password"
chmod 0600 "$APP_DIR/data/rclone-filters.txt"

printf '%s\n' "🔎 Konfiguration und Python-Dateien prüfen …"
RCLONE_SYNC_CONFIG="$APP_DIR/data/config.yaml" "$APP_DIR/venv/bin/python" - <<'PY'
from app.config_store import get_config
from app.config_validation import validate_config
validate_config(get_config().snapshot())
print("Konfiguration gültig")
PY
"$APP_DIR/venv/bin/python" -m compileall -q "$APP_DIR/app"

printf '%s\n' "⚙️ systemd installieren …"
SYSTEM_FILES_ROLLBACK=$(mktemp -d /run/rclone-sync-install.XXXXXX)
chmod 0700 "$SYSTEM_FILES_ROLLBACK"
for target in "${SYSTEM_TARGETS[@]}"; do
  name=$(basename "$target")
  if [[ -f "$target" ]]; then
    cp -a "$target" "$SYSTEM_FILES_ROLLBACK/$name"
  else
    touch "$SYSTEM_FILES_ROLLBACK/$name.missing"
  fi
done
SYSTEM_FILES_STARTED=1
install -m 0644 "$APP_DIR/systemd/rclone-sync-web.service" /etc/systemd/system/rclone-sync-web.service
install -m 0644 "$APP_DIR/systemd/rclone-sync.service" /etc/systemd/system/rclone-sync.service
install -m 0644 "$APP_DIR/systemd/rclone-sync.timer" /etc/systemd/system/rclone-sync.timer
install -m 0644 "$APP_DIR/systemd/sync-scheduler.service" /etc/systemd/system/sync-scheduler.service
install -m 0644 "$APP_DIR/systemd/sync-scheduler.timer" /etc/systemd/system/sync-scheduler.timer
install -m 0440 "$APP_DIR/systemd/sudoers-rclone-sync" /etc/sudoers.d/rclone-sync
visudo -cf /etc/sudoers.d/rclone-sync >/dev/null
systemd-analyze verify \
  /etc/systemd/system/rclone-sync-web.service \
  /etc/systemd/system/rclone-sync.service \
  /etc/systemd/system/sync-scheduler.service >/dev/null
systemctl daemon-reload

# Nur der Per-Pair-Scheduler darf automatisch laufen. Der Legacy-Timer würde
# dieselben Pairs zusätzlich pauschal starten.
systemctl disable --now rclone-sync.timer >/dev/null 2>&1 || true
systemctl enable --now rclone-sync-web.service
systemctl enable --now sync-scheduler.timer
systemctl restart rclone-sync-web.service

sleep 1
systemctl is-active --quiet rclone-sync-web.service
systemctl is-active --quiet sync-scheduler.timer
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8001/readyz >/dev/null

# Erst nach erfolgreichem Healthcheck wird die alte Python-Umgebung verworfen.
[[ -z "$VENV_ROLLBACK" ]] || rm -rf "$VENV_ROLLBACK"
VENV_ROLLBACK=""
PREVIOUS_GIT_HEAD=""
SOURCE_UPDATE_STARTED=0
[[ -z "$SYSTEM_FILES_ROLLBACK" ]] || rm -rf "$SYSTEM_FILES_ROLLBACK"
SYSTEM_FILES_ROLLBACK=""
SYSTEM_FILES_STARTED=0

IP=$(hostname -I | awk '{print $1}')
printf '\n%s\n' "✅ Installation abgeschlossen"
printf '%-30s %s\n' "Web-UI:" "http://${IP}:8001"
printf '%-30s %s\n' "Login:" "admin"
if [[ -n "${backup_dir:-}" ]]; then
  printf '%-30s %s\n' "Upgrade-Sicherung:" "$backup_dir"
fi
if [[ -n "$INITIAL_PASSWORD" ]]; then
  printf '%-30s %s\n' "Initial-Passwort:" "$INITIAL_PASSWORD"
  printf '%-30s %s\n' "Gespeichert in:" "$APP_DIR/data/.initial-password"
else
  printf '%-30s %s\n' "Passwort ändern:" "sudo -u $APP_USER $APP_DIR/venv/bin/python -m app.cli set-password"
fi
cat <<EOF

Proxmox Backup Server (optional):
  Für PBS-Backups aus der GUI muss proxmox-backup-client installiert sein:
    echo "deb http://download.proxmox.com/debian/pbs-client bookworm main" \
      > /etc/apt/sources.list.d/pbs-client.list
    curl -fsSL https://enterprise.proxmox.com/debian/proxmox-release-bookworm.gpg \
      -o /etc/apt/trusted.gpg.d/proxmox-release-bookworm.gpg
    apt update && apt install proxmox-backup-client

Sicherheitshinweis (Transportverschlüsselung):
  Die Web-UI lauscht unverschlüsselt auf 0.0.0.0:8001. Login-Passwort und
  Session-Cookie sind im LAN mitlesbar. Empfohlen: Reverse-Proxy mit TLS
  (z. B. Caddy/nginx auf 127.0.0.1:8001 weiterleiten), danach in der
  Konfiguration web.secure_cookie: auto und web.hsts_seconds setzen sowie
  web.allowed_hosts auf den Proxy-Hostnamen begrenzen.

Nächste Schritte:
  1. sudo -u $APP_USER -H rclone config
  2. Web-UI öffnen, Pairs konfigurieren und testen
  3. Plan prüfen und zuerst einen Dry-Run starten

Status:
  systemctl status rclone-sync-web sync-scheduler.timer
  journalctl -u rclone-sync-web -f
EOF
