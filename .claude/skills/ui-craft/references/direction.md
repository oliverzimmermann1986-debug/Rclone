# direction — Richtung finden, Slop vermeiden

Gemergt aus `taste-skill` (v2) mit den Ästhetik-Derivaten `minimalist-skill`,
`brutalist-skill`, `soft-skill`, dem `redesign-skill` und der Design-System-Ebene
von `stitch-skill`/`gpt-tasteskill`. Für Landing Pages, Portfolios, Marketing
und Redesigns. **Nicht** für Dashboards/Datentabellen/mehrstufige Produkt-UI —
dafür `verbatim/impeccable`.

## 1. Brief lesen, bevor irgendetwas entsteht

Signale in dieser Reihenfolge: **Seitentyp** (Landing SaaS/Consumer/Agency,
Portfolio Dev/Designer, Redesign preserve vs. overhaul, Editorial) ·
**Vibe-Wörter** des Nutzers („minimalistisch", „Linear-style", „Awwwards",
„brutalistisch", „premium", „Apple-y", „ernstes B2B") · **Referenzen**
(verlinkte URLs, Screenshots, genannte Produkte) · **Zielgruppe** (die wählt die
Ästhetik, nicht der eigene Geschmack) · **vorhandene Brand-Assets** (bei
Redesigns Ausgangsmaterial, nicht optional) · **stille Randbedingungen**
(Accessibility-first, öffentlicher Sektor, regulierte Branche, Kinder) — die
**überschreiben** jede Ästhetik-Vorliebe.

