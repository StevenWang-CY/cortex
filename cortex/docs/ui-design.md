# UI design and interaction contract

Last reviewed: 2026-10-11.

This contract applies to the PySide6 desktop shell, Chrome/Edge extension,
injected browser surfaces, and VS Code webview. The refinement pass was
informed by Emil Kowalski's public [Skills for Design
Engineers](https://github.com/emilkowalski/skills), pinned during review to
commit `e8a175de22ae1e49370fc144c1f3bb9aeedf988d` for this review, especially
`emil-design-eng`, `review-animations`, `improve-animations`,
`find-animation-opportunities`, and `apple-design`.

The reference is a decision framework, not a visual theme. Cortex remains a
restrained, warm, information-dense support tool: data must be legible, user
authority explicit, and motion subordinate to comprehension.

## Principles

| Before-risk | Contract | Why |
| --- | --- | --- |
| Parallel per-surface colors/timings | One generated token source | Small drift compounds into an incoherent product |
| `transition: all` or multi-second entrances | Named properties and sub-300 ms functional motion | Avoid layout animation and sluggish controls |
| Keyframes replayed on dynamic updates | Interruptible transitions or in-place updates | Rapid changes should retarget, not restart |
| Motion everywhere | Animate only feedback, spatial continuity, state legibility, or a jarring discontinuity | Functional dashboards benefit from restraint |
| Reduced motion removes all feedback | Remove movement/continuous decoration; retain brief opacity/color feedback | Accessibility without making state changes invisible |
| Blur-only glass surfaces | Opaque semantic-control fallback under reduced transparency | Material must not reduce legibility or violate an OS preference |
| Accent color used as small text | Contrast-safe accent text token | Brand color and readable foreground are different jobs |
| Ambiguous “Allow” copy | Name the data, scope, destination, and consequence | Consent must be informed and current |
| Disabled-looking pending state with no semantics | Busy/disabled state plus status text | Visual treatment and accessibility tree must agree |

## Token ownership

`cortex/libs/design/tokens.yaml` is the source of truth. Run
`uv run --project cortex --locked python -m cortex.scripts.sync_design_tokens`
from the repository root to generate:

- `cortex/apps/browser_extension/design-tokens.ts`;
- `cortex/apps/desktop_shell/tokens.py`.

Generated aliases include browser CSS variables, PySide QSS values, semantic
state foregrounds, strong easing curves, reduced-motion helpers, and a
contrast-safe `accent_text`. Do not hand-edit generated files or introduce a
new literal that duplicates a semantic token.

The palette uses warm paper/card surfaces and terracotta action accents in
light mode, with neutral dark materials and softened borders/shadows in dark
mode. Color never carries state alone. Small/normal text targets WCAG 2.2 AA
contrast; large display numerals still use a readable fallback. Filled
accent controls use dark ink rather than low-contrast white.

## Motion vocabulary

| Use | Duration | Curve/properties |
| --- | --- | --- |
| Press response | 120 ms | `transform: scale(.97)`, strong ease-out |
| Small control/state transition | 160 ms | opacity/color/border/transform only |
| Card, popover, overlay entrance | 200 ms | opacity + at most `translateY(8px) scale(.98)` |
| Larger drawer/sheet | 280 ms maximum | strong ease-out; centered modals keep center origin |
| Ambient pulse | 3,000 ms cycle | decorative only; paused when hidden/reduced-motion |
| Exit | 120–160 ms | faster than entry, same spatial direction |

Canonical curves:

```text
ease-out / default: cubic-bezier(0.23, 1, 0.32, 1)
ease-in-out:         cubic-bezier(0.77, 0, 0.175, 1)
drawer:              cubic-bezier(0.32, 0.72, 0, 1)
```

Do not animate geometry, height, width, padding, margin, or unrelated
properties on live dashboard paths. Avoid bounce in ordinary Cortex UI. Hover
movement is allowed only under `(hover: hover) and (pointer: fine)`. Keyboard
focus and frequent navigation should feel immediate.

Under `prefers-reduced-motion: reduce` or macOS Reduce Motion:

- disable transforms, particle fields, ripples, breathing/aura motion, and
  staged entrances;
- keep breathing guidance available as a fixed disc with non-counting copy
  (``Breathe at your pace`` or the current phase); desktop timers do not run
  solely to animate;
- render readable/actionable content immediately;
- retain 120–200 ms opacity, foreground, background, and border transitions
  where they make state changes legible;
- keep focus indicators and press acknowledgement.

Shadow DOM owns its reduced-motion block because document CSS cannot cross the
shadow boundary. Canvas loops must stop work, not merely hide their output.
Every loop has one cancelable owner and stops whenever its document is hidden.

Under `prefers-reduced-transparency: reduce`, every browser material that uses
`backdrop-filter` removes the blur and becomes the opaque semantic control
surface. Desktop windows retain Qt's content view and use the safe AppKit
window-background tint; `NSVisualEffectView` replacement is prohibited unless
a future packaged-app experiment proves visible first launch, quit, and
relaunch without orphaning Qt's content view.

## Interaction semantics

- Buttons expose hover (fine pointer), active, keyboard focus-visible,
  disabled, busy, success, and error states where applicable.
- A destructive or externally disclosing action names its consequence. A
  generic “Continue” is insufficient when the consequence is sending context,
  changing workspace state, or deleting data.
- Permission copy is scoped to the current site/account and never implies
  incognito collection or verified provider retention.
- Proposals and actions are visually distinct. Suggested actions remain inert
  until an exact authorization surface displays their target and consequence.
- Status leads value: unknown, warming-up, stale, estimated, and unavailable
  states are explicit; old measurements are not silently presented as live.
- Errors stay near the initiating control, preserve user input, use a live
  status region where appropriate, and include a correlation ID only when it
  helps support.
- Loading indicators do not block reading already available content and do not
  use endless decorative motion under Reduce Motion.

## Modal, sheet, and focus behavior

Dialogs have an accessible name/description, a clear primary action, a visible
secondary/cancel route, Escape behavior where safe, and deterministic initial
focus. Focus is trapped while modal and restored to the invoker on close.
Closing, going back, changing selected sources, or expiry must cancel any
prepared external-context handle. A modal never animates from a nearby button;
it is centered. Anchored popovers originate from their trigger.

## Desktop shell contracts

- **Appearance:** every Qt window pins the Aqua (light) appearance through
  `mac_native.apply_vibrancy(widget, appearance="light")`, and the
  `QApplication` palette comes from `mac_native.application_palette()`. Both
  hosts register the bundled display fonts and system text font before creating
  windows. The dashboard paints an opaque semantic background so first paint
  and a failed/unavailable native bridge remain legible. The
  shell never inherits the system dark theme half-way; the suggestion card is
  the one surface that pins `"dark"` on purpose.
- **Session semantics:** "End session" stops sensing, shows the recap, and
  leaves Cortex open in an *ended* phase that offers "Start session" (when the
  host can restart the daemon) and an explicit "Quit Cortex". Only explicit
  quit routes (dashboard Quit, the recap sheet's "Quit Cortex", tray / Cmd+Q)
  emit `gui_quit_requested`. The recap sheet's "View full report" keeps
  Cortex open on every route and renders the report already in hand.
  In both desktop hosts, closing the dashboard (red close control or Cmd+W)
  hides it without ending the session; the menu-bar item reopens it. Actual
  application Quit events enter the recap route before Qt teardown. Once that
  route finishes, the host removes its Quit interceptor so the final
  `QApplication.quit()` exits the event loop. `lastWindowClosed` must never
  trigger session shutdown.
- **Suggestion card:** a fixed-width (`POPUP_WIDTH`) notification anchored to
  the top-right of the cursor's screen. It shows with
  `WA_ShowWithoutActivating` on unsolicited arrival. The dashboard's explicit
  **Review suggestion** action activates the card and focuses **Dismiss**;
  Cmd-modified shortcuts and Escape then work without a global hotkey.
  Review is navigation, not authorization: no workspace action runs until its
  exact authorization control is used. Both hosts use `SuggestionReview`,
  which keeps a defensive copy of the exact payload. Its lifetime is at most
  five minutes from first receipt, shortened by an earlier manifest expiry;
  same-ID updates and reopening cannot renew that deadline. Action,
  dismissal, quiet mode, matching terminal transaction, stop, and disconnect
  retire the card and its dashboard entry. Late events for an older ID cannot
  clear a replacement; retired cards reject queued action clicks. A visible
  suggestion is counted once, even when updated or reopened.
  The breathing pacer (`BREATHING_PACER_SIZE`) appears only for breathing
  plans. One "Why this?" toggle reveals the full causal explanation.
- **Truthful indicators:** connection uses the info colour, not a state
  colour; extension dots carry text and accessible names; cost, session
  statistics, and "cost so far today" stay hidden until real data arrives;
  onboarding progress and "Get Started" record only steps that are really
  done; the menu-bar status names the evidence state ("Still gathering",
  "Not enough evidence") before naming a state.
- **Shared components:** `components.SegmentedControl`, `Disclosure`, the
  floating `Toast` (parented to the dashboard, positioned manually, 160 ms
  opacity in/out, hover pauses the timer), `status_pill_qss` /
  `status_dot_qss`, and `format_relative_age` are the single implementations.
  Buttons use the generated `BTN_*_QSS` recipes (accent, ghost, destructive,
  link, segment, pill), which reserve a 2 px transparent border so the
  keyboard focus ring never shifts layout and every recipe has a `:pressed`
  state. Cormorant is restricted to the wordmark and hero numerals
  (`HERO_NUMERAL_QSS`).
- **Windows, not pages:** Settings and Connections close on Escape and carry
  no "Back" control. Settings scrolls; the weekly schedule and the Advanced
  (debug logging) section sit behind disclosures. Connections renders every
  outcome inline as a per-card checklist with numbered steps and re-probes on
  show — no modal message boxes, no Terminal commands.
- **Thread safety:** anything that must touch a widget from the daemon or a
  worker thread goes through a queued Qt signal (`DaemonBridge.ui_task`,
  `calibration_progress`); `QTimer.singleShot` from a non-Qt thread is not
  allowed. Hidden visual surfaces stop their animation/refresh work. Expiry
  and shutdown watchdogs still enforce their deadlines when a card or window
  is hidden; hiding must not extend authority. The overlay's `suppress()`
  stops its visual work, while `retire_suggestion()` also invalidates its ID.

## Surface-specific posture

- **Desktop dashboard:** prioritize live status, signal quality, and explicit
  unavailable states. No geometry animation in data rows. Advanced diagnostics
  are visually subordinate to the consumer summary. A full-screen break always
  exposes an immediate "End early" control and Escape route. Intervention
  footer controls must fit the production `POPUP_WIDTH` (380 px) notification
  width; shortcut hints belong in tooltips/accessibility descriptions when
  visible copy would clip. Status/connection groups wrap, consumer content
  scrolls, and session controls stay reachable at compact sizes. Settings
  and onboarding scroll independently of their pinned completion controls.
  Disconnect clears current biometrics instead of retaining a live-looking
  last value, and reconnect requires a new state update. Boolean, non-finite,
  malformed, and nonpositive pulse values remain unavailable. Capture marked
  stale, explicitly not flowing or without a detected face suppresses biometrics
  in both consumer and advanced views. A pulse-unavailable reason overrides a
  cached pulse; supported pulse values are bounded to the transport's 30–220 bpm.
  Ten seconds without a fresh state clears both tabs' live diagnostics while
  leaving session controls usable; hiding the window does not extend freshness.
  Stop/disconnect clears live Advanced diagnostics, and queued updates cannot
  revive an ending/ended session. The camera section says **Camera estimates**,
  with an experimental caption, not a clinical
  measurement. Verify the dashboard at 380 × 500 logical pixels, Settings at
  440 px wide with schedule/privacy controls expanded, and onboarding at
  600 × 520: no horizontal scrolling, clipped controls, or inaccessible
  session/completion footer.
- **Desktop privacy sheet:** source selection → exact preview → explicit send.
  The review step shows destination, retention caveat, expiry, byte size,
  redactions, and exact outbound prompt; changing sources burns the preview.
- **Browser popup:** fast, compact controls; current-site page-context status;
  truthful disconnected and suggestion-only states; no marketing entrance.
  “Browser found,” native-host protocol, extension-profile detection, daemon
  reachability, and live WebSocket connection are distinct states. Repair copy
  names **Connect Extensions**, `Cmd+Shift+G`, and the mandatory `Cmd+Q`
  restart; it never sends a packaged user to a Python/Terminal command. Stop
  remains pending until the native helper or launcher confirms completion;
  durable stop intent only disables automatic reconnect. Errors remain inline
  and retryable. Quiet-mode radio controls implement roving focus and arrow,
  Home and End navigation.
- **Injected overlays:** one coherent 200 ms entrance, in-place content update,
  quick symmetric exit, no replay on a replacement proposal. Blocking
  surfaces use labelled modal semantics, initial focus, Tab containment,
  Escape where safe, and restoration; non-blocking cards use named regions and
  never steal focus from the active page.
- **Pulse Room/new tab:** decorative motion gets the available delight budget,
  but interactive controls are immediate, reduced-motion stops canvas work,
  hidden tabs cancel canvas work, reduced-transparency removes blur, and resume
  cards retain native link semantics inside a real labelled list. A persisted
  pause control is independent of new-tab opt-out; restore restarts at most
  one loop. Popup and new tab share the same signal projection: hydration
  preserves capture/store/biometric fields and monotonic sample age, invalid
  input/disconnect clears values, and ten seconds without an update expires
  current presentation. That deadline is a UI continuity guard, not a
  physiological accuracy claim. Art follows a fresh camera estimate; it is
  neither an ECG nor breathing guidance. Layout remains in document flow.
- **Onboarding:** progress is semantic, copy states what is detected versus
  configured, Return/click behavior is explicit, and no time-to-result promise
  substitutes for evidence requirements.
- **VS Code:** use theme variables, never assume light/dark colors, keep action
  authority and errors visible, and mirror reduced-motion semantics. The pacer
  owns one requestAnimationFrame loop, cancels it while hidden, and renders a
  static countdown-free guide under Reduce Motion.

## Review gate

For every proposed animation, answer in order:

1. Frequency: will repetition make it feel slow?
2. Purpose: feedback, spatial consistency, state indication, discontinuity,
   explanation, or rare delight?
3. Speed: can functional UI remain below 300 ms?
4. Function: does movement help the user understand or act?

Reject the animation if any answer fails. Review at normal speed, 10% speed,
with rapid interruption, keyboard-only, coarse pointer, light/dark mode, and
Reduce Motion.

## Verification

```bash
uv run --project cortex --locked python -m cortex.scripts.sync_design_tokens --check
QT_QPA_PLATFORM=offscreen uv run --project cortex --locked --extra dev pytest -q \
  cortex/tests/unit/test_desktop_refinement.py \
  cortex/tests/unit/test_shell_window_lifecycle.py \
  cortex/tests/unit/test_ui_contrast_contract.py \
  cortex/tests/unit/test_desktop_shell_contracts.py \
  cortex/tests/unit/test_dashboard_stop.py \
  cortex/tests/unit/test_desktop_tokens_smoke.py \
  cortex/tests/unit/test_overlay_animation.py \
  cortex/tests/unit/test_breathing_pacer_config.py \
  cortex/tests/unit/test_break_overlay_reduced_motion.py

cd cortex/apps/browser_extension
pnpm exec tsc --noEmit
pnpm exec vitest run __tests__/ambient_motion.spec.ts \
  __tests__/injected_motion.spec.ts \
  __tests__/newtab_a11y.spec.tsx \
  __tests__/popup_dialog_accessibility.spec.tsx

cd ../vscode_extension
npm run lint && npm run compile && npm test
```

The current implementation and acceptance record is
[evidence continuity and interaction refinement](../../docs/implementation-2026-10.md).
The October 2026 check includes real offscreen Qt layouts and four subprocess
Quit cases across both desktop hosts, inspected desktop/browser screenshots,
and headless startup, shutdown, and same-profile restart of the built arm64
app. It does not establish VoiceOver behavior, physical TCC/camera handling,
human usability, or clinical accuracy. Complete the
[manual release protocol](../../docs/release/manual-release-validation.md)
against the exact signed/notarized candidate before recording those cases as
passed.
Earlier plans remain in `plans/`. Mark a plan complete only after its
mechanical and interaction checks pass.
