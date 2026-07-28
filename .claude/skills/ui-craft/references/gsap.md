# gsap — implementieren

Gemergt aus den acht offiziellen GSAP-Skills (GreenSock): core, timeline,
scrolltrigger, react, frameworks, performance, plugins, utils.

## Wann GSAP, wann CSS

CSS-Transitions reichen für einfache Zustandswechsel (Hover, Dialog auf/zu,
Akkordeon) — und sind dort **besser**, weil unterbrechbar und ohne JS-Kosten.
GSAP nimmt man für: Sequenzen/Choreografie, scrollgetriebene Animation,
Morphing/SVG/Physik, framework-agnostische Wiederverwendung, präzise
Playback-Kontrolle (`pause/reverse/timeScale/seek`). GSAP läuft in jedem
Framework und in Vanilla JS (und treibt Webflow-Interaktionen).

## Core

```js
gsap.to(t, vars)              // von jetzt nach vars — der Normalfall
gsap.from(t, vars)            // von vars nach jetzt — gut für Eintritte
gsap.fromTo(t, fromV, toV)    // explizit, liest keinen Ist-Zustand
gsap.set(t, vars)             // sofort (duration 0)
```

Wichtige vars: `duration` (s, Default 0.5) · `delay` · `ease`
(`"power1.out"` Default, `"power3.inOut"`, `"back.out(1.7)"`,
`"elastic.out(1,0.3)"`, `"none"`) · `stagger` (`0.1` oder
`{ amount: .3, from: "center" }`, `{ each: .1, from: "random" }`) ·
`repeat`/`yoyo` · Callbacks `onStart/onUpdate/onComplete`.
`gsap.defaults({...})` setzt Projekt-Standards.

Die Motion-Doktrin gilt weiter: `power2.out`-Klasse für Eintritte, unter 300 ms
für UI, kein Überschwingen ohne Gesten-Impuls (`motion.md`).

## Timeline

```js
const tl = gsap.timeline({ defaults: { duration: .4, ease: "power2.out" } });
tl.to(a, { x: 100 })
  .to(b, { y: 50 }, "<")        // gleichzeitig mit dem vorigen Start
  .to(c, { opacity: 1 }, "-=.2")// 0.2 s vor Ende des vorigen
  .addLabel("mitte")
  .to(d, { scale: 1 }, "mitte+=.3");
```

Positionsparameter: absolut (`1`), relativ (`"+=.5"`, `"-=.2"`), Label
(`"name+=.3"`), Platzierung (`"<"` Start des zuletzt Hinzugefügten, `">"` dessen
Ende = Default). Optionen: `paused: true`, `repeat`, `yoyo`, `defaults`.
Timelines lassen sich verschachteln und als Ganzes steuern.

## ScrollTrigger

```js
gsap.registerPlugin(ScrollTrigger);
gsap.to(".panel", {
  xPercent: -100,
  ease: "none",
  scrollTrigger: {
    trigger: ".wrap", start: "top top", end: "+=2000",
    scrub: 1, pin: true, anticipatePin: 1,
  },
});
```

Kernoptionen: `trigger` · `start`/`end` (`"top bottom"`-Syntax, Default
`"top bottom"` bzw. `"top top"` bei Pin) · `endTrigger` · `scrub` (`true` =
direkt, Zahl = Sekunden Nachlauf) · `toggleActions`
(onEnter/onLeave/onEnterBack/onLeaveBack, Default `"play none none none"`) ·
`pin` (**nie das gepinnte Element selbst animieren — Kinder animieren**),
`pinSpacing` · `horizontal` · `markers: true` nur zur Entwicklung.
Bei scrub-Animationen `ease: "none"`, sonst fühlt sich der Scroll ungleichmäßig
an. Nach Layout-Änderungen `ScrollTrigger.refresh()`.

## React / Next.js

```jsx
import { useGSAP } from "@gsap/react";
gsap.registerPlugin(useGSAP);           // vor jedem GSAP-Code registrieren

const containerRef = useRef(null);
useGSAP(() => {
  gsap.from(".item", { opacity: 0, stagger: .1 });
}, { scope: containerRef });            // Selektoren bleiben im Container
```

