# Audit und Optimierungen – rclone-sync-container 1.8.2

Stand: 17.07.2026


## 1.8.2

- Frontend-Cache-Busting repariert: Die Asset-Version in index.html war auf 1.7.0 festgenagelt, Browser hielten daher nach Updates am alten app.js fest — neue GUI-Bereiche (z. B. der PBS-Tab) erschienen nicht ohne Hard-Reload. Die Version wird jetzt beim Ausliefern aus der App-Version injiziert.
- Lokal→lokal-Sync freigeschaltet: Das Remote-Feld akzeptiert in Pairs und Quick-Sync jetzt auch absolute lokale Pfade (Quick-Sync beschränkt auf die Browser-Wurzeln). Neuer „Lokal"-Button am Remote-Feld öffnet den lokalen Ordner-Browser. Ineinanderliegende lokale Pfade werden als Fehler abgewiesen (Endlos-Kopien).


## 1.8.1

- PBS-Target-Editor: „Ordner auswählen"-Button öffnet den vorhandenen lokalen Datei-Browser (beschränkt auf web.local_browse_roots) und hängt den gewählten Pfad an die Pfadliste an — eingebundene Festplatten unter /mnt lassen sich damit anklicken statt abtippen.


## Feature 1.8.0 — Proxmox-Backup-Server-Integration

- Neuer Job-Typ `pbs`: dateibasierte Backups konfigurierter Pfade per `proxmox-backup-client` in einen PBS-Datastore (`app/jobs/pbs_backup.py`). Nutzt dieselbe Prozess-, Log-, Cancel- und Job-Infrastruktur wie rclone; Läufe werden als `pair_runs` mit Prefix `pbs:` persistiert.
- Konfiguration über neue `pbs`-Sektion (Repository `user@realm!token@host:datastore`, Token-Secret maskiert wie web.password, TLS-Fingerprint, Namespace, Backup-ID, Timeout, optionales serverseitiges Prune via keep-*), vollständig validiert inkl. Cron-Zeitplänen je Target.
- Scheduler: `find_due_pbs_targets` mit derselben last_success-/Retry-Backoff-/Nachholfenster-Mechanik; der Minuten-Tick startet fällige PBS-Targets unter eigenem File-Lock parallel unabhängig vom rclone-Lock.
- API `/api/pbs/status` und `/api/pbs/run` (alle Targets oder einzeln), GUI: Einstellungs-Tab „Proxmox Backup" mit Target-Editor und Dashboard-Karte mit Sichern-Buttons; fehlender Client wird angezeigt.
- Mount-Schutz: fehlende Target-Pfade führen zu einem Fehler statt einem leeren Backup. Abbruch über den bestehenden Cancel wirkt auch auf PBS-Prozesse.
- Installer nennt am Ende die Installationsbefehle für `proxmox-backup-client` (pbs-client-Repository).



## Hotfix 1.7.4

- `Origin: null` ohne `Sec-Fetch-Site`-Header wird wieder akzeptiert (ältere Browser, Webviews und Privacy-Extensions, die Fetch-Metadata strippen, blieben sonst vom Login ausgesperrt). Abgelehnt wird nur noch, wenn `Sec-Fetch-Site` die Anfrage ausdrücklich als cross-/same-site meldet. Der Double-Submit-CSRF-Schutz mit SameSite=strict-Cookies bleibt wie vor 1.7.1 die maßgebliche Verteidigung.

## Hotfix 1.7.3

- Origin-Prüfung vergleicht nur noch Host:Port statt Schema+Host: Hinter TLS-Reverse-Proxys, deren `X-Forwarded-Proto` uvicorn nicht vertraut, sendet der Browser `https://…`, während die App `http` sieht — das blockierte Logins fälschlich. Der Host im Origin-Header ist nicht fälschbar; die Schutzwirkung bleibt erhalten.
- Explizit konfigurierte `web.allowed_hosts` (nicht der Wildcard-Default) gelten zusätzlich als vertrauenswürdige Origins, falls ein Proxy den Host-Header umschreibt.
- Fehlgeschlagene Origin-Prüfungen werden jetzt mit Origin, erwartetem Host und Pfad ins Journal geloggt (`journalctl -u rclone-sync-web`), inklusive Handlungshinweis.

