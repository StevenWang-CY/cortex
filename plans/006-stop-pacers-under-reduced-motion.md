# 006 — Stop continuous pacers under reduced motion and hidden views

- **Status**: DONE (2026-08-29)
- **Commit**: 1b18008
- **Severity**: HIGH
- **Category**: Accessibility, performance, purpose
- **Estimated scope**: 4 source files and focused tests, moderate

## Problem

Three breathing surfaces continue position/radius animation even when the user
has enabled Reduce Motion. The VS Code webview also keeps a 60 fps animation
frame loop alive while its document is hidden.

The follow-through repository search found a fourth lifecycle gap: Pulse Room
already rendered a static Reduce Motion state, but its ordinary canvas loop
continued after the new-tab document became hidden.

```py
# cortex/apps/desktop_shell/overlay.py:1017 — current
if level == "overlay_only" or ui_plan.get("show_overlay", True):
    self._pacer.start()
    self._pacer.show()
```

`BreathingPacer.start()` unconditionally starts a 33 ms timer; only the
headline opacity cue consults `mac_native.prefers_reduced_motion()`.

```py
# cortex/apps/desktop_shell/break_overlay.py:283 — current
self._timer = QTimer(self)
self._timer.setInterval(33)  # ~30 Hz animation
self._timer.timeout.connect(self._tick)
self._timer.start()
```

The full-screen break canvas always changes its radius on every tick. This is
the most consequential gap because the motion fills the display and the user
cannot choose another view while it runs.

```ts
// cortex/apps/vscode_extension/src/panel-provider.ts:1048 — current
requestAnimationFrame(drawPacer);
// ...
requestAnimationFrame(drawPacer);
```

The webview's CSS disables its small loading spinner under
`prefers-reduced-motion`, but CSS cannot stop this canvas loop. There is no
`matchMedia`, `visibilitychange`, or `cancelAnimationFrame` lifecycle for the
pacer.

## Target

- Desktop intervention: when `mac_native.prefers_reduced_motion()` is true,
  show one static pacer frame labelled `Breathe at your pace`; do not start the
  33 ms timer. Ordinary mode retains the configured breathing cadence.
- Full-screen break: keep elapsed-time/completion and phase text functional,
  but hold the disc at a constant radius ratio of `0.46` under Reduce Motion.
  Use a 250 ms status timer instead of a 33 ms animation timer in that mode.
- VS Code: keep one `requestAnimationFrame` identifier, stop it while
  `document.visibilityState !== 'visible'`, and never schedule it under
  `(prefers-reduced-motion: reduce)`. Render a static disc at `0.46 * maxR`,
  set the label to `Breathe at your pace`, and clear the countdown in the
  reduced path.
- Media-query and visibility changes take effect immediately. Resuming ordinary
  visible mode starts exactly one loop. Repeated events never create parallel
  loops.
- Pulse Room retains its existing static Reduce Motion path and pauses/resumes
  its ordinary canvas through the same single-owner visibility lifecycle.
- Do not change the 4-7-8, box, or coherent cadence in ordinary mode.

## Repo conventions to follow

- `cortex/apps/desktop_shell/overlay.py:1220` already wraps
  `mac_native.prefers_reduced_motion()` defensively; reuse that helper instead
  of introducing a second OS-preference source.
- `cortex/apps/desktop_shell/recap_sheet.py:484` demonstrates the desktop
  reduced-motion pattern: apply the final readable state synchronously and do
  not start a movement tween.
- `cortex/apps/browser_extension/contents/ambient.ts:175` demonstrates the
  web lifecycle: one nullable frame ID plus `shouldRunAnimationLoop()`,
  `stopAnimationLoop()`, media-query changes, and `visibilitychange`.
- Continuous predetermined motion uses linear time progression. Do not add a
  bounce or a new dependency.

## Steps

1. In `cortex/apps/desktop_shell/overlay.py`, give `BreathingPacer` a
   reduced-motion/static state. Its ordinary `start()` keeps the current timer;
   its reduced path stops the timer, marks the widget active, and paints the
   fixed `0.46` disc with `Breathe at your pace` and no decrementing seconds.
