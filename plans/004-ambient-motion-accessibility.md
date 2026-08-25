# 004 — Bound ambient motion and make flow-shield release responsive

- **Status**: DONE (2026-08-25)
- **Commit**: 27c05b6
- **Severity**: MEDIUM
- **Category**: Accessibility, easing, performance
- **Estimated scope**: 2 files, moderate

## Problem

The ambient content script starts a permanent 15fps particle loop and 3–45s
visual transitions without consulting reduced motion. Its flow shield restores
content using a five-second `ease-in`, delaying the first visible response to a
user-directed release:

```ts
// cortex/apps/browser_extension/contents/ambient.ts:190 — current
animFrameId = requestAnimationFrame(tick);
// cortex/apps/browser_extension/contents/ambient.ts:416 — current
htmlEl.style.transition = "opacity 5s ease-in, filter 5s ease-in";
```

## Target

- When `prefers-reduced-motion: reduce`, do not start or continue particle
  animation and make aura/tone changes opacity/color-only.
- Pause the animation loop while the document is hidden; restart with one
  frame on visibility return. Never maintain an idle rAF loop before a valid
  estimated state exists.
- Release the flow shield over 200ms with
  `cubic-bezier(0.23,1,0.32,1)`; entering the shield can remain deliberately
  gradual because it is an ambient, user-enabled focus transition.
- Cancel pending restore timers when state changes again; restore original
  inline styles exactly once.

## Repo conventions to follow

- The script already keeps `animFrameId` and centralizes state in `tick()`.
- Incognito remains excluded at bootstrap.
- `design-tokens.ts` defines the canonical curve, copied literally because a
  content script cannot assume shared document CSS.

## Steps

1. Add a persistent reduced-motion media-query listener and a document
   visibility listener with cleanup.
2. Replace the unconditional recursive rAF with `startAnimationLoop()` and
   `stopAnimationLoop()` guards.
3. Under reduced motion, clear particles and render only the static current
   color at bounded opacity.
4. Replace the five-second ease-in release with the 200ms target and track
   cancelable per-element restore timers.
5. Add fake-rAF and fake-timer tests for reduce-motion toggle, hidden tab,
   rapid activate/deactivate/reactivate, and teardown.

## Boundaries

- Do not change state inference or flow-shield eligibility.
- Do not increase particle count or opacity.
- Do not access incognito pages.

## Verification

- **Mechanical**: ambient tests, TypeScript compile, Chrome/Edge builds.
- **Feel check**: disabling the shield reveals content immediately; switching
  tabs stops CPU activity; reduced motion has no drifting particles.
- **Done when**: no `ease-in` remains, hidden/reduced-motion states schedule no
  recurring rAF, and rapid state reversal restores exact prior styles.
