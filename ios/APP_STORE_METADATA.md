# App Store-Metadaten – Sicherpfad

## Produktseite (Deutsch)

- Name: `Sicherpfad`
- Untertitel: `Backup prüfen & zurückholen`
- Kategorie: `Dienstprogramme`
- Preis: `Kostenlos`
- Verteilung: `Öffentlich`, alle Länder und Regionen
- Support-URL: `https://oliverzimmermann1986-debug.github.io/Rclone/`
- Datenschutz-URL: `https://oliverzimmermann1986-debug.github.io/Rclone/datenschutz.html`
- Copyright: `2026 Oliver Zimmermann`
- Schlüsselwörter: `backup,sicherung,recovery,rclone,server,dateien,fotos,self-hosted,restore`

### Werbetext

Der verifizierte Schutzpfad für deine eigenen Daten: direkt vom Gerät sichern, Versionsstände vergleichen und gezielt zurückholen.

### Beschreibung

Sicherpfad ist eine native Leitstelle für deine selbst gehostete Sicherungsinstallation. Die App bildet deinen Schutz als nachvollziehbaren Weg ab: vom Gerät oder lokalen Ordner über den geplanten Job bis zur verifizierten Kopie und gezielten Wiederherstellung.

Der eigens entwickelte Schutzstatus verdichtet aktive Datenwege, Zeitplanung, letzte erfolgreiche Läufe und Serverhinweise zu einer sofort verständlichen Lage: Bereit, Prüfen oder Handeln. Bei Problemen zeigt die App den nächsten sinnvollen Schritt, statt nur technische Rohdaten aufzulisten.

Funktionen:

- Geräte-Vault: ausgewählte Fotos und Dateien direkt vom Gerät in einen eigenen Datenweg sichern
- Langlebige Upload-Warteschlange mit bewusster Wiederaufnahme nach App-Neustart, SHA-256 und Zurücklesen der Zielkopie
- Fotos und Dateien aus dem Teilen-Menü lokal für den nächsten Upload vormerken
- Geführter Schutzassistent mit Vorschau der Datenweg- und Job-Einstellungen und erster Restore-Stichprobe
- Recovery-Zeitreise: Änderungsarchive ausdrücklich von vollständigen Ständen getrennt
- Optionale vollständige Stände mit Datei- und SHA-256-Manifest auf dem Server (maximal 50 GiB je Stand); getrennte vollständige Wiederherstellung
- Eigenständiger Schutzstatus mit konkretem nächsten Schritt
- Lokale und Cloud-Datenwege mit Dateianzahl und Größe
- Eigene Schutzpfad-Ansicht von der Quelle über zugewiesene Jobs zum Ziel
- Jobs mit Zeitplan, Reihenfolge und zugewiesenen Datenwegen
- Laufhistorie mit Status, Dauer, Protokoll und sicherem Neustart
- Sicheres Anlegen lokaler und entfernter Zielordner
- Zielgebundener Restore-Stichprobenbeleg mit sieben Tagen Gültigkeit; letzter Versuch und letzter Erfolg getrennt
- Recovery-Pass mit nachvollziehbarem Score, RPO/RTO und Schutzkalender
- Gezielte Wiederherstellung ausschließlich in ein getrenntes, prüfsummengeprüftes Staging
- Sicherheitsstopp vor destruktiven Läufen bei unerwartet geschrumpften Quellen
- Verschlüsselte Notfallakte: Vault-Inventar auf einem Ersatzserver bewusst zuordnen und Dateien aus dem Cloud-Ziel zurücklesen; keine Übernahme von Passwörtern oder Cloud-Schlüsseln
- Schutzstatus-Widget, Live Activity für Sicherungen und Geräte-Uploads sowie Siri-Kurzbefehle
- Passkey, physischer Sicherheitsschlüssel und mehrere Serverprofile ohne Passwortspeicherung
- Native Push-Mitteilungen bei Sicherungsfehlern mit Vorfallansicht und authentifizierter Pausenaktion
- Stillstands-Watchdog, Laufzeitgrenzen und kontrollierter Abbruch
- Integrierte lokale Demo ohne Server oder echte Daten

