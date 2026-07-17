# rclone-sync-container

**Version 1.8.3** – Web- und Scheduler-Dienst für sichere `rclone bisync`, `copy`- und `sync`-Läufe zwischen Cloud-Remotes und lokalen NAS-/Storage-Pfaden. Ausgelegt für einen Debian-/Ubuntu-LXC mit systemd.

## Kernfunktionen

- Mehrere Sync-Pairs mit Cron-Zeitplan, manuellen Läufen und begrenzter Parallelität
- Persistente Scheduler-Wartungsfenster mit automatischer Fortsetzung
- Frischeüberwachung pro Pair für zu alte oder fehlende erfolgreiche Läufe
- `bisync`, `pull` und `push`; einseitig wahlweise `copy` oder `sync`
- Prozessübergreifende Locks, Laufzeitstatus und Abbruch für Web, CLI und Scheduler
- Mount-, Sentinel-, Mindestdatei-, Freiplatz- und Remote-Prüfungen vor jedem Lauf
- Planvorschau und Dry-Run vor produktiven Änderungen
- Per-Pair-Logs, indexierte Pair-Historie, Job-Historie und Live-Fortschritt
- Read-only `rclone check`, Diagnosebereich und Speicherübersicht
- Pair-Filter, zentrale Filterdatei, Tuning und optionale Backup-Verzeichnisse
- Discord-, Telegram- und generische JSON-Webhooks
- Automatische Log-/Datenbankaufbewahrung
- Konfigurationsrevisionen, Rollbackdatei und sichere Upgrade-Sicherung
- Responsive Proxmox-Betriebskonsole mit Desktop-Sidebar und mobiler Bottom-Navigation
- Ressourcenübersicht für LXC/VM einschließlich cgroup-CPU/RAM/PID-Limits, Servicezustände, Pair-Gesundheit und 24-Stunden-Statistik
- Filterbare/paginierte Jobhistorie mit Detailansicht, Logsuche und Auto-Aktualisierung
- Lokales Auditprotokoll für Scheduler-, Konfigurations-, Start- und Recovery-Aktionen
- Kompakter `/readyz`-Endpunkt für Uptime Kuma und Readiness-Prüfungen


## Weboberfläche 1.7

Die Oberfläche ist als Betriebszentrale statt als reine Konfigurationsmaske aufgebaut:

- **Übersicht:** laufender Job, letzte Ergebnisse, Warnungen, Pair-Gesundheit, nächste Ausführungen und 24-Stunden-Kennzahlen
- **Proxmox-Systemkarte:** Virtualisierungstyp, Gast-Hostname, IP-Adressen, Uptime, CPU-Load, RAM und Datenträgerauslastung; cgroup-v2-Limits werden berücksichtigt
- **Startzentrale:** Planvorschau, Dry-Run, produktiver Lauf und Abbruch klar getrennt
- **Pair-Verwaltung:** responsive Karten, Suche, Statusfilter, sichere Copy-/Restore-/Bi-Sync-/Mirror-Vorlagen, Alle-öffnen/-schließen, Laufzeitfehler, nächste Ausführungen, Klonen, Sortieren, Dry-Run, Produktivlauf und Read-only-Check pro Pair
- **Entwurfsprüfung:** ungespeicherte Pair-Änderungen können direkt gegen rclone geprüft werden; Plan, Check und Start speichern offene Änderungen erst bewusst, damit nie versehentlich ein veralteter Stand läuft
- **Jobs:** Filter nach Typ und Status, Volltextsuche nach Pair/Fehler/Job-ID, Pagination, CSV-Export, Ergebnisdetails, Live-Log, Logsuche, Kopier- und Downloadfunktion
- **System:** Doctor-Prüfung, Servicezustände, Speicherübersicht, redigiertes Support-Bundle sowie kontrollierte Log-/Datenbankwartung
- **Recovery:** lokale, vollständige Konfigurations-Snapshots mit SHA-256-Prüfung, Pre-Restore-Sicherung, Revisionsschutz und erzwungener Neuanmeldung nach Restore
- **Einstellungen:** klar beschriftete Bereiche mit Änderungsstatus, Validierung und kurzen fachlichen Erklärungen
- **Scheduler-Betriebssteuerung:** Automatik dauerhaft deaktivieren oder für Wartungsfenster von 30 Minuten bis 31 Tage pausieren; manuelle Läufe bleiben möglich
- **Frischeüberwachung:** pro Pair optional festlegen, nach wie vielen Stunden ohne Erfolg eine Warnung erscheint
- **Audit:** wichtige Aktionen werden ohne Secrets lokal und filterbar protokolliert
- **Scheduler-Assistent:** verständliche Auswahl für manuell, täglich, werktags, wöchentlich oder Intervalle; Cron wird automatisch erzeugt, übersetzt und mit den nächsten fünf Terminen in der gewählten Zeitzone geprüft
- **Leistungsprofile:** „Schonend“, „Ausgewogen“ und „Schnell“ setzen rclone-Parallelität gemeinsam; eine Lastwarnung berücksichtigt aktive Pairs, Transfers und Checkers
- **Pair-Zeitpläne:** können den globalen Standard ausdrücklich übernehmen oder weiterhin einen eigenen verständlichen Zeitplan verwenden
- **Mobil:** Bottom-Navigation, touch-taugliche Bedienelemente und reduzierte Tabellenbreite für iPhone/Android
- **Darstellung:** Hell-, Dunkel- oder Systemmodus; Auswahl wird lokal im Browser gespeichert

