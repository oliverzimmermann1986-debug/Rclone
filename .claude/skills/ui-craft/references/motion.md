# motion — ob, wie schnell, mit welcher Kurve

Gemergt aus `emil-design-eng` und `apple-design` (Emil Kowalski / Apple WWDC
„Designing Fluid Interfaces"), plus den reduced-motion-Teilen der GSAP-Skills.

## 1. Soll das überhaupt animieren?

| Häufigkeit für den Nutzer | Entscheidung |
|---|---|
| 100+ ×/Tag (Tastenkürzel, Command-Palette) | **Nie** animieren |
| Dutzende ×/Tag (Hover, Listen-Navigation) | Entfernen oder drastisch reduzieren |
| Gelegentlich (Modal, Drawer, Toast) | Normale Animation |
| Selten/erstmalig (Onboarding, Erfolg, Feedback) | Darf Freude machen |

**Tastatur-ausgelöste Aktionen animieren nicht.** Raycast hat bewusst keine
Öffnen-Animation — das ist für etwas, das man hundertmal täglich macht, das
Optimum.

Zulässige Zwecke: **Feedback** (Button reagiert auf Druck), **Zustandswechsel**
(Button morpht in „gesendet"), **räumliche Konsistenz** (Toast kommt und geht
in derselben Richtung → Swipe-to-dismiss fühlt sich richtig an), **Erklärung**
(Marketing zeigt, wie etwas funktioniert), **Bruchvermeidung** (Elemente
erscheinen/verschwinden nicht hart). Ist der Zweck nur „sieht cool aus" und
der Nutzer sieht es oft: weglassen.

## 2. Kurve

```
Tritt es ein oder aus?      → ease-out   (startet schnell, wirkt reaktionsschnell)
Bewegt/morpht es on-screen? → ease-in-out
Hover-/Farbwechsel?         → ease
Dauerlauf (Marquee, Bar)?   → linear
sonst                       → ease-out
```

**Nie `ease-in` für UI.** Ein Dropdown mit `ease-in` bei 300 ms *fühlt* sich
langsamer an als `ease-out` bei 300 ms, weil genau der Moment verzögert wird,
den der Nutzer am genauesten beobachtet: der Anfang.

Die eingebauten CSS-Kurven sind zu schwach. Eigene verwenden:

```css
--ease-out:     cubic-bezier(0.23, 1, 0.32, 1);    /* UI-Interaktionen */
--ease-in-out:  cubic-bezier(0.77, 0, 0.175, 1);   /* Bewegung on-screen */
--ease-drawer:  cubic-bezier(0.32, 0.72, 0, 1);    /* iOS-artige Drawer */
```

(Kurven nicht selbst erfinden: easing.dev / easings.co.)

## 3. Dauer

| Element | Dauer |
|---|---|
| Button-Press-Feedback | 100–160 ms |
| Tooltip, kleines Popover | 125–200 ms |
| Dropdown, Select | 150–250 ms |
| Modal, Drawer | 200–500 ms |
| Marketing/erklärend | darf länger |

**Regel: UI bleibt unter 300 ms.** Wahrgenommene Geschwindigkeit ist real: ein
schneller drehender Spinner lässt dieselbe Ladezeit kürzer erscheinen; ein
180-ms-Select wirkt reaktionsschneller als 400 ms; Tooltips, die nach dem
ersten sofort erscheinen, machen die ganze Toolbar schneller.

## 4. Springs — für alles, was angefasst wird

Springs haben keine Dauer, sie *setzen sich*. Sie sind die richtige Wahl bei
Drag mit Impuls, Gesten, die mitten drin umgedreht werden, und Elementen, die
„lebendig" wirken sollen.

**Apples zwei Parameter statt der Physik-Triplets:** *Response* (wie schnell
der Zielwert erreicht wird, in Sekunden — **nicht** Dauer) und *Damping*
(Überschwingen).

```js
// Standard-UI: kritisch gedämpft, kein Überschwingen
{ type: "spring", bounce: 0,   duration: 0.4 }   // ≙ damping 1.0, response .3–.4
// Impulsgetrieben (Flick, Wurf, Drag-Release): leichter Überschwinger
{ type: "spring", bounce: 0.2, duration: 0.4 }   // ≙ damping ~0.8
```

Überschwingen nur, **wenn die Geste selbst Impuls hatte**. Ein Menü, das nur
eingeblendet wurde, darf nicht wackeln.

**Unterbrechbarkeit ist das wichtigste Prinzip.** Springs behalten ihre
Geschwindigkeit, wenn sie neu ausgerichtet werden; CSS-Keyframes starten bei
Null. Beim Richtungswechsel Geschwindigkeit **überblenden**, nicht hart
schneiden (sonst „Wand"). 2D-Bewegung in **zwei unabhängige Springs** für X
und Y zerlegen — eine einzelne Spring auf die 2D-Distanz desynchronisiert.

**Velocity-Handoff:** Die Loslass-Geschwindigkeit des Zeigers als
Anfangsgeschwindigkeit der Spring übergeben (Motion nimmt px/s direkt; bei
normalisierten APIs `gestureVelocity / (target − current)`). Weiter:
**Momentum-Projektion** — dorthin animieren, wo die Geste *hinwill*;
**Rubber-Banding** — an Grenzen progressiv widerstehen statt hart stoppen;
**Hinting** — in Gestenrichtung andeuten, was passieren wird.

Mausgetriebene Deko (Rotation, Parallax) über `useSpring` interpolieren statt
Werte direkt zu setzen — direkte Kopplung wirkt künstlich. Aber: Das ist
**Deko**. In einem funktionalen Diagramm wäre keine Animation besser.

## 5. CSS-Muster, die den Unterschied machen

```css
/* Pressbares reagiert sofort */
.button { transition: transform 160ms var(--ease-out); }
.button:active { transform: scale(0.97); }        /* 0.95–0.98 */

/* Nichts erscheint aus dem Nichts */
.entering { transform: scale(0.95); opacity: 0; } /* nie scale(0) */

/* Popover skaliert vom Trigger; Modal bleibt zentriert */
.popover { transform-origin: var(--transform-origin); }

/* Tooltip: erster verzögert, folgende sofort */
.tooltip { transition: transform 125ms var(--ease-out), opacity 125ms var(--ease-out); }
.tooltip[data-instant] { transition-duration: 0ms; }

/* Eintritt ohne JS */
.toast {
  opacity: 1; transform: translateY(0);
  transition: opacity 400ms ease, transform 400ms ease;
  @starting-style { opacity: 0; transform: translateY(100%); }
}

/* Unsauberen Crossfade mit Blur überbrücken (max 20px, in Safari teuer) */
.swapping { filter: blur(2px); opacity: .7; transition: filter 200ms ease, opacity 200ms ease; }
```

Weitere Regeln: **Transitions statt Keyframes**, wenn schnell wiederholt
ausgelöst wird (Transitions sind unterbrechbar und neu ausrichtbar).
`translateY(100%)` statt Pixelwerten — Prozente beziehen sich auf die
Elementgröße und passen sich dem Inhalt an. `scale()` skaliert Kinder mit (ist
ein Feature). Für Tiefe `rotateX/rotateY` mit `transform-style: preserve-3d`.

## 6. Zugänglichkeit

```css
@media (prefers-reduced-motion: reduce) {
  /* Slides/Springs/Parallax → kurze Opacity-Crossfades, kein Überschwingen */
}
@media (prefers-reduced-transparency: reduce) {
  /* Translucent-Flächen fester: Hintergrund-Opacity hoch, Blur weg */
}
```

Opacity-/Farbwechsel, die Verständnis stützen, bleiben. Bei GSAP läuft dasselbe
über `gsap.matchMedia()` (siehe `gsap.md`). Interaktionen bleiben in jedem
Fall unterbrechbar; Latenz ist der Feind — auf Pointer-Down reagieren, nicht
erst auf Click.
