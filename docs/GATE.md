# Automatische Prüfungen

Die Linux-Prüfungen laufen im bestehenden zentralen Prüfdienst auf `happysrv`.
Der Einstieg steht in `.pruefdienst.json`: Profil `rclone`,
`bash scripts/ci/verify.sh`, ohne PostgreSQL. Repository-Code läuft ausschließlich
als unprivilegierter Benutzer in einem frischen Container ohne Host-Mounts,
Docker-Socket oder Zugangsdaten.

## Verbindlicher Nachweis

Vor der Integration müssen für den aktuellen Commit beide Ergebnisse grün sein:

- `pruefdienst/release`: vollständige Linux-Prüfung mit gültigem Bericht.
- `build-and-test`: echte iOS-Simulatorprüfung auf dem macOS-Runner bei GitHub.

Die iOS-Prüfung bleibt automatisch für jeden Pull Request und jeden Push auf
`main` aktiv. Xcode und SwiftUI benötigen weiterhin einen Mac. Codemagic,
signierte Release-Tags und TestFlight bleiben eigene Schritte.

Der zentrale Dienst prüft `main`, Tags und nach ausdrücklicher Konfiguration die
offenen Pull Requests dieses Repositories gegen den Standardbranch. Es zählt der
exakte Head-SHA. Fremde Forks werden nicht automatisch ausgeführt. Feature-Branches
ohne Pull Request starten keinen Prüflauf. Ein SHA mit mehreren Referenzen teilt
sich einen Lauf; neue SHAs erhalten eigene Ergebnisse. Abfrage etwa alle zwei Minuten.

Die bisherigen Linux- und Browser-Workflows bei GitHub bleiben über
`workflow_dispatch` als manueller Rückfall erhalten. Ihre Ergebnisse ersetzen
den verbindlichen zentralen Status nicht.

## Umfang und Grenzen

`scripts/ci/verify.py` schreibt alle 21 Stufen vor dem ersten Befehl mit `not_run`
nach `reports/unified-quality-report.json`. Abgedeckt sind:

- Werkzeuge und bestehende zentrale Steuerung samt Rclone-PR-Erkennung.
- Python 3.10 bis 3.13: eigene virtuelle Umgebungen, Hash-Lockdatei, `pip check`,
  Ruff und vollständige Testsuiten. Reales rclone ist verfügbar; fehlende, leere
  oder übersprungene Tests verhindern ein grünes Ergebnis.
- Je zwei isolierte Google-OAuth-Zustandsfälle mit rclone 1.60.1 und 1.74.4,
  ohne Google-Konto oder echte Anmeldung.
- Shellcheck, JavaScript-Syntax und die fünf unveränderten systemd-Units. Im Image
  vorbereitete Benutzer/Pfade und harmlose ausführbare Platzhalter erlauben die
  Unit-Prüfung ohne Root-Rechte oder gestartete Dienste im Testlauf.
- Bestehende DOM-/Alpine-Verhaltenstests und 48 Chromium-Bildfälle mit synthetischen
  Antworten auf Desktop/Mobil in heller/dunkler Darstellung. Die 200-%-Fälle sind
  eine Reflow-Näherung; ein physischer Gerätezoom wird nicht behauptet.

Unabhängige Stufen laufen nach einem Fehler weiter; fehlende abhängige Stufen
bleiben `not_run`. Nur ein vollständiger Plan mit ausschließlich `passed` endet
erfolgreich. Ein fehlender Bericht ist beim Profil `rclone` auch bei Exit 0 ein
Fehler des zentralen Dienstes. Projekt-Tests, Browser und Builds laufen ausschließlich
in den vorgesehenen isolierten Umgebungen. Sie führen kein Deployment und keinen
produktiven Sicherungs-/Wiederherstellungslauf aus.

## Berichte

Das bestehende private Repository `pruefprotokolle` erhält Log und JSON-Bericht.
Der Bericht enthält Testzahlen, Fehler und ein SHA-256-geprüftes, auf 8 MiB
begrenztes ZIP mit JUnit, Logs, 48 Screenshots und sechs synthetischen Traces.
Die zentrale Aufbewahrung beträgt 30 Tage. Nach Herunterladen des Berichts lässt
sich das ZIP in einer geeigneten isolierten Umgebung wiederherstellen:

```bash
python scripts/ci/evidence.py bericht.json test-results/rclone-ci-evidence.zip
```

## Pflege

`infra/pruefdienst/Dockerfile` beschreibt das zusätzliche Image;
`infra/pruefdienst/runtime.json` nennt die registrierte unveränderliche Image-ID
und Werkzeugversionen. Beide vorhandenen Docker-Daemons verwenden denselben ID.

Vor einem Neubau den dort genannten Basis-ID lokal prüfen und genau diesen als
`pruefdienst-rclone-base-7ab1d115ae18` markieren; der Build nutzt `--pull=false`.
Zum Build-Kontext gehören `Dockerfile`, `uv-requirements.lock`,
`web/package.json`/`web/package-lock.json` aus `tests/web` und die unveränderten
zentralen `.cjs`-Dateien unter `controller/`. Systemwerkzeuge und Browser werden
im Image vorbereitet, nicht mit zusätzlichen Rechten im Testlauf installiert.

Die optionale Controller-Erweiterung unter `infra/pruefdienst/central` wird zuerst
auf einer privaten Kopie der freigegebenen Steuerung angewandt und mit deren
bisherigen Tests geprüft. Neues Profil und PR-Freigabe werden danach in der
root-geschützten Dienstkonfiguration ergänzt. Ein späteres Controller-Update muss
die Erweiterung bewusst erhalten oder neu prüfen. Die Zugangsdaten gelangen
nicht in Testcontainer. Der produktive Sicherpfad-Server ist kein Testziel.
