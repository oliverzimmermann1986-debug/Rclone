# audit — Bestehendes bewerten

Gemergt aus `review-animations`, `find-animation-opportunities`,
`improve-animations` (Emil Kowalski) und der Prozess-Ebene von `impeccable`.
Alle drei Animations-Skills sind **read-only**: sie planen und bewerten, sie
implementieren nicht. Wer implementiert, arbeitet nach `motion.md`/`gsap.md`.

## Welcher Modus?

| Frage des Nutzers | Modus |
|---|---|
| „Review meines Diffs / dieser Komponente" | **Diff-Review** (unten, Standards 1–10) |
| „Was könnte hier animieren?" / „lebendiger machen" | **Opportunity-Scan** (Gate unten) |
| „Audit die Motion der App" / „Roadmap" | **Bestandsaufnahme** (4 Phasen unten) |
| „Ist das Design gut?" (nicht nur Motion) | `verbatim/impeccable` |

## Diff-Review: zehn Standards

Jede Zeile im Diff gegen diese Liste prüfen — Verstöße sind Eskalationsauslöser,
nicht Geschmacksfragen:

1. `transition: all` (unbegrenzte Property-Animation)
2. `scale(0)`-Eintritt oder reiner Fade ohne Anfangs-Transform
3. `ease-in` bei irgendeiner UI-Interaktion; schwache Built-in-Kurve bei
   bewusst gesetzter Animation
4. Animation auf Tastenkürzel, Command-Palette oder 100+×/Tag-Aktion
5. UI-Dauer > 300 ms ohne genannten Grund
6. `transform-origin: center` an einem Trigger-verankerten
   Popover/Dropdown/Tooltip
7. Keyframes für Toasts/Toggles oder alles, was schnell wiederholt ausgelöst wird
   (nicht unterbrechbar)
8. Animation von Layout-Properties (`width/height/margin/padding/top/left`)
9. Motion-Props (`x/y/scale`), die laufen, während die Seite beschäftigt ist
10. Fehlendes `prefers-reduced-motion` bei Slide/Spring/Parallax

**Ausgabeformat (Pflicht):** eine Markdown-Tabelle, eine Zeile pro Befund.

| Before | After | Why |
| --- | --- | --- |
| `transition: all 300ms` | `transition: transform 200ms var(--ease-out)` | Nur benannte Properties; `all` animiert auch Layout |
| `transform: scale(0)` | `transform: scale(0.95); opacity: 0` | Nichts erscheint aus dem Nichts |
| `ease-in` am Dropdown | `ease-out` mit eigener Kurve | `ease-in` verzögert genau den beobachteten Moment |

Keine „Before:/After:"-Listen, keine Prosa-Absätze pro Befund.

## Opportunity-Scan: das Gate

Ein Vorschlag darf nur durch, wenn **alle drei** Fragen bestanden sind:

1. **Frequenz** — wie oft sieht ein Nutzer das? 100+×/Tag ⇒ ablehnen.
   Dutzende ⇒ nur reduzieren. Gelegentlich ⇒ Standard. Selten/erstmalig ⇒ darf
   Freude machen.
2. **Zweck** — Feedback, räumliche Konsistenz, Zustandswechsel, Bruchvermeidung,
   Erklärung. **Delight nur** in der Stufe „selten/erstmalig".
3. **Budget** — passt es in die Dauertabelle aus `motion.md`?

Ausgabe je Vorschlag: Ort (`Datei:Zeile`), Auslöser, Property, konkrete Werte
(Dauer, Kurve, Transform), Frequenzstufe, Zweck — plus eine ausdrückliche
**Ablehnungsliste**: was hier bewusst *nicht* animiert werden soll. Der Wert des
Scans liegt genauso in den Ablehnungen wie in den Vorschlägen.

## Bestandsaufnahme: vier Phasen

1. **Recon** (immer zuerst): Stack und Motion-Bibliotheken (Motion/Framer,
   React Spring, GSAP, CSS, WAAPI), Komponentenbibliotheken (Radix, Base UI,
   shadcn). Wo Motion lebt: CSS-Tokens (`--ease-*`, `--duration-*`),
   Tailwind-Config, Keyframes, `transition`/`animate`-Props, Gesten-Handler.
   Bestehende Konventionen erfassen — Pläne **erweitern** sie, statt ein
   Parallelsystem zu erfinden. Persönlichkeit des Produkts (spielerische
   Consumer-App vs. nüchternes Dashboard) und **Frequenzkarte** (was wird
   100+×/Tag getroffen?) bestimmen die Schwere der Befunde.
2. **Audit** — nach Dimensionen parallel: Kurven/Dauern, Unterbrechbarkeit,
   Layout-Properties, Kohäsion (viele Einzellösungen statt Tokens),
   Barrierefreiheit, Performance-Hotspots.
3. **Vetten und priorisieren** — jeden Befund gegen das Gate; nach
   Wirkung × Aufwand sortieren; Zweifelhaftes verwerfen statt „vielleicht"
   liefern.
4. **Pläne schreiben** — je Befund ein selbsttragender Plan mit Datei, exakten
   Werten und Abnahmekriterium, ausführbar auch von einem günstigeren Modell.

## impeccable-Prozess (wenn es um mehr als Motion geht)

Für vollständige Design→Build→Review-Zyklen an Produkt-UI die verbatim-Fassung
lesen: `verbatim/impeccable/SKILL.md` (plus `reference/` und `scripts/` im
selben Ordner). Kurzform des Ablaufs: Richtungsvertrag festlegen → Comp/Entwurf
abstimmen → bauen → gegen Vertrag und Qualitätsbalken reviewen → Design-System
dokumentieren. Der Skill bringt eigene Subagenten-Rollen mit (Finish-Reviewer,
Documenter, Asset-Producer); die sind in dieser Umgebung als Agent-Typen
verfügbar.