Produktive Aktionen bleiben bewusst visuell von Dry-Runs getrennt. Löschende Läufe zeigen weiterhin die vorhandenen Schutzbedingungen und werden serverseitig validiert; die GUI ist keine Umgehung der Sicherheitslogik.

Tastaturkürzel: `Strg/Cmd + S` speichert offene Konfigurations- bzw. Filteränderungen. `/` fokussiert auf der Pair- oder Jobseite direkt die Suche.

## Proxmox-Betrieb

Empfohlen ist ein eigener, möglichst unprivilegierter Debian-/Ubuntu-LXC oder eine kleine VM. Die Anwendung benötigt keine Docker- oder Nesting-Funktion. Für typische Installationen genügen 1–2 vCPU und 512 MiB bis 1 GiB RAM; große Remotes, viele parallele Transfers oder `--fast-list` benötigen entsprechend mehr Speicher.

Bei LXC-Bind-Mounts müssen drei Ebenen zusammenpassen:

1. Der Pfad ist im Proxmox-Gast tatsächlich eingehängt.
2. Der Dienstbenutzer `rclone-sync` besitzt Lese-/Schreibrechte auf dem Pfad.
3. Der Pfad ist sowohl in `web.local_browse_roots` als auch in den systemd-`ReadWritePaths` freigegeben.

Für jeden NAS-/Bind-Mount empfiehlt sich zusätzlich `require_mountpoint: true` und eine Sentinel-Datei. So wird ein leerer lokaler Ordner nach einem fehlgeschlagenen Mount nicht versehentlich als gültiges Ziel behandelt.

Die Systemkarte zeigt Werte **innerhalb des Gasts**. Bei einem LXC werden vorhandene cgroup-v2-Limits für CPU und RAM bevorzugt; andernfalls gelten die vom Kernel sichtbaren Werte; die vollständige Proxmox-Hostauslastung wird bewusst nicht abgefragt und benötigt keine PVE-API-Zugangsdaten.

## Wichtigste Schutzmechanismen

### Löschschutz

Produktive `sync`- und `bisync`-Läufe werden standardmäßig nur gestartet, wenn:

1. am Pair `allow_delete: true` gesetzt ist und
2. ein endliches `max_delete` vorhanden ist.

Das gilt auch für den Quick-Sync der Weboberfläche. `copy` und Dry-Runs löschen nicht und benötigen diese Freigabe nicht.

### rclone-Argumente

Frei definierbare Argumente dürfen standardmäßig keine internen Schutzoptionen überschreiben. Blockiert werden unter anderem:

