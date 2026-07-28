---
name: craft-animated-interfaces
description: Design, redesign, critique, prototype, implement, and polish distinctive frontend interfaces with purposeful motion. Use for landing pages, portfolios, dashboards, product UI, app shells, components, design systems, image-to-code work, micro-interactions, gesture-driven UI, scroll animation, or GSAP in React, Vue, Svelte, and vanilla JavaScript. Coordinate visual direction, UX clarity, motion craft, accessibility, responsiveness, and performance. Do not use for backend-only or non-UI work.
---

# Craft Animated Interfaces

Coordinate the installed Taste, Impeccable, Emil Kowalski, and official GSAP skills as one design-and-motion workflow. Produce a coherent interface instead of averaging every source into one visual style.

## Start with a design read

Inspect the brief, existing product, codebase conventions, assets, content, and constraints before proposing a direction. Preserve working behavior and recognizable brand choices unless the user explicitly requests replacement.

State a compact design read before substantial design work:

- audience and primary user action
- product character and visual archetype
- information density
- motion intensity
- evidence from the brief or current interface
- implementation and accessibility constraints

Ask one concise question only when a missing choice would materially change the result. Otherwise make a reversible assumption and continue.

## Select the operating mode

Choose one mode and keep its mutation boundary explicit:

1. **Audit**: Inspect and report prioritized findings. Do not modify source files.
2. **Direction**: Define the design system, page rhythm, components, and motion language.
3. **Prototype**: Build genuinely different variants behind an easy comparison surface.
4. **Build**: Implement a new interface from the brief and existing project conventions.
5. **Redesign**: Audit first, preserve product behavior, then implement targeted or full changes.
6. **Polish**: Improve hierarchy, states, copy, responsiveness, accessibility, and motion without changing the product concept.
7. **Image-first**: Generate or analyze visual references before implementation when the user explicitly wants concept imagery or when a visually critical brief genuinely benefits from it.

Do not turn an audit request into an implementation request. Do not generate imagery merely to avoid making design decisions in code.

## Route to specialist knowledge

Use this skill as the coordinator. Load only the companion skill needed for the current subproblem from the installed skills directory (`$CODEX_HOME/skills`, or `~/.codex/skills` when `CODEX_HOME` is unset).

| Need | Load |
| --- | --- |
| Broad frontend direction, anti-generic design, redesign | `taste-skill/SKILL.md` and `impeccable/SKILL.md` |
| A named visual style | The matching Taste skill, such as `minimalist-skill`, `soft-skill`, or `brutalist-skill` |
| Image-led web/mobile concepts or image-to-code | `imagegen-frontend-web`, `imagegen-frontend-mobile`, or `image-to-code-skill` |
| Motion philosophy, interaction feel, gesture behavior | `emil-design-eng` and, for Apple-like physicality, `apple-design` |
| Motion audit or opportunity discovery | `review-animations`, `improve-animations`, or `find-animation-opportunities` according to mutation boundary |
| Variant picker | `prototype` |
| Animation terminology | `animation-vocabulary` |
| GSAP implementation | The smallest relevant set among `gsap-core`, `gsap-timeline`, `gsap-scrolltrigger`, `gsap-react`, `gsap-frameworks`, `gsap-plugins`, `gsap-utils`, and `gsap-performance` |

Read [design-direction.md](references/design-direction.md) when defining layout, typography, color, components, or page art direction. Read [motion-engineering.md](references/motion-engineering.md) when specifying or implementing motion. Read [quality-gates.md](references/quality-gates.md) before final verification. Consult [sources.md](references/sources.md) for source provenance and the installed companion inventory.

## Resolve conflicts consistently

Apply priorities in this order:

1. Follow the user's explicit goals and the repository's local instructions.
2. Protect accessibility, usability, correctness, and performance.
3. Preserve established product behavior and brand signals in redesigns.
4. Follow official GSAP API, lifecycle, registration, cleanup, and performance guidance.
5. Prefer problem-specific interaction craft over decorative novelty.
6. Use aesthetic heuristics only after the above constraints are satisfied.

Treat anti-pattern lists as diagnostic prompts, not universal bans. Do not force asymmetry, dark mode, huge typography, bento grids, random layouts, scroll-jacking, or constant animation without evidence from the brief.

## Build the interface system

Define a small token layer before styling isolated components:

- semantic color roles and contrast targets
- type families, scale, line length, leading, and weight hierarchy
- spacing rhythm and container behavior
- radii, borders, shadows, and material rules
- interaction states and focus treatment
- breakpoints derived from content failure points
- motion durations, easings or springs, distances, and reduced-motion behavior

Create hierarchy with typography, spacing, alignment, and content order before adding decoration. Prefer one strong visual idea and a few repeated motifs over many unrelated effects.

Use realistic content. Avoid fake metrics, generic testimonials, placeholder names, redundant badges, and deeply nested card surfaces unless the domain requires them.

## Design motion before coding it

For every animation, specify:

- trigger and user intent
- property and visual distance
- duration or spring behavior
- easing
- interruption and reversal behavior
- exit behavior
- reduced-motion fallback
- cleanup and lifecycle ownership

Reject motion that has no feedback, orientation, continuity, hierarchy, or perceived-performance purpose. Keep frequent interactions fast and interruptible. Reserve expressive sequencing for low-frequency moments.

Use CSS transitions for simple state changes. Use WAAPI or a framework motion tool when it best matches existing project conventions. Use GSAP for coordinated timelines, scroll-driven animation, advanced SVG/text/plugin work, or framework-agnostic sequencing.

When using GSAP:

- register plugins once
- scope selectors to the component
- clean up contexts, triggers, and listeners on unmount
- prefer transforms and opacity over layout properties
- refresh scroll measurements after material layout changes
- use `useGSAP()` in React when available
- respect `prefers-reduced-motion`

## Verify the result

Inspect the rendered interface, not just source code. Test representative desktop and mobile widths, keyboard navigation, focus visibility, content overflow, empty/loading/error states when applicable, and reduced motion.

Run the project's relevant checks. Distinguish pre-existing failures from regressions introduced by the work. For animation, inspect start and end states, rapid repeated input, interruption, resize behavior, unmount cleanup, and performance under load.

Finish with a concise record of:

- chosen direction and why
- material changes
- checks performed
- remaining limitations or assumptions

Do not claim visual fidelity or smoothness without rendering and inspecting the result.