`useGSAP` räumt beim Unmount automatisch auf (ersetzt manuelles
`gsap.context()` + `revert()`). Ohne den Hook: `const ctx = gsap.context(fn,
ref)` im `useEffect` und `ctx.revert()` im Cleanup. Event-Handler, die
Animationen erzeugen, mit `contextSafe()` wrappen. In SSR nichts im
Modulscope animieren — alles in den Hook.

**Vue/Svelte:** Setup in `onMounted`/`onMount`, Aufräumen in
`onUnmounted`/`onDestroy`; Selektoren auf das Komponenten-Root scopen
(`gsap.context(fn, rootEl)`), sonst greifen Animationen in fremde Instanzen.

## Responsive & reduced-motion

```js
const mm = gsap.matchMedia();
mm.add("(min-width: 800px)", () => { /* Desktop-Setup */ });
mm.add("(prefers-reduced-motion: reduce)", () => { /* minimal/keine Animation */ });
```

`gsap.matchMedia()` (3.11+) führt Setup nur bei passender Query aus und
**revertet automatisch alles**, was darin erzeugt wurde — der saubere Weg für
Breakpoints und Barrierefreiheit.

## Performance

Nur `transform`/`opacity` animieren (GSAP schreibt `x/y/scale/rotation` in eine
Matrix), keine Layout-Properties. `will-change` gezielt und temporär. Lesen und
Schreiben nicht verschränken (Layout-Thrashing) — GSAP batcht selbst, eigene
`getBoundingClientRect()`-Aufrufe bündeln. Bei vielen Elementen `stagger` statt
N Einzeltweens, bei Listen `ScrollTrigger.batch()`. Häufig aktualisierte Werte
(Mausverfolger) über `gsap.quickTo()`/`quickSetter()` statt neuer Tweens pro
Event. `pin` nur wo nötig; `scrub: 1` statt `true` reduziert Arbeit pro Frame.
Auf schwacher Hardware messen, nicht auf dem Entwicklungsrechner.

## Plugins (registrieren, dann nutzen)

`gsap.registerPlugin(ScrollTrigger, Flip, Draggable, SplitText, …)`.

* **ScrollToPlugin / ScrollSmoother** — Sprünge zu Positionen, geglättetes
  Scrollen (letzteres sparsam: es entkoppelt native Scroll-Semantik).
* **Flip** — Layoutwechsel messen und animieren (Karte → Detailansicht).
* **Draggable + InertiaPlugin** — Ziehen mit Impuls; passt zur Spring-/Velocity-
  Doktrin aus `motion.md`.
* **Observer** — einheitliche Wheel/Touch/Pointer-Ereignisse ohne Scroll-Hijack.
* **SplitText** — Zeichen/Wörter/Zeilen. Nur splitten, was gebraucht wird
  (`type: "words,chars"`), `autoSplit: true` + Animation **in** `onSplit()`
  erzeugen und zurückgeben (Re-Split bei Font-Load/Resize), `aria: "auto"`
  lässt Screenreader das Label lesen statt der Einzelzeichen, `mask` für
  Reveal-Effekte.
* **MorphSVG** — Pfad-Morphing (`type: "rotational"` gegen Knicke,
  `map: "position"|"complexity"`, wenn Segmente nicht passen).
* **CustomEase / CustomWiggle / CustomBounce**, **GSDevTools** (Entwicklung),
  Physik- und Text-Plugins.

## utils

`gsap.utils.clamp/mapRange/normalize/interpolate/random/snap/toArray/wrap/pipe/
distribute/selector/splitColor/unitize` — statt eigener Hilfsfunktionen.
Typische Kombination: `pipe(clamp(0,1), mapRange(0,1,0,100))`;
`toArray()` normalisiert Selektor/NodeList/Array; `random(-20,20,1)` liefert
gerasterte Zufallswerte für Deko-Variation.
