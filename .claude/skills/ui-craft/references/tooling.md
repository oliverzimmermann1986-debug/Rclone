# tooling — Bibliothek, Prototyp, Vokabular, Ausgabedisziplin

Gemergt aus `pick-ui-library`, `prototype`, `animation-vocabulary`
(Emil Kowalski) und `output-skill` (taste-Familie).

## 1. Bibliothekswahl (kuratiert, meinungsstark)

Nicht selbst bauen, was hier schon steht — und nicht drei Bibliotheken für
dieselbe Aufgabe mischen.

| Aufgabe | Bibliothek |
|---|---|
| Ungestylte, zugängliche Primitive (Dialog, Popover, Menu, Select) | base-ui |
| Command-Menü (⌘K) | cmdk |
| Toasts | Sonner |
| OTP-/Code-Eingabe | input-otp |
| Regler-/Kontrollpanels (Dev-GUIs) | Leva (Alternative: dialkit) |
| Allzweck-Animation (Springs, Layout, Enter/Exit) | motion (Framer Motion) |
| Animierte Zahlen (Counter, Preise, Stats) | NumberFlow |
| Animierter Text | torph |
| 3D-Globus | Cobe |
| OG-Bilder aus HTML/CSS | Satori |
| Syntax-Highlighting | shiki |
| Charts (Dashboards, statisch/interaktiv) | recharts |
| Echtzeit-/Streaming-Charts | Liveline |
| Drag & Drop | dnd kit |
| Virtualisierung (lange Listen, große Tabellen) | Virtuoso |
| State-Management | zustand |
| Bedingte `className`-Strings | clsx |
| Variantenbasiertes Tailwind-Styling | cva |
| Theme-/Dark-Mode-Wechsel ohne Flash | next-themes |

Scroll-Choreografie, Morphing, SVG-Physik, framework-agnostische
Wiederverwendung → GSAP (`gsap.md`). Einfache Zustandswechsel → CSS.

**Vor der Empfehlung prüfen**, ob das Paket im Projekt vorhanden bzw. in der
genannten Version verfügbar ist — keine erfundenen Versionen, keine Bibliothek
zusätzlich installieren, wenn eine gleichwertige schon im Projekt liegt.

## 2. Prototyp (Wegwerf-Varianten zum Auswählen)

Zweck: mehrere Richtungen für **ein** UI-Element nebeneinander zeigen, damit
entschieden werden kann — nicht Produktionscode.

1. **Scope** — genau ein Element/eine Interaktion, 3–4 Varianten.
2. **Recon** — Stack, vorhandene Tokens (Farben, Radien, Spacing, Fonts,
   `--ease-*`/`--duration-*`), Produkt-Persönlichkeit, Kontext (Hintergrund,
   Nachbarn, Größen). Jede Variante muss aussehen, als könnte sie morgen in
   *diesem* Produkt ausgeliefert werden — die Persönlichkeit begrenzt, wie weit
   die kühnste Variante gehen darf.
3. **Richtungen wählen** — bewusst unterschiedliche Hypothesen, nicht drei
   Abstufungen derselben Idee.
4. **Harness** — isolierte Route (`/prototypes/<slug>`), eine Datei je Variante
   plus kleine Auswahlseite. **Produktionscode importiert nichts daraus.**
   Nach der Entscheidung wandert die gewählte Variante als sauberer Code ins
   Produkt, der Prototyp wird entfernt.

## 3. Effekt-Vokabular (Nachschlagen, nicht Entwerfen)

Wenn jemand einen Effekt beschreibt, aber den Begriff nicht kennt — hier die
häufigsten; die Vollfassung mit allen Kategorien (Entrances/Exits, Sequencing,
Movement, State-Transitions, Scroll, Feedback, Easing) steckt in
`verbatim/`-freien Beispielen unten:

* **Stagger** — mehrere Elemente zeitversetzt nacheinander (Kaskade).
* **Origin-aware** — Element wächst aus seinem Trigger statt aus der Mitte.
* **Morph** — eine Form wird stufenlos zu einer anderen (Dynamic Island).
* **Rubber-banding** — Widerstand und Zurückschnappen über die Grenze hinaus
  (iOS-Overscroll).
* **Pop in** — kurzer Scale-Eintritt mit leichtem Überschwingen.
* **Scrub** — Animationsfortschritt hängt direkt am Scrollwert.
* **Pin** — Element bleibt stehen, während der Inhalt daran vorbeiscrollt.
* **Velocity handoff** — Loslass-Geschwindigkeit der Geste geht in die Spring.
* **Crossfade** — zwei Zustände überblenden (ggf. mit Blur-Brücke).

Zweck ist ausschließlich **Benennung** — für das Entwerfen/Umsetzen dann
`motion.md`.

## 4. Ausgabedisziplin

* **Kein Platzhalter-Code.** Nie `/* ... rest unchanged ... */`, nie
  `// TODO: implement`, nie „hier analog fortsetzen". Was gefordert ist, wird
  vollständig geschrieben.
* **Keine stillen Kürzungen.** Wenn eine Datei zu groß für eine Antwort ist:
  sauber an einer Datei-/Komponentengrenze schneiden, das Ende explizit
  benennen und im nächsten Schritt exakt dort fortsetzen — nicht mitten in
  einer Funktion abbrechen und nicht behaupten, es sei komplett.
* **Keine erfundenen Dateien, Pfade oder Paketversionen.** Vor dem Verweis
  prüfen, ob es existiert.
