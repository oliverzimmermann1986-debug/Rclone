# Herkunft und Lizenzen

`ui-craft` ist ein **abgeleitetes Werk**: `SKILL.md` und die fünf Dateien unter
`references/` (`motion.md`, `direction.md`, `gsap.md`, `audit.md`, `tooling.md`)
sind aus den vier unten genannten Quellen **destilliert, gekürzt, neu
strukturiert und ins Deutsche übertragen** — sie sind nicht der Originaltext.
Alles unter `references/verbatim/` ist dagegen unverändert übernommen.

| Quelle | Repository | Lizenz | Was daraus stammt |
|---|---|---|---|
| GSAP Skills (GreenSock) | github.com/greensock/gsap-skills | MIT, © 2026 GreenSock | `references/gsap.md` |
| taste-skill (Leonxlnx) | github.com/Leonxlnx/taste-skill | MIT, © 2026 Leonxlnx | `references/direction.md`, Teile von `tooling.md`, `references/verbatim/{brandkit,imagegen-frontend-web,imagegen-frontend-mobile,image-to-code-skill}` |
| skills (Emil Kowalski) | github.com/emilkowalski/skills | MIT, © 2026 Emil Kowalski | `references/motion.md`, `references/audit.md`, `references/tooling.md` |
| impeccable (Paul Bakaus) | github.com/pbakaus/impeccable | Apache License 2.0 | `references/verbatim/impeccable/**` (unverändert), Prozess-Abschnitt in `audit.md` |

## Pflichten, die daraus folgen

* **MIT:** Lizenztext und Copyright-Hinweis müssen mitgeliefert werden — dafür
  ist diese Datei da. Volltexte: siehe die jeweiligen Repositories
  (identischer MIT-Standardtext, Copyright wie oben).
* **Apache 2.0 (impeccable):** Attribution wie oben, und **Änderungen sind zu
  kennzeichnen**. Kennzeichnung: `references/verbatim/impeccable/` ist
  unverändert; die Prozess-Zusammenfassung in `references/audit.md` ist eine
  gekürzte Wiedergabe durch Dritte und nicht Teil des Originals.
* Beim Weitergeben (auch intern an externe Entwickler) diese Datei mitgeben.

## Aktualisieren

Die Quellen entwickeln sich weiter. Beim Nachziehen: Repos neu klonen, die
verbatim-Ordner ersetzen, und die fünf destillierten Dateien gegen die neuen
Originale gegenprüfen — der Merge ist Handarbeit, kein Generat.

Stand der Übernahme: 2026-07-28.
