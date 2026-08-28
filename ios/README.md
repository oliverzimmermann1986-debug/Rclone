# Rclone Sync für iPhone

Native SwiftUI-App für das vorhandene rclone-sync-Backend. Sie verwendet dieselbe gehärtete Cookie-Sitzung wie das Web-Frontend, bezieht den Login-CSRF-Nonce über den strukturierten JSON-Vertrag `/api/auth/login` und sendet bei schreibenden Aufrufen den Double-Submit-CSRF-Header. Optional meldet sie sich über die WebAuthn-Origin des selbst gehosteten Servers mit Passkey oder physischem Sicherheitsschlüssel an und tauscht das Ergebnis über einen kurzlebigen Einmalcode gegen die App-Sitzung.

Die eigenständige App-Icon-Geometrie liegt reproduzierbar unter [`Brand/AppIconSource.svg`](Brand/AppIconSource.svg); das Asset-Catalog-PNG wird aus denselben Pfaden gerendert.

## Enthalten

- **Lagebild:** Eigener Schutzstatus, Systemzustand, Warnungen, Live-Fortschritt und kontrollierter Abbruch, Datenwege, letzter Lauf und Kopienübersicht mit lokalem/cloudseitigem Ordner, Dateizahl und Größe.
- **Restore-Nachweis:** Pro Datenweg sichtbarer Prüfsummenbeleg mit letzter Prüfung, letztem Erfolg, Stichprobengröße und direktem erneuten Restore-Test.
- **Recovery Center:** Recovery-Pass mit nachvollziehbarem Score, RPO/RTO, Schutzkalender, geführter Notfallübung, Anomalie-Quarantäne und selektivem Restore in ein getrenntes Server-Staging.
- **Notfallübergabe:** lokal teilbares AES-256-GCM-Paket mit redigierter Konfiguration und Nachweisen; Passphrase und Paket werden bewusst getrennt übermittelt.
- **Offline und mehrere Server:** letzter pfadloser Recovery-Pass bleibt auf dem Gerät; bis zu acht Serverprofile speichern ausschließlich Adresse und Benutzername.
- **Schutzpfad:** Native Topologie von Quelle über zugewiesene Jobs zum Ziel in einem schlanken Detailfenster.
- **Konfiguration:** native Datenweg- und Jobverwaltung mit geordneter Zuweisung, Zeitplan, Parallelität, Löschschutz, Plan und Probelauf; unbekannte neue Serverfelder bleiben beim Speichern erhalten.
- **Läufe:** unveränderliche Historie mit Suche, Filtern, CSV, redigiertem Log-Download und revisionssicherem Retry für Fehlerläufe.
- **System:** Ressourcen, Dienste, Diagnose, Push-Zustellung/Test, Wartungsfenster, Audit, Snapshots, Datenbank-/Logwartung, Support-Bundle sowie PBS-Konfiguration, Starts und Abbruch.
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

## Verbindung

Die Serveradresse muss auf den von außen erreichbaren Reverse Proxy der rclone-sync-Webanwendung zeigen, zum Beispiel `https://backup.example.de`. Eine lokale IP-Adresse ohne Schema, etwa `192.168.1.67`, wird automatisch als `http://192.168.1.67` über den HTTP-Standardport 80 verwendet. Abweichende Ports können explizit angegeben werden. Der interne Uvicorn-Port 8001 ist bei der Standardinstallation nur an `127.0.0.1` gebunden und vom iPhone nicht direkt erreichbar. HTTPS ist für produktive Installationen vorgesehen. Lokale Netzwerkverbindungen sind erlaubt; unsichere beliebige HTTP-Verbindungen werden durch App Transport Security nicht global freigegeben.

Das Passwort wird nur für den Login übertragen und nicht gespeichert. Bei WebAuthn verbleibt der private Schlüssel im iCloud-Schlüsselbund, System-Authenticator oder physischen FIDO2-Schlüssel; die App erhält nur einen einmal verwendbaren Sitzungscode. Serveradresse und Benutzername liegen in `UserDefaults`; die Sitzung bleibt in Apples Cookie-Speicher. Der Privacy-Manifest deklariert diesen ausschließlich app-internen `UserDefaults`-Zugriff mit Apples Grund `CA92.1`; Tracking und Datensammlung sind deaktiviert.

## Backend-Vertrag

Die App verwendet die kanonische Trennung **Datenwege → Jobs → Läufe**. Schreibvorgänge sind an die gelesene Konfigurationsrevision gebunden; HTTP 409 erzwingt ein bewusstes Neuladen statt stillen Überschreibens. Die gemeinsam versionierte Fixture [`../contracts/native_read_contract_v1.json`](../contracts/native_read_contract_v1.json) wird sowohl vom Backend als auch von den Swift-Decodierungstests geprüft.
