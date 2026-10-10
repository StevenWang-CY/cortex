# Cortex

Cortex 0.5.0 is a macOS alpha research prototype for local, user-controlled
workspace support. It combines bounded activity telemetry, optional
experimental camera signals, browser/editor context, and a deterministic or
schema-validated planner to present conservative suggestions.

This wiki source reflects the October 2026 implementation review. The application
version is `0.5.0`; its deterministic behavioral model is `2.5.0`, with feature
schema `support-features-v2.2.0`. These version numbers identify different
contracts. Local edits to `wiki/` do not publish the GitHub wiki or a release.

It does **not** diagnose or measure stress, overwhelm, attention, fatigue,
flow, apnea, productivity, or any medical/cognitive condition. Production
support scores are heuristic telemetry summaries with an explicit `unknown`
state. Camera-derived HRV, respiration, LF/HF, breath-pause detection, and
physiology-triggered breaks are unavailable. Webcam pulse is experimental and
does not affect the production support decision.

The default `suggest_only` mode cannot close/hide tabs, fold or edit code,
rearrange windows, or otherwise change a workspace. Optional effects require
an exact displayed manifest, one-time authorization, durable receipt,
postcondition verification, and scoped restore.

## Start here

- [Setup](Setup)
- [How it works](How-It-Works)
- [Architecture](Architecture)
- [Browser extension](Browser-Extension)
- [Calibration](Calibration)
- [Privacy](Privacy)
- [API reference](API-Reference)
- [Troubleshooting](Troubleshooting)
- [Limitations and prohibited uses](https://github.com/StevenWang-CY/cortex/blob/main/docs/limitations.md)
- [Engineering finding ledger](https://github.com/StevenWang-CY/cortex/blob/main/audit/findings.md)
- [Release verification](https://github.com/StevenWang-CY/cortex/blob/main/docs/release/README.md)

## Runtime boundary

The app is a local modular monolith. FastAPI (`127.0.0.1:9472`), WebSocket
(`127.0.0.1:9473`), and the optional launcher (`127.0.0.1:9471`) require a
local capability token for sensitive operations. Chrome/Edge and VS Code are
untrusted clients. The default planner makes no model network request.

The installed app owns the daemon in-process. A source checkout can run that
same host with `--in-process`, or run a separate daemon and connect the desktop
as a WebSocket client. Closing the dashboard keeps the session running; ending
a focus session does not quit the app. Explicit Quit and the browser's confirmed
Stop action follow the shutdown path.

Current refinements distinguish unavailable input from observed inactivity,
reset inference dwell across evidence gaps, and expire browser signal displays
when updates stop. Pulse Room has persistent motion pause and respects reduced
motion. Shutdown remains pending until the helper verifies process exit; an
unconfirmed stop remains visible as an error.

See [README.md](https://github.com/StevenWang-CY/cortex/blob/main/README.md) for developer commands and
[IMPLEMENTATION.md](https://github.com/StevenWang-CY/cortex/blob/main/IMPLEMENTATION.md) for the implementation history.
The current checkout's `docs/implementation-2026-10.md` records the latest audit,
design decisions, completed changes, validation and remaining release/research
requirements. A verified local ad-hoc arm64 build is not a published, notarized
release or physiological validation.
