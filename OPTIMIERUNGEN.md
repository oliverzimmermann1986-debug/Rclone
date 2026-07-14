# Audit und Optimierungen – rclone-sync-container 1.6.0

Stand: 14.07.2026


## Präzisere Einstellungen und Scheduler-Assistent in 1.6.0

- Einstellungsnavigation mit Nummerierung, Kurzbeschreibung und sichtbarem Speicherstatus neu strukturiert.
- Scheduler nicht mehr als reine Cron-/Zahlenmaske: Auswahl für manuell, täglich, werktags, wöchentlich, Stunden- und Minutenintervalle.
- Cron-Ausdruck wird aus der verständlichen Auswahl erzeugt; Expertenmodus und direkte Cron-Bearbeitung bleiben erhalten.
- Neue authentifizierte Vorschau-API validiert ungespeicherte Zeitpläne und berechnet die nächsten fünf Ausführungen in der konfigurierten Zeitzone.
- Nachholfenster, Fehler-Retry, Parallelität und Timeout fachlich erklärt und in einzelne Wirkungskarten aufgeteilt.
- Live-Zusammenfassung zeigt Standard-Zeitplan, Zeitzone, Parallelität, Timeout, Retry und nächste Termine.
- rclone-Leistungsprofile „Schonend“, „Ausgewogen“ und „Schnell“ sowie dynamische Lastwarnung für Proxmox-LXC ergänzt.
- Transfers, Checkers, Wiederholungen und Löschlimit enthalten konkrete Wirkungserklärungen; freie rclone-Argumente liegen in einem Expertenbereich.
- Aufbewahrung als eigener verständlicher Abschnitt mit Einheiten und automatischer Wartung neu gestaltet.
- Pair-Zeitpläne können den globalen Standard jetzt ausdrücklich übernehmen; Karten zeigen die tatsächlich wirksame Planung verständlich an.
- Mobile Darstellung für Zeitplan-Auswahl, Erklärungskarten, Leistungsprofile und Live-Vorschau optimiert.


## Neu und behoben in 1.5.3

- **Neu:** Baseline-Resync beim Erstlauf eines bisync-Pairs läuft automatisch (`auto_resync_first_run`, Standard an, pro Pair abschaltbar). Gilt ausschließlich, solange das Pair noch nie erfolgreich gelaufen ist — spätere Resync-Verlangen bleiben wie bisher gesperrt, bis `auto_resync` bewusst gesetzt wird. Bei DB-Fehlern bleibt der Resync fail-closed gesperrt. Vier neue Tests.
- **Behoben:** „Log kopieren" schlug über HTTP immer fehl, weil `navigator.clipboard` nur in Secure Contexts existiert. Jetzt Textarea-Fallback; die Fehlermeldung verweist auf den Download-Button.


## Bugfixes in 1.5.2

- **Behoben:** Der Remote-Pre-Check rief `rclone lsjson` mit dem nicht existierenden Flag `--no-size` auf — jeder Pair-Lauf mit Remote-Pfad scheiterte sofort mit „unknown flag". Das Flag existiert in keiner rclone-Version (lsjson kennt nur `--no-mimetype`/`--no-modtime`, siehe https://rclone.org/commands/rclone_lsjson/); es wurde entfernt.
- **Hinweis:** Die bisync-Flags der App (`--resilient`, `--recover`, `--conflict-resolve`) benötigen rclone ≥ 1.66. Debian-Paketstände (z. B. 1.60.1 in Debian 13) reichen nicht; aktuelles rclone von rclone.org installieren.


## Bugfixes in 1.5.1

- **Behoben:** Lock-Leak in der Jobs-API. Schlug `job_start()` nach dem Erwerb des In-Prozess-Locks fehl (z. B. gesperrte SQLite-DB, volle Platte), blieb der Lock dauerhaft belegt und jeder weitere Sync/Check/Quick-Lauf lieferte bis zum Service-Neustart HTTP 409 „Backup läuft bereits". Der Lock wird jetzt bei Anlage-Fehlern zuverlässig freigegeben; zwei Regressionstests sichern das Verhalten ab (`tests/test_job_lock_release.py`).
- **Behoben:** Manuell abgebrochene Läufe über `backup_cli` und `scheduler_cli` wurden als `error` statt `cancelled` in der Jobhistorie gespeichert. CLI und Scheduler persistieren den Status jetzt konsistent zur Web-Route.


## GUI-, Recovery- und Betriebs-Ausbau in 1.5.0

