# Sicherpfad für iPhone

Native SwiftUI-App für den Sicherpfad Server auf Basis von rclone. Sie verwendet dieselbe gehärtete Cookie-Sitzung wie das Web-Frontend, bezieht den Login-CSRF-Nonce über den strukturierten JSON-Vertrag `/api/auth/login` und sendet bei schreibenden Aufrufen den Double-Submit-CSRF-Header. Optional meldet sie sich über die WebAuthn-Origin des selbst gehosteten Servers mit Passkey oder physischem Sicherheitsschlüssel an und tauscht das Ergebnis über einen kurzlebigen Einmalcode gegen die App-Sitzung.

Das eigenständige App-Icon zeigt einen markanten Schutzpfad durch ein Verifikationstor und vermeidet bewusst Sync-Pfeile, Cloud- und rclone-Symbole.

## Enthalten

- **Lagebild:** Eigener Schutzstatus, Systemzustand, Warnungen, Live-Fortschritt und kontrollierter Abbruch, Datenwege, letzter Lauf und Kopienübersicht mit lokalem/cloudseitigem Ordner, Dateizahl und Größe.
- **Geräte-Vault:** Fotos und Dateien direkt auswählen, in wiederaufnehmbaren 1-MiB-Blöcken übertragen, per SHA-256 deduplizieren, am Ziel zurücklesen und anschließend wieder in Dateien exportieren.
- **Restore-Nachweis:** Pro Datenweg sichtbarer Prüfsummenbeleg mit letzter Prüfung, letztem Erfolg, Stichprobengröße und direktem erneuten Restore-Test.
- **Restore-Prüfplan:** Termine in der Server-Zeitzone, Nachweisablauf und Prüflücken vor einer ausdrücklich bestätigten, revisionsgebundenen Übernahme für alle aktiven Datenwege.
- **Recovery Center:** Recovery-Pass mit nachvollziehbarem Score, RPO/RTO, Schutzkalender, geführter Notfallübung, Anomalie-Quarantäne, Recovery-Zeitreise mit Änderungsvergleich und selektivem Restore in ein getrenntes Server-Staging.
- **Notfallübergabe:** lokal teilbares AES-256-GCM-Paket mit redigierter Konfiguration und Nachweisen; Passphrase und Paket werden bewusst getrennt übermittelt.
- **Offline und mehrere Server:** pfadlose Recovery-Pässe sind nach Server und Konto getrennt und schon vor der Anmeldung über die Offline-Notfallkarte erreichbar. Bis zu acht Serverprofile; Sitzungen können auf Wunsch geschützt im Geräteschlüsselbund gespeichert werden.
- **Schutzassistent:** Profil und vorhandenen Push-Datenweg wählen, konkrete Änderungen bestätigen und erste Restore-Stichprobe starten. Gemeinsame bestehende Jobs bleiben unverändert.
- **Vollständige Stände:** zusätzliche lokale Serverkopie mit SHA-256-Manifest, einschließlich unveränderter Dateien und leerer Verzeichnisse. Standard 5 GiB, maximal 50 GiB je Stand. Änderungsarchive bleiben ausdrücklich unvollständig. Diese lokalen Stände ersetzen kein externes Backup.
- **Serververlust:** verschlüsselte Akte am Ersatzserver prüfen, Vault-Inventar bewusst vorhandenen Zielen zuordnen und Zielkopien ohne alten lokalen Blob nach vollständiger SHA-256-Prüfung herunterladen. Cloud-Zugang zuvor separat einrichten.
- **Upload-Warteschlange und Teilen-Erweiterung:** lokal geschützte Dateien, server-/konto-/zielgebundene Wiederaufnahme über App-Neustarts, explizite Weitergabe aus Fotos/Dateien; keine unbegrenzte Hintergrundausführung.
- **Fehler je Vault-Datei:** Eine defekte Datei hält andere Dateien nicht auf. Erneut versuchen oder bewusst überspringen ist je Datei möglich; die lokale Ausgangsdatei bleibt für den Export erhalten. Verbindungs- und Anmeldefehler pausieren die Übertragung weiterhin.
- **Schutzpfad:** Native Topologie von Quelle über zugewiesene Jobs zum Ziel in einem schlanken Detailfenster.
- **Konfiguration:** native Datenweg- und Jobverwaltung mit geordneter Zuweisung, Zeitplan, Parallelität, Löschschutz, Plan und Probelauf; unbekannte neue Serverfelder bleiben beim Speichern erhalten.
- **Neue Remotes:** Einrichtungsassistent unter System und in der Cloud-Ordnerauswahl; pCloud, Google Drive, WebDAV und OneDrive werden bevorzugt angezeigt. Schrittweise rclone-Fragen, getrennte Entwürfe und ausdrückliches Speichern. OAuth verwendet derzeit `rclone authorize` auf einem Rechner mit Browser; der zurückgegebene Wert wird in der App eingefügt. Eine reine iPhone-OAuth-Anmeldung ist noch nicht enthalten.
- **Läufe:** unveränderliche Historie mit Suche, Filtern, CSV, redigiertem Log-Download und revisionssicherem Retry für Fehlerläufe.
- **System:** Ressourcen, Dienste, Diagnose, Push-Zustellung/Test, Wartungsfenster, Audit, Snapshots, Datenbank-/Logwartung, Support-Bundle sowie PBS-Konfiguration, Starts und Abbruch.
- **Betriebswerkzeuge:** Dateinamen über Sicherungsstände suchen und selektiv in Staging zurückholen, Speicherreserven mit Messverlauf und optionalem Zielbudget beobachten, einen unabhängigen Ausfallwächter konfigurieren sowie Wochenberichte als geschützte PDF exportieren oder ausdrücklich per E-Mail senden. Ein geführter Zielwechsel kopiert einen Push-Datenweg auf ein leeres Ziel, liest ihn zur SHA-256-Prüfung zurück und schaltet erst nach separater Bestätigung um. Die ursprüngliche Kopie und historische vollständige Stände bleiben erhalten. Grenzen und Einrichtung stehen unter [Betriebswerkzeuge](../README.md#dateisuche-ausfallwächter-und-betriebswerkzeuge).
- **Sichere Aktionen:** Bestätigungsdialoge für produktive Sicherungen, Restore-Tests, Snapshots, Zeitplansteuerung und globale Abmeldung.
- **Passwortlose Anmeldung:** Passkeys und USB-/NFC-Sicherheitsschlüssel über den geschützten iOS-Systembrowser; keine feste Serverdomain und kein privates Schlüsselmaterial in der App.
- **Push:** Fehlerbenachrichtigungen über APNs öffnen Lauf oder Recovery-Vorfall; nach Geräteauthentifizierung können Zeitpläne direkt für eine Stunde pausiert werden. Offline-Abmeldungen werden lokal beendet und die serverseitige Token-Löschung später nachgeholt.
- **iOS-Systemflächen:** Schutzstatus-Widget, Live Activity für laufende Sicherungen sowie Siri-/Kurzbefehle für Recovery Center und letzten Schutzstatus.
- **Native Bedienung:** Dynamic Type, VoiceOver-Beschriftungen, Pull-to-refresh, Systemmaterialien, Dark Mode und haptische Erfolgsrückmeldung.

## Projekt erzeugen

Falls ein Mac vorhanden ist: Xcode 26 oder neuer und [XcodeGen](https://github.com/yonaskolb/XcodeGen). Ein eigener Mac ist nicht nötig; die produktive Pipeline läuft auf Codemagic.

```bash
cd ios
xcodegen generate
open RcloneMobile.xcodeproj
```

Danach in den Targets `RcloneMobile` und `RcloneProtectionWidget` dasselbe Apple-Entwicklungsteam wählen und auf einem Simulator oder iPhone starten. Für das Widget muss die App Group `group.de.oliverzimmermann.rclonesync` in App Store Connect/Developer Portal für beide Bundle-IDs freigeschaltet sein. Es gibt keine externen Swift-Pakete.

## Ohne Mac nach TestFlight

Die Datei [`../codemagic.yaml`](../codemagic.yaml) erzeugt das Xcode-Projekt auf einem Cloud-Mac, führt die Unit-Tests aus, signiert die App und lädt die IPA zu TestFlight. Die einmalige Apple-/Codemagic-Einrichtung steht in [`TESTFLIGHT.md`](TESTFLIGHT.md).

Jeder Tag im Format `ios-vX.Y.Z`, beispielsweise `ios-v1.0.0`, startet danach automatisch einen Release. Pull Requests und vertragsrelevante Backend-/iOS-Änderungen werden zusätzlich über GitHub Actions mit Xcode 26 kompiliert und getestet, jedoch nicht veröffentlicht.

Die Exporttests prüfen im Simulator echte Dateiinhalte, getrennte temporäre Ordner, POSIX-Rechte sowie die angeforderte Schutzklasse und das Aufräumen bei Schutzfehlern. `FileManager` liefert im Simulator kein verlässliches Data-Protection-Attribut zurück ([Apple DTS](https://developer.apple.com/forums/thread/780632)); die Rückleseprüfung der Schutzklasse läuft deshalb nur auf physischen Geräten. Ein grüner Simulatorlauf belegt nicht die Verschlüsselung und den Zugriffsschutz bei gesperrtem iPhone.

## Verbindung

Die Serveradresse muss auf den von außen erreichbaren Reverse Proxy des Sicherpfad-Servers zeigen, zum Beispiel `https://backup.example.de`. Eine lokale IP-Adresse ohne Schema, etwa `192.168.50.10`, wird automatisch als `http://192.168.50.10` über den HTTP-Standardport 80 verwendet. Abweichende Ports können explizit angegeben werden. Der interne Uvicorn-Port 8001 ist bei der Standardinstallation nur an `127.0.0.1` gebunden und vom iPhone nicht direkt erreichbar. HTTPS ist für produktive Installationen vorgesehen. Lokale Netzwerkverbindungen sind erlaubt; unsichere beliebige HTTP-Verbindungen werden durch App Transport Security nicht global freigegeben.

Öffentliche DNS-Namen verwenden ohne explizites Schema HTTPS; Namensanfänge wie `features` oder `fd` gelten nicht als lokale IPv6-Adresse. Die App lehnt eingebettete URL-Zugangsdaten ab. HTTP-Umleitungen bleiben auf Schema, Host und Port des ausgewählten Servers beschränkt, damit Anmelde- oder Bestätigungsdaten nicht an ein anderes Ziel weitergeleitet werden. Pfadwechsel innerhalb desselben Servers bleiben möglich.

Das Passwort wird für die Anmeldung und bewusste Passwortbestätigungen übertragen und nicht gespeichert. Auch die Anlage eines neuen Passkeys benötigt das aktuelle Serverpasswort. Bei WebAuthn verbleibt der private Schlüssel im iCloud-Schlüsselbund, System-Authenticator oder physischen FIDO2-Schlüssel; die App erhält nur einen einmal verwendbaren Sitzungscode. Serveradresse und Benutzername liegen in `UserDefaults`. Mit „Angemeldet bleiben“ werden ausschließlich HTTPS-Sitzungen und deren CSRF-Cookie im nicht synchronisierenden Geräteschlüsselbund gespeichert, getrennt nach Schema, Host, Port und Basispfad. Lokales HTTP bleibt während der laufenden App-Sitzung nutzbar, erfordert nach einem Neustart aber eine neue Anmeldung; ältere gespeicherte HTTP-Sitzungen werden beim Clientstart entfernt. Ohne diese Option, nach Abmeldung, Widerruf oder Ablauf ist eine neue Anmeldung nötig. Passkeys benötigen weiterhin eine geeignete HTTPS-Origin. Der Privacy-Manifest deklariert den app-internen `UserDefaults`-Zugriff mit Apples Gründen `CA92.1` (app-eigene Defaults) und `1C8F.1` (App-Group-Defaults mit Widget); Tracking und Datensammlung sind deaktiviert.

Beim erneuten Anmelden am selben Server wartet die App dessen laufende Abmeldung ab. Andere Serverprofile bleiben währenddessen nutzbar. Ein verspätetes Ergebnis eines früheren Logins, Logouts oder Passkey-Dialogs ersetzt keine neuere Sitzung und entfernt keine neueren gespeicherten Cookies. Eine neue Sitzung wird erst nach Prüfung des aktuellen Anmeldevorgangs gespeichert.

## Backend-Vertrag

Die App verwendet die kanonische Trennung **Datenwege → Jobs → Läufe**. Schreibvorgänge sind an die gelesene Konfigurationsrevision gebunden; HTTP 409 erzwingt ein bewusstes Neuladen statt stillen Überschreibens. Die gemeinsam versionierte Fixture [`../contracts/native_read_contract_v1.json`](../contracts/native_read_contract_v1.json) wird sowohl vom Backend als auch von den Swift-Decodierungstests geprüft.


### Datenschutz und Betreiberpflichten

Vault-Dateien und Datensätze bleiben lokal unter `paths.device_vault_dir` (Standard `<data_dir>/device-vault`) bis zur ausdrücklich bestätigten Entfernung erhalten. Es gibt keine automatische Blob-Löschung, weil die lokale Datei die letzte erreichbare Kopie sein kann. Die Entfernung der lokalen Vault-Kopie löscht keine Zielkopie oder bereits exportierte Notfallakte. Audit-Wartung erhält mindestens 365 Tage und die neuesten 1000 Einträge, die deshalb länger verbleiben können. App-Logout entfernt die Sitzung und clientgebundene temporäre Exporte; Profile, Offline-Pässe und Upload-Warteschlange werden gesondert verwaltet.

Hosting-/Supportanbieter, genaue Kontakt-Aufbewahrung, Auftragsverarbeitungsverträge und Drittlandgrundlagen müssen anhand der tatsächlich verwendeten Dienste bestätigt werden. Der App-Quelltext belegt diese externen Betreiberangaben nicht. Die Datenschutzhinweise ersetzen diese Prüfung nicht.
