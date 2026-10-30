#!/usr/bin/env bash
# Idempotenter Installer für Debian/Ubuntu-LXC.
set -Eeuo pipefail

APP_USER="rclone-sync"
APP_GROUP="rclone-sync"
APP_DIR="/opt/rclone-sync"
REPO_URL="${REPO_URL:-https://github.com/oliverzimmermann1986-debug/Rclone.git}"
BRANCH="${BRANCH:-main}"
SOURCE_COMMIT="${SOURCE_COMMIT:-}"
BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/rclone-sync}"
BACKUP_KEEP="${BACKUP_KEEP:-10}"
BACKUP_MARKER=".rclone-sync-backup-v1"
WEB_WAS_ACTIVE=0
WEB_WAS_ENABLED=0
SCHEDULER_TIMER_WAS_ACTIVE=0
SCHEDULER_TIMER_WAS_ENABLED=0
OPERATIONS_TIMER_WAS_ACTIVE=0
OPERATIONS_TIMER_WAS_ENABLED=0
LEGACY_TIMER_WAS_ACTIVE=0
LEGACY_TIMER_WAS_ENABLED=0
UNIT_STATES_CAPTURED=0
PREVIOUS_GIT_HEAD=""
SOURCE_UPDATE_STARTED=0
VENV_ROLLBACK=""
SYSTEM_FILES_ROLLBACK=""
SYSTEM_FILES_STARTED=0
RUNTIME_MUTATION_STARTED=0
RUNTIME_HELPER_DIR=""
ROLLBACK_RUNTIME_HELPER=""
SYSTEM_TARGETS=(
  /etc/systemd/system/rclone-sync-web.service
  /etc/systemd/system/rclone-sync.service
  /etc/systemd/system/rclone-sync.timer
  /etc/systemd/system/sync-scheduler.service
  /etc/systemd/system/sync-scheduler.timer
  /etc/systemd/system/sicherpfad-operations.service
  /etc/systemd/system/sicherpfad-operations.timer
  /etc/sudoers.d/rclone-sync
)
RUNTIME_BACKUP_FILES=(
  config.yaml
  config.yaml.bak
  rclone-sync.db
)

runtime_backup_dir() {
  local candidate leaf
  [[ -n "${backup_dir:-}" ]] || return 1
  candidate=$(realpath -m -- "$backup_dir") || return 1
  case "$candidate" in
    "$BACKUP_ROOT_CANONICAL"/*) ;;
    *) return 1 ;;
  esac
  leaf=${candidate##*/}
  [[ "$leaf" =~ ^[0-9]{8}-[0-9]{6}$ ]] || return 1
  [[ -d "$candidate" && ! -L "$candidate" ]] || return 1
  [[ -f "$candidate/$BACKUP_MARKER" && ! -L "$candidate/$BACKUP_MARKER" ]] || return 1
  [[ "$(< "$candidate/$BACKUP_MARKER")" == "rclone-sync-backup-v1" ]] || return 1
  [[ -f "$candidate/source-path.txt" && ! -L "$candidate/source-path.txt" ]] || return 1
  [[ "$(< "$candidate/source-path.txt")" == "$APP_DIR_CANONICAL" ]] || return 1
  [[ -d "$candidate/runtime-state" && ! -L "$candidate/runtime-state" ]] || return 1
  printf '%s\n' "$candidate"
}

restore_runtime_backup() {
  local restore_dir
  restore_dir=$(runtime_backup_dir) || {
    echo "Rollback-Sicherung konnte nicht sicher validiert werden; Dienste bleiben gestoppt." >&2
    return 1
  }
  [[ -n "$ROLLBACK_RUNTIME_HELPER" && -f "$ROLLBACK_RUNTIME_HELPER" ]] || return 1
  /usr/bin/python3 -I "$ROLLBACK_RUNTIME_HELPER" \
    "$APP_DIR_CANONICAL" "$RUNTIME_UID" "$RUNTIME_GID" restore "$restore_dir"
}

restore_enabled_state() {
  local unit="$1"
  local was_enabled="$2"
  if (( was_enabled )); then
    systemctl enable "$unit" >/dev/null 2>&1 || true
  else
    systemctl disable "$unit" >/dev/null 2>&1 || true
  fi
}

