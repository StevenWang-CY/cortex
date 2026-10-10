# Product screenshots

The root [README](../../README.md) uses captures of the implemented interface
from the October refinement review. Displayed measurements, activity and
workspace context are **synthetic fixtures**. They illustrate layout and
interaction states, not measured physiology, inference accuracy or a live user
session.

| File | Current example | Dimensions |
| --- | --- | --- |
| `dashboard.png` | Compact desktop Dashboard with camera estimates and session controls | 400 × 680 |
| `suggestion-review.png` | Explicit Review suggestion surface within the desktop dashboard | 400 × 680 |
| `pulse-room.jpg` | Light Pulse Room with decorative ambient motion and pause control | 1280 × 720 |

These files were copied without pixel edits from the inspected application
captures under the ignored local `dist/review-2026-10/screenshots/` evidence
directory. The public copies are intentional documentation assets. The
[implementation record](../../docs/implementation-2026-10.md) describes the
broader rendered inspection and its limits.

`overlay.png` is retained as **historical demo media** and is not used in the
current README. Its wording and automatic tab-closing example do not represent
the current suggestion-only default or exact-authorization workflow.

## Updating the examples

- Capture the actual current interface; use synthetic or explicitly approved
  demonstration data and label fixtures in the accompanying document.
- Keep disabled or unavailable signals unavailable. Do not add HRV, infer
  cognitive state from the camera, or portray the Pulse Room trace as an ECG.
- Show suggestions as proposals. If demonstrating an enabled workspace effect,
  include the exact authorization, resulting receipt and scoped restore flow.
- Preserve the implemented palette, typography, proportions and motion controls.
  Keep the screenshot files and README references synchronized.
- Exclude private tab titles, file paths, user identities, tokens and logs.
  Use an isolated profile; never capture a real workspace merely for decoration.
