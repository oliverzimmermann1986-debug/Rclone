# Gemeinsamer Regelkern v1

**Version v1; gilt erst durch ausdrückliche Einbindung.** Dieser Kern gilt in einem Zielprojekt erst nach geprüfter Übernahme und ausdrücklicher Einbindung durch dessen Anweisungseinstieg. Er ergänzt lokale Regeln als nachrangiger Default. Der konkrete Auftrag und die Anweisungshierarchie des Assistenten bleiben maßgeblich.

## Arbeiten am bestehenden Projekt

1. **Auftrag und Quelle klären.** Ziel, Umfang und Abnahmekriterium aus dem Auftrag bestimmen. Die lokale `AGENTS.md`, ihre Weiterleitungen, einschlägige Unterverzeichnisregeln und den Projektadapter lesen. Vorhandene Fachverträge behalten ihre Zuständigkeit.
2. **Belege vor Annahmen.** Den aktuellen Checkout, betroffene Implementierung und vorhandene Prüfungen lesen. Quelltext, ausgeführter Test, CI, Gerätetest und Produktion sind unterschiedliche Belege. Unsicherheit benennen und zunächst mit verfügbaren Quellen untersuchen.
3. **Bestehende Arbeit erhalten.** Fremde und eigene uncommittierte Änderungen vor Bearbeitung berücksichtigen. Nichts zurücksetzen, löschen, überschreiben oder bereinigen, um einen sauberen Stand zu erzwingen. Bei echter Kollision Zuständigkeit klären; ein schmutziger Arbeitsbaum allein ist kein Hindernis.
4. **Den zuständigen Code ändern.** Die kleinste vollständige Lösung am vorhandenen fachlichen Eigentümer umsetzen. Keine parallelen Regelimplementierungen oder spekulativen Plattformen anlegen. Lokale Architektur- und Freigabegrenzen bleiben wirksam.
5. **Prüfungen passend wählen.** Beobachtbares Verhalten und Fehlerursache prüfen; einen Bug möglichst zuerst reproduzieren. Lokale Testvorgaben und Release-Gates befolgen. Reine Textänderungen brauchen Inhalts- und Linkprüfung statt erfundener Verhaltenstests. Fehlende, fehlgeschlagene und übersprungene Prüfungen offen ausweisen.
6. **Befugnisse nicht erweitern.** Zugangsdaten, Produktrollen und Werkzeugzugriff sind keine pauschale Freigabe für Änderungen an Daten, Diensten oder fremden Konten. Commit, Push, Veröffentlichung, Deployment und destruktive Eingriffe nur innerhalb der tatsächlich erteilten Befugnis und der lokalen Regeln durchführen. Bestehende Autorisierung nicht erneut abfragen.
7. **Parallel klar arbeiten.** Unabhängige Aufgaben mit klaren Dateigrenzen und Rückgabekriterien delegieren, wenn es hilft. Gemeinsame Änderungen erhalten und Ergebnisse integrieren. Modellwahl und verfügbare Agentenzahl folgen der jeweiligen Umgebung und dem Auftrag; dieser Kern pinnt keine Modelle.
8. **Entscheidungen gezielt einholen.** Technische Fragen anhand von Belegen lösen. Nur nicht belegbare Produkt-, Präferenz- oder Befugnisentscheidungen zur Entscheidung vorlegen, mit konkreten Optionen und Folgen. Währenddessen unabhängige Arbeit fortsetzen.
9. **Nachvollziehbar übergeben.** Ergebnis, betroffene Dateien, ausgeführte Prüfungen, offene Grenzen und den nächsten notwendigen Schritt nennen. Bestehende Dokumentation am zuständigen Ort pflegen. Keine zusätzliche Markdown-Taskqueue, keine automatische Sicherung durch WIP-Commits und keine zweite Regelquelle einführen.

Den Ablauf für Auftraggeber und Entwickler beschreibt [ENTWICKLER.md](ENTWICKLER.md). Projektspezifische Quellen stehen im jeweils eingebundenen Adapter unter `projects/`.
