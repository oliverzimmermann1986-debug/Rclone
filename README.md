# rclone-sync-container

Schlanker Proxmox-LXC-Container für **rclone bisync/copy/sync** zwischen Cloud (pCloud/GDrive/...) und lokalen NAS-Pfaden. Mit Web-UI für Pair-Verwaltung, Live-Status, Cancel und Job-History.

Ausgegliedert aus [Scrappercontainer](https://github.com/appear7240/Scrappercontainer), damit Verantwortlichkeiten getrennt sind.

## Features

- Mehrere Sync-Paare mit begrenzter Parallelität (`max_parallel`)
- Per-Pair-Schedule via Cron-Expression, inklusive `manual/off`
- Unterstützt `bisync`, `pull`, `push` sowie `copy` oder `sync`
- Prozessübergreifender Lock zwischen Web, CLI und Scheduler
- Robuster Cancel: beendet rclone inklusive Prozessgruppe
- Mount-Schutz über `min_local_files`, damit ein leerer NAS-Mount nicht die Cloud leert
- Dry-Run-Modus plus **Plan-Vorschau** mit den realen rclone-Kommandos
- **Doctor/Diagnose-Seite** für rclone, DB, Logs, Config, Scheduler und Mount-Schutz
- **Read-only Integritätscheck** pro Pair via `rclone check`
- Pair-spezifische Include/Exclude/Filter-Regeln direkt in der UI
- Strukturierte rclone-Tuning-Werte (`transfers`, `checkers`, `retries`, `max_delete`, `fast_list`)
- Konflikt-Auflösung konfigurierbar (`auto/newer/older/larger/path1/path2`)
- Optionaler Trash via `backup_dir`, Filter-Datei via `--filter-from`
- Log-Wartung: alte Logs suchen und löschen
- Web-UI mit bcrypt-Login + Session-Cookies
- Webhooks für Discord, Telegram oder generische JSON-Endpoints inkl. Test-Button

## Installation (Proxmox-LXC)

```bash
# In einem frischen Debian/Ubuntu-LXC als root:
curl -fsSL https://raw.githubusercontent.com/appear7240/rclone-sync-container/main/scripts/install.sh | bash
```

oder manuell:

```bash
git clone https://github.com/appear7240/rclone-sync-container.git /opt/rclone-sync
cd /opt/rclone-sync
sudo bash scripts/install.sh
```

Nach der Installation:

1. `sudo -u rclone-sync rclone config` — Remote(s) konfigurieren
2. Web-UI öffnen: `http://<container-ip>:8001`
3. Login mit `admin` + initialem Passwort aus `/opt/rclone-sync/data/.initial-password`
4. Pairs anpassen → Test → zuerst Dry-Run starten

## Wichtige Konfiguration

Liegt in `/opt/rclone-sync/data/config.yaml`. Beispiel + Erklärungen in `config/config.example.yaml`.

Besonders wichtig:

- `backup.max_parallel`: für pCloud/GDrive eher `1–2`, für lokale Ziele ggf. höher
- `backup.auto_resync`: default `false`, weil automatisches `bisync --resync` bewusst geprüft werden sollte
- `backup.timeout_hours`: killt hängende rclone-Prozesse sauber
- `pair.min_local_files`: Schutz gegen nicht gemountete NAS-Pfade
- `pair.direction`: `bisync`, `pull` oder `push`
- `pair.mode`: bei `pull/push` entweder `copy` ohne Löschen oder `sync` als Mirror

## Services

| Unit | Funktion |
|---|---|
| `rclone-sync-web.service` | FastAPI Web-UI auf Port 8001 |
| `rclone-sync.timer` | Nightly Default-Sync aller aktiven Pairs |
| `sync-scheduler.timer` | Pro Minute: prüft fällige Per-Pair-Schedules |

## CLI

```bash
# Passwort setzen
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli set-password

# Secret-Key neu generieren
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli gen-secret

# Diagnose / Plan anzeigen
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli doctor
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli plan --pairs Serien,Filme

# Read-only Vergleich eines Pairs
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli check-pair Serien --one-way

# Alte Logs prüfen / löschen
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli prune-logs --days 30 --dry-run
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli prune-logs --days 30 --delete

# Manuell Backup starten
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.jobs.backup_cli --dry-run
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.jobs.backup_cli --pairs Serien,Filme
```

## API-Endpoints

| Method | Pfad | Funktion |
|---|---|---|
| GET | `/api/jobs/status/current` | Was läuft gerade? |
| GET | `/api/jobs/list?limit=50` | Job-Historie |
| GET | `/api/jobs/{id}/log?tail=500` | Job-Log lesen |
| POST | `/api/jobs/backup/run?dry_run=false` | Backup starten |
| POST | `/api/jobs/backup/cancel` | Laufenden Backup abbrechen |
| POST | `/api/jobs/backup/run-pair/{name}` | Einzelnes Pair triggern |
| GET | `/api/jobs/backup/plan` | rclone-Kommandos/Warnungen ohne Ausführung |
| POST | `/api/jobs/backup/check/{name}` | Read-only `rclone check` als Job |
| GET | `/api/jobs/backup/progress` | Live-Status |
| GET | `/api/diagnostics/doctor` | Doctor-Prüfung für System/Config/Pairs |
| GET | `/api/maintenance/logs` | Logs auflisten |
| POST | `/api/maintenance/logs/prune` | Alte Logs löschen/dry-run |
| GET | `/api/config` | Config lesen, sensitiv redacted |
| PUT | `/api/config` | Config schreiben |
| POST | `/api/test/rclone` | Remote-/Pair-Konnektivität testen |
| GET | `/api/browse/rclone?path=` | rclone-Pfad-Browser |
| GET | `/api/browse/local?path=` | Lokaler Pfad-Browser |
| GET | `/healthz`, `/healthz/deep` | Health-Checks |

## Reverse-Proxy

Beispiel-Cloudflare-Tunnel-Eintrag:

```yaml
- hostname: rclone-sync.example.com
  service: http://<container-ip>:8001
```

Cloudflare Access als MFA-Layer davorstellen.

## Disaster-Recovery

Regelmäßig sichern:

- `/opt/rclone-sync/data/config.yaml`
- `/opt/rclone-sync/data/rclone-filters.txt`
- `/opt/rclone-sync/data/rclone-sync.db`
- `/opt/rclone-sync/data/.rclone-cache/` — wichtig für bisync-State
- `/home/rclone-sync/.config/rclone/rclone.conf` — rclone-Remotes

Für bisync ist der Cache/Workdir kritisch. Ohne den State verlangt rclone häufig `--resync`; das sollte bewusst geprüft werden.

## Neue empfohlene Schutz-Konfiguration

Für produktive Mirror-Jobs (`mode: sync`) ist `max_delete` sinnvoll. Beispiel:

```yaml
backup:
  tuning:
    max_delete: 500
  pairs:
    - name: Filme
      direction: pull
      mode: sync
      min_local_files: 10
      max_delete: 100
```

Damit bricht rclone ab, wenn mehr als das Limit gelöscht würde. Zusätzlich sollte jeder neue oder geänderte Pair zuerst über **Plan anzeigen** und **Dry-Run** geprüft werden.
