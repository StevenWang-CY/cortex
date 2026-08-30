# Signed release-candidate validation protocol

Complete this on clean supported arm64 and Intel Macs. Use the exact notarized
DMG hashes staged in the CI-created draft release. Create
`manual-release-evidence-arm64.json` and
`manual-release-evidence-x86_64.json` from
[`manual-release-evidence.template.json`](manual-release-evidence.template.json),
validated against
[`manual-release-evidence.schema.json`](manual-release-evidence.schema.json).
Do not include usernames, source code, page bodies, credentials, raw camera
data, or institution-specific paths in evidence.

Name each uploaded evidence asset `manual-evidence-<architecture>.<extension>`
or `manual-evidence-<architecture>-<case>.<extension>`. Assets must be non-empty,
must match the record's architecture, and may be referenced by multiple cases
when one redacted recording or log genuinely covers them.

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

## Browser/editor installation

- Install Chrome and Edge packages from the app, inspect the native-host
  manifest/copy and allowed origin, then fully quit with Cmd+Q and reopen.
- Verify native `get_status`, authenticated launch, reconnect, and stop framing.
- Revoke optional page-context permission and confirm content is immediately
  cleared while metadata-only telemetry remains bounded.
- Install/update/uninstall the VS Code VSIX from a Finder-launched app whose
  shell PATH is minimal.

## Runtime, authority, and recovery

- Start/stop repeatedly in external-daemon and in-process modes.
- Exercise camera warm-up, face loss, iPhone Continuity Camera appearance/index
  reshuffle, camera busy, permission revoked, sleep/wake, browser worker
  suspension, daemon restart, and network/provider unavailable.
- Confirm proposal receipt performs no mutation. Authorize one reversible test
  manifest; confirm exact effect, durable receipt, postcondition, restore, and
  repeated restore idempotence.
- Inject disconnect/crash at durable-intent/before-apply, after-apply/before-ack,
  partial apply, and shutdown. Confirm truthful recovery/visible unresolved
  state and that user changes are never overwritten by undo.
- Stop from every UI. Verify ports 9471–9473 close, no matching process remains,
  no orphan owns the camera, pending WS frames drain, and unresolved restore is
  recovered on next launch.

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

Each record must contain exactly the 14 catalogued case IDs, every case must be
`passed`, and every case must reference at least one evidence asset uploaded to
the draft. “Not run,” screenshots without artifact hash, ad-hoc builds, and
tests against a source checkout do not satisfy the signed-release gate. A
builder and an independent reviewer sign each record using stable organization
handles in `reviewer_id`; no independent reviewer may also be a builder on
either architecture.

Before requesting protected-environment approval, download all draft assets and
run the same promotion validator locally:

```bash
uv run --project cortex --locked --extra dev \
  python -m cortex.scripts.validate_release_records \
  --records-dir release-assets \
  --asset-dir release-assets \
  --expected-version 0.3.12 \
  --expected-commit "$(git rev-parse HEAD)" \
  --output release-assets/release-promotion-validation.json
```

The `Publish validated macOS release` workflow repeats this validation, verifies
GitHub provenance for both DMGs, records the machine-readable promotion
decision, and only then converts the draft into a public release.
