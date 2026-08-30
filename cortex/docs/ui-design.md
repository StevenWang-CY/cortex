# UI design and interaction contract

Last reviewed: 2026-08-29.

This contract applies to the PySide6 desktop shell, Chrome/Edge extension,
injected browser surfaces, and VS Code webview. The refinement pass was
informed by Emil Kowalski's public [Skills for Design
Engineers](https://github.com/emilkowalski/skills), pinned during review to
commit `d23d7f88a2e21c9e4b1418c7abe420f5c1052ba7` (2026-08-21), especially
`emil-design-eng`, `review-animations`, `improve-animations`,
`find-animation-opportunities`, and `apple-design`.

The reference is a decision framework, not a visual theme. Cortex remains a
restrained, warm, information-dense support tool: data must be legible, user
authority explicit, and motion subordinate to comprehension.

## Principles

| Before-risk | Contract | Why |
| --- | --- | --- |
| Parallel per-surface colors/timings | One generated token source | Small drift compounds into an incoherent product |
| `transition: all` or multi-second entrances | Named properties and sub-300 ms functional motion | Avoid layout animation and sluggish controls |
| Keyframes replayed on dynamic updates | Interruptible transitions or in-place updates | Rapid changes should retarget, not restart |
| Motion everywhere | Animate only feedback, spatial continuity, state legibility, or a jarring discontinuity | Functional dashboards benefit from restraint |
| Reduced motion removes all feedback | Remove movement/continuous decoration; retain brief opacity/color feedback | Accessibility without making state changes invisible |
| Blur-only glass surfaces | Opaque semantic-control fallback under reduced transparency | Material must not reduce legibility or violate an OS preference |
| Accent color used as small text | Contrast-safe accent text token | Brand color and readable foreground are different jobs |
| Ambiguous “Allow” copy | Name the data, scope, destination, and consequence | Consent must be informed and current |
| Disabled-looking pending state with no semantics | Busy/disabled state plus status text | Visual treatment and accessibility tree must agree |

## Token ownership

`cortex/libs/design/tokens.yaml` is the source of truth. Run
`python -m cortex.scripts.sync_design_tokens` to generate:

- `cortex/apps/browser_extension/design-tokens.ts`;
- `cortex/apps/desktop_shell/tokens.py`.

Generated aliases include browser CSS variables, PySide QSS values, semantic
state foregrounds, strong easing curves, reduced-motion helpers, and a
contrast-safe `accent_text`. Do not hand-edit generated files or introduce a
new literal that duplicates a semantic token.

The palette uses warm paper/card surfaces and terracotta action accents in
light mode, with neutral dark materials and softened borders/shadows in dark
mode. Color never carries state alone. Small/normal text targets WCAG 2.2 AA
contrast; large display numerals still use a readable fallback. Filled
accent controls use dark ink rather than low-contrast white.

## Motion vocabulary

| Use | Duration | Curve/properties |
| --- | --- | --- |
| Press response | 120 ms | `transform: scale(.97)`, strong ease-out |
| Small control/state transition | 160 ms | opacity/color/border/transform only |
| Card, popover, overlay entrance | 200 ms | opacity + at most `translateY(8px) scale(.98)` |
| Larger drawer/sheet | 280 ms maximum | strong ease-out; centered modals keep center origin |
| Ambient pulse | 3,000 ms cycle | decorative only; paused when hidden/reduced-motion |
| Exit | 120–160 ms | faster than entry, same spatial direction |

Canonical curves:

```text
ease-out / default: cubic-bezier(0.23, 1, 0.32, 1)
ease-in-out:         cubic-bezier(0.77, 0, 0.175, 1)
drawer:              cubic-bezier(0.32, 0.72, 0, 1)
```

Do not animate geometry, height, width, padding, margin, or unrelated
properties on live dashboard paths. Avoid bounce in ordinary Cortex UI. Hover
movement is allowed only under `(hover: hover) and (pointer: fine)`. Keyboard
focus and frequent navigation should feel immediate.

Under `prefers-reduced-motion: reduce` or macOS Reduce Motion:

- disable transforms, particle fields, ripples, breathing/aura motion, and
  staged entrances;
- keep breathing guidance available as a fixed disc with non-counting copy
  (``Breathe at your pace`` or the current phase); desktop timers do not run
  solely to animate;
- render readable/actionable content immediately;
- retain 120–200 ms opacity, foreground, background, and border transitions
  where they make state changes legible;
- keep focus indicators and press acknowledgement.

Shadow DOM owns its reduced-motion block because document CSS cannot cross the
shadow boundary. Canvas loops must stop work, not merely hide their output.
Every loop has one cancelable owner and stops whenever its document is hidden.

Under `prefers-reduced-transparency: reduce`, every browser material that uses
`backdrop-filter` removes the blur and becomes the opaque semantic control
surface. Desktop windows retain Qt's content view and use the safe AppKit
window-background tint; `NSVisualEffectView` replacement is prohibited unless
a future packaged-app experiment proves visible first launch, quit, and
relaunch without orphaning Qt's content view.

## Interaction semantics

- Buttons expose hover (fine pointer), active, keyboard focus-visible,
  disabled, busy, success, and error states where applicable.
- A destructive or externally disclosing action names its consequence. A
  generic “Continue” is insufficient when the consequence is sending context,
  changing workspace state, or deleting data.
- Permission copy is scoped to the current site/account and never implies
  incognito collection or verified provider retention.
- Proposals and actions are visually distinct. Suggested actions remain inert
  until an exact authorization surface displays their target and consequence.
- Status leads value: unknown, warming-up, stale, estimated, and unavailable
  states are explicit; old measurements are not silently presented as live.
- Errors stay near the initiating control, preserve user input, use a live
  status region where appropriate, and include a correlation ID only when it
  helps support.
- Loading indicators do not block reading already available content and do not
  use endless decorative motion under Reduce Motion.

## Modal, sheet, and focus behavior

Dialogs have an accessible name/description, a clear primary action, a visible
secondary/cancel route, Escape behavior where safe, and deterministic initial
focus. Focus is trapped while modal and restored to the invoker on close.
Closing, going back, changing selected sources, or expiry must cancel any
prepared external-context handle. A modal never animates from a nearby button;
it is centered. Anchored popovers originate from their trigger.

## Surface-specific posture

- **Desktop dashboard:** prioritize live status, signal quality, and explicit
  unavailable states. No geometry animation in data rows. Advanced diagnostics
  are visually subordinate to the consumer summary. A full-screen break always
  exposes an immediate "End early" control and Escape route. Intervention
  footer controls must fit the production 460 px overlay width; shortcut hints
  belong in tooltips/accessibility descriptions when visible copy would clip.
- **Desktop privacy sheet:** source selection → exact preview → explicit send.
  The review step shows destination, retention caveat, expiry, byte size,
  redactions, and exact outbound prompt; changing sources burns the preview.
- **Browser popup:** fast, compact controls; current-site page-context status;
  truthful disconnected and suggestion-only states; no marketing entrance.
- **Injected overlays:** one coherent 200 ms entrance, in-place content update,
  quick symmetric exit, no replay on a replacement proposal. Blocking
  surfaces use labelled modal semantics, initial focus, Tab containment,
  Escape where safe, and restoration; non-blocking cards use named regions and
  never steal focus from the active page.
- **Pulse Room/new tab:** decorative motion gets the available delight budget,
  but interactive controls are immediate, reduced-motion stops canvas work,
  hidden tabs cancel canvas work, reduced-transparency removes blur, and resume
  cards retain native link semantics inside a real labelled list.
- **Onboarding:** progress is semantic, copy states what is detected versus
  configured, Return/click behavior is explicit, and no time-to-result promise
  substitutes for evidence requirements.
- **VS Code:** use theme variables, never assume light/dark colors, keep action
  authority and errors visible, and mirror reduced-motion semantics. The pacer
  owns one requestAnimationFrame loop, cancels it while hidden, and renders a
  static countdown-free guide under Reduce Motion.

## Review gate

For every proposed animation, answer in order:

1. Frequency: will repetition make it feel slow?
2. Purpose: feedback, spatial consistency, state indication, discontinuity,
   explanation, or rare delight?
3. Speed: can functional UI remain below 300 ms?
4. Function: does movement help the user understand or act?

Reject the animation if any answer fails. Review at normal speed, 10% speed,
with rapid interruption, keyboard-only, coarse pointer, light/dark mode, and
Reduce Motion.

## Verification

```bash
python -m cortex.scripts.sync_design_tokens --check
pytest -q cortex/tests/unit/test_ui_contrast_contract.py \
  cortex/tests/unit/test_desktop_tokens_smoke.py \
  cortex/tests/unit/test_overlay_animation.py \
  cortex/tests/unit/test_breathing_pacer_config.py \
  cortex/tests/unit/test_break_overlay_reduced_motion.py

cd cortex/apps/browser_extension
pnpm exec tsc --noEmit
pnpm exec vitest run __tests__/ambient_motion.spec.ts \
  __tests__/injected_motion.spec.ts \
  __tests__/newtab_a11y.spec.tsx \
  __tests__/popup_dialog_accessibility.spec.tsx

cd ../vscode_extension
npm run lint && npm run compile && npm test
```

The implementation plans and acceptance evidence for this refinement live in
the repository's `plans/` directory. Mark a plan complete only after its
mechanical and interaction checks pass.
