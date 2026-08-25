# 005 — Normalize press and status feedback across surfaces

- **Status**: DONE (2026-08-25)
- **Commit**: 27c05b6
- **Severity**: LOW
- **Category**: Physicality, accessibility, cohesion
- **Estimated scope**: 8–12 files, mechanical

## Problem

Only a subset of the browser popup's pressables receive active feedback
(`popup.tsx:838`, `851`), the VS Code waiting spinner pulses indefinitely
without a reduced-motion alternative (`panel-provider.ts:664`), and most Qt
button styles define hover but no pressed state. The result feels inconsistent
even though each isolated component works.

## Target

- Browser pressables: `transform: scale(.97)` for 120ms with
  `cubic-bezier(.23,1,.32,1)`, except keyboard-high-frequency navigation and
  disabled controls. Preserve `focus-visible` rings.
- Qt: because QSS cannot transform widgets, use a subtle immediate pressed
  color/border state from shared token QSS; never shift padding or geometry.
- VS Code: waiting pulse may continue for sighted users, but reduced motion
  becomes a static dot with a 160ms opacity/color transition.
- Hover-only visual changes are gated to fine pointers in browser/webview CSS.

## Repo conventions to follow

- Central Qt button styles live in `cortex/apps/desktop_shell/tokens.py`.
- Browser classes are injected once per surface and reuse `CX` tokens.
- Accessibility focus rings must remain visually distinct from hover/press.

## Steps

1. Inventory every pressable and classify its shared style family.
2. Add active feedback to the shared browser classes and remove component-local
   duplicates.
3. Add pressed variants to shared Qt QSS; replace the privacy sheet's current
   pressed padding shift with a color state.
4. Add the VS Code reduced-motion rule and fine-pointer hover gates.
5. Add style-source tests that require focus, active, disabled, and
   reduced-motion variants for each shared family.

## Boundaries

- Do not animate keyboard shortcuts, list navigation, text inputs, or data.
- Do not move Qt widget geometry for press feedback.
- Do not weaken focus-visible contrast.

## Verification

- **Mechanical**: desktop style/a11y tests, browser tests/build, VS Code
  tests/compile/package.
- **Feel check**: repeated clicks feel immediate but not bouncy; keyboard use
  stays instant; reduced motion retains clear press/focus/status feedback.
- **Done when**: shared button families cover active/focus/disabled states and
  every continuous status animation has a reduced-motion alternative.