## Hotfix 1.7.2

- Login-Regression aus 1.7.1 behoben: Wegen `Referrer-Policy: no-referrer` senden manche Browser (v. a. Firefox) auch bei same-origin Formular-POSTs `Origin: null`; die harte Ablehnung blockierte den Login mit „Origin-Prüfung fehlgeschlagen". `Origin: null` wird jetzt über `Sec-Fetch-Site` entschieden: `same-origin`/`none` sind erlaubt, `cross-site`/fehlend bleibt abgelehnt.


## Härtung und Fehlerkorrekturen in 1.7.1

- Zwei in der 1.7.0-Dev-Lieferung verlorene Stände aus dem Repository reintegriert: der Baseline-Resync beim allerersten bisync-Lauf (`auto_resync_first_run`, v1.5.3) und die Lock-Freigabe, wenn `job_start` beim Anlegen eines Web-Jobs fehlschlägt (sonst dauerhaft HTTP 409 bis zum Neustart). Beide Regressionstests sind wieder enthalten.

- Quick-Sync kann jetzt bewusst in ein neues, leeres lokales Ziel laufen: neues Feld `min_local_files` in der Quick-API plus GUI-Checkbox „Neues leeres Ziel"; das Verzeichnis wird nach bestandenem Precheck wie bei regulären Pairs angelegt.
- `Origin: null` wird bei state-changing Requests nicht mehr als same-origin akzeptiert (sandboxed iframes/Redirect-Ketten); das Double-Submit-CSRF bleibt als zweite Schicht bestehen.
- Logout erhöht die Session-Version und widerruft damit alle ausgestellten Session-Tokens serverseitig (Single-Admin-Modell); nur Cookies zu löschen ließ kopierte Tokens gültig.
- 500-Antworten des globalen Exception-Handlers erhalten jetzt ebenfalls die Security-Header und `Cache-Control: no-store` (der Handler läuft außerhalb der Security-Middleware).
- Cancel-Webhooks werden in einem Hintergrund-Thread verschickt und blockieren den Abbruch-Request nicht mehr (Webhook-Timeout bis 60 s).
- Doppelte Implementierungen zusammengeführt: `bounded_int`/`bounded_number` in `app/utils.py`, gemeinsames `read_log_tail`/`parse_final_stats` in `app/jobs/rclone_sync.py` (zuvor leicht abweichende Stats-Regexe in Web-API und Sync-Kern).
- Installer weist am Ende ausdrücklich auf fehlende Transportverschlüsselung hin und empfiehlt Reverse-Proxy mit TLS, `secure_cookie: auto`, HSTS und enge `allowed_hosts`.



## Scheduler-Betrieb, Frischeüberwachung und Audit in 1.7.0

- Automatische Scheduler-Läufe können persistent für 30 Minuten bis 31 Tage pausiert werden; nach Ablauf wird automatisch fortgesetzt.
- Dauerhafte globale Scheduler-Deaktivierung über `backup.enabled` wird im Scheduler jetzt tatsächlich beachtet.
- Dashboard und Einstellungsseite zeigen den Unterschied zwischen aktiv, temporär pausiert und dauerhaft deaktiviert verständlich an.
- Schnelle Wartungsaktionen für eine Stunde oder bis zum nächsten Morgen erleichtern Proxmox-Backups und NAS-Neustarts.
- Pairs können mit `max_success_age_hours` überwacht werden; fehlende oder zu alte erfolgreiche Läufe erscheinen als Warnung.
- Lokales SQLite-Auditprotokoll erfasst Scheduler-, Konfigurations-, Start-, Passwort- und Recovery-Aktionen ohne Secrets.
- Auditdaten werden in Support-Bundles aufgenommen und gemeinsam mit der Historie kontrolliert aufbewahrt.
- Neuer `/readyz`-Endpunkt prüft Datenbank und beschreibbares Datenverzeichnis ohne sensible Details; der Installer verwendet ihn für den Upgrade-Healthcheck.
- GUI für Desktop und Mobil um Scheduler-Wartungsfenster, Frischewarnungen und Aktivitätsprotokoll erweitert.


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