- Neue löschsichere Pair-Vorlagen: `push/copy` als Standard, zusätzlich Restore-Copy, Bi-Sync und Mirror; alle Vorlagen starten deaktiviert und manuell.
- Pair-Entwürfe werden direkt gegen die ungespeicherten GUI-Werte geprüft. Start, Plan und Read-only-Check speichern offene Änderungen dagegen bewusst vor Ausführung.
- Pair-Karten zeigen Konfigurationshinweise getrennt von echten Laufzeitfehlern, konkrete Fehlertexte, letzten und nächsten Lauf sowie Sammelaktionen zum Öffnen/Schließen.
- Volltextsuche der Jobhistorie nach Pair-Namen, Fehlertext, Logdatei oder exakter Job-ID; CSV-Export übernimmt aktive Filter.
- Joblogs können als gestreamte Datei heruntergeladen werden, ohne die gesamte Datei im Webprozess zu puffern.
- GUI-Konfiguration lässt sich vor dem Speichern vollständig validieren; parallele Änderungen werden sichtbar gemacht.
- Vollständige lokale Konfigurations-Snapshots mit restriktiven Rechten, maximaler Aufbewahrung, SHA-256-Prüfung und automatischem Pre-Restore-Snapshot.
- Restore erhält aktuelle Login-Identität und Secrets, erhöht aber die Session-Version und erzwingt eine Neuanmeldung.
- Redigiertes Support-Bundle mit Konfiguration, System-/SQLite-Diagnose, letzter Jobhistorie und Log-Inventar; Passwörter, Session-Secrets, Webhook-URLs und Log-Inhalte werden nicht exportiert.
- Markupfehler im Pair-Bereich behoben, der Karten je nach Browser aus dem Abschnitt ziehen konnte.

## Proxmox- und GUI-Ausbau in 1.4.0

- Vollständig neue responsive Betriebskonsole mit Desktop-Sidebar und mobiler Bottom-Navigation.
- Dashboard mit Gast-Hostname, Virtualisierungstyp, IP-Adressen, Uptime, CPU-Load, RAM-, PID- und Datenträgerauslastung; Proxmox-LXC-cgroup-v2-Limits werden berücksichtigt.
- systemd-Servicezustände, 24-Stunden-Jobstatistik, Pair-Gesundheit, nächste Ausführungen und priorisierte Warnungen in einer Übersicht.
- Sichere Startzentrale mit klarer Trennung von Plan, Dry-Run, produktivem Lauf und Abbruch.
- Pair-Verwaltung als Kartenansicht mit Suche, Filtern, Plausibilitätswarnungen, Sortieren, Klonen sowie Aktionen pro Pair.
- Filterbare und paginierte Jobhistorie mit Detailmodal, Ergebnisdaten, Logsuche, Kopierfunktion und Live-Aktualisierung.
- Überarbeiteter Doctor-/Wartungsbereich für Proxmox-Gastbetrieb, Logs, Datenbank und Speicher.
- Konfiguration in fachliche Tabs aufgeteilt; `secure_cookie: auto` bleibt in der GUI erhalten und wird nicht mehr versehentlich in einen booleschen Wert umgewandelt.
- Modernisierte Login-Seite und Hell-/Dunkel-/Systemmodus.
- Neue APIs für Betriebsübersicht, Jobfilter/Pagination und sicheren Pair-Dry-Run. Dashboard-Abfragen bündeln Pair-Historien und cachen kurzlebig systemd-Zustände.
- Zusätzliche Template-Regressionstests prüfen eindeutige HTML-IDs, Navigationsziele und alle direkt aufgerufenen Alpine-Komponentenmethoden.

## Behobene kritische Funktionsfehler

1. **Verb-spezifische Flags:** bisync-Optionen werden nicht mehr an `copy` oder `sync` angehängt.
2. **Doppelte Zeitsteuerung:** der Per-Pair-Scheduler ist Standard; der Legacy-Nachttimer bleibt deaktiviert.
3. **Falsche Umgebungsvariable:** alle Units verwenden `RCLONE_SYNC_CONFIG` statt einer fremden Altvariable.
4. **Backup-Verzeichnisse:** einseitige Läufe legen Backups auf der tatsächlichen Zielseite ab; bisync nutzt `--backup-dir1/2`.
5. **bisync-Filter:** zentrale Filter werden als `--filters-file` eingebunden und damit korrekt in den bisync-State einbezogen.
6. **Prozessübergreifender Abbruch:** auch rclone-Prozesse aus CLI- und Scheduler-Läufen können kontrolliert beendet werden.
7. **Verwaiste Zustände:** laufende Jobs und Pair-Status werden nach Abstürzen als `stale` abgeschlossen.
8. **Scheduler-Retry:** geplante und manuelle Trigger werden unterschieden; nur Scheduler-Fehler lösen Backoff-Retries aus.
9. **Löschsperren:** produktive `sync`- und `bisync`-Läufe benötigen Bestätigung und begrenztes `max_delete`.
10. **CLI-Registrierung:** vorhandene Befehle `validate-config` und `restore-config-backup` sind jetzt tatsächlich erreichbar.
11. **Request-Limit:** echte Body-Bytes werden auch ohne `Content-Length` gezählt; Framework-Parserfehler werden zuverlässig als HTTP 413 ausgegeben.
12. **Security-Middleware:** inkompatibles Entfernen des Server-Headers wurde durch eine versionssichere Variante ersetzt.

