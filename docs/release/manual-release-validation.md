# Signed release-candidate validation protocol

Complete this on a clean supported Mac for every architecture you can exercise
(both arm64 and Intel for an independently reviewed release; at least one for a
self-attested release). Use the exact notarized DMG hashes staged in the
CI-created draft release. Create one `manual-release-evidence-<arch>.json` per
exercised architecture from
[`manual-release-evidence.template.json`](manual-release-evidence.template.json),
validated against
[`manual-release-evidence.schema.json`](manual-release-evidence.schema.json).
Do not include usernames, source code, page bodies, credentials, raw camera
data, or institution-specific paths in evidence.

Name each uploaded evidence asset `manual-evidence-<architecture>.<extension>`
or `manual-evidence-<architecture>-<case>.<extension>`. Assets must be non-empty,
must match the record's architecture, and may be referenced by multiple cases
when one redacted recording or log genuinely covers them.

The October 2026 arm64 development build passed headless packaged startup,
SIGTERM shutdown, and same-profile restart, with camera/input acquisition
disabled. Those results and offscreen UI screenshots do not execute this
protocol. Its physical, signed/notarized, clean-profile checks remain required
under the selected assurance tier. See the
[current local validation](README.md#current-local-validation).

## Installation and identity

- Verify SHA-256 and GitHub provenance/SBOM attestations.
- Verify `stapler`, `spctl`, `codesign`, bundle ID, version, architecture, and
  minimum OS; record commands and exit codes.
- Open the DMG, drag Cortex to `/Applications`, eject, then launch the installed
  copy—not an App Translocation or mounted-DMG path.
- Confirm the first launch contains no unsigned-helper or malware warning.

## Onboarding and permissions

- Complete every onboarding step with keyboard alone and with VoiceOver.
- Deny camera, input monitoring, automation, notification, and optional browser
  permissions one at a time; confirm visible degradation and a recovery path.
- Grant each permission through System Settings; confirm no global `tccutil`
  reset is requested or executed.
- Confirm BYOK lands in Keychain and no credential appears in app resources,
  logs, process arguments, exported settings, or child-process environment.
- At a short window height, scroll through onboarding while keeping its
  completion control reachable. Confirm that denied permissions remain
  actionable, and that optional provider setup explains local default
  planning and exact-preview authorization before external sharing.

## Desktop layout and keyboard review

Record these observations under `onboarding.keyboard_voiceover` and
`authority.transaction_restore`, as applicable; retain the existing 14-case
catalog rather than inventing additional case IDs.

- Resize the dashboard to 380 × 500 logical pixels, Settings to 440 px wide,
  and onboarding to 600 × 520. Expand schedule/privacy controls and display
  long state text and multiple status chips. Require readable wrapping, no
  horizontal scroll or clipped controls, and reachable session/completion
  controls. Check both consumer and advanced dashboard tabs.
- With keyboard focus in another app, receive a non-sensitive test proposal.
  Confirm arrival does not activate Cortex or intercept typing. Open the
  dashboard normally, Tab to **Review suggestion**, and activate it by
  keyboard. Require the exact current card to open with **Dismiss** focused;
  Tab, Shift+Tab, Escape, and the advertised Command shortcuts must work.
  Reviewing alone must produce no workspace change or action receipt.
- Dismiss the card and confirm its dashboard entry disappears. Repeat with
  quiet mode, End session, disconnect, and a controlled earlier manifest
  expiry. A same-ID update or reopening must not extend the original deadline
  (at most five minutes from first receipt). An expired/retired card must not
  dispatch an action from a delayed click.
- Receive a replacement proposal and then a late terminal/dismissal event for
  the prior ID; the replacement must remain reviewable. Reopening or updating
  one visible suggestion must not inflate the displayed suggestion count.
- Run the same keyboard route with VoiceOver and with Reduce Motion enabled.
  Require meaningful names/status, visible focus, and static readable feedback
  when animation is disabled. Record actual results, including any untested
  combinations, rather than deriving accessibility claims from screenshots.

## Browser/editor installation

- Install Chrome and Edge packages from the app. Confirm each manifest points
  exactly to `/Applications/Cortex.app/Contents/MacOS/CortexNativeHost`, has
  only that browser's expected origins, and that the nested executable's
  signature and architecture match the application. Fully quit with Cmd+Q and
  reopen after each registration change.
- Verify framed `status`, authenticated launch, reconnect, `get_auth_token`,
  dashboard raise, and stop behavior. Confirm a missing/malformed helper is
  reported as failure rather than connected, and confirm native-host logs stay
  bounded without leaking the token.
- Revoke optional page-context permission and confirm content is immediately
  cleared while metadata-only telemetry remains bounded.
- Install/update/uninstall the VS Code VSIX from a Finder-launched app whose
  shell PATH is minimal.

## Runtime, authority, and recovery

- Exercise the in-process app and WebSocket client separately; never start two
  daemons on the same ports. In each mode close the dashboard with its red
  close control and Cmd+W, then reopen it from the menu bar. The session must
  continue. Closing the last visible Settings/Connections window must not
  start a recap or stop the daemon.
- Choose **End session** and dismiss/read the recap. Sensing must stop while
  Cortex stays open. In-process mode must offer **Start session** and start a
  fresh session successfully; the WebSocket client must not promise it can
  restart its external daemon. Confirm **View full report** keeps the app
  open and displays the report already received.
- Quit using the dashboard, menu-bar item, and Cmd+Q, including while a recap
  is pending and after the daemon has already stopped. Final quit must exit
  the GUI process; it must not leave an invisible app or a Quit event loop
  running. Repeat a launch → healthy → quit → launch sequence using the same
  fixture profile/storage. Require a new boot ID, retained intended history,
  no duplicate lifecycle work, and no port-conflict errors.
- Exercise the browser/editor stop paths while those clients are connected.
  Confirm the daemon stops while Chrome/Edge, their network services, and the
  editor/extension host remain alive.
- Exercise camera warm-up, face loss, iPhone Continuity Camera appearance/index
  reshuffle, camera busy, permission revoked, sleep/wake, browser worker
  suspension, daemon restart, and network/provider unavailable.
- After a displayed camera estimate, disconnect the daemon or mark capture
  stale/not flowing. The value must clear. Reconnect without a fresh state
  update and confirm the desktop continues to show waiting/unavailable copy.
  Restore fresh valid input and confirm the value returns; missing, boolean,
  non-finite, or nonpositive pulse input must never become a plausible live
  reading. Browser popup/new-tab cached readings must expire after ten seconds
  without a new state update. This tests presentation freshness, not accuracy.
- Confirm proposal receipt performs no mutation. Authorize one reversible test
  manifest; confirm exact effect, durable receipt, postcondition, restore, and
  repeated restore idempotence.
- Inject disconnect/crash at durable-intent/before-apply, after-apply/before-ack,
  partial apply, and shutdown. Confirm truthful recovery/visible unresolved
  state and that user changes are never overwritten by undo.
- On completed daemon stop, verify HTTP 9472 and WebSocket 9473 close, camera
  ownership is released, pending WS frames drain, and unresolved restore is
  recovered on next launch. An ended in-process session deliberately retains
  the desktop GUI; require its exit only after explicit Quit. Port 9471 belongs
  to the optional launcher and is checked separately when that service is
  stopped or uninstalled. Inspect listening ownership, not every process with
  a client connection to a Cortex port.

## Update, export, deletion, uninstall

- Install the prior supported version, create non-sensitive fixture history,
  update to the candidate, and verify migration backup/checksum/integrity plus
  intervention recovery.
- Export a scoped fixture dataset. Confirm it contains documented fields and no
  frames, prompts, code/page bodies, key text, or credentials.
- Attempt deletion while restore evidence is active; require refusal. Resolve
  it, type `DELETE CORTEX DATA`, and verify SQLite/storage/backups are gone.
- Clear browser local storage and optional permissions; uninstall Chrome/Edge
  native hosts and VS Code extension; delete requested Keychain items and the
  app. Verify no ports/process/camera ownership and document any OS-managed
  logs or backups Cortex cannot erase.

## Acceptance

Each record must contain exactly the 14 catalogued case IDs and declare its
`assurance_tier` ([ADR 0007](../adr/0007-tiered-release-assurance.md)):

- `self-attested` — at least one architecture record signed by the
  `maintainer`; the core cases `artifact.identity`, `install.launch`,
  `browser.chrome_native`, `runtime.lifecycle_camera_tcc`, and
  `uninstall.cleanup` are `passed` with evidence; every other case is `passed`
  with evidence or `not_run` with a `reason` of at least 20 characters. An
  architecture without a record is published as CI-verified only.
- `independently-reviewed` — records for both architectures, every case
  `passed`, a `builder` and an `independent_reviewer` on each record, and no
  identity appearing in both roles on either architecture.

"Not run" is an honest, published state. Every record must identify the exact
candidate artifact hash. Screenshots of an ad-hoc build or tests against a
source checkout cannot establish a manual pass for the signed candidate.
`failed` and `blocked` cases block promotion.

Before requesting protected-environment approval, download all draft assets and
run the same promotion validator locally:

```bash
uv run --project cortex --locked --extra dev \
  python -m cortex.scripts.validate_release_records \
  --records-dir release-assets \
  --asset-dir release-assets \
  --expected-version "$(uv run --project cortex --locked python -m cortex.scripts.sync_versions --print)" \
  --expected-commit "$(git rev-parse HEAD)" \
  --tier self-attested \
  --output release-assets/release-promotion-validation.json \
  --notes-output release-assets/assurance-notes.md
```

The `Publish validated macOS release` workflow repeats this validation, verifies
GitHub provenance for both DMGs, appends the rendered assurance section to the
release notes, records the machine-readable promotion decision, and only then
converts the draft into the latest public release.