2. In `OverlayWindow.show_intervention`, resolve the existing
   `_reduce_motion_enabled()` value and pass it to the pacer. Add a focused test
   proving the timer is inactive and the static flag is set under Reduce Motion,
   while ordinary mode still starts the timer.
3. In `cortex/apps/desktop_shell/break_overlay.py`, resolve
   `mac_native.prefers_reduced_motion()` defensively at `run()` start. Select a
   250 ms timer interval in reduced mode. In `_tick()`, compute the current phase
   and completion exactly as today but pass `0.46` to `_canvas.set_state` rather
   than the animated ratio. Add tests for fixed radius across inhale/exhale
   timestamps and ordinary-mode unchanged math.
4. In `cortex/apps/vscode_extension/src/panel-provider.ts`, store the rAF ID,
   implement `renderStaticPacer`, `startPacer`, `stopPacer`, and `syncPacer`, and
   add both the reduced-motion media listener and document visibility listener.
   Schedule the next frame only inside the ordinary visible path.
5. Add VS Code webview-source tests proving the emitted script contains one
   cancelable frame owner, both listeners, no reduced-motion scheduling, and a
   static accessible label. Preserve existing in-place state updates.
6. In `cortex/apps/browser_extension/newtab.tsx`, change the ordinary canvas to
   one nullable frame owner, stop it on hidden visibility, resume it exactly
   once on visible, and add an executable lifecycle test.

## Boundaries

- Do not change intervention eligibility, break duration, audio behavior, or
  breathing-pattern configuration. The separately discovered agency defect is
  limited to making the existing explicit dismissal route immediately usable.
- Do not remove the pacer for users who have not selected Reduce Motion.
- Do not add dependencies or animate layout properties.
- Do not change Pulse Room's visual design or ambient content-script motion;
  only close the newly identified new-tab hidden-view ownership gap.
- If these cited seams drift from commit `1b18008`, stop and re-audit instead
  of improvising.

## Verification

- **Mechanical**:
  - `uv run --project cortex --locked --extra dev pytest -q cortex/tests/unit/test_overlay_animation.py cortex/tests/unit/test_breathing_pacer_config.py <new break-overlay test>`
  - `uv run --project cortex --locked --extra dev ruff check cortex/apps/desktop_shell/overlay.py cortex/apps/desktop_shell/break_overlay.py`
  - `npm --prefix cortex/apps/vscode_extension run lint`
  - `npm --prefix cortex/apps/vscode_extension run compile`
  - `npm --prefix cortex/apps/vscode_extension test`
- **Feel check**: with Reduce Motion off, observe one complete cycle on each
  surface and confirm cadence is unchanged. Enable Reduce Motion before opening
  each surface: the disc remains fixed, guidance stays readable, and no frame
  loop/timer drives radius changes. Hide and show the VS Code panel repeatedly;
  DevTools must show zero hidden-view frames and exactly one resumed loop.
  Inspect ordinary motion at 10% playback and confirm there is no bounce or
  phase discontinuity.
- **Done when**: all three pacers have a static reduced-motion state, the VS
  Code and Pulse Room canvases have one cancelable visibility-aware loop each,
  and focused plus full client gates pass.

## Completion evidence

- Desktop intervention tests prove ordinary cadence still starts its 33 ms
  timer, Reduce Motion renders the fixed `0.46` prompt without a timer or
  countdown, and screen-share suppression stops a hidden pacer.
- Full-screen break tests prove the ordinary inhale/exhale radius math is
  unchanged, all phases hold at `0.46` under Reduce Motion, the status cadence
  drops to 250 ms, and Escape is immediately effective.
- VS Code source contracts prove one nullable/cancelable frame owner,
  preference and visibility listeners, a static accessible label, and no
  unowned `requestAnimationFrame` scheduling.
- Pulse Room's executable lifecycle test proves hidden cancellation and
  idempotent single-loop resume without changing its visuals.
- Focused offscreen Qt: 36 passed. VS Code: lint and compile passed; 32 tests
  passed. Canonical Python: 2,703 passed / 3 documented skips. Browser:
  TypeScript, 253 tests, and Chrome/Edge production builds passed.
- No Cortex application or physical camera was launched. Ordinary-motion
  visual feel and interruption on real displays remain part of the explicitly
  authorized physical release matrix, not fabricated automated evidence.
