---
name: ui-craft
description: Frontend-Handwerk — Design-Richtung, Politur, Motion und Animations-Audits in einem Skill. Gemergt aus impeccable (Paul Bakaus), taste-skill (Leonxlnx), den Design-Engineering-/Animations-Skills von Emil Kowalski und den offiziellen GSAP-Skills. Use when designing, redesigning, polishing, animating or reviewing ANY interface — landing pages, portfolios, product UI, dashboards, components, micro-interactions, scroll effects, springs/gestures, brand looks, image-to-code. Also use to decide whether something should animate at all, to pick easing/duration values, or to review UI code for polish.
---

# ui-craft

Vier Quellen, ein Skill. Die Regeln unten gelten **immer**; die Detailtiefe
steckt in fünf Referenzdateien, von denen pro Aufgabe **höchstens eine oder
zwei** gelesen werden.

| Datei | Wofür |
|---|---|
| `references/motion.md` | Ob/wie animieren: Entscheidungsrahmen, Easing- und Dauer-Tabellen, Springs, Gesten, CSS-Muster, reduced-motion |
| `references/direction.md` | Design-Richtung finden: Brief-Lesart, drei Regler, Anti-Slop-Verbote, Layout-Disziplin, Ästhetik-Familien, Redesign-Audit |
| `references/gsap.md` | GSAP implementieren: core/timeline/ScrollTrigger/React/Frameworks/Performance/Plugins/utils |
| `references/audit.md` | Bestehendes bewerten: Animations-Audit, Review-Format, impeccable-Prozess |
| `references/tooling.md` | Bibliothek wählen, Prototypen, Effekt-Vokabular, vollständige Ausgaben |
| `references/verbatim/` | Unverändert: `impeccable`, `brandkit`, `imagegen-frontend-web`, `imagegen-frontend-mobile`, `image-to-code-skill` — Prompt-Rezepte, die sich nicht destillieren lassen |

## Immer gültig (die zehn Punkte)

1. **Brief lesen, dann bauen.** Vor dem ersten Code eine einzeilige Design-Lesart
   nennen: „Lese das als: ‹Seitentyp› für ‹Zielgruppe›, ‹Vibe›, Richtung
   ‹System/Ästhetik›." Bei echter Mehrdeutigkeit **genau eine** Rückfrage,
   nie ein Fragenbündel — sonst entscheiden und loslegen.
2. **Anti-Default-Disziplin.** Nicht reflexhaft: AI-Lila-Gradients, zentrierter
   Hero über Dark-Mesh, drei gleiche Feature-Cards, Glasmorphismus überall,
   Endlos-Micro-Loops, Inter + slate-900, Cards-in-Cards-in-Cards,
   Zickzack-Layout mehr als zweimal in Folge.
3. **Erst fragen, ob überhaupt animiert wird.** Was ein Nutzer 100×/Tag sieht
   (Command-Palette, Tastaturaktionen), animiert **nicht**. Seltenes
   (Onboarding, Erfolg) darf auftragen. Jede Animation braucht einen Zweck:
   Feedback, Zustandswechsel, räumliche Konsistenz, Erklärung.
4. **Motion-Grundwerte:** Eintritt/Austritt → `ease-out`; Bewegung auf dem
   Schirm → `ease-in-out`; Hover/Farbe → `ease`; Dauerlauf → `linear`.
   **Nie `ease-in` für UI.** UI-Übergänge bleiben **unter 300 ms**
   (Button 100–160, Tooltip 125–200, Dropdown 150–250, Modal 200–500).
   Eigene Kurven statt der schwachen CSS-Defaults:
   `--ease-out: cubic-bezier(.23,1,.32,1)`,
   `--ease-in-out: cubic-bezier(.77,0,.175,1)`,
   `--ease-drawer: cubic-bezier(.32,.72,0,1)`.
5. **Nur benannte Properties animieren** (`transition: transform 200ms
   var(--ease-out)`, nie `all`) und nur **Transform + Opacity** — keine
   `top/left/width/height`. `will-change` sparsam und gezielt.
6. **Physik dort, wo angefasst wird.** Alles, was der Nutzer zieht/wischt,
   bekommt eine **Spring** statt fixer Dauer: Standard kritisch gedämpft
   (`bounce: 0`/`damping 1.0`, `duration ~0.3–0.4 s`), Überschwingen
   (`bounce .1–.3`) nur, wenn die Geste selbst Impuls hatte. Unterbrechbarkeit
   ist das wichtigste Prinzip — Geschwindigkeit beim Richtungswechsel
   mitnehmen, nicht hart neu starten. Deshalb auch: **CSS-Transitions statt
   Keyframes** für alles, was schnell wiederholt ausgelöst wird.
7. **Die kleinen Wahrheiten:** nichts erscheint aus `scale(0)` — aus ~`.95`
   plus Opacity; Popover skaliert **vom Trigger** (`transform-origin`), Modal
   bleibt zentriert; Pressbares braucht `:active { transform: scale(.97) }`;
   Tooltips verzögern beim ersten, öffnen danach sofort; `translateY(100%)`
   statt Pixelwerten; `filter: blur(2px)` (max 20) rettet unsaubere Crossfades.
8. **`prefers-reduced-motion: reduce` respektieren** — Slides/Springs/Parallax
   werden zu kurzen Opacity-Crossfades, Überschwingen fällt weg. Ebenso
   `prefers-reduced-transparency`. Bei GSAP über `gsap.matchMedia()`.
9. **Zugänglichkeit schlägt Ästhetik.** Kontrast, sichtbarer Fokus, echte
   Buttons/Labels, `min-h-[100dvh]` statt `h-screen`, CSS-Grid statt
   Flex-Prozentrechnung, keine handgemalten SVG-Icons. Eine Oberfläche, die
   axe-critical wirft, ist nicht fertig.
10. **Review als Tabelle, Ausgabe vollständig.** UI-/Motion-Reviews immer als
    `| Before | After | Why |`, eine Zeile pro Befund. Nie
    Platzhalter-Code (`/* ... */`), nie abgeschnittene Dateien.

## Routing

* **Produkt-UI, Dashboards, App-Shells, ganzer Design→Build→Review-Prozess** →
  `references/verbatim/impeccable/SKILL.md`
* **Landing/Portfolio/Marketing/Redesign, „soll nicht templated aussehen"** →
  `references/direction.md`
* **Komponenten-Feinschliff, Micro-Interactions, „fühlt sich billig an"**,
  Gesten/Sheets/Drag, Easing- oder Dauerfrage → `references/motion.md`
* **Animation implementieren** — Sequenz, Scroll-Antrieb, Framework-Einbindung,
  Ruckeln → `references/gsap.md`. Vorher prüfen: einfache Zustandswechsel
  gehören in CSS-Transitions, dafür kein GSAP.
* **„Was könnte hier animieren?", Motion-Audit, Diff-Review** →
  `references/audit.md`
* **Bibliothekswahl, Wegwerf-Prototyp, „wie heißt dieser Effekt?"** →
  `references/tooling.md`
* **Referenzbilder/Brand-Boards erzeugen, Bild → Code** →
  `references/verbatim/{brandkit,imagegen-frontend-web,imagegen-frontend-mobile,image-to-code-skill}/SKILL.md`

Mehrere Aufgaben in einem Auftrag: Richtung → Feinschliff → Motion.
