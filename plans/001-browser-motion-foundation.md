# 001 — Consolidate browser motion tokens and reduced-motion behavior

- **Status**: DONE (2026-08-25)
- **Commit**: 27c05b6
- **Severity**: HIGH
- **Category**: Accessibility, cohesion, performance
- **Estimated scope**: 5 files, small mechanical refactor

## Problem

The browser surfaces use weaker, parallel curves and globally reduce every
transition to `0.01ms`, which removes useful focus/press/color feedback as well
as movement:

```ts
// cortex/apps/browser_extension/design-tokens.ts:128 — current
durationFast: "150ms",
durationNormal: "200ms",
durationSlow: "400ms",
easeDefault: "cubic-bezier(0.4, 0, 0.2, 1)",
easeOut: "cubic-bezier(0, 0, 0.2, 1)",
easeIn: "cubic-bezier(0.4, 0, 1, 1)",

// cortex/apps/browser_extension/design-tokens.ts:168 — current
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: 0.01ms !important; ... }
}
```

Two Shadow DOM surfaces also use `transition: all`, which can animate layout
and unrelated state:

```css
/* cortex/apps/browser_extension/background.ts:2340 — current */
.btn { transition: all .12s; }
/* cortex/apps/browser_extension/background.ts:2504 — current */
.sk { transition: all .12s; }
```

## Target

Use one crisp professional vocabulary:

```ts
durationMicro: "120ms",
durationFast: "160ms",
durationNormal: "200ms",
durationSlow: "280ms",
easeDefault: "cubic-bezier(0.23, 1, 0.32, 1)",
easeOut: "cubic-bezier(0.23, 1, 0.32, 1)",
easeInOut: "cubic-bezier(0.77, 0, 0.175, 1)",
easeDrawer: "cubic-bezier(0.32, 0.72, 0, 1)",
```

Reduced motion removes transforms and continuous decorative animation while
retaining `opacity`, `color`, `background-color`, `border-color`, and press
feedback for `120–200ms`. Replace every `transition: all` with named
properties. No UI transition exceeds 300ms unless it is explicitly ambient.

## Repo conventions to follow

- Motion values live in `cortex/apps/browser_extension/design-tokens.ts`.
- `popup.tsx` already assigns shared `CX.duration*` values rather than local
  constants.
- Shadow-root CSS must carry its own reduced-motion block because document
  styles cannot cross the shadow boundary.

## Steps

1. Update the token values above and replace `easeIn` with `easeInOut`; migrate
   all consumers.
2. Replace the global `0.01ms` reduced-motion rule with explicit helpers that
   suppress `cx-breathe`, `cx-rise`, and transform movement but retain brief
   opacity/color feedback.
3. In `background.ts`, replace `.btn` with
   `transition: background-color 120ms cubic-bezier(.23,1,.32,1), transform 120ms cubic-bezier(.23,1,.32,1)`
   and `.sk` with named `color`, `border-color`, and `transform` properties.
4. Add `:active { transform: scale(.97) }` to pressable Shadow DOM controls;
   do not animate keyboard-opened surfaces.
5. Add reduced-motion blocks to each Shadow DOM stylesheet and to VS Code's
   webview stylesheet.

## Boundaries

- Do not add a motion dependency.
- Do not animate layout properties.
- Do not change ambient state timing in this plan.

## Verification

- **Mechanical**: `pnpm test && pnpm exec tsc --noEmit && pnpm build` in the
  browser extension; VS Code `npm test && npm run compile`.
- **Feel check**: inspect popup and injected panels at 10% playback; buttons
  respond immediately, no unrelated property interpolates, and reduced motion
  removes movement without removing focus/press/color feedback.
- **Done when**: `rg 'transition:\s*all|easeIn' cortex/apps` finds no UI use and
  every shadow root declares reduced-motion behavior.
