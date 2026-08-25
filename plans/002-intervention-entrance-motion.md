# 002 — Make intervention entrances immediate and interruptible

- **Status**: DONE (2026-08-25)
- **Commit**: 27c05b6
- **Severity**: HIGH
- **Category**: Purpose, performance, interruptibility
- **Estimated scope**: 3 files, moderate

## Problem

The desktop overlay animates a widget's `geometry` and delays the causal row
until a 250ms first phase ends. Important support content therefore arrives in
two beats over 430ms, and geometry animation forces layout work:

```py
# cortex/apps/desktop_shell/overlay.py:1196 — current
anim = QPropertyAnimation(self._headline, b"geometry")
anim.setDuration(HEADLINE_SCALE_DURATION_MS)  # 250ms
...
QTimer.singleShot(HEADLINE_SCALE_DURATION_MS, fade.start)  # then 180ms
```

Browser intervention cards use entry keyframes in multiple inline shadow-root
styles (`background.ts:2287`, `2486`, `2589`). Keyframes restart rather than
retargeting when a new intervention replaces an existing one, and some roots
lack reduced-motion handling.

## Target

- Desktop: all readable content is present immediately. If motion is enabled,
  apply a single concurrent opacity transition of 160ms with
  `QEasingCurve.Type.OutCubic`; never animate geometry. Under Reduce Motion,
  render at full opacity immediately.
- Browser: card entry uses opacity plus `translateY(8px) scale(.98)` for 200ms
  with `cubic-bezier(.23,1,.32,1)`. A replacement updates in place without
  replaying entry motion. Exits use the same spatial direction for 160ms.
- Press controls use 120–160ms `scale(.97)` feedback.

## Repo conventions to follow

- `mac_native.prefers_reduced_motion()` is the desktop source of truth.
- Browser motion tokens come from `design-tokens.ts`; inline Shadow DOM styles
  must embed exact equivalents.
- The intervention transaction and authorization code must remain untouched.

## Steps

1. Remove `HEADLINE_SCALE_DURATION_MS`, the geometry animation, and the staged
   timer from `overlay.py`.
2. Fade the headline and causal row concurrently for 160ms using opacity
   effects; retain animation objects and stop/retarget them on replacement.
3. Add tests proving geometry never changes, content is never hidden after a
   cancellation, and reduced motion applies the final state synchronously.
4. Replace browser card entry keyframes with an idempotent mounted-state CSS
   transition, and do not remount/replay it for content-only updates.
5. Implement a matching exit before removal and a reduced-motion opacity-only
   fallback of 160ms.

## Boundaries

- No delay before text becomes readable or actionable.
- No changes to consent, action manifests, or dismissal timers.
- No bounce; Cortex is a restrained support dashboard.

## Verification

- **Mechanical**: focused desktop overlay tests, browser unit tests, TypeScript
  compile, Chrome and Edge builds.
- **Feel check**: at 10% playback the card appears as one coherent object;
  replacing its text does not replay entrance motion; rapidly dismissing and
  reopening never jumps to an old start frame.
- **Done when**: overlay code has no `b"geometry"` animation and all browser
  intervention roots have symmetric entry/exit plus reduced-motion handling.