## Daten- und Konkurrenzsicherheit

- Prozess- und thread-sicherer YAML-Speicher mit `flock`, atomarem `os.replace` und `fsync`.
- Optimistische Konfigurationsrevisionen verhindern verlorene Updates in der Weboberfläche.
- Auch die ältere interne Kombination `set()`/`save()` erkennt parallele Änderungen.
- Eine restriktive `config.yaml.bak` ermöglicht kontrolliertes Rollback.
- Filterdateien besitzen eigene SHA-256-Revisionen, File-Lock, atomare Speicherung und `.bak`.
- Lockdateien werden ohne Symlink-Folgen geöffnet und erst nach erfolgreichem `flock()` beschrieben.
- Konkurrierende Prozesse können die Besitzer-PID eines Locks nicht mehr leeren.
- Cancel-Marker werden atomar ersetzt und folgen keinen vorhandenen Symlinks.
- Pair-Ergebnisse liegen zusätzlich in einer indexierten SQLite-Tabelle statt nur in großen Job-JSONs.
- Bestehende Jobhistorien werden einmalig in die Pair-Tabelle migriert.
- Login-Sperren sind in SQLite persistent und funktionieren über Neustarts und Prozesse hinweg.

## rclone-Sicherheit

- Alle Quell-/Zielpfade werden mit `--` von Optionen getrennt; Pfade wie `--config` werden nicht als Flags interpretiert.
- Frei konfigurierbare Argumente können interne Config-, Cache-, Filter-, Backup-, RC-, Logging-, Dry-Run-, Resync- und Löschschutzoptionen nicht überschreiben.
- Eine bewusst aktivierbare Expertenausnahme bleibt vorhanden.
- Alle rclone-Unterprozesse erhalten `RCLONE_ASK_PASSWORD=false` und `stdin=DEVNULL`.
- systemd setzt einen eindeutigen `RCLONE_CONFIG`-Pfad und erlaubt dort gezielt OAuth-Token-Erneuerungen.
- Remote-Erreichbarkeit nutzt `lsjson --stat` statt einer potenziell großen Verzeichnisauflistung.
- Remote-Dateibrowser nutzt begrenztes `rclone lsf` und beendet den Prozess nach 1.001 Einträgen oder Timeout.
- Lokale Browserlisten werden speicherbegrenzt mit `heapq.nsmallest` erstellt.
- Beispiel-Pairs sind deaktiviert, bis Pfade, Mount, Dry-Run und Löschgrenzen geprüft wurden.

## Web- und Sitzungssicherheit

- bcrypt-Passwörter, Mindestlänge 12 und automatische Entfernung der Initialpasswortdatei.
- Session-Version und Benutzerbindung invalidieren Sessions bei Passwort-, Secret- oder Benutzerwechsel.
- Login-CSRF, API-CSRF und Same-Origin-Prüfung.
- Persistent begrenzte Login-Fehler pro Client-IP.
- Host-Allowlist, sichere Cookie-Optionen und optionales HSTS.
- Security-Header, `no-store` und Request-ID für Fehlerkorrelation.
- API-Request-Größe wird anhand real empfangener Bytes begrenzt.
- Webhook-URLs werden im Browser vollständig maskiert und anhand stabiler IDs erhalten.
- Redigierte Konfigurationsexporte enthalten weder Login-Secrets noch Webhook-Ziele.

## Webhook-Härtung

- HTTPS ist Standard; HTTP muss ausdrücklich aktiviert werden.
- Private, Loopback-, Link-Local- und reservierte Ziele werden standardmäßig blockiert.
- DNS-Auflösung wird geprüft und die Verbindung an die geprüfte IP gepinnt.
- Jeder Redirect wird erneut validiert; Zahl der Redirects ist begrenzt.
- Host-Header und TLS-SNI bleiben auf dem ursprünglichen Hostnamen.
- Request-/Response-Größe, Timeout und Parallelität sind begrenzt.
- Beschädigte manuelle Altwerte werden auf sichere Grenzen zurückgesetzt.

## Scheduler und Laufzeit

