# Motion refinement plans

Audit baseline: `27c05b6`, using Emil Kowalski's design-engineering and motion
review criteria. Cortex is an information-dense support dashboard, so the
recommended posture is crisp, restrained motion with no delays before readable
or actionable content.

| Plan | Title | Severity | Status | Depends on |
| --- | --- | --- | --- | --- |
| 001 | Consolidate browser motion tokens and reduced-motion behavior | HIGH | DONE | — |
| 002 | Make intervention entrances immediate and interruptible | HIGH | DONE | 001 |
| 003 | Restrain new-tab motion and keep interaction immediate | MEDIUM | DONE | 001 |
| 004 | Bound ambient motion and make flow-shield release responsive | MEDIUM | DONE | 001 |
| 005 | Normalize press and status feedback across surfaces | LOW | DONE | 001–004 |
| 006 | Stop continuous pacers under reduced motion and hidden views | HIGH | DONE | 001, 004 |

Plans 001–006 are complete. Plan 006 was the only actionable result from the
2026-08-29 full motion re-audit; it extends the established preference/lifecycle
contracts to the three remaining canvas/timer pacers and restores immediate
exit agency to the full-screen break. A follow-through search also closed the
Pulse Room canvas's hidden-tab lifecycle instead of preserving a false audit
assumption.

Completion evidence (2026-08-25): generated design-token parity and contrast
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
