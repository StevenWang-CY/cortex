# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security reports.

Use **[GitHub Security Advisories](https://github.com/StevenWang-CY/cortex/security/advisories/new)**
to file a private report. You will receive an acknowledgement within a
few days. If you do not have GitHub access, email the maintainer
listed in [pyproject.toml](cortex/pyproject.toml).

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
2. **No biometrics in LLM payloads.** Heart rate, HRV, blink, posture,
   and respiration never leave the machine. The LLM call carries only
   workspace text context (file paths, error messages, tab titles).
3. **Local-only network surface.** FastAPI (`:9472`), WebSocket
   (`:9473`), and the launcher agent (`:9471`) bind to `127.0.0.1`.
4. **Capability-token gate.** Every mutating HTTP route and the
   WebSocket handshake require a 256-bit token written to
   `~/Library/Application Support/Cortex/auth.token` at mode `0600`
   and rotatable from the desktop UI.
5. **Consent ladder.** Workspace mutations require earned trust through
   a 5-level consent system (OBSERVE → SUGGEST → PREVIEW →
   REVERSIBLE_ACT → AUTONOMOUS_ACT). Destructive actions are
   reversible via the undo stack.

If you believe any of these invariants is violated by current code,
please file a security advisory.

## Out of scope

The following are not security vulnerabilities for the purposes of
this project:

- Bugs that require root, physical machine access, or a malicious
  app already running on the user's Mac.
- LLM output content (the LLM is a third-party service; Cortex
  validates output against a strict schema and degrades gracefully).
- Issues caused by user-supplied API keys leaking outside the
  Cortex runtime (Keychain storage is the documented path).
- Issues in macOS, Chrome, Edge, MediaPipe, OpenCV, or other
  upstream dependencies — please report those to their respective
  maintainers.