- Config-, Cache-, Workdir- und Logpfade
- `--dry-run`, `--resync` und Löschgrenzen
- Filter-, Include-/Exclude- und Backup-Verzeichnisse
- RC-/Dump-/Passwort- und SSH-Schlüsseloptionen

Eine Expertenausnahme ist mit `backup.allow_unsafe_rclone_args: true` möglich, sollte aber nur nach Prüfung des erzeugten Plans genutzt werden.

### Pfad- und Mountschutz

- Lokaler Browser und Quick-Sync bleiben innerhalb von `web.local_browse_roots`.
- Remote-Browser akzeptiert nur tatsächlich konfigurierte rclone-Remotes.
- Symlink- und String-Prefix-Ausbrüche werden verhindert.
- `require_mountpoint`, `mountpoint` und `sentinel_file` schützen vor versehentlich nicht eingehängten NAS-Pfaden.
- `min_local_files`, `min_remote_files` und `min_free_gb` verhindern riskante Läufe auf leeren oder falschen Zielen.

## Sicherheit

- bcrypt-Passwörter mit mindestens 12 Zeichen
- persistente, IP-basierte Login-Sperre in SQLite
- Session-Versionierung; Passwort-, Benutzer- oder Secret-Wechsel invalidiert bestehende Sessions
- Login-CSRF und Double-Submit-CSRF für alle schreibenden APIs
- Same-Origin-Prüfung, Host-Allowlist und begrenzte reale Request-Größe, auch bei Chunked Requests
- Sicherheitsheader, `no-store` für sensible Antworten und restriktive Cookies
- Konfiguration und Filterdatei atomar, prozessgesperrt und mit Modus `0600`
- Optimistische Revisionen verhindern stilles Überschreiben paralleler Änderungen
- `config.yaml.bak` enthält genau eine geschützte Vorversion
- vollständige GUI-Snapshots verbleiben mit Modus `0600` im lokalen Datenverzeichnis; Wiederherstellungen prüfen Name, Größe, SHA-256, aktuelle Revision und Passwort
- das herunterladbare Support-Bundle enthält nur eine redigierte Konfiguration, System-/DB-Diagnose und Log-Inventar, niemals Log-Inhalte, Passwörter, Session-Secrets oder Webhook-URLs
- Webhook-URLs werden im Browser maskiert und beim Speichern anhand stabiler IDs erhalten
- Webhooks standardmäßig nur über HTTPS; private/lokale Ziele und Redirect-SSRF werden blockiert
- DNS-Pinning zwischen Prüfung und Verbindung reduziert Rebinding-Risiken
- rclone-Unterprozesse erhalten `RCLONE_ASK_PASSWORD=false` und können keine Dienste blockierend nach einem Passwort fragen
- systemd-Sandbox mit leerem Capability-Set, `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectProc=invisible`, deaktiviertem Uvicorn-Serverheader und restriktiven Schreibpfaden

Die mitgelieferte Unit startet absichtlich genau **einen Uvicorn-Worker**. Die zentralen Daten, Locks und Login-Sperren sind prozessübergreifend, aber ein einzelner Worker hält Jobsteuerung und Web-Lebenszyklus eindeutig und vereinfacht den Betrieb.

## Installation

In einem frischen Debian-/Ubuntu-LXC als root:

```bash
curl -fsSL https://raw.githubusercontent.com/appear7240/rclone-sync-container/main/scripts/install.sh | bash
```

Oder aus einem lokalen Checkout:

```bash
git clone https://github.com/appear7240/rclone-sync-container.git /opt/rclone-sync
cd /opt/rclone-sync
sudo bash scripts/install.sh
```

Der Installer:

