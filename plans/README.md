# Motion refinement plan history

Plans 001–007 are historical implementation records from the earlier motion and
desktop refinement passes. Their baselines, screenshots, test totals and
completion dates describe those passes; they are not the current acceptance
record and should not be rewritten to imply otherwise.

The active specification and completion record is
[`docs/implementation-2026-10.md`](../docs/implementation-2026-10.md).
Current cross-surface behavior is defined in
[`cortex/docs/ui-design.md`](../cortex/docs/ui-design.md).
The October refinement extends the existing warm palette and restrained motion:

- Browser popup/new-tab hydration preserves sample age and availability;
  disconnect invalidates current readings and ten-second expiry prevents held
  values from looking live.
- Pulse Room has persisted motion pause, complete hidden/reduced-motion/opt-out
  lifecycle handling, stable assistive status and a responsive scrolling layout.
- Quiet-mode radio controls implement roving focus, arrows and Home/End.
- Desktop status groups wrap, consumer content scrolls, unavailable readings
  clear, and a suggestion has an explicit keyboard-operable Review path.
- Stop reflects verified process exit, serializes with launch, reports uncertainty
  and preserves recovery receipts while clearing presentation. Closing a window
  and ending a focus session remain distinct from explicit Quit.

These changes were implemented and validated in the source checkout. A local
ad-hoc arm64 build and synthetic UI inspections do not establish a published
release, physical camera accuracy or end-user accessibility validation. Current
gate counts and artifact evidence are maintained in the active specification
instead of duplicated across historical plans.

## Earlier plans

Audit baseline: `27c05b6`, using Emil Kowalski's design-engineering and motion
review criteria. Cortex is an information-dense support dashboard, so the
recommended posture is crisp, restrained motion with no delays before readable
or actionable content.

| Plan | Title | Severity | Status | Depends on |
| --- | --- | --- | --- | --- |
| [001](001-browser-motion-foundation.md) | Consolidate browser motion tokens and reduced-motion behavior | HIGH | DONE | — |
| [002](002-intervention-entrance-motion.md) | Make intervention entrances immediate and interruptible | HIGH | DONE | 001 |
| [003](003-newtab-motion-restraint.md) | Restrain new-tab motion and keep interaction immediate | MEDIUM | DONE | 001 |
| [004](004-ambient-motion-accessibility.md) | Bound ambient motion and make flow-shield release responsive | MEDIUM | DONE | 001 |
| [005](005-press-and-status-feedback.md) | Normalize press and status feedback across surfaces | LOW | DONE | 001–004 |
| [006](006-stop-pacers-under-reduced-motion.md) | Stop continuous pacers under reduced motion and hidden views | HIGH | DONE | 001, 004 |
| [007](007-desktop-shell-truthful-states.md) | Desktop shell: truthful states, session semantics, window posture | HIGH | DONE | 005, 006 |

Plans 001–007 are complete. Plan 007 (2026-09-05) fixes the desktop shell's untrue states and quit-on-end-session routes; its contracts are recorded in `cortex/docs/ui-design.md` under "Desktop shell contracts". Plan 006 was the only actionable result from the
2026-08-29 full motion re-audit; it extends the established preference/lifecycle
contracts to the three remaining canvas/timer pacers and restores immediate
exit agency to the full-screen break. A follow-through search also closed the
Pulse Room canvas's hidden-tab lifecycle instead of preserving a false audit
assumption.

Historical completion evidence (2026-08-25): generated design-token parity and contrast
contracts pass; 35 selected desktop/UI modules pass in isolated Qt processes;
the browser passes TypeScript, 253 tests, and Chrome/Edge production builds;
the VS Code extension passes lint, compilation, packaging, and 32 tests. The
canonical Python gate passes 2,703 tests with three documented skips. Mechanical
search finds no UI `transition: all`, geometry animation, legacy `easeIn`
token, or global `0.01ms` reduced-motion reset. Interaction semantics and the
pinned upstream reference are recorded in
[`cortex/docs/ui-design.md`](../cortex/docs/ui-design.md).

Deliberately rejected motion candidates:

- Dashboard/history keyboard navigation: high-frequency and keyboard-driven;
  animation would add latency.
- Live biometrics and charts: functional data should not move decoratively.
- Privacy sheet stage changes: the confirmation is high stakes and benefits
  from immediate, legible state changes rather than flourish.
- Continuous bounce or celebration after an external request: the outcome is
  a privacy-sensitive network send, not a celebratory moment.
- Recap-sheet position movement: it is an occasional, spatially anchored
  200 ms sheet transition with an immediate Reduce Motion path. Qt has no
  transform-only QWidget equivalent, so replacing it would add complexity
  without a user-visible gain.