Sicherpfad stellt keinen Cloudspeicher bereit. Für den produktiven Einsatz benötigst du eine eigene kompatible Sicherpfad-Serverinstallation auf Basis von rclone. Passwörter werden nicht dauerhaft in der App gespeichert. Auf Wunsch speichert der Geräteschlüsselbund die Sitzung, auch für bestätigte HTTP-Verbindungen. HTTP bleibt unverschlüsselt; HTTPS wird empfohlen. Übertragungen pausieren gegebenenfalls im Hintergrund und können in der App fortgesetzt werden. Widgets zeigen den zuletzt geladenen Stand, keine ständige Serververbindung. Vollständige Stände auf dem Server sind keine externe Katastrophensicherung; die Notfallakte enthält keine Nutzdateien und ersetzt keinen Cloud-Zugang.

## App-Prüfung

- Anmeldung erforderlich: `Ja` für echte Upload-, Snapshot- und Restore-Prüfungen.
- Prüfserver: `https://rclone-review.mausbaeren.me`. Den bestehenden isolierten Review-Zugang in App Store Connect hinterlegen und vor Einreichung am vorgesehenen Build testen. Zugangsdaten nicht in Repository oder Video veröffentlichen.
- Prüfweg: Sichern → Fotos & Dateien sichern → kleine Testdatei hochladen → bestätigte Zielkopie zurückholen. Danach Wiederherstellen → Testdatenweg → Recovery-Zeitreise → vollständigen Stand erstellen → gesamten Stand getrennt zurückholen → Vorgang und Ergebnis öffnen. Für den Serververlust-Ablauf eine eigene aktuelle Notfallakte exportieren, am vorbereiteten Ersatzserver zuordnen und eine Vault-Datei wirklich herunterladen.
- Die lokale Demo unter „App mit Beispieldaten ansehen“ ist eine zusätzliche, ausdrücklich simulierte Vorschau. Sie beweist keine echte Übertragung oder Wiederherstellung und ersetzt den nutzbaren Review-Zugang nicht.
- Vor Einreichung: vollständiger iOS-Build einschließlich Share Extension, Gerätetest, aktuelles Video, aktuelle Screenshots und erfolgreicher Ende-zu-Ende-Test des isolierten Review-Servers. Lokale Code-Änderungen allein sind keine Veröffentlichung.

### Hinweis zu Guideline 4.3(a)

Der Schwerpunkt von Sicherpfad ist überprüfbare Wiederherstellbarkeit eigener Daten: Zielkopien werden tatsächlich zurückgelesen, Stichprobenbelege gelten nur für das geprüfte Ziel und einen begrenzten Zeitraum, vollständige Stände werden anhand von Dateimanifesten geprüft und ein Ersatzserver kann das Vault-Inventar ohne alte Server-Blobs zurückholen. Das native SwiftUI-Frontend führt über Übersicht, Sichern und Wiederherstellen; technische Verwaltung liegt unter Mehr. Bitte diese konkreten Abläufe am isolierten Review-Server und im aktuellen Video prüfen. Die technische rclone-Basis wird offen benannt. Daraus wird keine weltweite Einzigartigkeit oder garantierte Freigabe abgeleitet.
- Veröffentlichung: `Manuell`, damit die Freigabe nach Apples Genehmigung kontrolliert erfolgt.

## Datenschutzangaben

- Tracking: `Nein`
- Vom Entwickler erfasste Daten: `Keine`
- Datenschutz-URL: siehe oben

Die App verbindet sich nur mit dem vom Benutzer angegebenen, selbst betriebenen Server. Ein optionaler APNs-Geräte-Token wird an diesen Server übermittelt; der Entwickler erhält ihn nicht.

## Altersfreigabe und Rechte

- Inhaltsrechte: Die App zeigt ausschließlich vom Benutzer konfigurierte Server- und Sicherungsdaten; keine fremden Medieninhalte werden bereitgestellt.
- Altersfreigabe: Alle abgefragten Inhalts- und Interaktionskategorien `Nein` beziehungsweise `Keine`.
- Verschlüsselung: `ITSAppUsesNonExemptEncryption = false`; ausschließlich von Apple bereitgestellte Standardverschlüsselung.
