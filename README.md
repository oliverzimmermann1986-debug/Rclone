# rclone-sync-container

Schlanker Proxmox-LXC-Container für **rclone bisync** zwischen Cloud (pCloud/GDrive/...) und lokalen NAS-Pfaden. Mit Web-UI für Pair-Verwaltung, Live-Progress, Cancel und Job-History.

Ausgegliedert aus [Scrappercontainer](https://github.com/appear7240/Scrappercontainer) damit Verantwortlichkeiten getrennt sind.

## Features

- Mehrere Sync-Paare parallel (`ThreadPoolExecutor`)
- Per-Pair-Schedule via Cron-Expression (z.B. tägl. 03:00, oder `manual`)
- Live-Progress mit Speed + ETA aus rclone-Stats
- Cancel-Mechanismus killt laufende Subprozesse sauber
- Dry-Run-Modus
- Konflikt-Auflösung konfigurierbar (newer/older/larger/path1/path2/auto)
- Immutable-Mode (Ransomware-Schutz)
- Filter-Datei via `--filter-from`
- Web-UI mit bcrypt-Login + Session-Cookies

## Installation (Proxmox-LXC)

```bash
# In einem frischen Debian/Ubuntu-LXC als root:
curl -fsSL https://raw.githubusercontent.com/USERNAME/rclone-sync-container/main/scripts/install.sh | bash
```

oder manuell:
```bash
git clone https://github.com/USERNAME/rclone-sync-container.git /opt/rclone-sync
cd /opt/rclone-sync
sudo bash scripts/install.sh
```

Nach der Installation:
1. `sudo -u rclone-sync rclone config` — Remote(s) konfigurieren
2. Web-UI öffnen (`http://<container-ip>:8001`)
3. Login mit `admin` + initialem Passwort aus `/opt/rclone-sync/data/.initial-password`
4. Pairs anpassen → Test → Backup starten

## Konfiguration

Liegt in `/opt/rclone-sync/data/config.yaml`. Beispiel + Erklärungen in `config/config.example.yaml`.

Wichtige Sektionen:
- `web` — Username + bcrypt-Password-Hash + Session-Secret
- `backup.pairs` — Liste der Sync-Paare mit `name`, `remote`, `local`, `schedule`
- `backup.default_schedule` — Cron für Pairs ohne eigenen Schedule
- `backup.rclone_args` — globale rclone-Flags
- `backup.conflict_resolve` — bei bisync-Konflikten
- `backup.filter_file` — Pfad zur rclone-Filter-Datei

## Services

| Unit                              | Funktion                                     |
|-----------------------------------|----------------------------------------------|
| `rclone-sync-web.service`         | FastAPI Web-UI auf Port 8001                 |
| `rclone-sync.timer`               | Nightly Default-Sync aller Pairs             |
| `sync-scheduler.timer`            | Pro Minute: triggert fällige Per-Pair-Schedules |

## CLI

```bash
# Passwort setzen
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli set-password

# Secret-Key neu generieren
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli gen-secret

# Manuell Backup starten (CLI statt Web-UI)
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.jobs.backup_cli --dry-run
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.jobs.backup_cli --pairs Serien,Filme
```

## API-Endpoints

| Method | Pfad                                  | Funktion                          |
|--------|---------------------------------------|-----------------------------------|
| GET    | `/api/jobs/status/current`            | Was läuft gerade?                 |
| GET    | `/api/jobs/list?limit=50`             | Job-Historie                      |
| GET    | `/api/jobs/{id}/log?tail=500`         | Job-Log lesen                     |
| POST   | `/api/jobs/backup/run?dry_run=false`  | Backup starten                    |
| POST   | `/api/jobs/backup/cancel`             | Laufenden Backup abbrechen        |
| POST   | `/api/jobs/backup/run-pair/{name}`    | Einzelnes Pair triggern           |
| GET    | `/api/jobs/backup/progress`           | Live-Progress + per-Pair-Stats    |
| GET    | `/api/config`                          | Config lesen (sensitiv redacted)  |
| PUT    | `/api/config`                          | Config schreiben                  |
| POST   | `/api/test/rclone`                     | Remote-Konnektivität testen       |
| GET    | `/api/browse/rclone?path=`             | rclone-Pfad-Browser               |
| GET    | `/api/browse/local?path=`              | Lokaler Pfad-Browser              |
| GET    | `/healthz`, `/healthz/deep`            | Health-Checks                     |

## Reverse-Proxy

Beispiel-Cloudflare-Tunnel-Eintrag:
```yaml
- hostname: rclone-sync.example.com
  service: http://<container-ip>:8001
```

Mit Cloudflare Access als MFA-Layer davorstellen.

## Disaster-Recovery

Alle relevanten Files in `data/`:
- `config.yaml` — Pair-Definitionen
- `rclone-filters.txt` — Filter-Patterns
- `rclone-sync.db` — Job-History (optional)
- `.rclone-cache/` — rclone-Bisync-State (KRITISCH bei bisync!)

Zusätzlich `/home/rclone-sync/.config/rclone/rclone.conf` — die rclone-Remotes selbst.

Diese 2 Verzeichnisse regelmäßig sichern (z.B. via `tar` in eine Cloud).
