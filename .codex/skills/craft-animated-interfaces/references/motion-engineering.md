# Motion engineering

## Contents

- Motion gate
- Timing and behavior
- Interaction patterns
- GSAP routing
- Framework lifecycle
- Performance and accessibility

## Motion gate

Animate only to provide at least one of:

- immediate feedback
- spatial orientation
- continuity between states
- hierarchy and attention
- perceived performance
- direct manipulation or physical affordance

Reject movement that competes with the task, repeats too frequently, or exists only to signal visual sophistication.

## Timing and behavior

Keep common micro-interactions fast, usually within roughly 100–250 ms. Allow moderately longer transitions for overlays, navigation, or spatial changes. Use longer choreography only for infrequent, user-initiated moments.

Prefer asymmetric timing when it supports attention: entrances may be slightly expressive; exits should usually clear quickly. Make state transitions interruptible and reversible. Never make a user wait for an animation before the interface accepts the next input.

Use easing by behavior:

- ease-out for entering or responding
- ease-in for leaving
- ease-in-out for movement that remains visible throughout
- springs for direct manipulation, gesture handoff, and physical settling
- linear only for continuous progress or scrubbed relationships

Avoid `scale(0)` entrances. Preserve a readable shape and spatial origin. For popovers, sheets, and menus, align transform origin with the trigger or gesture.

## Interaction patterns

For drag and gesture work:

- track input one-to-one during manipulation
- capture the pointer
- hand off velocity into the settling motion
- use damping near boundaries
- allow interruption during the settle
- protect against unintended multi-touch behavior

For lists and repeated elements, stagger lightly and keep the total sequence short. For hover, confirm the device supports hover. For destructive actions, use motion to clarify commitment rather than dramatize it.

## GSAP routing

Use the smallest capable GSAP surface:

- `gsap-core`: tweens, easing, stagger, matchMedia
- `gsap-timeline`: sequencing, labels, nesting, playback
- `gsap-scrolltrigger`: scroll-linked motion, pinning, scrub, batching
- `gsap-react`: `useGSAP`, scoping, cleanup, SSR
- `gsap-frameworks`: Vue, Svelte, and other lifecycle patterns
- `gsap-plugins`: Flip, Draggable, SplitText, SVG, physics, and other plugins
- `gsap-utils`: clamp, mapRange, normalize, interpolate, snap, wrap
- `gsap-performance`: transform/opacity choices, batching, and jank reduction

Register plugins once per application boundary. Attach ScrollTrigger to a top-level tween or timeline. Refresh after layout changes that alter measurements. Kill triggers and revert contexts during cleanup.

## Framework lifecycle

Scope animation targets to a component container. Avoid global selectors inside reusable components.

In React, prefer `useGSAP()` with a scope and explicit dependencies. Make event callbacks context-safe. In Vue or Svelte, create animation after mount and revert or kill it during unmount. Guard browser-only code in server-rendered environments.

## Performance and accessibility

Prefer transforms and opacity. Batch DOM reads before writes. Avoid continuous layout-triggering properties. Use `quickSetter` or `quickTo` for high-frequency pointer-following updates when appropriate.

Define a reduced-motion path that removes large travel, parallax, pinning, and nonessential loops while preserving feedback and state clarity. Test resize, content changes, slow devices, rapid repeated input, and cleanup after route or component changes.
