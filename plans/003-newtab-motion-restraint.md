# 003 — Restrain new-tab motion and keep interaction immediate

- **Status**: DONE (2026-08-25)
- **Commit**: 27c05b6
- **Severity**: MEDIUM
- **Category**: Purpose, duration, performance
- **Estimated scope**: 2 files, moderate

## Problem

The new-tab centerpiece takes 2.5 seconds to enter and resume cards take two
seconds after a 500ms delay:

```tsx
// cortex/apps/browser_extension/newtab.tsx:452 — current
animation: "activityFadeIn 2.5s cubic-bezier(0.16, 1, 0.3, 1) forwards"
// cortex/apps/browser_extension/newtab.tsx:560 — current
animation: "activityFadeIn 2s ... 0.5s forwards"
```

The launch button uses `transition: all` (`newtab.tsx:533`), and hover motion
is driven by pointer handlers without a fine-pointer media gate. This makes a
functional launch surface feel like a marketing page and creates false hover
behavior on touch-capable browsers.

## Target

- Centerpiece entrance: 240ms, `opacity` plus `translateY(8px)`, strong
  `cubic-bezier(0.23,1,0.32,1)` ease-out.
- Resume cards: 240ms with 50ms per-card stagger, never blocking interaction.
- Reduced motion: 160ms opacity only, no aura/logo transforms or canvas
  particles.
- Launch button: explicitly transition `background-color`, `border-color`,
  `box-shadow`, `opacity`, and `transform` for 160ms; press to `scale(.97)`.
- Hover lift only under `(hover: hover) and (pointer: fine)`.

## Repo conventions to follow

- Reuse `CX.duration*` and `CX.easeOut` from `design-tokens.ts` after plan 001.
- Keep the existing `matchMedia` listener and canvas stop behavior.

## Steps

1. Replace both multi-second keyframe applications with the target timings.
2. Express card stagger using an index-derived 50ms delay capped at 100ms.
3. Replace launch `transition: all` and add class-based active/focus styling.
4. Replace JS hover mutation with a class and fine-pointer media query.
5. Ensure `reducedMotion` removes canvas/aura/logo transforms and uses opacity
   only for content reveal.

## Boundaries

- Do not change the breathing pacer's semantic timing when motion is enabled.
- Do not animate functional numbers or progress values for decoration.
- Do not add dependencies.

## Verification

- **Mechanical**: new-tab tests, TypeScript compile, production build.
- **Feel check**: fresh new tab is usable immediately; at 10% playback the
  stagger is subtle; touch emulation never applies hover lift; reduced motion
  leaves a calm static page with readable feedback.
- **Done when**: no new-tab UI entrance exceeds 300ms and no `transition: all`
  remains.
