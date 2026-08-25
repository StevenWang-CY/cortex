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

Recommended execution order is the table order. Plan 001 establishes the
shared vocabulary; 002 closes the highest-impact interaction delay; 003 and
004 can then proceed independently; 005 is the final cohesion sweep.

Completion evidence (2026-08-25): generated design-token parity and contrast
contracts pass; 35 selected desktop/UI modules pass in isolated Qt processes;
the browser passes TypeScript, 241 tests, and Chrome/Edge production builds;
the VS Code extension passes 30 tests and TypeScript compilation. Mechanical
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
