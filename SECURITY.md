# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security reports.

Use **[GitHub Security Advisories](https://github.com/StevenWang-CY/cortex/security/advisories/new)**
to file a private report. Reports are reviewed on a best-effort basis.
Private advisories are the only supported reporting channel;
the project does not publish a maintainer email address.

## Supported versions

Cortex is a personal portfolio project maintained by a single author
on a best-effort basis. Only the most recent tagged release receives
security fixes.

| Version | Supported |
|---------|-----------|
| latest tagged release | ✅ |
| older tags            | ❌ |

## Privacy & biometric boundary commitments

Cortex processes physiological data locally. The codebase enforces
these invariants and changes that weaken them are considered security
regressions:

1. **No video is persisted.** Webcam frames are processed in memory
   and discarded. No frame buffer is written to disk.
2. **No raw biometrics in LLM payloads.** Camera frames, landmarks,
   waveforms, inter-beat intervals, heart rate, HRV, blink, posture, and
   respiration never leave the machine. The default planner mode makes
   no model network call at all. In the explicit external mode, the
   request carries only the redacted workspace text the user selected
   and previewed (basenames, error summaries, tab titles) plus, when the
   user selects it, the heuristic support label with its confidence and
   dwell time — never a biometric measurement.
3. **Local-only network surface.** FastAPI (`:9472`), WebSocket
   (`:9473`), and the launcher agent (`:9471`) bind to `127.0.0.1`.
4. **Capability-token gate.** Every mutating HTTP route and the
   WebSocket handshake require a 256-bit token written to
   `~/Library/Application Support/Cortex/auth.token` at mode `0600`
   and rotatable from the desktop UI.
5. **Exact action authority.** The default is `suggest_only`. Enabled
   workspace effects require the allowed ceiling in the
   five-level consent system (OBSERVE → SUGGEST → PREVIEW →
   REVERSIBLE_ACT → AUTONOMOUS_ACT), an exact unexpired action manifest and
   one-time authorization. Effects use durable receipts, postcondition checks
   and scoped restoration; a model response, consent level or generic undo
   stack alone is insufficient authority.
6. **Lifecycle authority.** Stop targets only the identified daemon, never
   browser/editor client sockets. The UI does not report stopped until a helper
   verifies exit. Pending launches cannot re-enable sensing after Stop;
   unresolved restoration records survive presentation cleanup.

If you believe any of these invariants is violated by current code,
please file a security advisory.

## Out of scope

The following are not security vulnerabilities for the purposes of
this project:

- Bugs that require root, physical machine access, or a malicious
  app already running on the user's Mac.
- Ordinary LLM output quality. A prompt injection that bypasses context
  disclosure, action authorization or target validation is in scope.
- Issues caused by user-supplied API keys leaking outside the
  Cortex runtime (Keychain storage is the documented path).
- Upstream product defects should also be reported to the relevant maintainer.
  A vulnerable bundled dependency or unsafe Cortex integration remains relevant
  to Cortex's dependency and release review.

## Dependency and release evidence

`make audit` checks Python, browser and editor dependency reports. Browser
exceptions identify advisory, dependency path, maximum severity, mitigation and
expiry; they are reviewed exceptions, not evidence of zero vulnerabilities.
The [implementation record](docs/implementation-2026-10.md) records the latest
local results. An ad-hoc development build does not establish notarization,
Intel parity, hardware validation or independent security certification.

See the [security model](cortex/docs/security.md),
[privacy contract](cortex/docs/privacy.md) and [release guide](docs/release/README.md).