restore_active_state() {
  local unit="$1"
  local was_active="$2"
  if (( was_active )); then
    systemctl start "$unit" >/dev/null 2>&1 || true
  else
    systemctl stop "$unit" >/dev/null 2>&1 || true
  fi
}

on_error() {
  local line="$1"
  local status="${2:-1}"
  local rollback_failed=0
  if (( status == 0 )); then status=1; fi
  trap - ERR
  set +e
  echo "Installation fehlgeschlagen (Zeile $line). Rollback wird versucht." >&2
  systemctl stop sync-scheduler.timer rclone-sync.timer sync-scheduler.service rclone-sync-web.service rclone-sync.service sicherpfad-operations.timer sicherpfad-operations.service >/dev/null 2>&1 || true
  for unit in sync-scheduler.timer rclone-sync.timer sync-scheduler.service rclone-sync-web.service rclone-sync.service sicherpfad-operations.timer sicherpfad-operations.service; do
    if systemctl is-active --quiet "$unit" 2>/dev/null; then
      echo "Rollback: Dienst $unit konnte nicht gestoppt werden." >&2
      rollback_failed=1
    fi
  done
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
  if (( RUNTIME_MUTATION_STARTED )) && [[ -n "${backup_dir:-}" ]]; then
    restore_runtime_backup || rollback_failed=1
  fi
  if (( rollback_failed == 0 )) && [[ -n "$RUNTIME_HELPER_DIR" ]]; then
    rm -rf -- "$RUNTIME_HELPER_DIR"
    RUNTIME_HELPER_DIR=""
  fi
  [[ -z "$SYSTEM_FILES_ROLLBACK" ]] || rm -rf "$SYSTEM_FILES_ROLLBACK"
  systemctl daemon-reload >/dev/null 2>&1 || true
  if (( UNIT_STATES_CAPTURED )); then
    restore_enabled_state rclone-sync-web.service "$WEB_WAS_ENABLED"
    restore_enabled_state sync-scheduler.timer "$SCHEDULER_TIMER_WAS_ENABLED"
    restore_enabled_state sicherpfad-operations.timer "$OPERATIONS_TIMER_WAS_ENABLED"
    restore_enabled_state rclone-sync.timer "$LEGACY_TIMER_WAS_ENABLED"
    if (( rollback_failed == 0 )); then
      restore_active_state rclone-sync-web.service "$WEB_WAS_ACTIVE"
      restore_active_state sync-scheduler.timer "$SCHEDULER_TIMER_WAS_ACTIVE"
      restore_active_state sicherpfad-operations.timer "$OPERATIONS_TIMER_WAS_ACTIVE"
      restore_active_state rclone-sync.timer "$LEGACY_TIMER_WAS_ACTIVE"
    else
      echo "Rollback unvollständig; alle Anwendungsdienste bleiben gestoppt." >&2
    fi
  fi
  # Die Scheduler-Oneshot-Unit wird nie direkt neu gestartet: der Timer
  # übernimmt den nächsten Lauf, ohne einen abgebrochenen Tick zu duplizieren.
  exit "$status"
}
trap 'on_error "$LINENO" "$?"' ERR

if [[ ${EUID} -ne 0 ]]; then
  echo "Bitte als root oder mit sudo ausführen" >&2
  exit 1
fi

