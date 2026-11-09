# Projektadapter: Rclone / Sicherpfad

**Version v1; gilt erst durch ausdrückliche Einbindung.** Ergänzt nach Übernahme den [Regelkern](../AI.md) und [Arbeitsablauf](../ENTWICKLER.md); bestehende Projektquellen haben Vorrang.

Lesebasis vom 16.10.2026: Ordner `Rclone`, Branch `codex/restore-incident-warnings-20260909`, Commit `171e0fd95efc233ef1884f8df01d2fc606f2d172`. Alle folgenden Quellpfade beziehen sich auf dieses Zielrepository. Die gelesene `README.md` enthält lokale Änderungen; sie bleiben erhalten und sind nicht Teil der Regelübernahme. Andere Branches wurden nicht geprüft.

## Bestehende Einstiege

| Quelle | Zuständigkeit |
| --- | --- |
| `README.md` | Betrieb, Lösch-/Pfadschutz, Konfiguration, Wiederherstellung und Tests |
| `PRODUCT.md` | Produktvertrag und serverseitige Sicherheitsgrenzen |
| `ios/README.md`, `ios/TESTFLIGHT.md` | Nativer Vertrag, Build- und Auslieferungsweg |
| `.github/workflows/ci.yml`, `.github/workflows/ios.yml`, `codemagic.yaml` | Bestehende Prüf- und Releasekonfiguration |
| `contracts/native_read_contract_v1.json` | Gemeinsamer Backend-/Swift-Lesevertrag |

Im untersuchten Checkout existiert keine versionierte `AGENTS.md`, `AI.md` oder `CLAUDE.md`. Ein untergeordnetes Projekt wie `Restaurant/` wird durch diesen Adapter nicht stillschweigend übernommen.

## Bei der Übernahme erhalten

- Der Produktnutzer ist ein selbst hostender Administrator. Seine vollen Produktrechte sind keine pauschalen Befugnisse des Assistenten (`PRODUCT.md:11`).
- Produktive Sync-/Bisync-Läufe können Daten löschen und verlangen die bestehenden Sicherheitsbedingungen und bewusste Bestätigung. Servervalidierung bleibt maßgeblich; ein UI-Dialog ersetzt sie nicht (`PRODUCT.md:26`, `PRODUCT.md:31`).
- Löschlimits, Authentifizierung, CSRF, Konfigurationsrevisionen, Laufzeitsperren und Pfad-/Mountschutz bleiben erhalten. Eine gemeinsame Autonomieregel erlaubt weder ihren Abbau noch einen realen Backup-/Restore-Lauf ohne passenden Auftrag.
- `ios/README.md:45` beschreibt Tags im Format `ios-vX.Y.Z` als Releaseauslöser. Tags sind daher keine harmlose Dokumentationsaktion. GitHub-Prüfung, Codemagic-Signierung, Upload und tatsächliche Store-Veröffentlichung bleiben getrennte Nachweise.
