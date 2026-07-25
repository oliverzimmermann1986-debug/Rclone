# Gesamtpatch 25.07.2026

## Backend
- Dashboard-Cache mit separatem Build-Lock gegen Cache-Stampede geschützt.
- Double-checked locking: wartende Requests übernehmen den frisch erzeugten Snapshot.

## GUI / UX
- UI-Helfer in eigenes Asset ausgelagert (Storage, Fokus, mobile Dichte).
- Umschaltbare komfortable/kompakte Darstellung mit persistenter Auswahl.
- Skeleton-Loading für Dashboard und Jobhistorie.
- Einheitliche, schließbare Toast-Meldungen mit Statussymbolen.
- Mobile Aktionsleisten bleiben oberhalb der Bottom-Navigation erreichbar.
- Mobile Abstände, Tabellen und kompakte Darstellung verbessert.
- ARIA-busy und Screenreader-Texte für asynchrone Listen ergänzt.

## Bewusst nicht geändert
Die bestehende Alpine-Auslieferung benötigt weiterhin die aktuelle CSP-Ausnahme. Ein Entfernen von `unsafe-eval` erfordert eine Migration auf Alpine CSP oder ein anderes Frontend-Buildsystem und wäre kein risikoarmer Patch.
