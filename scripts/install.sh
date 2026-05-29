#!/usr/bin/env bash
# Installer für rclone-sync-container in Proxmox-LXC.
# Idempotent: re-runs sicher.
set -euo pipefail

APP_USER="rclone-sync"
APP_DIR="/opt/rclone-sync"
REPO_URL="${REPO_URL:-https://github.com/appear7240/rclone-sync-container.git}"
BRANCH="${BRANCH:-main}"

if [[ $EUID -ne 0 ]]; then
  echo "Bitte als root oder mit sudo ausführen"
  exit 1
fi

echo "📦 Pakete..."
apt-get update -qq
apt-get install -y --no-install-recommends \
  ca-certificates curl wget gnupg git \
  python3 python3-venv python3-pip python3-dev \
  build-essential \
  rclone \
  sqlite3 \
  sudo \
  tzdata

ln -sf /usr/share/zoneinfo/Europe/Berlin /etc/localtime
echo "Europe/Berlin" > /etc/timezone

echo "👤 User $APP_USER..."
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd -r -m -d /home/$APP_USER -s /bin/bash $APP_USER
fi

echo "📥 Repo..."
if [[ ! -d "$APP_DIR" ]]; then
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
  # Bei Re-Run: git als der Owner-User aufrufen damit kein 'dubious ownership'-Fehler
  if id "$APP_USER" >/dev/null 2>&1 && [[ "$(stat -c %U "$APP_DIR" 2>/dev/null)" == "$APP_USER" ]]; then
    sudo -u "$APP_USER" git -C "$APP_DIR" pull
  else
    cd "$APP_DIR" && git pull
  fi
fi

# Damit zukünftige 'git pull' als root nicht mit 'dubious ownership' brechen
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true

cd "$APP_DIR"

echo "🐍 venv..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip wheel
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "📁 Verzeichnisse..."
mkdir -p "$APP_DIR/data" "$APP_DIR/logs" "$APP_DIR/temp" "$APP_DIR/data/.rclone-cache"
chown -R $APP_USER:$APP_USER "$APP_DIR"

if [[ ! -f "$APP_DIR/data/config.yaml" ]]; then
  echo "📝 Default-Config mit zufälligem Passwort + Secret..."
  cp "$APP_DIR/config/config.example.yaml" "$APP_DIR/data/config.yaml"
  # openssl statt 'tr | head' — vermeidet SIGPIPE-Fehler unter 'set -e'
  GEN_PASS=$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)
  GEN_SECRET=$(openssl rand -base64 48 | tr -d '/+=' | cut -c1-48)
  # Default-Felder befüllen
  python3 - <<PY
import yaml
p = "$APP_DIR/data/config.yaml"
with open(p) as f: d = yaml.safe_load(f)
d.setdefault("web", {})
d["web"]["password"] = "$GEN_PASS"
d["web"]["secret_key"] = "$GEN_SECRET"
with open(p, "w") as f: yaml.dump(d, f, allow_unicode=True, sort_keys=False)
PY
  chown $APP_USER:$APP_USER "$APP_DIR/data/config.yaml"
  chmod 600 "$APP_DIR/data/config.yaml"
  echo "$GEN_PASS" > "$APP_DIR/data/.initial-password"
  chown $APP_USER:$APP_USER "$APP_DIR/data/.initial-password"
  chmod 600 "$APP_DIR/data/.initial-password"
  INITIAL_PASSWORD="$GEN_PASS"
fi

if [[ ! -f "$APP_DIR/data/rclone-filters.txt" ]]; then
  cp "$APP_DIR/config/rclone-filters.example.txt" "$APP_DIR/data/rclone-filters.txt"
  chown $APP_USER:$APP_USER "$APP_DIR/data/rclone-filters.txt"
fi

echo "⚙️  systemd-Units..."
cp "$APP_DIR/systemd/rclone-sync-web.service"  /etc/systemd/system/
cp "$APP_DIR/systemd/rclone-sync.service"       /etc/systemd/system/
cp "$APP_DIR/systemd/rclone-sync.timer"         /etc/systemd/system/
cp "$APP_DIR/systemd/sync-scheduler.service"    /etc/systemd/system/
cp "$APP_DIR/systemd/sync-scheduler.timer"      /etc/systemd/system/
install -m 0440 "$APP_DIR/systemd/sudoers-rclone-sync" /etc/sudoers.d/rclone-sync

systemctl daemon-reload

echo "🚀 Services starten..."
systemctl enable --now rclone-sync-web.service
systemctl enable --now rclone-sync.timer
systemctl enable --now sync-scheduler.timer

sleep 2
systemctl status rclone-sync-web.service --no-pager -l | head -n 8 || true

IP=$(hostname -I | awk '{print $1}')
echo ""
echo "✅ Installation abgeschlossen!"
echo "🌐 Web-UI:                    http://${IP}:8001"
echo "👤 Login:                     admin"
if [[ -n "${INITIAL_PASSWORD:-}" ]]; then
  echo "🔑 Initial-Passwort:          $INITIAL_PASSWORD"
  echo "                              (auch in $APP_DIR/data/.initial-password)"
else
  echo "🔑 Passwort:                  siehe data/config.yaml oder:"
  echo "                              sudo -u $APP_USER $APP_DIR/venv/bin/python -m app.cli set-password"
fi
echo ""
echo "📁 App-Verzeichnis:           $APP_DIR"
echo "📝 Config:                    $APP_DIR/data/config.yaml"
echo ""
echo "Nächste Schritte:"
echo "  1. rclone konfigurieren:    sudo -u $APP_USER rclone config"
echo "  2. Web-UI öffnen → Pairs anpassen → Test"
echo "  3. Erstes Dry-Run starten"
echo ""
echo "Service-Befehle:"
echo "  systemctl status rclone-sync-web"
echo "  journalctl -u rclone-sync-web -f"
echo "  sudo -u $APP_USER /opt/rclone-sync/venv/bin/python -m app.cli set-password"
