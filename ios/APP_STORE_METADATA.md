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
- Wiederaufnehmbare Blockübertragung, SHA-256-Deduplizierung und serverseitiges Zurücklesen der Zielkopie
- Recovery-Zeitreise mit historischen Versionsständen, Änderungsübersicht und selektiver Wiederherstellung
- Eigenständiger Schutzstatus mit konkretem nächsten Schritt
- Lokale und Cloud-Datenwege mit Dateianzahl und Größe
- Eigene Schutzpfad-Ansicht von der Quelle über zugewiesene Jobs zum Ziel
- Jobs mit Zeitplan, Reihenfolge und zugewiesenen Datenwegen
- Laufhistorie mit Status, Dauer, Protokoll und sicherem Neustart
- Sicheres Anlegen lokaler und entfernter Zielordner
- Dokumentierter Restore-Nachweis pro Datenweg mit Prüfsummenstatus und Stichprobengröße
- Recovery-Pass mit nachvollziehbarem Score, RPO/RTO und Schutzkalender
- Gezielte Wiederherstellung ausschließlich in ein getrenntes, prüfsummengeprüftes Staging
- Sicherheitsstopp vor destruktiven Läufen bei unerwartet geschrumpften Quellen
- Verschlüsseltes Notfall-Übergabepaket ohne Passwörter oder Cloud-Schlüssel
- Schutzstatus-Widget, Live Activity für Sicherungen und Geräte-Uploads sowie Siri-Kurzbefehle
- Passkey, physischer Sicherheitsschlüssel und mehrere Serverprofile ohne Passwortspeicherung
- Native Push-Mitteilungen bei Sicherungsfehlern mit Vorfallansicht und authentifizierter Pausenaktion
- Stillstands-Watchdog, Laufzeitgrenzen und kontrollierter Abbruch
- Integrierte lokale Demo ohne Server oder echte Daten

Sicherpfad stellt keinen Cloudspeicher bereit. Für den produktiven Einsatz benötigst du eine eigene kompatible Sicherpfad-Serverinstallation auf Basis von rclone. Passwörter werden nicht dauerhaft in der App gespeichert.

## App-Prüfung

- Anmeldung erforderlich: `Nein`
- Prüfweg: Auf der Anmeldeseite `App mit Beispieldaten ansehen` wählen.
- Anmerkung: Die lokale Demo enthält ausschließlich mitgelieferte Beispieldaten und stellt keine Netzwerkverbindung her. Bitte auf der Startseite zuerst „App mit Beispieldaten ansehen“ und danach unter „Direkt vom Gerät“ den „Geräte-Vault“ öffnen. Mit „Demo-Sicherung abspielen“ wird der vollständige verifizierte Uploadfluss lokal demonstriert. Unter System → Recovery Center → Fotos → Recovery-Zeitreise sind Versionsstände, Änderungsübersicht und das getrennte Recovery-Staging sichtbar. Lage, Datenwege, Jobs, Läufe, Systemdiagnose, Recovery-Pass, RPO/RTO, Schutzkalender und Sicherheitsstopps sind ebenfalls vollständig zugänglich.

### Hinweis zu Guideline 4.3(a)

Sicherpfad ist keine umbenannte Vorlage und kein generischer rclone-Wrapper. Die App besitzt einen eigenen nativen SwiftUI-Codebestand, eine eigenständige visuelle Identität und speziell entwickelte Funktionen, die über eine übliche Administrationsoberfläche hinausgehen: Geräte-Vault mit resumierbarer und deduplizierter Übertragung, End-to-End-Zielprüfung, Recovery-Zeitreise, Änderungsvergleich, isoliertes selektives Staging, Recovery-Pass mit RPO/RTO, Anomalie-Quarantäne sowie Widget, Live Activity und Siri-Integration. Der Offline-Prüfweg macht diese Differenzierung ohne Zugang zu privater Infrastruktur nachvollziehbar.
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