- Konfigurierbare Zeitzone, standardmäßig `Europe/Berlin`.
- Kontrollierte Erstläufe, Grace Window und Fehler-Backoff.
- Manuelle Fehler erzeugen keine ungeplanten automatischen Retries.
- Pair-Erfolg wird unabhängig vom Gesamtstatus eines Sammeljobs gespeichert.
- Überlappende lokale oder Remote-Pfade werden seriell ausgeführt.
- Laufzeitmarker enthalten PID und Prozessstart-Ticks gegen PID-Reuse.
- Registrierte rclone-Prozessgruppen werden zunächst mit SIGTERM, danach optional mit SIGKILL beendet.
- Ungültige numerische Altwerte werden begrenzt statt den Scheduler zu stoppen.

## Performance und Wartung

- Vollständige Pre-/Post-Traversierungen sind standardmäßig deaktiviert.
- Dashboard und Speicherübersicht nutzen indexierte letzte Pair-Erfolge.
- Log-Tailing liest nur begrenzte Dateibereiche.
- Loglisten und lokale Verzeichnislisten bleiben speicherbegrenzt.
- Remote-Größen werden optional und begrenzt parallel ermittelt.
- SQLite-Aufbewahrung behält unabhängig vom Alter eine Mindestanzahl neuer Jobs.
- Automatische Log-Retention, Auth-Fehlerbereinigung und WAL-Checkpoint.
- Doctor prüft Konfiguration, SQLite-Integrität, Pfade, Remotes, Timer, Mounts, Sentinel-Dateien, Filter und Löschfreigaben.

## Installer und systemd

- Upgrade-Sicherung für Laufzeitdaten und rclone-Konfiguration unter `/var/backups/rclone-sync`.
- Abbruch bei ungesicherten Änderungen an getrackten Repository-Dateien.
- Vorheriger Git-Stand, Python-Venv und systemd-/sudoers-Dateien bleiben bis zum erfolgreichen Healthcheck als Rollback erhalten.
- Fehlerpfad stellt die alte Version und zuvor aktive Dienste bestmöglich wieder her.
- Konfiguration, Python-Bytecode, sudoers und systemd-Units werden vor Aktivierung geprüft.
- Nach dem Start erfolgt ein lokaler Healthcheck.
- systemd-Sandbox erweitert um `ProtectKernelLogs`, `ProtectProc`, `RestrictNamespaces`, `MemoryDenyWriteExecute`, private Keyring und weitere Einschränkungen.
- Scheduler ist nicht mehr unnötig vom Webdienst abhängig.
- Forwarded-Header werden nur von explizit erlaubten Proxy-IP-Adressen vertraut; die produktive Uvicorn-Unit sendet keinen Serverheader.

## Testabdeckung

**59 automatisierte Regressionstests** decken unter anderem ab:

- atomare Config- und Filterupdates, Revisionen, Backups und Konflikte
- persistente Login-Sperren und DB-Migration
- geschützte rclone-Argumente, Options-/Pfadtrennung und Backup-Ziele
- produktive Löschsperren für `sync` und `bisync`
- Scheduler-Trigger, Retry und manuelle Fehler
- verwaiste Laufzeit- und Datenbankzustände
- SSRF-Blockaden und Webhook-Grenzwerte
- CSRF, Login, Secret-Redaktion und Sessionablauf
- tatsächliche Request-Größenbegrenzung ohne `Content-Length`
- sichere File-Locks und atomare Cancel-Marker
- CLI-Registrierung und Wartungsgrenzen
- Dashboard-Systemdaten, Jobfilter/Pagination, Volltextsuche, CSV-/Log-Download und Pair-Entwurfsprüfung
- Snapshot-Erstellung/-Restore, SHA-256-Konflikte und Support-Bundle-Redaktion
- GUI-Template-Integrität, eindeutige IDs und Alpine-Methodenbindung
- cgroup-v2-CPU-/RAM-/PID-Limits und gebündelte Pair-Historien

Zusätzlich geprüft:

- Ruff-Linting und Formatierung
- Python-Kompilierung
- JavaScript-Syntax
- Shell-Syntax
- Patch-Anwendbarkeit und Paket-Neutest vor Auslieferung

## Bewusste Grenzen

- `auto_resync` bleibt standardmäßig deaktiviert. Ein bisync-Resync ist eine fachliche Entscheidung.
- Die produktiven Remotes und Mounts müssen nach Installation mit Plan und Dry-Run getestet werden; die Testumgebung enthält keine Zugangsdaten.
- Zusätzliche lokale Wurzeln benötigen sowohl `web.local_browse_roots` als auch eine passende systemd-`ReadWritePaths`-Freigabe.
- Ein Reverse-Proxy außerhalb des Containers muss mit seiner festen IP ausdrücklich in `--forwarded-allow-ips` eingetragen werden.
- Die mitgelieferte Unit nutzt bewusst einen Uvicorn-Worker.
