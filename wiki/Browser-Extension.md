# Chrome and Edge extension

The Plasmo/React Manifest V3 extension is an authenticated, untrusted client of
the local Cortex daemon. It provides connection controls, proposal UI, focus
sessions, bounded activity/resume metadata, learning-site helpers, and optional
active-page context.

## Authority boundary

Receiving an intervention or break message renders information only. It does
not close/group/hide tabs, block a site, or change a page. An optional browser
effect runs only after the daemon sends an exact, unexpired authorization for
the displayed manifest. The capability executor validates target, digest,
nonce, mode, and idempotency, then returns a typed receipt for verification and
restore.

Production policy is deterministic. The extension does not run a contextual
bandit, infer biology-driven breaks, or autonomously restructure tabs.

Applying a proposal is one explicit flow with one state machine shared by the
popup and the in-page panel: *Applying…* (the button is disabled), *Applied ·
Undo* (undo stays available for at least a minute), *2 of 3 applied · Undo*,
or *Nothing changed — reason*. A failure is never shown as "Done". The panel
is removed when the daemon restores or times out an intervention, and a
timeout is reported as `expired`, never as a user dismissal.

**Stop Cortex** in the popup asks for confirmation because it shuts down the
camera and the app. Closing a popup, ending a focus timer and stopping the
daemon are separate actions.

## Connection, signals and quiet controls

The popup and Pulse Room share the same presentation rules. Hydration includes
biometrics, camera availability, storage status and the age of the worker's
original sample. A disconnect clears current readings immediately. Ten seconds
without a new state update changes the display to “Waiting for fresh signals”
and hides held pulse/blink values; rereading the cache cannot renew them. A
missing face, stopped frames, stale capture or unavailable pulse hides the
corresponding measurement. Behavioral support may remain available without pulse.

The ten-second deadline concerns UI continuity, not physiological accuracy or
the inference engine's separate three-second evidence-gap reset.

Quiet mode offers **Off**, **Snooze 15m**, **Quiet** and **Pause**. Snooze and
Quiet suppress suggestions; Pause also releases the camera until resumed.
The selector has one Tab stop. Arrow keys move and select with wrapping;
Home/End select the first/last option; Space/Enter activate the focused option.
The countdown/status is announced without moving keyboard focus.

## Verified Stop and launch ordering

The worker owns a single pending Stop operation and remembers the stop intent
until explicit Start/Open. Keep-alive and worker restart cannot silently clear
that intent. The popup stays pending and offers a retryable error if shutdown
cannot be confirmed; disconnect alone never produces “Cortex is stopped.”

Ordinary Open/Start tries the HTTP launcher, then native messaging, then an
existing daemon's direct WebSocket connection. Only a successful connected
continuation can send the camera-enable setting. Duplicate launch requests share
that attempt; a Stop interrupts its remaining stages.

1. Invalidate any launch continuation and wait for its already-issued bounded
   helper request. Duplicate launches share one operation. No later launch
   fallback or camera-enable message may run after Stop interrupts it.
2. Send authenticated WebSocket `SHUTDOWN`, allow a 300 ms flush, then try
   authenticated HTTP `POST /shutdown`. Keep the socket available for shutdown
   receipts/restoration until the stop chain finishes.
3. Give a responding daemon a 20-second grace window. Endpoint loss ends that
   poll but does not prove process exit.
4. Ask the native helper to stop and verify the daemon, with a 40-second
   request budget. If native messaging is unavailable, try the authenticated
   launcher with the same budget. A native timeout or a completed negative
   result remains an error; it does not start a competing stop chain.
5. The helper identifies only the daemon using its pidfile, listening socket
   or anchored executable identity. Its own accepted graceful request has a
   20-second grace; it then uses SIGTERM and a five-second grace, then SIGKILL
   only if needed, and verifies that both the daemon PID and endpoint are gone.
6. Clear current browser readings and proposal presentation, including injected
   panels. Retain transaction receipts and tab snapshots for exact recovery.

The popup has a 150-second watchdog covering a pending launch plus shutdown.
It applies to the initiating popup, broadcasts and reopening while Stop is
pending. A watchdog error is an unconfirmed outcome, not success. Starting is
rejected while the worker's Stop operation is still active.

## Permissions

The extension requests only what shipping features use: `activeTab`,
`scripting`, `tabs`, `tabGroups`, `storage`, `alarms`, `nativeMessaging`, and
`notifications`, plus optional per-origin host access that you grant from the
popup. It does not request `bookmarks` or `webNavigation`.

## New tab page

Installing the extension replaces the browser's new-tab page with the Pulse
Room. If you prefer the browser's default page, turn the override off in the
popup settings; the page then shows a single link to the browser's default
new tab and remembers your choice.

Pulse Room keeps status, the central artwork and resume cards in responsive
document flow. Short windows can scroll and long resume lists scroll within
their row. **Pause motion** persists locally; **Resume motion** restores it.
Reduced Motion, a hidden tab and the disabled new-tab surface stop the canvas
loop. Returning to an enabled visible surface starts one loop only when motion
is permitted.

The art follows the latest experimental pulse estimate decoratively. It is not
a breathing pacer or a beat-by-beat cardiac recording, and it is hidden from
assistive technology. Screen readers receive stable signal status rather than
per-frame breathing instructions. Pausing artwork does not pause sensing; use
the separate Quiet-mode **Pause** or **Stop Cortex** control for that intent.

## Privacy

- Static content scripts are limited to declared learning sites.
- Incognito exits before initialization and background rejects incognito
  senders.
- Activity records contain sanitized bounded metadata, not page excerpts or
  source code, and are capped at 200.
- Optional page context requires both Cortex consent for the exact origin and
  browser host permission. Revocation clears current content and attempts to
  remove optional permission.
- URL userinfo, fragments, tracking/secret-like query parameters, and
  credentials are removed before storage.

## Install from source

```bash
cd cortex/apps/browser_extension
pnpm install --frozen-lockfile
pnpm exec plasmo build
pnpm exec plasmo build --target=edge-mv3
```

Use the Node version in `.node-version` and pnpm version in `package.json`.

Load `build/chrome-mv3-prod` or `build/edge-mv3-prod` from the browser's
extensions page in Developer mode.

Install the native host once from the repository environment:

```bash
uv run --project cortex --locked --extra dev --extra codegen python -m cortex.scripts.install_native_host
```

Run this native-host command from the repository root, not from the browser
extension directory.

Then fully quit Chrome/Edge with Cmd+Q and reopen it. Reloading the extension
or closing tabs is insufficient after a native-host manifest change. The
installer uses an absolute interpreter in the installed host copy and includes
detected extension origins; the tracked source shebang remains portable.

## Development gates

```bash
pnpm exec tsc --noEmit
pnpm test
pnpm exec plasmo build
pnpm exec plasmo build --target=edge-mv3
```

Never edit `.plasmo/`; it is generated. New capabilities must add required
manifest permissions, generated wire schemas, producer/consumer tests,
authorization/receipt behavior if mutating, and reduced-motion/keyboard/focus
coverage. See [UI design](https://github.com/StevenWang-CY/cortex/blob/main/cortex/docs/ui-design.md) and
[privacy](https://github.com/StevenWang-CY/cortex/blob/main/cortex/docs/privacy.md).

The October review passed 360 browser tests in 76 files, TypeScript and both
production targets. Light/dark, compact, offline/retry, stale and paused states
were inspected using isolated synthetic fixtures. These checks do not stand in
for physical camera, browser-profile or assistive-technology validation.
