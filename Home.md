# Cortex

Cortex 0.3.3 is a macOS alpha research prototype for local, user-controlled
workspace support. It combines bounded activity telemetry, optional
experimental camera signals, browser/editor context, and a deterministic or
schema-validated planner to present conservative suggestions.

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

- [Setup](Setup.md)
- [How it works](How-It-Works.md)
- [Architecture](Architecture.md)
- [Browser extension](Browser-Extension.md)
- [Calibration](Calibration.md)
- [Privacy](Privacy.md)
- [API reference](API-Reference.md)
- [Limitations and prohibited uses](docs/limitations.md)
- [Engineering finding ledger](audit/findings.md)
- [Release verification](docs/release/README.md)

## Runtime boundary

The app is a local modular monolith. FastAPI (`127.0.0.1:9472`), WebSocket
(`127.0.0.1:9473`), and the optional launcher (`127.0.0.1:9471`) require a
local capability token for sensitive operations. Chrome/Edge and VS Code are
untrusted clients. The default planner makes no model network request.

See [README.md](README.md) for developer commands and
[IMPLEMENTATION.md](IMPLEMENTATION.md) for the complete audit, design,
research basis, work packages, risks, and definition of done.
