# Quality gates

## Before implementation

- Confirm the operating mode and mutation boundary.
- Inspect repository instructions and existing conventions.
- Identify the primary user action and content hierarchy.
- Record the design read and three qualitative dials.
- Decide whether a real design system, a local token system, or a named aesthetic is appropriate.
- Write a motion purpose for every planned animation.

## During implementation

- Keep semantic HTML and keyboard behavior intact.
- Use semantic tokens instead of unrelated hard-coded values.
- Implement all interaction states, including focus and disabled states.
- Keep responsive behavior content-led.
- Scope animation selectors and lifecycle ownership.
- Prefer transform and opacity for motion.
- Avoid invented product facts and generic placeholder copy.

## Visual verification

- Render and inspect representative desktop and mobile sizes.
- Check first viewport hierarchy and primary action.
- Check long text, translated-like expansion, and content overflow.
- Check empty, loading, error, success, and selected states when relevant.
- Check contrast, keyboard order, focus visibility, and target sizes.
- Check reduced motion and zoom.
- Compare image-to-code work against the reference at the system level: grid, type, spacing, color, imagery, and state behavior.

## Motion verification

- Inspect first and final frames.
- Trigger interactions rapidly and repeatedly.
- Interrupt and reverse transitions.
- Resize during and after animation.
- Navigate or unmount while animation is active.
- Confirm scroll measurements refresh after layout changes.
- Look for layout shifts, dropped frames, lingering listeners, and orphaned triggers.

## Engineering verification

- Run the narrowest relevant formatter, type checker, tests, and build.
- Report pre-existing failures separately.
- Confirm no accidental dependency or lockfile changes.
- Inspect the final diff for unrelated edits.

## Final verdict

Do not declare completion until the result is rendered and the relevant gates pass. Report any unverified visual, device, or browser behavior as a limitation.