Dann **eine Zeile** ausgeben: „Lese das als: ‹Typ› für ‹Zielgruppe›, ‹Vibe›,
Richtung ‹System/Ästhetik›." Nur bei echt divergierender Lesart **eine**
Rückfrage („Näher an Linear-clean oder Awwwards-experimentell?").

## 2. Drei Regler setzen

`DESIGN_VARIANCE` (1 Symmetrie … 10 Chaos) · `MOTION_INTENSITY` (1 statisch …
10 cinematisch) · `VISUAL_DENSITY` (1 Galerie … 10 Cockpit).

| Fall | VARIANCE | MOTION | DENSITY |
|---|---|---|---|
| Landing SaaS (mainstream) | 7 | 6 | 4 |
| Landing Agency/creative | 9 | 8 | 3 |
| Landing Premium-Consumer | 7 | 6 | 3 |
| Portfolio Designer/Studio | 8 | 7 | 3 |
| Portfolio Developer | 6 | 5 | 4 |
| Editorial/Blog | 6 | 4 | 3 |
| Öffentlicher Sektor | 3 | 2 | 5 |
| Redesign „preserve" | wie Ist | Ist+1 | wie Ist |
| Redesign „overhaul" | Ist+2 | Ist+2 | wie Ist |

Alle Layout-, Motion- und Dichte-Entscheidungen hängen an diesen Werten.

## 3. Typografie

* Display: `text-4xl md:text-6xl tracking-tighter leading-none`;
  Body: `text-base leading-relaxed max-w-[65ch]`.
* **`Inter` nicht als Default** — erst `Geist`, `Outfit`, `Cabinet Grotesk`,
  `Satoshi` erwägen. Inter ist richtig, wenn ausdrücklich „neutral/Linear-artig"
  gewünscht ist oder es um Accessibility-first/öffentlichen Sektor geht.
  Paarungen: Geist + Geist Mono · Satoshi + JetBrains Mono · Cabinet Grotesk +
  Inter Tight · GT America + IBM Plex Mono.
* **Serif ist als Default sehr unerwünscht.** „Fühlt sich kreativ/premium an"
  ist kein Grund — das ist der am häufigsten getestete AI-Tell. Für Agentur,
  Studio, moderne Marke, Premium-Consumer, Portfolio: **Sans-Display**
  (Geist Display, PP Neue Montreal, GT Walsheim, Cabinet Grotesk Display …).
  Ausdrücklich verboten als Default: `Fraunces`, `Instrument_Serif`.
* **Betonung innerhalb einer Headline** über Italic/Bold **derselben** Familie —
  nie ein fremdes Serif-Wort in eine Sans-Headline mischen.
* Italic + Unterlänge (`y g j p q`): `leading-none` schneidet ab —
  mindestens `leading-[1.1]` plus `pb-1` Reserve.
* **Hero-Skalendisziplin:** Schriftgröße und Bildgröße zusammen planen.
  Headline > 6 Wörter → nicht bei `text-7xl` anfangen; Normalfall
  `text-4xl md:text-5xl lg:text-6xl`, `text-6xl md:text-7xl` nur bei 3–5
  Wörtern. Eine vierzeilige Hero-Headline ist immer ein Größenfehler.

## 4. Farbe

Maximal **eine** Akzentfarbe, Sättigung standardmäßig < 80 %. Neutrale Basis
(Zinc/Slate/Stone) plus ein kontrastreicher Akzent (Emerald, Electric Blue,
Deep Rose, Burnt Orange …). **Die Lila-Regel:** kein AI-Purple/Blue-Glow, keine
automatischen Neon-Gradients, keine Purple-Button-Glows. Für
Premium-Consumer ist die übliche Beige-Creme-Palette als Reflex ebenfalls
verbrannt — sie macht jede Marke unsichtbar.

## 5. Layout-Disziplin (Verstöße = kaputt ausgeliefert)

* **Zickzack-Kappe:** höchstens **2** aufeinanderfolgende „Bild links / Text
  rechts"-Sektionen. Die dritte ist ein Fail — stattdessen Full-Width, Vertical
  Stack, Bento, Marquee.
* **Kein Split-Header als Default** (große Headline links, kleiner
  Erklärabsatz rechts) — eine Sektion, eine Botschaft; sonst vertikal stapeln
  (max. 65ch).
* **Hero enthält nur Value Prop + primäre CTA.** Raus aus dem Hero:
  Trust-Mikrostreifen, Logo-Wall („Used by"), Pricing-Teaser, Feature-Bullets,
  Avatar-Reihe — alles in eigene Sektionen darunter.
* **CTA:** Text passt am Desktop auf **eine** Zeile (primär max. 3 Wörter);
  umgebrochene CTAs sind ein Fail. **Ein Label pro Intent** — nicht „Get in
  touch" + „Contact us" + „Let's talk" auf derselben Seite.
* Kleine Mono-Uppercase-Labels über Headlines: maximal `ceil(Sektionen / 3)`.
* `min-h-[100dvh]` statt `h-screen` (iOS-Adressleiste). CSS-Grid statt
  `w-[calc(33%-1rem)]`-Flex-Mathematik.
* Keine handgemalten SVG-Icon-Pfade — fehlendes Glyph ⇒ zweite Icon-Bibliothek.
* Kontinuierliche Eingabewerte (Maus, Scroll, Pointer-Physik) **nie** in
  `useState` — `useMotionValue`/`useTransform`/`useScroll`, sonst rendert der
  Baum bei jeder Bewegung neu und bricht auf Mobilgeräten ein.

## 6. Design-System oder Ästhetik?

Echtes System nehmen, wenn der Brief eines nahelegt: shadcn/ui (eigene
Komponenten, Default-Look **nie** so ausliefern), GOV.UK Frontend/USWDS
(öffentlicher Sektor), Material/Fluent bei Vorgabe. Ist der Brief dagegen eine
**Ästhetik**, dann eine der drei Familien fahren:

* **minimalist** — warmes Monochrom, typografischer Kontrast, flache
  Bento-Grids, gedämpfte Pastelltöne. Keine Gradients, keine schweren Schatten.
* **brutalist** — Swiss-Print × Militär-Terminal: starre Raster, extremer
  Größenkontrast, utilitaristische Farbe, analoge Degradation. Für
  datendichte Dashboards, Portfolios, Editorial mit „declassified"-Anmutung.
* **soft/premium** — Agentur-Anmutung: definierte Fonts, großzügiges Spacing,
  weiche mehrschichtige Schatten, klare Kartenstruktur, zurückhaltende
  Animation. Blockiert bewusst die Billig-Defaults.

## 7. Redesign: erst auditieren, dann anfassen

1. Ist-Zustand aufnehmen: Palette, Typo, Spacing-System, Komponenten,
   Motion-Niveau — plus vorhandene Brand-Assets.
2. Generische AI-Muster benennen (Lila-Glow, drei gleiche Cards,
   Glasmorphismus, Inter+slate).
3. Regler aus Tabelle „preserve" oder „overhaul" setzen.
4. Upgraden **ohne Funktion zu brechen**: Klassennamen/DOM-Struktur nur ändern,
   wo nötig; jede Änderung begründbar.
5. Vor Abgabe mechanisch prüfen: Zickzack-Zähler, CTA-Umbrüche,
   Label-Duplikate, Mono-Label-Anzahl, Hero-Zeilen, Italic-Unterlängen,
   Dependency-Verifikation (existiert das Paket wirklich in der genannten
   Version?).