if [[ ! "$SOURCE_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "Abbruch: SOURCE_COMMIT muss die geprüfte 40-stellige Git-Commit-ID sein." >&2
  echo "Der Installer führt aus Sicherheitsgründen niemals einen beweglichen Branch-HEAD als root aus." >&2
  exit 1
fi
SOURCE_COMMIT=${SOURCE_COMMIT,,}
if [[ ! "$BRANCH" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || [[ "$BRANCH" == *..* ]]; then
  echo "Abbruch: BRANCH enthält ungültige Zeichen." >&2
  exit 1
fi

# Never ask Git or a checkout-supplied program whether a service-writable
# checkout is safe. Reject it before packages, services, hooks or helpers run.
if [[ -e "$APP_DIR" || -L "$APP_DIR" ]]; then
  if [[ ! -x /usr/bin/python3 ]]; then
    echo "Abbruch: Für die sichere Prüfung des bestehenden Checkouts wird /usr/bin/python3 benötigt." >&2
    exit 1
  fi
  if ! /usr/bin/python3 -I - "$APP_DIR" <<'CHECKOUT_TRUST_PY'
import os
import stat
import sys
from pathlib import Path


def trusted_metadata(path, *, directory=False):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"Symbolischer Link im unveränderlichen Checkout: {path}")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"Echtes Checkout-Verzeichnis erforderlich: {path}")
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise ValueError(f"Spezialdatei im unveränderlichen Checkout: {path}")
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError(f"Checkout muss root gehören und darf nicht gruppen-/öffentlich schreibbar sein: {path}")
    return info


def assert_trusted_checkout(application):
    application = Path(application)
    if not application.is_absolute() or ".." in application.parts:
        raise ValueError("APP_DIR muss ein sicherer absoluter Pfad sein")
    if application == Path(application.anchor):
        raise ValueError("Das Wurzelverzeichnis darf kein Checkout sein")
    for ancestor in reversed(application.parents):
        trusted_metadata(ancestor, directory=True)
    trusted_metadata(application, directory=True)
    trusted_metadata(application / ".git", directory=True)
    runtime_directories = {"data", "logs", "temp", "venv", "venv.rollback"}
    pending = [application]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                path = current / entry.name
                if current == application and entry.name in runtime_directories:
                    # Runtime contents can belong to the service. An existing
                    # venv intentionally contains interpreter links and will be
                    # replaced, not executed as root during source admission.
                    if not stat.S_ISDIR(path.lstat().st_mode):
                        raise ValueError(f"Runtime-Pfad muss ein echtes Verzeichnis sein: {path}")
                    continue
                info = trusted_metadata(path)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)


if __name__ == "__main__":
    try:
        assert_trusted_checkout(sys.argv[1])
    except (OSError, ValueError) as exc:
        print(f"Unsicherer bestehender Checkout: {exc}", file=sys.stderr)
        raise SystemExit(1)
CHECKOUT_TRUST_PY
  then
    echo "Abbruch: Bestehenden Checkout außerhalb des Installers sichern und aus einer unabhängig geprüften Quelle neu bereitstellen." >&2
    echo "Kein automatisches chown und kein Git-Befehl werden auf dem untrusted Checkout ausgeführt." >&2
    exit 1
  fi
fi

APP_DIR_CANONICAL=$(realpath -m -- "$APP_DIR")
BACKUP_ROOT_CANONICAL=$(realpath -m -- "$BACKUP_ROOT")
case "$BACKUP_ROOT_CANONICAL" in
  "$APP_DIR_CANONICAL"|"$APP_DIR_CANONICAL"/*)
    echo "Abbruch: BACKUP_ROOT darf nicht innerhalb von APP_DIR liegen." >&2
    exit 1
    ;;
esac
if [[ "$BACKUP_ROOT_CANONICAL" == "/" ]]; then
  echo "Abbruch: Das Wurzelverzeichnis ist kein zulässiges BACKUP_ROOT." >&2
  exit 1
fi

# Backup paths are used by root for copying and pruning. Reject writable or
# linked ancestors before creating any directory or following a backup path.
/usr/bin/python3 -I - "$BACKUP_ROOT" <<'BACKUP_TRUST_PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
if not root.is_absolute() or ".." in root.parts:
    raise SystemExit("BACKUP_ROOT muss ein sicherer absoluter Pfad sein")
for path in [*reversed(root.parents), root]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        continue
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit(f"Unsicherer Backup-Pfad oder Vorfahr: {path}")
BACKUP_TRUST_PY

if [[ -e "$APP_DIR" || -L "$APP_DIR" ]] && [[ ! -d "$APP_DIR/.git" ]]; then
  echo "Abbruch: APP_DIR existiert, ist aber kein Git-Checkout: $APP_DIR" >&2
  echo "Den Pfad manuell prüfen, sichern und entfernen oder REPO_URL/APP_DIR korrigieren." >&2
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
if systemctl is-enabled --quiet rclone-sync-web.service 2>/dev/null; then WEB_WAS_ENABLED=1; fi
if systemctl is-active --quiet sync-scheduler.timer 2>/dev/null; then SCHEDULER_TIMER_WAS_ACTIVE=1; fi
if systemctl is-enabled --quiet sync-scheduler.timer 2>/dev/null; then SCHEDULER_TIMER_WAS_ENABLED=1; fi
if systemctl is-active --quiet sicherpfad-operations.timer 2>/dev/null; then OPERATIONS_TIMER_WAS_ACTIVE=1; fi
if systemctl is-enabled --quiet sicherpfad-operations.timer 2>/dev/null; then OPERATIONS_TIMER_WAS_ENABLED=1; fi
if systemctl is-active --quiet rclone-sync.timer 2>/dev/null; then LEGACY_TIMER_WAS_ACTIVE=1; fi
if systemctl is-enabled --quiet rclone-sync.timer 2>/dev/null; then LEGACY_TIMER_WAS_ENABLED=1; fi
UNIT_STATES_CAPTURED=1

if [[ -d "$APP_DIR/.git" ]]; then
  if [[ -n "$(git -c safe.directory="$APP_DIR" -C "$APP_DIR" status --porcelain --untracked-files=no)" ]]; then
    echo "Abbruch: Das bestehende Repository enthält lokale Änderungen." >&2
    echo "Änderungen vor dem Upgrade committen oder außerhalb von APP_DIR sichern." >&2
    exit 1
  fi
fi

printf '%s\n' "🛑 Dienste für konsistentes Upgrade stoppen …"
systemctl stop sync-scheduler.timer rclone-sync.timer sync-scheduler.service rclone-sync-web.service rclone-sync.service sicherpfad-operations.timer sicherpfad-operations.service >/dev/null 2>&1 || true
for unit in sync-scheduler.timer rclone-sync.timer sync-scheduler.service rclone-sync-web.service rclone-sync.service sicherpfad-operations.timer sicherpfad-operations.service; do
  if systemctl is-active --quiet "$unit" 2>/dev/null; then
    echo "Abbruch: Dienst $unit konnte vor der Datensicherung nicht gestoppt werden." >&2
    false
  fi
done

printf '%s\n' "💾 Laufzeitdaten sichern …"
if [[ -d "$APP_DIR/data" || -d "/home/$APP_USER/.config/rclone" ]]; then
  stamp=$(date +%Y%m%d-%H%M%S)
  backup_dir="$BACKUP_ROOT/$stamp"
  if [[ -e "$backup_dir" || -L "$backup_dir" ]]; then
    echo "Abbruch: Sicherungsziel existiert bereits: $backup_dir" >&2
    false
  fi
  install -d -m 0700 "$backup_dir"
  printf '%s\n' "rclone-sync-backup-v1" > "$backup_dir/$BACKUP_MARKER"
  chmod 0600 "$backup_dir/$BACKUP_MARKER"
  if [[ -d "$APP_DIR/data" ]]; then
    if [[ -L "$APP_DIR/data" ]]; then
      echo "Abbruch: Das Laufzeitdatenverzeichnis darf kein Symlink sein." >&2
      false
    fi
    install -d -m 0700 "$backup_dir/data"
    shopt -s dotglob nullglob
    data_items=("$APP_DIR/data"/*)
    shopt -u dotglob nullglob
    for item in "${data_items[@]}"; do
      case "$(basename "$item")" in
        rclone-sync.db|rclone-sync.db-wal|rclone-sync.db-shm) continue ;;
      esac
      cp -a -- "$item" "$backup_dir/data/"
    done
    if [[ -f "$APP_DIR/data/rclone-sync.db" ]]; then
      sqlite_backup="$backup_dir/data/rclone-sync.db"
      if [[ "$sqlite_backup" == *"'"* || "$sqlite_backup" == *$'\n'* ]]; then
        echo "Backup-Pfad enthält für sqlite3 nicht unterstützte Zeichen." >&2
        false
      fi
      sqlite3 -cmd ".timeout 30000" "$APP_DIR/data/rclone-sync.db" ".backup '$sqlite_backup'"
      if [[ "$(sqlite3 "$sqlite_backup" "PRAGMA quick_check;")" != "ok" ]]; then
        echo "SQLite-Sicherung ist inkonsistent." >&2
        false
      fi
      chmod 0600 "$sqlite_backup"
    fi
  fi
  install -d -m 0700 "$backup_dir/runtime-state"
  for name in "${RUNTIME_BACKUP_FILES[@]}"; do
    if [[ -L "$APP_DIR/data/$name" ]] ||
      [[ -e "$APP_DIR/data/$name" && ! -f "$APP_DIR/data/$name" ]]; then
      echo "Abbruch: Nicht reguläre Runtime-Datei kann nicht sicher gesichert werden: $name" >&2
      false
    elif [[ -f "$APP_DIR/data/$name" ]]; then
      touch "$backup_dir/runtime-state/$name.present"
    else
      touch "$backup_dir/runtime-state/$name.missing"
    fi
  done
  [[ ! -d "/home/$APP_USER/.config/rclone" ]] || cp -a "/home/$APP_USER/.config/rclone" "$backup_dir/rclone-config"
  printf '%s\n' "$APP_DIR_CANONICAL" > "$backup_dir/source-path.txt"
  chmod -R go-rwx "$backup_dir"
  if [[ "$BACKUP_KEEP" =~ ^[0-9]+$ ]] && (( BACKUP_KEEP > 0 )); then
    mapfile -t old_backups < <(
      find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' |
        while IFS= read -r candidate; do
          if [[ "$candidate" =~ ^[0-9]{8}-[0-9]{6}$ ]] &&
            [[ -f "$BACKUP_ROOT/$candidate/$BACKUP_MARKER" ]] &&
            [[ ! -L "$BACKUP_ROOT/$candidate/$BACKUP_MARKER" ]] &&
            [[ "$(< "$BACKUP_ROOT/$candidate/$BACKUP_MARKER")" == "rclone-sync-backup-v1" ]]; then
            printf '%s\n' "$candidate"
          fi
        done |
        sort -r |
        tail -n +$((BACKUP_KEEP + 1))
    )
    for old in "${old_backups[@]}"; do
      [[ "$old" =~ ^[0-9]{8}-[0-9]{6}$ ]] || continue
      [[ -f "$BACKUP_ROOT/$old/$BACKUP_MARKER" ]] || continue
      [[ ! -L "$BACKUP_ROOT/$old/$BACKUP_MARKER" ]] || continue
      [[ "$(< "$BACKUP_ROOT/$old/$BACKUP_MARKER")" == "rclone-sync-backup-v1" ]] || continue
      rm -rf -- "${BACKUP_ROOT:?}/$old"
    done
  fi
fi

printf '%s\n' "📥 Repository aktualisieren …"
if [[ -d "$APP_DIR/.git" ]]; then
  PREVIOUS_GIT_HEAD=$(git -c safe.directory="$APP_DIR" -C "$APP_DIR" rev-parse HEAD)
  SOURCE_UPDATE_STARTED=1
fi
if [[ ! -d "$APP_DIR/.git" ]]; then
  git clone --branch "$BRANCH" --single-branch --no-checkout "$REPO_URL" "$APP_DIR"
else
  git -c safe.directory="$APP_DIR" -C "$APP_DIR" fetch --prune origin \
    "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
fi
git -c safe.directory="$APP_DIR" -C "$APP_DIR" cat-file -e "$SOURCE_COMMIT^{commit}"
git -c safe.directory="$APP_DIR" -C "$APP_DIR" merge-base --is-ancestor \
  "$SOURCE_COMMIT" "origin/$BRANCH"
# Pin the reviewed helper before checkout or venv replacement. Rollback must
# survive resetting both and must never execute a helper from the old checkout.
RUNTIME_HELPER_DIR=$(mktemp -d /run/rclone-sync-runtime.XXXXXX)
chmod 0700 "$RUNTIME_HELPER_DIR"
ROLLBACK_RUNTIME_HELPER="$RUNTIME_HELPER_DIR/runtime_files.py"
git -c safe.directory="$APP_DIR" -C "$APP_DIR" show --no-textconv \
  "$SOURCE_COMMIT:scripts/runtime_files.py" > "$ROLLBACK_RUNTIME_HELPER"
chmod 0400 "$ROLLBACK_RUNTIME_HELPER"
git -c safe.directory="$APP_DIR" -C "$APP_DIR" checkout --detach "$SOURCE_COMMIT"
CHECKED_OUT_COMMIT=$(git -c safe.directory="$APP_DIR" -C "$APP_DIR" rev-parse HEAD)
if [[ "$CHECKED_OUT_COMMIT" != "$SOURCE_COMMIT" ]]; then
  echo "Abbruch: ausgecheckter Commit stimmt nicht mit SOURCE_COMMIT überein." >&2
  false
fi

cd "$APP_DIR"
printf '%s\n' "🐍 Python-Umgebung …"
VENV_ROLLBACK="$APP_DIR/venv.rollback"
rm -rf "$VENV_ROLLBACK"
if [[ -d "$APP_DIR/venv" ]]; then
  mv "$APP_DIR/venv" "$VENV_ROLLBACK"
fi
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --disable-pip-version-check --require-hashes --only-binary=:all: -r "$APP_DIR/requirements.lock"

printf '%s\n' "📁 Laufzeitverzeichnisse …"
RUNTIME_UID=$(id -u "$APP_USER")
RUNTIME_GID=$(getent group "$APP_GROUP" | cut -d: -f3)
RUNTIME_MUTATION_STARTED=1
RUNTIME_HELPER="$ROLLBACK_RUNTIME_HELPER"
"$APP_DIR/venv/bin/python" -I "$RUNTIME_HELPER" \
  "$APP_DIR" "$RUNTIME_UID" "$RUNTIME_GID" prepare
sudo -u "$APP_USER" -g "$APP_GROUP" -H install -d -m 0700 -o "$APP_USER" -g "$APP_GROUP" \
  "/home/$APP_USER/.config" "/home/$APP_USER/.config/rclone"

INITIAL_PASSWORD=""
if [[ ! -f "$APP_DIR/data/config.yaml" ]]; then
  printf '%s\n' "📝 Sichere Erstkonfiguration erzeugen …"
  INITIAL_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)
  GEN_SECRET=$(openssl rand -base64 64 | tr -d '/+=' | cut -c1-64)
  INITIAL_PASSWORD="$INITIAL_PASSWORD" GEN_SECRET="$GEN_SECRET" \
    "$APP_DIR/venv/bin/python" -I "$RUNTIME_HELPER" \
    "$APP_DIR" "$RUNTIME_UID" "$RUNTIME_GID" initialize
fi

"$APP_DIR/venv/bin/python" -I "$RUNTIME_HELPER" \
  "$APP_DIR" "$RUNTIME_UID" "$RUNTIME_GID" filters
"$APP_DIR/venv/bin/python" -I "$RUNTIME_HELPER" \
  "$APP_DIR" "$RUNTIME_UID" "$RUNTIME_GID" permissions
find "$APP_DIR/data" "$APP_DIR/logs" "$APP_DIR/temp" -xdev \( -type d -o -type f -links 1 \) -exec chown -h "$APP_USER:$APP_GROUP" {} +

printf '%s\n' "🔎 Konfiguration und Python-Dateien prüfen …"
sudo -u "$APP_USER" -H env RCLONE_SYNC_CONFIG="$APP_DIR/data/config.yaml" \
  "$APP_DIR/venv/bin/python" - <<'PY'
from app.config_store import get_config
from app.config_validation import validate_config

config = get_config()
normalized, warnings = validate_config(config.snapshot())
config.replace(normalized)
for warning in warnings:
    print(f"Warnung: {warning}")
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
install -m 0644 "$APP_DIR/systemd/sicherpfad-operations.service" /etc/systemd/system/sicherpfad-operations.service
install -m 0644 "$APP_DIR/systemd/sicherpfad-operations.timer" /etc/systemd/system/sicherpfad-operations.timer
install -m 0440 "$APP_DIR/systemd/sudoers-rclone-sync" /etc/sudoers.d/rclone-sync
visudo -cf /etc/sudoers.d/rclone-sync >/dev/null
systemd-analyze verify \
  /etc/systemd/system/rclone-sync-web.service \
  /etc/systemd/system/rclone-sync.service \
  /etc/systemd/system/rclone-sync.timer \
  /etc/systemd/system/sync-scheduler.service \
  /etc/systemd/system/sync-scheduler.timer \
  /etc/systemd/system/sicherpfad-operations.service \
  /etc/systemd/system/sicherpfad-operations.timer >/dev/null
systemctl daemon-reload

# Nur der Per-Pair-Scheduler darf automatisch laufen. Der Legacy-Timer würde
# dieselben Pairs zusätzlich pauschal starten.
systemctl disable --now rclone-sync.timer >/dev/null 2>&1 || true
systemctl enable rclone-sync-web.service
systemctl enable --now sync-scheduler.timer
systemctl enable --now sicherpfad-operations.timer
systemctl restart rclone-sync-web.service

sleep 1
systemctl is-active --quiet rclone-sync-web.service
systemctl is-active --quiet sync-scheduler.timer
systemctl is-active --quiet sicherpfad-operations.timer
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8001/readyz >/dev/null

# Erst nach erfolgreichem Healthcheck wird die alte Python-Umgebung verworfen.
[[ -z "$VENV_ROLLBACK" ]] || rm -rf "$VENV_ROLLBACK"
VENV_ROLLBACK=""
PREVIOUS_GIT_HEAD=""
SOURCE_UPDATE_STARTED=0
[[ -z "$SYSTEM_FILES_ROLLBACK" ]] || rm -rf "$SYSTEM_FILES_ROLLBACK"
SYSTEM_FILES_ROLLBACK=""
SYSTEM_FILES_STARTED=0
UNIT_STATES_CAPTURED=0
RUNTIME_MUTATION_STARTED=0
[[ -z "$RUNTIME_HELPER_DIR" ]] || rm -rf -- "$RUNTIME_HELPER_DIR"
RUNTIME_HELPER_DIR=""
trap - ERR

printf '\n%s\n' "✅ Installation abgeschlossen"
printf '%-30s %s\n' "Web-UI (lokal):" "http://127.0.0.1:8001"
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
  Für PBS-Backups aus der GUI muss proxmox-backup-client installiert sein
  (Repo muss zum Debian-Release passen, sonst scheitert libfuse3):
    CODENAME=\$(. /etc/os-release && echo \$VERSION_CODENAME)
    echo "deb http://download.proxmox.com/debian/pbs-client \$CODENAME main" \
      > /etc/apt/sources.list.d/pbs-client.list
    curl -fsSL https://enterprise.proxmox.com/debian/proxmox-release-\$CODENAME.gpg \
      -o /etc/apt/trusted.gpg.d/proxmox-release-\$CODENAME.gpg
    apt update && apt install proxmox-backup-client

Sicherheitshinweis (Transportverschlüsselung):
  Die Web-UI lauscht standardmäßig nur auf 127.0.0.1:8001. Für Zugriff aus
  dem LAN einen Reverse-Proxy mit TLS (z. B. Caddy/nginx) verwenden, danach in der
  Konfiguration web.secure_cookie: auto und web.hsts_seconds setzen sowie
  web.allowed_hosts auf den Proxy-Hostnamen begrenzen. web.trusted_proxy_ips
  darf nur die direkten Proxy-Peers enthalten; der Proxy muss X-Forwarded-Proto
  selbst setzen und eingehende Client-Werte überschreiben.

Nächste Schritte:
  1. sudo -u $APP_USER -H rclone config
  2. Web-UI öffnen, Pairs konfigurieren und testen
  3. Plan prüfen und zuerst einen Dry-Run starten

Status:
  systemctl status rclone-sync-web sync-scheduler.timer
  journalctl -u rclone-sync-web -f
EOF