1. installiert rclone, Python und die benötigten Systempakete,
2. erstellt das Systemkonto `rclone-sync`,
3. sichert bei Upgrades Laufzeitdaten und rclone-Konfiguration,
4. hält alten Git-Stand, Python-Umgebung und systemd-Dateien bis zum Healthcheck als Rollback zurück,
5. erzeugt bei Neuinstallation ein bcrypt-gehashtes Initialpasswort und ein zufälliges Session-Secret,
6. validiert Konfiguration, Python-Dateien, sudoers und systemd-Units,
7. aktiviert Web-UI und Per-Pair-Scheduler,
8. deaktiviert den Legacy-Voll-Run-Timer, damit keine Doppelstarts entstehen.

Danach rclone als Dienstbenutzer konfigurieren:

```bash
sudo -u rclone-sync -H rclone config
```

Web-UI: `http://<container-ip>:8001`

Das Initialpasswort steht einmalig in `/opt/rclone-sync/data/.initial-password`. Beim Ändern des Passworts wird die Datei automatisch entfernt.

## Upgrade

Normaler Upgrade-Aufruf:

```bash
sudo bash /opt/rclone-sync/scripts/install.sh
```

Der Installer bricht bei lokalen Änderungen an getrackten Repository-Dateien ab. Diese vorher committen oder sichern. Nur bewusst kann die Prüfung übergangen werden:

```bash
sudo ALLOW_DIRTY_UPGRADE=1 bash /opt/rclone-sync/scripts/install.sh
```

Laufzeit-Sicherungen landen standardmäßig unter `/var/backups/rclone-sync/<Zeitstempel>`. Es werden zehn Sicherungen behalten. Anpassung:

```bash
sudo BACKUP_KEEP=20 BACKUP_ROOT=/srv/backups/rclone-sync \
  bash /opt/rclone-sync/scripts/install.sh
```

Nach dem Upgrade prüfen:

```bash
systemctl is-enabled rclone-sync.timer       # disabled
systemctl is-enabled sync-scheduler.timer    # enabled
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli doctor
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli validate-config
```

## Konfiguration

Datei: `/opt/rclone-sync/data/config.yaml`

Vollständiges Beispiel: `config/config.example.yaml`

Wichtige globale Werte:

- `backup.timezone`: Zeitzone für Cron-Auswertung, standardmäßig `Europe/Berlin`
- `backup.max_parallel`: maximale Zahl paralleler, nicht überlappender Pairs
- `backup.timeout_hours`: Timeout pro rclone-Lauf
- `backup.scheduler_retry_minutes`: Backoff nach fehlgeschlagenen geplanten Läufen
- `backup.scheduler_grace_minutes`: zulässiges Startfenster rund um den Cron-Zeitpunkt
- `backup.run_on_first_tick`: noch nie gelaufene Pairs nicht sofort beim ersten Timer-Tick starten
- `backup.collect_pre_post_stats`: zusätzliche Volltraversierungen; standardmäßig `false`
- `backup.require_delete_confirmation`: Freigabe für produktive `sync`-/`bisync`-Läufe erzwingen
- `backup.require_max_delete_for_sync`: endliches Löschlimit erzwingen
- `backup.allow_unsafe_rclone_args`: Schutz interner rclone-Flags nur bewusst deaktivieren
- `backup.auto_resync`: standardmäßig `false`; `bisync --resync` erst nach Log-/Planprüfung
- `web.allowed_hosts`: in Produktion konkrete DNS-Namen/IPs statt `*`
- `web.secure_cookie`: `false` bei direktem HTTP, `true` hinter festem HTTPS oder `auto` nur bei korrekt vertrautem Proxy
- `web.hsts_seconds`: erst bei dauerhaftem HTTPS aktivieren
- `web.local_browse_roots`: erlaubte lokale Browser-/Quick-Sync-Wurzeln
- `notifications.allow_http` und `allow_private_targets`: nur für bewusst vertrauenswürdige interne Ziele
- `maintenance.job_retention_days`, `keep_latest_jobs` und `log_retention_days`: Aufbewahrung

Wichtige Pair-Werte:

