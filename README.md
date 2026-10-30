# Sicherpfad Server

**Version 2.0.0** – Web- und Scheduler-Dienst für sichere `rclone bisync`, `copy`- und `sync`-Läufe zwischen Cloud-Remotes und lokalen NAS-/Storage-Pfaden. Ausgelegt für einen Debian-/Ubuntu-LXC mit systemd.

## Kernfunktionen

- Mehrere Datenwege, geordnete Jobdefinitionen mit Cron-Zeitplan sowie manuelle Läufe mit begrenzter Parallelität
- Persistente Scheduler-Wartungsfenster mit automatischer Fortsetzung
- Frischeüberwachung pro Pair für zu alte oder fehlende erfolgreiche Läufe
- Ausbleib-Alarm: meldet auch den Fall, dass ein Lauf gar nicht erst startet
- Restore-Drill: holt Stichproben zurück und vergleicht sie per Prüfsumme mit der Quelle
- Recovery Center mit exportierbarem Schutzpass, RPO/RTO-Nachweis, Schutzkalender und sicheren Profilen
- Anomalie-Quarantäne stoppt destruktive Läufe bei unerwartet geschrumpften Quellen vor dem Start
- Selektive Wiederherstellung schreibt ausschließlich in ein getrenntes, prüfsummengeprüftes Staging
- AES-256-GCM-verschlüsselte Notfallübergabe ohne Passwörter oder Cloud-Schlüssel
- Kopien-Matrix je Datenbestand für die 3-2-1-Frage statt nur Pair-Zeilen
- `bisync`, `pull` und `push`; einseitig wahlweise `copy` oder `sync`
- Prozessübergreifende Locks, Laufzeitstatus und Abbruch für Web, CLI und Scheduler
- Mount-, Sentinel-, Mindestdatei-, Freiplatz- und Remote-Prüfungen vor jedem Lauf
- Planvorschau und Dry-Run vor produktiven Änderungen
- Per-Pair-Logs, indexierte Pair-Historie, Job-Historie und Live-Fortschritt
- Read-only `rclone check`, Diagnosebereich und Speicherübersicht
- Pair-Filter, zentrale Filterdatei, Tuning und optionale Backup-Verzeichnisse
- Native APNs-Fehlerbenachrichtigungen mit dauerhafter Outbox, Deduplizierung und Retry
- Automatische Log-/Datenbankaufbewahrung
- Konfigurationsrevisionen, Rollbackdatei und sichere Upgrade-Sicherung
- Responsive Proxmox-Betriebskonsole mit Desktop-Sidebar und mobiler Bottom-Navigation
- Ressourcenübersicht für LXC/VM einschließlich cgroup-CPU/RAM/PID-Limits, Servicezustände, Pair-Gesundheit und 24-Stunden-Statistik
- Filterbare/paginierte Jobhistorie mit Detailansicht, Logsuche und Auto-Aktualisierung
- Lokales Auditprotokoll für Scheduler-, Konfigurations-, Start- und Recovery-Aktionen
- Kompakter `/readyz`-Endpunkt für Uptime Kuma und Readiness-Prüfungen
- Notfall-Lockdown, Cloud-Bitrot-Scrub, PBS-Boot-Drill, Airgap-USB, ZFS-Snapshot-Quelle und weitere Erweiterungen; Überblick unter [Betriebserweiterungen](#betriebserweiterungen)


## Weboberfläche

Die Oberfläche ist als Betriebszentrale statt als reine Konfigurationsmaske aufgebaut:

- **Übersicht:** laufender Job, letzte Ergebnisse, Warnungen, Pair-Gesundheit, nächste Ausführungen und 24-Stunden-Kennzahlen
- **Proxmox-Systemkarte:** Virtualisierungstyp, Gast-Hostname, IP-Adressen, Uptime, CPU-Load, RAM und Datenträgerauslastung; cgroup-v2-Limits werden berücksichtigt
- **Startzentrale:** Planvorschau, Dry-Run, produktiver Lauf und Abbruch klar getrennt
- **Datenwege:** responsive Karten, Suche, Statusfilter, sichere Copy-/Bi-Sync-/Mirror-Vorlagen, Klonen, Sortieren, Dry-Run und Read-only-Check
- **Entwurfsprüfung:** ungespeicherte Pair-Änderungen können direkt gegen rclone geprüft werden; Plan, Check und Start speichern offene Änderungen erst bewusst, damit nie versehentlich ein veralteter Stand läuft
- **Jobs:** geordnete Datenweg-Zuweisung, Zeitplan, sequenzielle oder parallele Ausführung, Retry-Intervall, Planvorschau und bewusster Start
- **Läufe:** Filter nach Typ und Status, Volltextsuche nach Datenweg/Definition/Fehler/ID, Pagination, CSV-Export, sicherer Retry, Ergebnisdetails, Live-Log, Logsuche, Kopier- und Downloadfunktion
- **System:** Doctor-Prüfung, Servicezustände, Speicherübersicht, Push-Status/Test, redigiertes Support-Bundle sowie kontrollierte Log-/Datenbankwartung
- **Recovery:** lokale, vollständige Konfigurations-Snapshots mit SHA-256-Prüfung, Pre-Restore-Sicherung, Revisionsschutz und erzwungener Neuanmeldung nach Restore
- **Einstellungen:** klar beschriftete Bereiche mit Änderungsstatus, Validierung und kurzen fachlichen Erklärungen
- **Scheduler-Betriebssteuerung:** Automatik dauerhaft deaktivieren oder für Wartungsfenster von 30 Minuten bis 31 Tage pausieren; manuelle Läufe bleiben möglich
- **Frischeüberwachung:** pro Pair optional festlegen, nach wie vielen Stunden ohne Erfolg eine Warnung erscheint
- **Audit:** wichtige Aktionen werden ohne Secrets lokal und filterbar protokolliert
- **Scheduler-Assistent:** verständliche Auswahl für manuell, täglich, werktags, wöchentlich oder Intervalle; Cron wird automatisch erzeugt, übersetzt und mit den nächsten fünf Terminen in der gewählten Zeitzone geprüft
- **Leistungsprofile:** „Schonend“, „Ausgewogen“ und „Schnell“ setzen rclone-Parallelität gemeinsam; eine Lastwarnung berücksichtigt aktive Pairs, Transfers und Checkers
- **Job-Zeitpläne:** werden gemeinsam mit Datenweg-Reihenfolge, Parallelität und Fehler-Retry verwaltet
- **Mobil:** Bottom-Navigation, touch-taugliche Bedienelemente und reduzierte Tabellenbreite für iPhone/Android
- **Darstellung:** Hell-, Dunkel- oder Systemmodus; Auswahl wird lokal im Browser gespeichert

Produktive Aktionen bleiben bewusst visuell von Dry-Runs getrennt. Löschende Läufe zeigen weiterhin die vorhandenen Schutzbedingungen und werden serverseitig validiert; die GUI ist keine Umgehung der Sicherheitslogik.

Tastaturkürzel: `Strg/Cmd + S` speichert offene Konfigurations- bzw. Filteränderungen. `/` fokussiert auf der Pair- oder Jobseite direkt die Suche.

### Betriebserweiterungen

Mehrere der folgenden Funktionen brauchen Rechte, die die Standard-Units nicht vergeben; die nötigen Drop-ins stehen unter [Privilegierte Erweiterungen freischalten](#privilegierte-erweiterungen-freischalten).

- Der Notfall-Lockdown (Panik-Knopf) beendet laufende Läufe, pausiert den Scheduler für 31 Tage, versucht die Mountpoints aktiver Datenwege mit `require_mountpoint` schreibgeschützt neu einzuhängen und meldet alle Sitzungen ab. Auslösen und Aufheben verlangen das aktuelle Passwort. Beim Aufheben nennt die Antwort die nötigen `mount -o remount,rw`-Befehle und führt sie absichtlich nicht selbst aus.
- Die PBS-Ansicht zeigt Belegung und Dedup-Faktor des Datastores. Die Wartungsdrossel stellt fällige rclone-Läufe des Schedulers zurück, solange auf dem PBS Garbage Collection oder Verify läuft; manuelle Läufe bleiben unberührt. Ist die PBS-API nicht erreichbar, gilt die Drossel als inaktiv (fail-open), damit Backups nicht dauerhaft warten.
- Der QEMU-Boot-Drill holt das jüngste VM-Disk-Image einer PBS-Gruppe als temporäre Raw-Datei zurück (kein ZFS-Klon), startet es headless und gilt als bestanden, sobald der QEMU Guest Agent auf einen Ping antwortet. Der Arbeitsbereich wird in jedem Fall gelöscht, die Konsolenausgabe steht in der Job-Zusammenfassung. Abbruch und Lockdown beenden auch das Zurückholen des Images. Arbeitsbereiche, die ein hart beendeter Lauf hinterlassen hat, löscht der nächste Lauf zu Beginn (`stale_cleaned` in der Zusammenfassung).
- Der Cloud-Bitrot-Scrub zieht eine Stichprobe aus den Cloud-Dateien, die noch keine Baseline haben oder seit `rotation_days` ungeprüft sind, die ältesten zuerst. Er bildet ihre SHA-256, vergleicht sie mit einer lokalen Baseline-Tabelle und alarmiert, wenn der Hash einer Datei mit unveränderter Größe und Änderungszeit abweicht. Die Limits in `backup.bitrot_scrub` halten den Egress klein.
- Die GFS-Ausdünnung teilt die Versionsordner standardmäßig in 7 Tages-, 4 Wochen- und 12 Monatsstufen. Ein Ordner unter dem `backup_dir` enthält nur die Dateien, die ein Lauf verdrängt hat. Deshalb führt sie die jüngeren Ordner einer Stufe in deren ältesten Ordner zusammen (`rclone move --ignore-existing`; je Pfad bleibt die älteste Vorversion, übersprungene jüngere Duplikate werden danach gelöscht) und löscht nur Ordner, die in keine Stufe mehr fallen. Sie betrachtet nur Ordner mit Zeitstempelnamen, zeigt vorab geplante Zusammenführungen und Löschungen und dünnt von Hand nur mit Passwort aus. Ist `backup.versions_lifecycle.enabled` gesetzt, läuft sie zusätzlich nach jedem erfolgreichen produktiven Lauf ohne weitere Bestätigung; alle Versionen eines Ordners löscht sie nie, und bei aktivem Lockdown bricht sie ab. Ein globales absolutes `backup_dir`, `backup_dir1` oder `backup_dir2` (etwa `b2:versions/{date}`) ohne Pair-eigenen Wert ist für alle Datenwege dieselbe Wurzel; sie wird nicht ausgedünnt, die Vorschau meldet das als Warnung. Von Hand dünnt sie nur aus, solange kein Backup läuft, und hält dabei den Backup-Lock. Wer sie einschaltet, `keep_*`-Werte senkt oder ein `backup_dir` ändert, muss beim Speichern das aktuelle Passwort eingeben.
- Bei Airgap-USB bindet ein Datenweg mit `airgap_device` das Gerät per `udisksctl mount` nur für die Dauer des Jobs ein. Danach folgen `sync`, Unmount und Power-off. Der lokale Pfad des Datenwegs muss auf dem Gerät liegen.
- Mit `zfs_snapshot: true` (nur bei `direction: push`) liest rclone aus `<Mountpoint>/.zfs/snapshot/<Name>/…` statt aus dem laufenden Dateisystem. Der Snapshot wird nach dem Lauf wieder zerstört. Eigene Snapshots (`sicherpfad-…`) desselben Datasets, die älter als 24 Stunden sind, stammen aus abgebrochenen Läufen; sie werden vor dem neuen Snapshot zerstört.
- Das S3-Object-Lock-Siegel liest die S3-Zugangsdaten aus der rclone-Konfiguration und fragt je Bucket `?object-lock` und `?versioning` per SigV4 ab. Es stuft jeden Bucket als `immutable`, `versioned`, `unprotected` oder `unknown` ein; aktives Object Lock ohne Standardregel heißt `lock_no_default`. `crypt`- oder `alias`-Remotes über S3 löst es nicht auf.
- Bisync benennt Konfliktkopien nach ihrer Herkunft (`datei.conflict-path1-1` aus der Cloud, `datei.conflict-path2-1` von lokal). Die Konfliktauflösung behält je Datei die Fassung von Seite 1 (Cloud, path1), Seite 2 (lokal) oder beide. Ist die Herkunft nicht eindeutig, etwa bei mehreren Kopien einer Seite oder alten `conflict3`-Kopien, geht nur „Beide behalten“. Wer dabei Kopien löscht, bestätigt mit dem Passwort.
- Nach einem Dry-Run listet die Änderungsmatrix die geplanten Änderungen, und einzelne Pfade lassen sich vor dem produktiven Lauf abwählen. Bei bisync geht das nicht, weil eine geänderte Filtermenge einen Resync erzwingen würde.
- Der Restore-Diff vergleicht eine Datei im Staging zeilenweise mit der Live-Datei des Datenwegs. Er akzeptiert UTF-8-Textdateien bis 1 MiB und vergleicht höchstens die ersten 5000 Zeilen.
- Der VPN-Sensor zeigt den WireGuard-Status (`wg show` braucht `CAP_NET_ADMIN`), den Tailscale-Status und TCP-Probes zu den Zielen aus `backup.vpn_probes`.
- Die Bandbreiten-Heatmap wertet die Laufhistorie nach Wochentag und Stunde aus und schlägt für Zeitpläne mit fester Stunde ein besseres Fenster vor. Ein Vorschlag verschiebt nur die Stunde, nie die Häufigkeit, und wirkt erst, wenn der Zeitplan gespeichert wird.
- Der Crypt-Assistent legt ein rclone-`crypt`-Remote über einem vorhandenen Basis-Remote an und zeigt die Schlüssel genau einmal an. Sie fehlen im Notfallpaket und gehören in einen Passwortmanager. „Zero-Knowledge“ gilt nur gegenüber dem Cloud-Anbieter: `rclone.conf` enthält die Schlüssel obscured, also umkehrbar verschleiert und nicht verschlüsselt.
- Datenwege tragen bis zu 10 `tags`; die Oberfläche filtert danach und startet gleich markierte Datenwege gesammelt.

Nicht umgesetzt ist eine Content-Security-Policy ohne `'unsafe-eval'`, weil die Oberfläche Alpine-Ausdrücke direkt im HTML nutzt und die CSP-Variante von Alpine sie nicht unterstützt. Die iOS-App zeigt die neuen Funktionen noch nicht.

### Dateisuche, Ausfallwächter und Betriebswerkzeuge

Unter **System → Betriebswerkzeuge** stehen Dateisuche, Speicherprognose,
Ausfallwächter, Zielwechsel und wöchentliche Schutzberichte bereit. Dieselben
Werkzeuge sind nativ in der iPhone-App unter **System → Betriebswerkzeuge**
erreichbar.

- **Dateisuche:** Dateinamen über aktuelle Kopien, Änderungsarchive und vollständige
  Sicherungsstände suchen. Jede Anfrage ist begrenzt; „Weitere Stände durchsuchen“
  setzt die Suche fort. Angezeigte Warnungen und Umfangsgrenzen gehören zum
  Ergebnis. Ein vorhandener Manifest-Hash belegt gespeicherte Metadaten, keine
  erneute Prüfung der aktuellen Zielbytes. Änderungsarchive sind unvollständige
  Stände. Gefundene Dateien werden über den bestehenden geprüften Staging-Restore
  zurückgeholt; die Restore-Grenze bleibt ausdrücklich wählbar.
- **Speicherprognose:** Tägliche, dauerhaft gespeicherte Messungen der lokalen
  Volumes und verfügbaren Cloud-Kontingente. Mindestens drei Tageswerte über zwei
  Tage sind für einen Trend erforderlich. Unbekannte Quoten, schwankende Trends
  und veraltete Messungen werden nicht als freie Kapazität ausgegeben. Eine
  eigene Budgetvorgabe ist für den konfigurierten Cloud-Zielordner möglich; in
  diesem Fall wird dessen Bestand separat gemessen. Ohne Budget beziehen sich
  Cloud-Kontingente auf das Konto beziehungsweise Volume, nicht ausschließlich
  auf den ausgewählten Unterordner.
- **Ausfallwächter:** Bei einem unabhängigen Heartbeat-Empfänger eine geheime
  HTTPS-Ping-Adresse erzeugen und hier speichern. Intervall und mindestens drei
  Intervalle Toleranz sowie die tatsächlichen Alarmempfänger werden beim
  Empfänger eingerichtet. Ein eigener Minutentakt sendet die Lebenszeichen auch
  während langer Sicherungen. Der Empfänger benötigt ein unabhängiges System,
  um einen vollständigen Ausfall dieses Servers melden zu können. Auslösen und
  Konfiguration verlangen Passwortbestätigung; URL und SMTP-Passwort werden nie
  in Statusantworten oder Support-Exporten ausgegeben. Lockdown hält ausgehende
  Integrationen an; bei geplanter Wartung den externen Wächter passend pausieren.
- **Zielwechsel:** Einen Push-Datenweg auf ein leeres lokales oder Cloud-Ziel
  vorbereiten, Kopie vollständig zurücklesen und erst danach bewusst umschalten.
  Auch vor der Umschaltung werden die Inhalte erneut geprüft. Beide langen
  Schritte laufen als abbrechbare Jobs im bestehenden Backup-Scope. Konfiguration,
  Sitzungsgeneration und Ziele bleiben an die geprüfte Vorschau gebunden. Die
  ursprüngliche Kopie bleibt erhalten. Bisync-/Pull-/Airgap-Datenwege, gebundene
  Vault-Belege und externe Versionsarchive benötigen eine getrennte Übernahme
  und werden in dieser Fassung ausdrücklich abgewiesen. Vollständige lokale
  Stände bleiben über einen gesondert signierten Migrationsnachweis zugänglich;
  ihre unveränderten Manifeste werden als historische Bindung ausgewiesen und
  zählen nicht als aktueller Restore-Nachweis des neuen Ziels. Nachweis und
  privater Signierschlüssel gehören gemeinsam in die Sicherung des Datenverzeichnisses. Der bestätigte Umfang
  ist begrenzt (standardmäßig 5 GiB, höchstens 50 GiB).
- **Schutzberichte:** Die letzte abgeschlossene Kalenderwoche in der gewählten
  Zeitzone als Zusammenfassung und PDF erstellen. Der Bericht enthält aggregierte
  Laufzahlen, Schutz-/Restore-Nachweise und Speicherreserven, keine Dateinamen,
  Pfade oder rohen Logtexte. Standardmäßig sind automatische Erstellung und
  E-Mail-Versand aus. SMTP verwendet geprüftes TLS beziehungsweise STARTTLS;
  manueller Versand verlangt eine separate Passwortbestätigung. Zustellfehler
  bleiben sichtbar und automatische Wiederholungen sind begrenzt.

Der Installer richtet `sicherpfad-operations.timer` und
`sicherpfad-operations.service` zusätzlich zum Backup-Scheduler ein. Der separate
Minutentakt führt `python -m app.operations_cli` aus und ist von pausierten oder
lang laufenden Backup-Jobs unabhängig. Die Einstellungen liegen unter
`notifications.heartbeat`, `backup.storage_forecast` und `reports.weekly`;
[config/config.example.yaml](config/config.example.yaml) enthält deaktivierte
Beispiele für ausgehende Integrationen. Der Quelltext installiert keinen externen
Heartbeat-Empfänger und richtet kein fremdes E-Mail-Konto ein.

## Native iPhone-App

Unter [`ios/`](ios/) liegt eine dependency-freie SwiftUI-App ab iOS 17. Sie bietet das verdichtete Lagebild, lokale/cloudseitige Kopien mit Dateizahl und Größe, Jobs, Live-Fortschritt samt Watchdog-Status und Abbruch, Laufhistorie mit Logs und sicherem Retry, getrennte Datenwege, Push-Diagnose, Scheduler-Wartungsfenster, Systemdiagnose und PBS-Starts. Das native Recovery Center ergänzt Offline-Notfallkarte, selektive Staging-Restores, verschlüsselte Übergabe, Schutzkalender und Serverprofile ohne Passwortspeicherung. Widget, Live Activity, Siri-Kurzbefehle und interaktive Fehler-Pushs bilden eigene iOS-Systemoberflächen. Seltene oder riskante Aktionen öffnen in nativen Detailansichten und Bestätigungsdialogen.

Das Xcode-Projekt wird mit XcodeGen erzeugt. Ohne eigenen Mac übernimmt Codemagic Build, Signierung, native App-Store-Screenshots und den Upload zu App Store Connect; GitHub Actions kompiliert und testet jede iOS-Änderung mit Xcode 26. Einrichtung, Sicherheitsmodell und den nativen Funktionsumfang beschreibt [`ios/README.md`](ios/README.md). [Support](https://oliverzimmermann1986-debug.github.io/Rclone/) und [Datenschutz](https://oliverzimmermann1986-debug.github.io/Rclone/datenschutz.html) werden ohne Tracking über GitHub Pages bereitgestellt.

## Restore-Stichprobe: 19/20 oder 20/20

„19 von 20 erfolgreich geprüft“ bedeutet: Die 19 zurückgeholten Dateien wurden
erfolgreich verglichen, der angeforderte Prüfumfang wurde jedoch nicht erreicht.
Ein solcher begrenzter Prüfumfang ist ein Hinweis, kein Prüfsummenfehler und kein
vollständig erfüllter Stichproben-Nachweis. Übertragungs-, Prüfsummen- und
Bereinigungsfehler bleiben Fehler.

Die Auswahl versucht, die gewünschte Dateizahl innerhalb des vorhandenen
Datenlimits zu erreichen. Dazu kann sie größere Kandidaten durch kleinere
ersetzen. Das gilt nur für den begrenzten, zufällig gebildeten Kandidatenpool;
20/20 ist dadurch nicht für jeden Datenbestand garantiert.

Falls weiterhin das Datenlimit die Auswahl begrenzt, kann der Administrator in
der bestehenden Serverkonfiguration unter `backup.restore_test` das Limit
bewusst erhöhen. Beispiel für 20 Dateien und höchstens 512 MiB pro Datenweg:

```yaml
backup:
  restore_test:
    sample_files: 20
    max_total_mb: 512
```

Nur diese Werte in die vorhandene Konfiguration übernehmen; andere Einstellungen
wie Zeitplan, Aktivierung und Datenwege beibehalten. Trotz des Namens
`max_total_mb` verwendet das Limit MiB (1.048.576 Bytes). Standard sind 256 MiB.
Ein höheres Limit benötigt mehr temporären Speicher und kann mehr Cloud-Egress
verursachen. Mindestens 20 geeignete Dateien müssen zusammen in das Limit passen
und erfolgreich zurückgeholt und geprüft werden. Ein Scanlimit oder weniger
geeignete Dateien kann die Auswahl unabhängig davon begrenzen. Ein Hinweis
löst keinen stündlichen Fehler-Retry aus; der nächste geplante Termin bleibt
bestehen. `restore_test_warning` ist ein eigener, ausdrücklich zu abonnierender
Benachrichtigungstyp.

Auch 20/20 bestätigt ausschließlich diese Stichprobe, nicht sämtliche Dateien
der Sicherung.

## Aktuelle Restore-Nachweise

Ein vollständiger erfolgreicher Restore-Test gilt sieben Tage und ist an den
Datenweg sowie Quelle und Ziel gebunden. Eine Umbenennung erhält den Nachweis;
ein geändertes Ziel, eine fehlgeschlagene neue Prüfung oder eine unvollständige
Stichprobe ergibt keinen aktuellen vollständigen Nachweis. Nach Ablauf muss eine
neue Stichprobe erfolgreich geprüft werden. Ein normaler Sicherungs-Probelauf
(Dry-Run) erneuert den Restore-Nachweis nicht.

Das Incident Center unterscheidet fehlende, abgelaufene und nicht mehr passende
Nachweise und bietet den Restore-Test für den betroffenen Datenweg direkt an.
Der Test holt Dateien ausschließlich in ein temporäres Prüfverzeichnis zurück;
Originaldateien werden nicht überschrieben.

Die Schutzvorlagen empfehlen wöchentliche Restore-Prüfungen. Sie ändern einen
bereits konfigurierten Restore-Zeitplan nicht automatisch. Ein monatlicher Plan
lässt deshalb zwischen Prüfungen eine Nachweislücke. Für lückenlos aktuelle
Nachweise sollten Prüfungen mit Reserve vor Ablauf stattfinden, etwa zweimal
wöchentlich: Auch Laufzeit und die Zeitumstellung können einen exakt
wöchentlichen Termin über die siebentägige Frist verschieben.

Unter **Einstellungen → Scheduler → Restore-Prüfplan** zeigt die Weboberfläche
die nächsten Termine in der Server-Zeitzone, den Ablauf vorhandener Nachweise
und mögliche Prüflücken. In der iPhone-App liegt derselbe Plan unter
**Wiederherstellen → Restore-Prüfplan**. Täglich, wöchentlich, Sonntag/Mittwoch
oder ein eigener Cron-Ausdruck sind möglich. Eine Vorschau speichert nichts;
erst die bestätigte Übernahme ändert den Restore-Plan für alle aktiven
Datenwege. Parallele Konfigurationsänderungen erfordern ein erneutes Laden.
Bestehende Sicherungsjobs und Zeitpläne werden dabei nicht umgestellt.

## Proxmox-Betrieb

Empfohlen ist ein eigener, möglichst unprivilegierter Debian-/Ubuntu-LXC oder eine kleine VM. Die Anwendung benötigt keine Docker- oder Nesting-Funktion. Für typische Installationen genügen 1–2 vCPU und 512 MiB bis 1 GiB RAM; große Remotes, viele parallele Transfers oder `--fast-list` benötigen entsprechend mehr Speicher.

Bei LXC-Bind-Mounts müssen drei Ebenen zusammenpassen:

1. Der Pfad ist im Proxmox-Gast tatsächlich eingehängt.
2. Der Dienstbenutzer `rclone-sync` besitzt Lese-/Schreibrechte auf dem Pfad.
3. Der Pfad ist sowohl in `web.local_browse_roots` als auch in den systemd-`ReadWritePaths` freigegeben.

Für jeden NAS-/Bind-Mount empfiehlt sich zusätzlich `require_mountpoint: true` und eine Sentinel-Datei. So wird ein leerer lokaler Ordner nach einem fehlgeschlagenen Mount nicht versehentlich als gültiges Ziel behandelt.

Die Systemkarte zeigt Werte **innerhalb des Gasts**. Bei einem LXC werden vorhandene cgroup-v2-Limits für CPU und RAM bevorzugt; andernfalls gelten die vom Kernel sichtbaren Werte; die vollständige Proxmox-Hostauslastung wird bewusst nicht abgefragt und benötigt keine PVE-API-Zugangsdaten.

### Privilegierte Erweiterungen freischalten

Die mitgelieferten Units `systemd/rclone-sync-web.service` und `systemd/sync-scheduler.service` laufen mit leerem `CapabilityBoundingSet=`, `NoNewPrivileges=true`, `PrivateDevices=true` und `MemoryDenyWriteExecute=true`. Darunter scheitern fünf Erweiterungen. Alle melden den Fehler, statt ihn zu verschweigen:

- Lockdown-Remount: Ohne `CAP_SYS_ADMIN` schlägt `mount -o remount,ro` fehl, und der Lockdown-Bericht führt den Mountpoint als fehlgeschlagen.
- ZFS-Snapshot-Quelle: Ohne `/dev/zfs` kann `zfs` keinen Snapshot anlegen, der Lauf des Datenwegs scheitert.
- Airgap-USB: Ohne `/dev/disk` und Polkit-Freigabe kann `udisksctl` das Gerät nicht einbinden.
- Boot-Drill: Ohne `/dev/kvm` und mit `MemoryDenyWriteExecute=true` startet QEMU nicht, weil der JIT von TCG blockiert wird.
- VPN-Sensor: `wg show` braucht `CAP_NET_ADMIN`; der Sensor meldet WireGuard als nicht verfügbar.

Ein Drop-in gehört an die Unit, die den Lauf ausführt: `rclone-sync-web.service` für die Weboberfläche und manuell gestartete Läufe, `sync-scheduler.service` für geplante Läufe, `rclone-sync.service` für den manuellen Voll-Run. Wer mehrere Wege nutzt, legt es an jeder dieser Units an.

```bash
sudo systemctl edit rclone-sync-web.service
sudo systemctl edit sync-scheduler.service
```

`systemctl edit` schreibt nach `/etc/systemd/system/<Unit>.d/override.conf`. Der Installer ersetzt nur die Unit-Dateien selbst, die Drop-ins überstehen daher ein Upgrade.

Für die ZFS-Snapshot-Quelle `/dev/zfs` freigeben und dem Dienstkonto die Rechte auf das Dataset delegieren:

```ini
[Service]
PrivateDevices=false
DeviceAllow=/dev/zfs rw
```

```bash
sudo zfs allow rclone-sync snapshot,destroy,mount <dataset>
```

Für Airgap-USB müssen die Gerätedateien sichtbar sein, und Polkit muss `udisksctl` für das Dienstkonto zulassen. Das Einhängeverzeichnis von udisks2 braucht Schreibrechte; es muss beim Dienststart existieren. Eine engere Variante wäre `BindReadOnlyPaths=/dev/disk` (ungeprüft).

```ini
[Service]
PrivateDevices=false
ReadWritePaths=/run/media/rclone-sync
```

Die Polkit-Regel (`/etc/polkit-1/rules.d/50-rclone-sync-udisks.rules`):

```javascript
polkit.addRule(function (action, subject) {
  var allowed = [
    "org.freedesktop.udisks2.filesystem-mount",
    "org.freedesktop.udisks2.filesystem-mount-other-seat",
    "org.freedesktop.udisks2.filesystem-unmount-others",
    "org.freedesktop.udisks2.power-off-drive",
    "org.freedesktop.udisks2.power-off-drive-other-seat"
  ];
  if (subject.user === "rclone-sync" && allowed.indexOf(action.id) !== -1) {
    return polkit.Result.YES;
  }
});
```

Für den Boot-Drill:

```ini
[Service]
MemoryDenyWriteExecute=false
PrivateDevices=false
DeviceAllow=/dev/kvm rw
SupplementaryGroups=kvm
```

Auf dem Host müssen `qemu-system-x86` und `proxmox-backup-client` installiert sein, im Gast der `qemu-guest-agent`. Ohne dessen Antwort besteht der Drill nicht. QEMU läuft mit `-nodefaults` und ohne Netzwerkkarte, der zurückgeholte Gast kommt also nicht ins Netz. Das Image liegt unter `pbs.boot_drill.scratch_dir` (Standard `paths.temp_dir`) und braucht Platz für seine Größe plus 512 MiB; ein eigener Pfad muss in `ReadWritePaths` stehen. In einem LXC muss `/dev/kvm` zuerst vom Proxmox-Host durchgereicht werden. Ohne `/dev/kvm` bootet QEMU per TCG und deutlich langsamer; dann `pbs.boot_drill.timeout_seconds` erhöhen (Standard 60, Maximum 900).

Für die PBS-Ansicht und die Wartungsdrossel ist keine Unit-Änderung nötig, aber `pbs.enabled: true` und ein API-Token mit `Datastore.Audit` auf `/datastore/<store>` sowie `Sys.Audit` auf `/system/tasks`. Bei einem selbstsignierten Zertifikat ist `pbs.fingerprint` Pflicht, sonst lehnt die Anwendung die Verbindung ab.

Der VPN-Sensor läuft in der Web-Unit. Er braucht neben der Capability den Adresstyp `AF_NETLINK`, den die Standard-Unit nicht erlaubt:

```ini
[Service]
CapabilityBoundingSet=CAP_NET_ADMIN
AmbientCapabilities=CAP_NET_ADMIN
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
```

In einem LXC sieht `wg` nur Schnittstellen im Netzwerk-Namespace des Containers.

Für den Lockdown-Remount gibt es kein Beispiel. `CAP_SYS_ADMIN` kommt Root-Rechten sehr nahe, und ob `mount` mit einer ambienten Capability für ein Nicht-Root-Konto arbeitet, ist nicht geprüft. Ein fehlgeschlagener Remount hält die übrigen Lockdown-Schritte nicht auf. Den Schreibschutz der Mounts setzt der Betreiber dann selbst, etwa auf dem Proxmox-Host.

Rechte gezielt per Drop-in an der Unit lockern, die sie braucht, und nicht in den mitgelieferten Dateien. Die Beispiele sind nicht auf einem Zielsystem getestet; Gerätepfade, Polkit-Aktionen und Gruppennamen vor dem Einsatz prüfen.

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
- optionale WebAuthn-Anmeldung mit getrennt registrierten Passkeys und physischen FIDO2-Sicherheitsschlüsseln; Challenges und native Übergabecodes sind kurzlebig und nur einmal verwendbar
- neue Passkeys benötigen auch in der nativen App das aktuelle Serverpasswort; offene WebAuthn-Freigaben sind an die aktuelle Kontogeneration gebunden und werden bei deren Widerruf ungültig
- das Entfernen eines Passkeys sperrt auch dessen noch laufende Anmeldungen und offene native Anmeldecodes; bereits ausgestellte Sitzungen bleiben bis zu ihrem eigenen Widerruf gültig
- Session-Versionierung; Passwort-, Benutzer- oder Secret-Wechsel invalidiert bestehende Sessions
- laufende Fortschrittsstreams prüfen den Sitzungswiderruf und Ablauf vor weiteren Daten; Konfigurations-Rollbacks erzeugen ein neues Session-Secret
- Login-CSRF und Double-Submit-CSRF für alle schreibenden APIs
- Same-Origin-Prüfung, Host-Allowlist und begrenzte reale Request-Größe, auch bei Chunked Requests
- Sicherheitsheader, `no-store` für sensible Antworten und restriktive Cookies
- Konfiguration und Filterdatei atomar, prozessgesperrt und mit Modus `0600`
- Optimistische Revisionen verhindern stilles Überschreiben paralleler Änderungen
- Änderungen an sämtlichen wirksamen Parametern des globalen oder datenwegspezifischen Anomalieschutzes benötigen das aktuelle Passwort
- aktive Quarantänen binden die gespeicherten Endpunkte und gelten auch bei abgeschaltetem Anomalieschutz sowie für überlappende produktive Quick-Sync-Läufe; sie bleiben nach Neuanlage oder Entfernung eines Datenwegs bis zur ausdrücklichen Freigabe bestehen
- aktive Quarantänen werden bei Historienlimits nicht verworfen; das Entfernen quarantänisierter Datenwege oder Ändern ihrer Endpunkte benötigt das aktuelle Passwort
- `config.yaml.bak` enthält genau eine geschützte Vorversion
- vollständige GUI-Snapshots verbleiben mit Modus `0600` im lokalen Datenverzeichnis; Wiederherstellungen prüfen Name, Größe, SHA-256, aktuelle Revision und Passwort
- das herunterladbare Support-Bundle enthält nur eine redigierte Konfiguration, System-/DB-Diagnose und Log-Inventar, niemals Log-Inhalte, Passwörter, Session-Secrets, APNs-Schlüssel oder Gerätetokens
- APNs-Schlüsseldateien müssen innerhalb des geschützten Datenverzeichnisses liegen; Gerätetokens laufen per Lease aus und endgültig abgewiesene Tokens werden entfernt
- Push-Ereignisse werden vor dem Netzversuch persistent gespeichert, dedupliziert und mit begrenztem Backoff erneut zugestellt
- native Push-Aktionen sind an Server und aktuelle App-Sitzung gebunden; Mitteilungen älterer Server ohne diese Bindung bleiben sichtbar, lösen aber keine Aktion aus
- rclone-Unterprozesse erhalten `RCLONE_ASK_PASSWORD=false` und können keine Dienste blockierend nach einem Passwort fragen
- systemd-Sandbox mit leerem Capability-Set, `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectProc=invisible`, deaktiviertem Uvicorn-Serverheader und restriktiven Schreibpfaden
- der Lockdown-Aufruf dauert je nach Zahl der Prozesse und Mountpoints einige Sekunden bis etwa 30 Sekunden, weil Abbrüche zuerst geordnet erfolgen und erst danach SIGKILL folgt
- der Lockdown-Zustand wird zuerst gesetzt; Job-Starts, Scheduler-Steuerung, Konfliktauflösung und Versions-Ausdünnung antworten daher schon während des Aufrufs mit HTTP 423, bis der Lockdown aufgehoben ist; der Server sendet das Push-Ereignis `lockdown`
- Crypt-Schlüssel erscheinen nur einmal im Assistenten und fehlen im Notfallpaket; ohne sie sind die verschlüsselten Cloud-Daten verloren, deshalb gehören sie in einen Passwortmanager
- die S3-Abfragen des Object-Lock-Siegels nutzen die Schlüssel aus `rclone.conf` nur serverseitig; das Siegel ist eine Momentaufnahme und wird eine Stunde zwischengespeichert (`?refresh=true` erzwingt eine neue Abfrage)

Neue Push-Nutzlasten enthalten generische Titel und Texte sowie technische IDs; Job-/Datenwegnamen und rohe Fehlertexte bleiben auf dem Server. Token, Text und technische Kennungen werden über Apples APNs übertragen; Verarbeitung in den USA ist möglich. Der Serverbetreiber prüft Rechtsgrundlage und Drittlandgarantien vor Aktivierung. Geräteregistrierungen: standardmäßig 7 Tage Lease, höchstens 90; erledigte Outbox: 30 Tage. Das Systemjournal hat eine getrennte Betreiberfrist.
Zugangsdaten werden in Nutzlasten, Logs und Support-Bundles maskiert.

Die mitgelieferte Unit startet absichtlich genau **einen Uvicorn-Worker**. Die zentralen Daten, Locks und Login-Sperren sind prozessübergreifend, aber ein einzelner Worker hält Jobsteuerung und Web-Lebenszyklus eindeutig und vereinfacht den Betrieb.

## Installation

In einem frischen Debian-/Ubuntu-LXC als root:

```bash
source_commit=$(git ls-remote \
  https://github.com/oliverzimmermann1986-debug/Rclone.git refs/heads/main | cut -f1)
install_script=$(mktemp)
curl -fsSL "https://raw.githubusercontent.com/oliverzimmermann1986-debug/Rclone/$source_commit/scripts/install.sh" \
  -o "$install_script"
less "$install_script"
sudo SOURCE_COMMIT="$source_commit" bash "$install_script"
rm -f "$install_script"
```

Damit sind sowohl der geprüfte Installer als auch der später ausgecheckte
Quellstand an dieselbe unveränderliche Commit-ID gebunden.
Alternativ aus einem lokalen Checkout:

```bash
git clone https://github.com/oliverzimmermann1986-debug/Rclone.git /opt/rclone-sync
cd /opt/rclone-sync
source_commit=$(git rev-parse HEAD)
sudo SOURCE_COMMIT="$source_commit" bash scripts/install.sh
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

Danach können neue Remotes in der Webversion und iPhone-App unter **System →
Neues Remote einbinden** sowie direkt in der Cloud-Ordnerauswahl eingerichtet
werden. pCloud, Google Drive, WebDAV und OneDrive stehen oben in der
Anbieterauswahl; weitere Anbieter stammen aus der installierten rclone-Version.
Der Assistent fragt die erforderlichen Angaben schrittweise ab. Cloud-Anmeldungen
verwenden die von rclone angezeigte Autorisierung auf einem Rechner mit Browser
und installiertem rclone; das Ergebnis wird anschließend im Assistenten
eingefügt. Je nach Anbieter sind eigene OAuth-Client-Zugangsdaten erforderlich.
Eine reine OAuth-Anmeldung im iPhone-Browser ist damit noch nicht umgesetzt.
Die Anbieterfragen können auf Englisch erscheinen.

Erst **Remote speichern** übernimmt die neue Verbindung in die rclone-Konfiguration
des Dienstbenutzers. Vorhandene Namen werden nicht überschrieben. Abgebrochene
Entwürfe werden entfernt, offene Einrichtungen laufen nach 30 Minuten ab und
überstehen keinen Serverneustart. Nach dem Speichern lässt sich die Verbindung
über die Ordnerauswahl prüfen; ein Datenweg oder Job wird nicht automatisch
angelegt. Die Datei muss für das Dienstkonto schreibbar sein. Eine verschlüsselte
rclone-Konfiguration benötigt weiterhin die serverseitige Entsperrung.
Der Einrichtungszustand liegt im einzelnen Web-Worker der Standardinstallation.

Alternativ rclone als Dienstbenutzer konfigurieren:

```bash
sudo -u rclone-sync -H rclone config
```

Die Web-UI lauscht standardmäßig ausschließlich lokal auf
`http://127.0.0.1:8001`. Für Fernzugriff einen HTTPS-Reverse-Proxy oder
einen SSH-Tunnel verwenden. Ein bewusstes LAN-Binding muss als
systemd-Drop-in konfiguriert und mit TLS, engen `allowed_hosts` und sicheren
Cookies abgesichert werden.

Das Initialpasswort steht einmalig in
`/opt/rclone-sync/data/.initial-password`. Die Datei wird nach dem ersten
erfolgreichen Login oder beim Ändern des Passworts automatisch entfernt.

## Upgrade

Voraussetzung ist ein vertrauenswürdiger Quellbaum im Eigentum von root,
einschließlich `.git`, der für den Dienstbenutzer nicht beschreibbar ist.
Altinstallationen mit einem durch den Dienstbenutzer beschreibbaren Checkout
oder `.git` weist der Installer vor Git-Befehlen als root, Paketinstallation
und Dienststeuerung zurück. Vor dem folgenden Aufruf solche Installationen
außerhalb des Installers sichern und den Quellbaum aus einer unabhängig
geprüften Quelle unter root neu bereitstellen. Ein bloßer Eigentümerwechsel
belegt nicht die Vertrauenswürdigkeit bereits veränderbarer Quellen.

Normaler Upgrade-Aufruf:

```bash
git -C /opt/rclone-sync fetch origin main
source_commit=$(git -C /opt/rclone-sync rev-parse origin/main)
sudo SOURCE_COMMIT="$source_commit" bash /opt/rclone-sync/scripts/install.sh
```

Der Installer bricht bei lokalen Änderungen an getrackten Repository-Dateien
immer ab. Diese vorher committen oder außerhalb von `/opt/rclone-sync` sichern.
Auch ein bereits vorhandenes Verzeichnis `/opt/rclone-sync`, das kein
Git-Checkout ist, wird niemals automatisch überschrieben oder gelöscht.

Laufzeit-Sicherungen landen standardmäßig unter
`/var/backups/rclone-sync/<Zeitstempel>`. Es werden zehn vom Installer erzeugte
und markierte Sicherungen behalten; fremde Verzeichnisse im Sicherungswurzelpfad
werden nicht gelöscht. `BACKUP_ROOT` darf nicht innerhalb von
`/opt/rclone-sync` liegen. Anpassung:

```bash
sudo BACKUP_KEEP=20 BACKUP_ROOT=/srv/backups/rclone-sync \
  SOURCE_COMMIT="$source_commit" \
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
- `backup.timeout_hours`: Stillstands-Timeout; nur echter Transfer-/Check-/Dateifortschritt verlängert die Frist
- `backup.max_runtime_hours`: optionale absolute Laufzeitgrenze (`0` = deaktiviert); systemd setzt kein zusätzliches Hardlimit
- `backup.scheduler_retry_minutes`: Backoff nach fehlgeschlagenen geplanten Läufen
- `backup.scheduler_grace_minutes`: zulässiges Startfenster rund um den Cron-Zeitpunkt
- `backup.run_on_first_tick`: noch nie gelaufene Pairs nicht sofort beim ersten Timer-Tick starten
- `backup.collect_pre_post_stats`: zusätzliche Volltraversierungen; standardmäßig `false`
- `backup.require_delete_confirmation`: Freigabe für produktive `sync`-/`bisync`-Läufe erzwingen
- `backup.require_max_delete_for_sync`: endliches Löschlimit erzwingen
- `backup.allow_unsafe_rclone_args`: Schutz interner rclone-Flags nur bewusst deaktivieren
- `backup.auto_resync`: standardmäßig `false`; `bisync --resync` erst nach Log-/Planprüfung
- `web.allowed_hosts`: in Produktion konkrete DNS-Namen/IPs statt `*`
- `web.secure_cookie`: standardmäßig `auto` (Secure-Cookies bei HTTPS, direktes lokales HTTP bleibt nutzbar); hinter festem HTTPS `true` verwenden. Bei TLS-Proxys `web.trusted_proxy_ips` auf die tatsächlichen Proxy-Hops begrenzen; `X-Forwarded-Proto` wird ausschließlich von diesen Peers übernommen.
- `web.hsts_seconds`: erst bei dauerhaftem HTTPS aktivieren
- `web.local_browse_roots`: erlaubte lokale Browser-/Quick-Sync-Wurzeln
- `notifications.apns`: native Fehler-Pushs mit dauerhafter Outbox, Retry und Geräte-Lease; neue Events `bitrot_detected`, `boot_drill_ok`, `boot_drill_error`, `lockdown`
- `backup.versions_lifecycle`: gestaffelte Aufbewahrung von Vorversionen (`enabled: false`, `keep_daily` 7, `keep_weekly` 4, `keep_monthly` 12; pro Pair überschreibbar)
- `backup.bitrot_scrub`: Prüfsummen-Stichprobe gegen stille Datenfäulnis; standardmäßig aus, Lauf pro `schedule` (Standard `0 4 * * *`), begrenzt durch `sample_percent`, `max_files`, `max_total_mb`, `max_scan_files`, `rotation_days`
- `backup.vpn_probes`: bis zu 10 Erreichbarkeitstests im Format `host:port` bzw. `[IPv6]:port`
- `pbs.gc_throttle`: Drosselung gegenüber der PBS-Garbage-Collection (`enabled: true`, `poll_seconds` 60, Bereich 15–900)
- `pbs.boot_drill`: Boot-Test eines PBS-Wiederherstellungspunkts, standardmäßig aus; bei `enabled` ist `group` (`vm/<id>`, `ct/<id>` oder `host/<id>`) Pflicht, bootbar sind aber nur VM-Disk-Images; weitere Felder `schedule`, `archive`, `memory_mb`, `timeout_seconds`, `scratch_dir`
- `maintenance.job_retention_days`, `keep_latest_jobs` und `log_retention_days`: Aufbewahrung

Wichtige Pair-Werte:

- `id`: stabile, automatisch erzeugte technische Identität; Namen dürfen geändert werden, ohne Historie zu verlieren
- `enabled`: Beispiel-Pairs sind absichtlich deaktiviert
- `direction`: `bisync`, `pull` oder `push`
- `mode`: bei einseitigen Pairs `copy` oder `sync`
- `allow_delete`: produktive Mirror-Löschungen ausdrücklich freigeben
- `max_delete`: maximal erlaubte Löschungen
- `require_mountpoint`, `mountpoint`, `sentinel_file`: Mountschutz
- `min_local_files`, `min_remote_files`, `min_free_gb`: Plausibilitätsgrenzen
- `allow_empty_remote_target`: hebt den erzwungenen Mount-Drop-Schutz für ein bewusst leeres lokales Ziel auf (`min_remote_files` bleibt dann bei 0)
- `rclone_args`: zusätzliche, validierte Argumente
- `airgap_device`: optionales Block-Gerät unter `/dev/` (leer = nicht gesetzt)
- `zfs_snapshot`: ZFS-Snapshot vor dem Lauf, nur bei `direction: push` (Standard `false`)
- `tags`: bis zu 10 Kleinbuchstaben-Marken (`a-z`, `0-9`, `_`, `-`, höchstens 24 Zeichen)
- `versions_lifecycle`: Teil-Override des globalen Blocks für dieses Pair

Sicherheitsrelevante Änderungen – etwa Benutzername, PBS-Ziel/Secret,
lokale Browser-Wurzeln oder das Abschalten von Schutzschaltern –
verlangen beim Speichern zusätzlich das aktuelle Webpasswort.

Passkeys werden unter **Einstellungen → Sicherheit** aktiviert. Dafür müssen
`web.webauthn_rp_id` (DNS-Name) und `web.webauthn_origin` (exakte öffentliche
HTTPS-Origin) zusammen gesetzt sowie `web.secure_cookie: true` aktiviert sein.
Danach registriert oder widerruft **Einstellungen → Konto → Sichere Anmeldung**
Passkeys und USB-/NFC-Sicherheitsschlüssel. Private Schlüssel verlassen das
jeweilige Gerät beziehungsweise den Hardware-Schlüssel nicht.

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

Der Timer startet jede Minute einen kurzen Scheduler-Tick. Dieser entscheidet pro aktiver Jobdefinition anhand von Cron-Ausdruck, Zeitzone, Jobhistorie und Fehler-Backoff.

- Geplante und manuelle Läufe werden getrennt gespeichert.
- Nur fehlgeschlagene Scheduler-Läufe lösen automatische Backoff-Wiederholungen aus.
- Ein manueller Fehler startet nicht unbeabsichtigt eine automatische Retry-Schleife.
- Datenwege laufen in der gespeicherten Reihenfolge oder – nur innerhalb einer Definition – bis zum festgelegten Parallelitätslimit.
- Mehrere fällige Jobdefinitionen werden unter einem globalen Prozess-Lock nacheinander ausgeführt; gemeinsam verwendete Pfade können dadurch nicht kollidieren.
- Der alte tägliche Voll-Run-Timer bleibt standardmäßig deaktiviert.

## systemd

| Unit | Funktion | Standard |
|---|---|---|
| `rclone-sync-web.service` | FastAPI-Web-UI auf Port 8001 | aktiv |
| `sync-scheduler.timer` | prüft jede Minute fällige Jobdefinitionen | aktiv |
| `sicherpfad-operations.timer` | unabhängige Lebenszeichen, Speichermessungen und Schutzberichte | aktiv |
| `sync-scheduler.service` | führt einen Scheduler-Tick aus | timer-gesteuert |
| `rclone-sync.service` | manueller Voll-Run aller aktiven Jobdefinitionen | inaktiv |
| `rclone-sync.timer` | alter täglicher Voll-Run | deaktiviert |

Die Units verwenden fest:

```text
RCLONE_CONFIG=/home/rclone-sync/.config/rclone/rclone.conf
RCLONE_ASK_PASSWORD=false
```

Das rclone-Konfigurationsverzeichnis ist gezielt beschreibbar, damit OAuth-Tokens erneuert werden können. Bei lokalen Datenpfaden außerhalb `/mnt`, `/media`, `/srv` oder `/opt/rclone-sync` müssen die `ReadWritePaths` per systemd-Drop-in ergänzt werden. Erweiterungen mit höherem Rechtebedarf (ZFS, Airgap-USB, Boot-Drill, VPN-Sensor) beschreibt [Privilegierte Erweiterungen freischalten](#privilegierte-erweiterungen-freischalten).

## Reverse-Proxy

Hinter einem HTTPS-Reverse-Proxy:

```yaml
web:
  secure_cookie: true
  hsts_seconds: 31536000
  allowed_hosts:
    - sync.example.org
```

Die Unit vertraut Forwarded-Header standardmäßig nur von `127.0.0.1` und `::1`.
Läuft der Proxy in einem anderen Container, dessen feste IP gezielt bei
`--forwarded-allow-ips` in einem systemd-Drop-in ergänzen. Kein pauschales `*`
verwenden. Der Proxy muss eingehende `Forwarded`- und `X-Forwarded-*`-Header
überschreiben und der lokale Backend-Port darf nicht zusätzlich aus einem
unvertrauenswürdigen Netz erreichbar sein; sonst können Header-Spoofing,
Secure-Cookie- und Client-IP-Annahmen umgangen werden.

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
- `POST /api/jobs/{id}/retry?dry_run=false` – nur revisionsgebundene Fehlerläufe
- `GET /api/jobs/definitions`
- `GET /api/jobs/definitions/{id}/plan?dry_run=true`
- `POST /api/jobs/definitions/{id}/run?dry_run=false`
- `POST /api/jobs/definitions/run-all?dry_run=false`
- `GET /api/jobs/backup/plan`
- `POST /api/jobs/backup/run?dry_run=true` – mit `pairs=` und Body `exclude_paths` lassen sich einzelne Pfade ausnehmen (nicht bei bisync)
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
- `GET /api/push/status`
- `POST /api/push/test`
- `GET /api/lockdown`
- `POST /api/lockdown` – löst aus, verlangt Passwort (optional `reason`)
- `POST /api/lockdown/release` – verlangt Passwort
- `GET /api/pbs/insights`
- `POST /api/pbs/boot-drill`
- `GET /api/pbs/boot-drill/status`
- `POST /api/jobs/bitrot-scrub`
- `GET /api/jobs/bitrot-scrub/status`
- `GET /api/jobs/bitrot-scrub/baseline`
- `GET /api/jobs/versions/preview`
- `POST /api/jobs/versions/prune` – verlangt Passwort
- `GET /api/jobs/{id}/preflight`
- `GET /api/pairs/{name}/conflicts`
- `POST /api/pairs/{name}/conflicts/resolve` – Löschen von Kopien verlangt Passwort
- `GET /api/recovery/staging/{id}/diff?path=`
- `GET /api/diagnostics/vpn`
- `GET /api/storage/object-lock`
- `GET /api/jobs/bandwidth/heatmap`
- `GET /api/jobs/bandwidth/suggestions`
- `GET /api/remotes/crypt/candidates`
- `POST /api/remotes/crypt`
- `POST /api/remotes/crypt/{name}/verify`
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

Die Anwendung hält mindestens `maintenance.keep_latest_jobs` Jobs, auch wenn diese älter als die Retention sind. SQLite-Integrität, Checkpoint und Log-Retention sind über Weboberfläche und CLI prüfbar. Abgelaufene Push-Geräte und alte abgeschlossene Outbox-Einträge werden bei der automatischen oder manuellen Datenbankwartung ebenfalls entfernt.

Fehlt der bisync-State, nicht blind automatisch resyncen. Zuerst beide Seiten, Filter, Richtung, Konfliktstrategie und Dry-Run prüfen.

## Tests

### Abhängigkeits-Lockdatei pflegen

Der produktive Installer verwendet `requirements.lock` mit `--require-hashes`
und `--only-binary=:all:`. Die Datei enthält ausschließlich Hashes nicht
zurückgezogener Wheels der exakt in `requirements.txt` gepinnten Versionen;
Plattformmarker und Extras bleiben erhalten. Gibt es für Python oder Plattform
kein passendes Wheel, bricht die Installation ab; ein Quellbuild ist kein Fallback.

Nach einer bewusst geprüften Änderung an `requirements.txt` (auch durch
Dependabot) die Lockdatei aus den offiziellen PyPI-Release-Metadaten erneuern:

```bash
python scripts/update_dependency_lock.py
python scripts/update_dependency_lock.py --check
```

Der erste Aufruf benötigt HTTPS-Zugriff auf `pypi.org`, installiert keine Pakete
und ersetzt die bisherige Lockdatei erst nach vollständigem Erfolg. `--check`
prüft offline Pins, Extras, Marker und Hashsyntax; es ersetzt keine Prüfung der
Herkunft neuer Versionen. Beide Dateien gemeinsam reviewen und übernehmen.

### Externe Release-Grenzen

Release-Tags `ios-v*` können signierte Builds auslösen. Ihre Erstellung und
Änderung müssen zusätzlich durch ein GitHub-Ruleset auf die zuständigen
Release-Verantwortlichen beschränkt werden. Die Codemagic-Signiergruppe
`ios-release` muss in der Plattform ausschließlich für freigegebene Builds des
geschützten `main` zugänglich sein. Eine YAML im zu prüfenden Tag kann sich
selbst ändern und erzwingt diese externen Grenzen nicht. Der Betreiber muss die
tatsächlich wirksamen Berechtigungen in GitHub/Codemagic prüfen.

Die Codemagic-Provenienzprüfung verlangt ein bereits vertrauenswürdig
bereitgestelltes `gpg`. Fehlt es, bricht der Build ab; vor der Herkunftsprüfung
wird kein `brew install` mehr ausgeführt. Build-Image und GPG-Bereitstellung sind
Teil der externen Runner-Vertrauensbasis.

CI prüft Python 3.10 bis 3.13. Lokal nach Installation von
`requirements-dev.txt`:

```bash
python -m pip check
python -m pytest -q
ruff check .
ruff format --check .
python -m compileall -q app tests
node --check app/static/app.js
node --check app/static/workspace.js
npm ci --prefix tests/web --ignore-scripts --no-audit --no-fund
npm test --prefix tests/web
bash -n scripts/install.sh
shellcheck scripts/install.sh
git diff --check
```

Die Web-Prüfungen laufen mit dem mitgelieferten Alpine und einem isolierten DOM
ohne Browser oder Backend. Sie prüfen die Unteransichten, erhaltene Aktionen,
Warnhinweise und Tastaturbedienung. Eine Sichtprüfung auf Desktop und Mobilgerät
bleibt zusätzlich erforderlich.


### Recovery-Aufbewahrung und lokale Pfade

`backup.recovery_snapshot_retention_days` ist standardmäßig 90 Tage. Nach einem
erfolgreichen neuen Vollstand werden nur überprüfbar zu diesem Datenweg gehörende
ältere Stände aufgeräumt; der neue Stand bleibt erhalten. Unbekannte Altordner
und Stände entfernter Datenwege werden nicht automatisch gelöscht.
`backup.recovery_staging_max_mb` begrenzt die Summe aufbewahrter Restore-Daten
standardmäßig auf 51200 MiB. Staging lässt sich in Web und App mit Passwortbestätigung
entfernen. Recovery-Prozesse werden pro Auftrag registriert und unter ihren Laufzeitsperren
abgebrochen; temporäre Daten werden erst nach sicher festgestelltem Prozessende bereinigt.

Lokale Vault-Veröffentlichung und Ordneranlage benötigen POSIX-Verzeichnisdeskriptoren;
auf nicht unterstützten Hosts wird diese Aktion abgewiesen. Unter Linux müssen die
Vorfahren lokaler Sync-Endpunkte root oder dem Dienstkonto gehören und gegen Austausch
durch andere Benutzer geschützt sein. Gruppen-/weltschreibbare Vorfahren und Symlinks
werden abgewiesen; Root-Sticky-Verzeichnisse sind nur mit einem bereits geschützten
Kind zulässig. Das friert keine von berechtigten Benutzern gleichzeitig geänderten
Dateiinhalte ein.

### Datenschutz und Betreiberpflichten

Vault-Dateien und Datensätze bleiben lokal unter `paths.device_vault_dir` (Standard `<data_dir>/device-vault`) bis zur ausdrücklich bestätigten Entfernung erhalten. Es gibt keine automatische Blob-Löschung, weil die lokale Datei die letzte erreichbare Kopie sein kann. Die Entfernung der lokalen Vault-Kopie löscht keine Zielkopie oder bereits exportierte Notfallakte. Audit-Wartung erhält mindestens 365 Tage und die neuesten 1000 Einträge, die deshalb länger verbleiben können. App-Logout entfernt die Sitzung und clientgebundene temporäre Exporte; Profile, Offline-Pässe und Upload-Warteschlange werden gesondert verwaltet.

Hosting-/Supportanbieter, genaue Kontakt-Aufbewahrung, Auftragsverarbeitungsverträge und Drittlandgrundlagen müssen anhand der tatsächlich verwendeten Dienste bestätigt werden. Der App-Quelltext belegt diese externen Betreiberangaben nicht. Die Datenschutzhinweise ersetzen diese Prüfung nicht.
Die Browserprüfung bedient die tatsächliche Weboberfläche in Chromium und
mobilem WebKit mit lokalen API-Fixtures. Sie startet keine produktiven Jobs:

```bash
npm ci
npx playwright install chromium webkit
npm run test:web
node --test scripts/test_web_requests.cjs
```

Die iOS-CI führt zusätzlich native Navigationstests im Offline-Vorschaumodus
aus, einschließlich der größten Bedienungshilfen-Schriftgröße. Simulator-Build,
Testresultate und Screenshots werden als CI-Artefakte abgelegt; dies ist kein
TestFlight-Upload und keine Installation auf einem iPhone.
