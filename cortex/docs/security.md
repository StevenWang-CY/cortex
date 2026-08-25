# Security model

Last reviewed: 2026-08-25.

Cortex is a local macOS application with browser/editor clients and optional
cloud planning. Its security model is capability-based and fail-closed, but it
does not claim to isolate data from malware already running as the same user,
an administrator, a compromised browser profile, or a compromised provider
account.

## Assets and trust boundaries

| Asset | Boundary and control |
| --- | --- |
| Camera and behavioral observations | Local process memory; quality/availability gates; no external planner field |
| Local API and WebSocket | `127.0.0.1` only; canonical 256-bit bearer capability; authenticated WS handshake |
| Workspace context | Untrusted client input; schema/size validation; external send only through the privacy broker |
| Workspace effects | Exact action manifest and one-time authorization; idempotency, receipt, verification, restore |
| Provider credentials | macOS Keychain, Vertex ADC, or provider environment; excluded from bundles and persistence |
| Durable lifecycle data | Single-owner SQLite path with migrations, integrity checks, bounded writer, retention/export/delete |
| Browser activity metadata | Extension local storage; incognito excluded; untrusted messages validated and minimized |
| Release artifacts | Generated-contract, dependency, build, signing, and release-evidence gates |

## Local transport

HTTP mutation routes require the same capability token used by the desktop,
browser, and editor clients. WebSocket peers authenticate before application
messages. Tokens are owner-readable (`0600`), rotate through the supported
settings flow, and are not accepted from query strings. Correlation IDs aid
incident reconstruction without putting payload content in logs.

Loopback binding prevents ordinary remote access; it does not make an
unauthenticated localhost endpoint safe. A same-user process that can read the
token or instrument the app is inside the threat boundary.

## Native messaging and macOS

The native-host manifest has a deterministic extension allowlist and an
installer-patched absolute interpreter path. Chrome/Edge must be fully
restarted after manifest changes. Terminal.app foreground launch is a macOS
TCC requirement for camera permission, not a privilege-separation boundary.
Never reset global Camera TCC state. Bundled apps use the canonical installed
path to avoid App Translocation ambiguity, and GUI-launched tools are resolved
through known application-bundle paths rather than a shell `PATH`.

## Browser and editor clients

Clients are untrusted transport adapters. Message types and size bounds are
validated; generated TypeScript declarations are checked against Pydantic
schemas. Incognito collection is disabled. Browser page-body extraction needs
an exact-origin grant plus an explicit Cortex consent record. Proposal frames
cannot execute actions. Unknown, expired, changed, replayed, or wrong-client
commands fail closed.

## Model and prompt injection

Workspace text is tainted data. The prompt marks origin/disclosure metadata,
but no prompt can reliably neutralize every injection. Safety therefore does
not depend on the model following instructions:

1. the privacy broker decides what may leave the device;
2. output must parse into the local proposal schema;
3. the allowed action catalog is locally generated;
4. a separate exact authorization transaction grants any capability;
5. adapters verify the effect and persist a receipt/inverse.

An injected page can influence a recommendation if its text was explicitly
selected. It cannot create authority, widen a target, or bypass consent.

## Secrets, logs, and errors

Redaction detects common provider tokens, private keys, credentials, JWTs,
generic secret assignments, paths, and bounded high-entropy candidates. It is
not a DLP guarantee. Logs record classifications, counts, IDs, state changes,
and generic errors—not raw prompts, camera frames, API keys, or workspace
excerpts. Provider exceptions are converted to bounded local error messages;
raw SDK payloads are not returned to clients.

## Primary abuse cases

| Scenario | Expected result |
| --- | --- |
| Remote host connects to API | Loopback bind prevents reachability |
| Local process lacks token | Authenticated HTTP/WS operation rejected |
| Stale or replayed context preview | Handle absent/expired; no network request |
| Preview body changes | Digest mismatch; deterministic no-content fallback |
| Prompt asks to close/delete/change files | At most an inert proposal; no capability |
| Extension sends arbitrary activity object | Allowlisted runtime shape, bounds, URL/text minimization, or rejection |
| Incognito content script emits | Initialization and background boundaries reject it |
| Apply acknowledgement is lost | Durable idempotency/receipt reconciliation; no blind duplicate |
| User changes a Cortex-owned effect | Restore respects user supersession |
| Shutdown during mutation | Restore is attempted; unresolved receipts recover next start |
| Provider/account claims ZDR | UI reports it as unverified until the user verifies the provider account/contract |

## Residual risks

- Same-user malware, browser compromise, provider compromise, malicious
  dependencies, and an untrusted signed update are not solved by localhost
  authentication.
- Page titles, URL paths, diagnostic messages, and local activity history can
  be sensitive even when they are not page-body excerpts.
- Heuristic redaction can miss novel or fragmented secrets and can also redact
  benign high-entropy values.
- Ad-hoc signing is appropriate for local development, not a substitute for a
  Developer ID, notarization, update verification, and release provenance.
- Experimental physiological estimates are wellness/research signals, not
  medical measurements or diagnoses.

## Reporting and verification

Report vulnerabilities privately through [GitHub Security
Advisories](https://github.com/StevenWang-CY/cortex/security/advisories/new).
Do not include live credentials, private source code, camera frames, or other
people's data in a report. Include the app version, platform, bounded steps,
expected/actual authorization state, and a redacted correlation ID where
available.

The security/privacy regression set is described in
[`privacy.md`](privacy.md) and `IMPLEMENTATION.md` section 13.5. Release
candidates must also run secret scanning, dependency policy, SBOM/provenance,
bundle-content inspection, signed-install lifecycle tests, and local-data
deletion verification.