- `enabled`: Beispiel-Pairs sind absichtlich deaktiviert
- `direction`: `bisync`, `pull` oder `push`
- `mode`: bei einseitigen Pairs `copy` oder `sync`
- `schedule`: Cron-Ausdruck oder `manual`
- `allow_delete`: produktive Mirror-Löschungen ausdrücklich freigeben
- `max_delete`: maximal erlaubte Löschungen
- `require_mountpoint`, `mountpoint`, `sentinel_file`: Mountschutz
- `min_local_files`, `min_remote_files`, `min_free_gb`: Plausibilitätsgrenzen
- `rclone_args`: zusätzliche, validierte Argumente

### Richtungslogik

| Richtung | Modus | Quelle → Ziel | Löscht im Ziel? |
|---|---|---|---|
| `bisync` | `bisync` | Remote ↔ Lokal | möglich, beidseitig |
| `pull` | `copy` | Remote → Lokal | nein |
| `pull` | `sync` | Remote → Lokal | ja |
| `push` | `copy` | Lokal → Remote | nein |
| `push` | `sync` | Lokal → Remote | ja |

Vor jedem produktiven `sync` oder `bisync`: Pfade, Mount/Sentinel, Plan, Dry-Run und Löschlimit prüfen.

### Filter

- Pair-spezifische Filter werden strukturiert erzeugt.
- Die zentrale Filterdatei wird bei `bisync` als `--filters-file`, bei `copy/sync` als `--filter-from` eingebunden.
- Die Weboberfläche nutzt eine eigene Dateirevision; parallele Änderungen führen zu HTTP 409 statt Datenverlust.
- Eine Vorversion wird als `rclone-filters.txt.bak` gesichert.
- Änderungen an bisync-Filtern können einen kontrollierten Resync erforderlich machen.

### Backup-Verzeichnisse

- `copy/sync`: `backup_dir` wird relativ zum tatsächlichen Ziel aufgelöst.
- `bisync`: ein relatives `backup_dir` wird als `--backup-dir1/2` auf beide Seiten verteilt.
- Für absolute oder unterschiedliche bisync-Ziele `backup_dir1` und `backup_dir2` explizit setzen.

## Scheduler

Der Timer startet jede Minute einen kurzen Scheduler-Tick. Dieser entscheidet pro Pair anhand von Cron-Ausdruck, Zeitzone, Historie und Fehler-Backoff.

- Geplante und manuelle Läufe werden getrennt gespeichert.
- Nur fehlgeschlagene Scheduler-Läufe lösen automatische Backoff-Wiederholungen aus.
- Ein manueller Fehler startet nicht unbeabsichtigt eine automatische Retry-Schleife.
- Erfolgreiche Pairs eines teilweise fehlgeschlagenen Sammeljobs werden nicht erneut ausgeführt.
- Überlappende lokale oder Remote-Pfade werden automatisch seriell verarbeitet.
- Der alte tägliche Voll-Run-Timer bleibt standardmäßig deaktiviert.

## systemd

| Unit | Funktion | Standard |
|---|---|---|
| `rclone-sync-web.service` | FastAPI-Web-UI auf Port 8001 | aktiv |
| `sync-scheduler.timer` | prüft jede Minute fällige Pair-Zeitpläne | aktiv |
| `sync-scheduler.service` | führt einen Scheduler-Tick aus | timer-gesteuert |
| `rclone-sync.service` | manueller Voll-Run aller aktiven Pairs | inaktiv |
| `rclone-sync.timer` | alter täglicher Voll-Run | deaktiviert |

Die Units verwenden fest:

```text
RCLONE_CONFIG=/home/rclone-sync/.config/rclone/rclone.conf
RCLONE_ASK_PASSWORD=false
```

Das rclone-Konfigurationsverzeichnis ist gezielt beschreibbar, damit OAuth-Tokens erneuert werden können. Bei lokalen Datenpfaden außerhalb `/mnt`, `/media`, `/srv` oder `/opt/rclone-sync` müssen die `ReadWritePaths` per systemd-Drop-in ergänzt werden.

## Reverse-Proxy

Hinter einem HTTPS-Reverse-Proxy:

```yaml
web:
  secure_cookie: true
  hsts_seconds: 31536000
  allowed_hosts:
    - sync.example.org
```

Die Unit vertraut Forwarded-Header standardmäßig nur von `127.0.0.1` und `::1`. Läuft der Proxy in einem anderen Container, dessen feste IP gezielt bei `--forwarded-allow-ips` in einem systemd-Drop-in ergänzen. Kein pauschales `*` verwenden.

Ein äußerer MFA-Layer wie Cloudflare Access kann die Webanmeldung zusätzlich absichern.

## CLI

```bash
# Passwort setzen und alle Sessions invalidieren
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli set-password

# Session-Secret rotieren
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli gen-secret

# Konfiguration prüfen
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli validate-config

# Letzte automatische Config-Vorversion wiederherstellen
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli restore-config-backup --yes

# Diagnose und Ausführungsplan
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli doctor
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli plan --pairs Serien,Filme

# Read-only Vergleich
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli check-pair Serien --one-way

# Logs prüfen oder löschen
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli prune-logs --days 30 --dry-run
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli prune-logs --days 30 --delete

# SQLite prüfen und optional alte Jobs löschen
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli db-maintenance
sudo -u rclone-sync /opt/rclone-sync/venv/bin/python -m app.cli db-maintenance --days 180 --keep-latest 500 --delete

# Manueller Voll-Run
sudo systemctl start rclone-sync.service
```

## API

Alle `/api/*`-Endpunkte benötigen eine gültige Session. Schreibende Methoden benötigen zusätzlich den CSRF-Header der Weboberfläche. Die Konfigurations- und Filter-APIs verlangen aktuelle Revisionen.

Wichtige Endpunkte:

- `GET /api/jobs/status/current`
- `GET /api/jobs/search?kind=&status=&q=&limit=&offset=`
- `GET /api/jobs/export.csv?kind=&status=&q=`
- `GET /api/jobs/{id}/log/download`
- `GET /api/jobs/backup/plan`
- `POST /api/jobs/backup/run?dry_run=true`
- `POST /api/jobs/backup/run-pair/{pair}?dry_run=true`
- `POST /api/jobs/backup/cancel`
- `POST /api/jobs/backup/check/{pair}`
- `GET /api/jobs/backup/progress`
- `POST /api/jobs/backup/quick`
- `GET /api/diagnostics/doctor`
- `GET /api/diagnostics/overview`
- `GET /api/config`
- `PUT /api/config`
- `POST /api/config/validate`
- `GET|PUT /api/config/filter-file`
- `GET /api/maintenance/logs`
- `POST /api/maintenance/logs/prune`
- `POST /api/maintenance/database/prune`
- `GET|POST /api/maintenance/config/snapshots`
- `POST /api/maintenance/config/snapshots/restore`
- `GET /api/maintenance/support-bundle`
- `GET /healthz` – Prozess-Liveness
- `GET /readyz` – DB-/Datenverzeichnis-Readiness für Uptime Kuma/systemd
- `GET /healthz/deep` – authentifiziert

## Wartung und Wiederherstellung

Regelmäßig sichern:

- `/opt/rclone-sync/data/config.yaml*`
- `/opt/rclone-sync/data/rclone-filters.txt*`
- `/opt/rclone-sync/data/rclone-sync.db*`
- `/opt/rclone-sync/data/.rclone-cache/`, insbesondere das bisync-Workdir
- `/home/rclone-sync/.config/rclone/rclone.conf`

Die Anwendung hält mindestens `maintenance.keep_latest_jobs` Jobs, auch wenn diese älter als die Retention sind. SQLite-Integrität, Checkpoint und Log-Retention sind über Weboberfläche und CLI prüfbar.

Fehlt der bisync-State, nicht blind automatisch resyncen. Zuerst beide Seiten, Filter, Richtung, Konfliktstrategie und Dry-Run prüfen.

## Tests

```bash
python -m pytest -q
ruff check .
ruff format --check .
python -m compileall -q app tests
node --check app/static/app.js
bash -n scripts/install.sh
git diff --check
```
