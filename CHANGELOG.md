# Changelog

All notable changes to Cortex. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v0.3.3] — 2026-08-25

This patch makes the post-notarization bundle scanner distinguish genuine
secrets from official provider fixtures and opaque native-library bytes. It
contains no runtime algorithm, authority, privacy, or interaction-policy
change from v0.3.0.

### Fixed

* Generic credential signatures now run only against UTF-8/text-like bundle
  members. Exact sensitive build-environment values and non-generic personal
  roots remain byte-scanned in every member, including native binaries.
* Complete private keys require a base64 PEM payload and matching footer,
  including compact PKCS#8 and encrypted-key forms; parser marker strings
  embedded by TLS libraries no longer count as keys.
* AWS access-key IDs ending in the documented `EXAMPLE` fixture suffix and
  same-named SDK constants/parameters no longer count as credentials. Real AWS
  secret assignments retain length and alphabet validation.
* The macOS bundle now collects only immutable `*.sql` migration resources,
  preventing ignored local bytecode caches and their absolute builder paths
  from entering an otherwise clean distributable.
* Added a release-failure corpus covering boto3/botocore example IDs, native
  crypto/parser literals, random opaque bytes, complete text credentials,
  exact secrets inside binaries, boundary-spanning tokens, local paths, and
  unreadable files. The scanner also completes with zero findings across the
  full locked Python `site-packages` tree.

### Release process

* The immutable v0.3.2 tag is retained as the artifact-free candidate whose
  arm64 DMG was signed, accepted by Apple, and stapled before third-party
  fixtures triggered the fail-closed scanner. v0.3.3 supersedes it.

## [v0.3.2] — 2026-08-25

This patch makes post-notarization bundle scanning distinguish complete
credentials and non-generic local build paths from redaction rules and
third-party debug metadata. It contains no runtime algorithm, authority,
privacy, or interaction-policy change from v0.3.0.

### Fixed

* Replaced prefix-only secret matching with bounded, high-confidence rules for
  Anthropic/OpenAI keys, permanent and temporary AWS credentials, private
  keys, current GitHub token families, and current Slack token families. The
  scanner continues to detect matches split across streaming read boundaries
  and now fails closed if a bundle member cannot be read.
* Scoped personal-path detection to the actual non-generic home roots involved
  in a build. Generic GitHub runner paths and dependency metadata no longer
  fail a release merely because they contain `/Users/` or `/home/`.
* Added regression cases for complete credentials, boundary-split keys,
  explicit personal build roots, the browser's own `sk-ant-` redaction regex,
  and generic runner debug paths.

### Release process

* The immutable v0.3.1 tag is retained as the artifact-free candidate that
  reached successful signing, Apple notarization, and stapling before the
  over-broad scanner rejected upstream metadata. v0.3.2 supersedes it.

## [v0.3.1] — 2026-08-25

This patch preserves the exact Node toolchain selected by the release runner.
It contains no runtime algorithm, authority, privacy, or interaction-policy
change from v0.3.0.

### Fixed

* The macOS builder now appends Homebrew locations as GUI-launch fallbacks
  instead of prepending them. The previous order could replace the repository's
  pinned `actions/setup-node` binary with a different preinstalled runner
  version after all pre-release gates had passed.
* Added a release-tooling regression contract that rejects reintroducing the
  precedence inversion.

### Release process

* The immutable v0.3.0 tag is retained as the failed, artifact-free candidate
  that exposed this packaging defect. v0.3.1 supersedes it as the first binary
  release candidate for the reviewed v0.3 line.

## [v0.3.0] — 2026-08-25

This minor release rebuilds Cortex's safety-critical path around truthful
observations, conservative support inference, explicit user authority, durable
recovery, and a traceable dual-architecture macOS release. It remains an alpha
research prototype: physiological outputs are quality-gated estimates, and the
support state is not a medical diagnosis or a calibrated probability.

### Added

* **Canonical observation and time contracts.** Typed envelopes carry wall and
  monotonic clocks, sequence identity, provenance, quality, missingness, and
  reason codes across capture, fusion, inference, transports, and storage.
* **Evidence-aware sensing.** Capture continuity, face-loss invalidation,
  elapsed-time kinematics, continuous rPPG beat tracking, IBI/HRV eligibility,
  low-frequency respiration estimation, posture evidence, and calibration now
  share explicit quality and publication policies.
* **Conservative support inference.** Missing or stale channels reduce evidence
  instead of silently biasing the result toward FLOW. Published support states
  expose evidence coverage and abstain or degrade when inputs cannot justify a
  stronger claim.
* **Transactional intervention authority.** Immutable action manifests,
  proposal-only presentation, exact authorization records, per-effect receipts,
  postcondition checks, and idempotent restore make workspace mutations
  inspectable and recoverable across disconnects and restarts.
* **Transactional local persistence.** Versioned SQLite migrations, durable
  intent/effect/recovery records, startup reconciliation, retention controls,
  scoped export, and guarded deletion replace fragmented attribution files.
* **Policy and evaluation lifecycle.** One decision maps to at most one durable
  outcome; the no-action arm is explicit; safeguards remain outside learned
  choice; observational summaries no longer overclaim causal identification.
* **Privacy context broker.** Browser/editor context is minimized by purpose,
  consent, origin, age, and field; optional access revocation clears stored
  content; planner inputs expose provenance without transferring raw page or
  code bodies by default.
* **Application architecture.** A shared application kernel, coordinators,
  typed events, runtime-data ownership, and supervised task lifecycle reduce
  transport duplication and decompose the former daemon, desktop, browser, and
  editor orchestrators.
* **Refined interaction surfaces.** Desktop privacy controls, context previews,
  truthful degraded/empty states, accessible dialogs and focus behavior,
  restrained reduced-motion-aware transitions, contrast contracts, and
  transactional browser/editor prompts align UI feedback with actual authority.
* **Traceable release pipeline.** Locked arm64 and Intel builders now require
  Developer ID signing, Apple notarization/stapling, installed-artifact smoke
  checks, architecture-specific dependency audits, SBOMs, checksums, GitHub
  provenance attestations, and complete independent real-device evidence before
  a draft may become public.

### Changed

* Runtime schemas are generated from the Pydantic boundary and dispatch sites
  use exhaustive message catalogs; configuration, version, design-token, link,
  action-pin, and CI/release-parity contracts are enforced in the main gate.
* Browser and VS Code intervention adapters now stage, apply, acknowledge, and
  restore effects through the same transaction semantics as the daemon.
* The macOS package builds architecture-specific DMGs from one reviewed source
  wheel, embeds verified browser/editor artifacts, scans for secrets and local
  paths, and emits machine-readable release evidence.
* Release notarization accepts exactly one complete credential set: an App
  Store Connect API key or an Apple ID with app-specific password. Partial or
  mixed credentials fail closed before keychain creation.

### Security

* Capability authentication, bounded sequencing, manifest validation, least-
  authority execution, privacy revocation, secret scanning, and durable restore
  recovery are enforced at the daemon/browser/editor trust boundaries.
* macOS Intel remains pinned to the final compatible MediaPipe/NumPy branch.
  Its reviewed Protobuf exception is narrowly scoped, continuously audited, and
  expires on 2026-09-22; the excluded parser boundary is rejected in production
  source by a repository contract.

### Fixed

* Corrected clock-epoch mixing, synthetic valid data after face loss,
  duplicated overlapping-window beats, incomplete IBI validation, unreachable
  low-rate respiration logic, nominal-frame kinematics, and parallel
  lower-fidelity calibration paths.
* Removed silent consent downgrade execution, mutation-before-presentation,
  non-durable restore, ambiguous no-action behavior, dead classifier/adaptation
  paths, and shutdown/task ownership gaps that could leave unresolved work.

## [v0.2.2] — 2026-06-23

Patch release closing a full multi-phase production audit: real,
test-backed fixes for latent correctness, contract, and pipeline gaps
found by walking every backend module, frontend surface, data pipeline,
and API contract. No behavioural feature was removed; every fix makes a
claimed feature actually work from real input.

### Fixed

* **Signal-quality staleness was dead code.** Feature fusion stamped the
  physio/kinematics channels with a wall-clock (`time.time()`) epoch but
  compared it against a `time.monotonic()` clock at fuse time, so the
  staleness penalty could never engage. All fusion-staleness stamps are
  now monotonic.
* **Session-report rollups were fabricated.** The four `SessionReport`
  producers (intervention triggered/accepted counters, activity and
  distraction recorders) had zero call sites, so chronotype trends fell
  back to a crude HYPER proxy and task-patterns were always empty. They
  are now wired to the real intervention-deliver / engage / activity-sync
  paths.
* **Consent-ladder lost-write race.** Lazy load now runs under the lock
  and flips its loaded flag only after the awaited store read, so a
  concurrent approval can no longer be clobbered by a stale read.
* **Redis loss mid-session.** The store now degrades to its in-memory
  fallback (and flips `degraded`) on a runtime Redis failure, not only at
  connect time.
* **Atomic writes** use a unique temp file so concurrent writers can no
  longer interleave and corrupt the destination.
* **`daily_cost_budget_usd = 0`** ("unlimited") now keeps recording spend
  and simply never fires the kill-switch, instead of raising and silently
  disabling the entire cost tracker.
* **Intervention failures were invisible.** `INTERVENTION_FAILED` (every
  workspace mutation failed) is now surfaced on every surface — desktop
  toast, WS-mode bridge, and a browser-popup error banner; the apply CTA
  is disabled while it is set. `INTERVENTION_PROMPT` now syncs to the
  popup, and the `start_timer` overlay action drives a real countdown
  instead of doing nothing.
* **Screen-share safety.** Always-on-top intervention overlays are
  suppressed while the display is being captured or mirrored
  (`CGDisplayIsCaptured` / `CGDisplayIsInMirrorSet`), and the overlay now
  appears on the screen under the cursor on multi-monitor setups.
* **Contract drift.** The WebSocket cost frame now carries the same
  token/model keys as `GET /api/cost`; the no-handler trends frame sends a
  schema-valid object; schedule-failure paths emit a valid error literal;
  and several payload docstrings were realigned to their producers.

### Removed / housekeeping

* Pruned advertised LeetCode capabilities to the ones the intervention
  matrix can actually emit; removed a dead schema-versioning helper and an
  orphaned, drifted top-level `tests/` directory.
* `make typecheck` now runs `--strict` and `make test` mirrors CI exactly;
  the CI ↔ release parity guard also compares the lint step.

## [v0.2.1] — 2026-05-19

The v0.2.x series replaces the v0.1.x release with an
architectural-grade rewrite of the LLM stack, a project-wide
adversarial audit (56 of 56 findings closed across two sessions plus
two Architectural Debts), and the first user-facing polish layer. This
tag is the snapshot ready for portfolio review.

### Highlights since v0.1.0

* **Anthropic SDK migration.** Cortex now talks to Claude exclusively
  through the Anthropic SDK with three pluggable transports —
  AWS Bedrock (default), GCP Vertex AI, and the direct Anthropic API
  — selected by a single `CORTEX_LLM__PROVIDER` env var and mirrored
  into `ANTHROPIC_PROVIDER` at startup. Legacy `CORTEX_LLM__MODE`
  values map to the rule-based fallback rather than raising, so
  0.1.x `.env` files boot cleanly. Logical model tiers (`sonnet-4-6`,
  `haiku-4-5`, `opus-4-7`) resolve to provider-specific IDs at call
  time. Removed: Azure OpenAI, self-hosted Qwen, local Ollama clients.
* **Debt-1 — Schema codegen drift gate.** Pydantic models in
  `cortex/libs/schemas/` are the single source of truth for the
  daemon ↔ browser-extension wire format; `cortex/scripts/generate_ts_schemas.py`
  emits `cortex/apps/browser_extension/types/generated/cortex_schemas.d.ts`
  with an `AUTOGENERATED — DO NOT EDIT BY HAND` header. Both the
  pre-commit hook and the `schema-codegen-check` CI job reject any
  drift. Six findings (F42–F45 family) collapse into a class of bug
  that's now structurally impossible.
* **Debt-2 — Capability-token auth.** Every mutating HTTP route now
  requires `Authorization: Bearer <token>` (with the legacy
  `X-Cortex-Auth-Token` header still accepted as a fallback) and the
  WebSocket connection opens with an `AUTH` handshake before
  `IDENTIFY`; pre-AUTH frames close with code 1011. The 256-bit token
  lives at `~/Library/Application Support/Cortex/auth.token`
  (`0600`) and is rotatable from the desktop Settings UI. `SHUTDOWN`
  payloads carry a defence-in-depth inline token check.
* **F19 — End-to-end correlation IDs.** Every mutating request is
  stamped with a UUID surfaced as `X-Cortex-Request-ID` on responses
  and injected into structured logs via `structlog.contextvars`. The
  dashboard error toast quotes the cid back so users can copy it
  into a support ticket.
* **Phase I — Performance work.** Capture loop sub-samples MediaPipe;
  parallel WS broadcast with a hard budget; lazy mediapipe + keyring
  imports for sub-2s startup; content-script-only LeetCode observer.
* **Phase J — User-facing polish.** Onboarding now detects Continuity
  Cameras and surfaces a "Why we need this" expander on every card;
  daemon errors raise a top-bar toast with cid quote-back; empty
  states for both biometrics and advanced tabs; overlay micro-
  interactions (scale-in + fade-in) that honour the macOS Reduce
  Motion accessibility preference; a11y sweep with explicit focus
  policy + accessible names on previously-overlooked surfaces.

### Engineering signals shipped this series

* 56 of 56 Ledger findings closed (`audit/findings.md` +
  `audit/execution-log.md`).
* CI matrix expanded to four required gates: schema-codegen drift,
  ruff + mypy strict + pytest, eval-regression baseline (synthetic-
  trace replay with 3 % relative tolerance), and explicit dependency
  hygiene via dependabot.
* 124 pytest files, 1,334 test functions, 17 vitest specs.
* Atomic-write discipline retrofitted onto every persisted artifact
  (handover snapshots, causal reports, project config, ML
  classifier, cost ledger, session recorder) so SIGKILL or disk-full
  no longer truncates state.
* Multi-layer kill chain (WS → HTTP → native-msg → SIGTERM →
  SIGKILL) documented and regression-tested for the stop button.
* Open-source meta layer added: `LICENSE` (MIT, Steven Wang),
  `NOTICE` (third-party attribution incl. MediaPipe FaceLandmarker),
  `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`,
  `SUPPORT.md`, `.github/ISSUE_TEMPLATE/*`,
  `.github/PULL_REQUEST_TEMPLATE.md`, `.github/dependabot.yml`,
  `Makefile`, `.editorconfig`. README restructured for portfolio
  readability.

### Phase J — user-facing polish

This release closes the audit's user-facing polish layer that sits on
top of the foundational fixes (Phase A through Phase H). The
hostile auditor wouldn't flag any of it; a real first-time user would
notice all of it.

### Added

* **Onboarding refinement.** The first-run wizard now (a) detects a
  paired iPhone / iPad Continuity Camera and surfaces an inline "we
  will skip your iPhone camera" callout on the Camera card, and (b)
  renders a "Why we need this" expand-on-click chevron on every card
  with rationale copy explaining where the data lives (e.g. "video
  stream never leaves your Mac", "Bedrock token stays in the macOS
  Keychain"). Expander buttons carry accessible names so VoiceOver
  announces them semantically.
* **Top-bar error toast with cid quote-back.** Daemon errors now
  surface in a dashboard top-bar toast that shows the F19 correlation
  id (rendered in the mono font as `ref: <cid>`) selectable so the
  user can copy it into a support ticket. Auto-dismisses after 8 s; a
  manual close button is always available. Hooked to a new
  `DaemonBridge.error_occurred(str, str, str)` Signal so any daemon
  callback that has a correlation context bound can surface a toast.
* **Empty states.** Before the first capture frame arrives, both the
  consumer biometrics card ("Start a session to see your
  biometrics.") and the developer-debug advanced tab ("Start a
  session to populate signal quality, heart-rate trace, and state
  scores.") render explicit empty-state placeholders. The flag is
  sticky — a transient WS disconnect does not collapse the UI back to
  an empty state because the cached numerics are more useful than a
  placeholder.
* **Overlay micro-interactions.** The intervention overlay plays a
  subtle scale-in (250 ms, OutCubic) on the headline and a fade-in
  (180 ms, InOutSine) on the causal-explanation row when the overlay
  appears. The two read as one continuous motion (the fade starts
  exactly when the headline tween finishes). Strictly purposeful: the
  dismiss button and micro-step checkboxes are not animated; the
  breathing pacer keeps its existing rhythm.
* **Reduce Motion support.** `mac_native.prefers_reduced_motion`
  consults `NSWorkspace.accessibilityDisplayShouldReduceMotion`; when
  the user has the System Settings → Accessibility → Display → Reduce
  motion preference enabled, the overlay's tweens are skipped and the
  end state is applied directly.
* **A11y sweep on remaining surfaces.** Segmented-control tab buttons,
  the dashboard Connect / Stop buttons, the connections-panel back
  button, and every `_primary_button` on the connections panel now
  carry `setFocusPolicy(Qt.StrongFocus)` so they participate in the
  keyboard tab cycle on every Qt build (macOS Qt sometimes inherits
  `WheelFocus` which silently excludes a button from tabbing).
  Segmented-control buttons gain explicit accessible names + a
  descriptive long-form description for VoiceOver.

### Known limitations (residual a11y, intentionally deferred)

These are documented here so a future polish pass (or the v0.3 audit)
can pick them up; they did not block the Phase J close-out.

* **VoiceOver rotor item announcement on the biometrics numerics
  (P3).** The Cormorant numerics (BPM / HRV / BLK) are decorative font
  glyphs; VoiceOver reads them as raw text. A future commit could wrap
  each numeric in a `QAccessibleWidget` subclass that surfaces a
  semantic "62 beats per minute" string for screen readers. The
  current behaviour is not WCAG-failing — the labels above the
  numerics provide context — but it is not as rich as the visual
  affordance.
* **High-contrast mode (P2).** Cortex respects the system light/dark
  appearance but does not yet honour the macOS Increase Contrast
  accessibility preference. Under increased contrast, the warm
  greyscale label tints would benefit from a flatter palette. The
  token registry is already structured to support a third tier
  (`SEMANTIC_HIGH_CONTRAST`), so the work is plumbing rather than
  design.
* **Live-region announcements on state transitions (P3).** When
  Cortex detects an overwhelm transition, VoiceOver does not announce
  the new state — the dashboard's state pill updates but is not
  registered as a live region. A future commit should
  `setAccessibleRole(QAccessible.Role.StaticText)` on the state pill
  and emit `QAccessibleEvent.UpdateContents` on every state change.
* **Reduce Motion on non-overlay surfaces (P3).** The Phase J pass
  honours Reduce Motion on the overlay tweens only. The dashboard's
  HR-trace plot, the breathing pacer, the focus-ring transitions, and
  the connections-panel status pill colour transitions are not yet
  gated by `prefers_reduced_motion`. They are all small (≤ 200 ms,
  easing curves) and below the typical perceptual threshold for
  motion sensitivity, but a thorough Reduce Motion pass would gate
  them too.

### Verification

```bash
QT_QPA_PLATFORM=offscreen pytest \
    cortex/tests/unit/test_dashboard_toast.py \
    cortex/tests/unit/test_dashboard_empty_state.py \
    cortex/tests/unit/test_onboarding_hints.py \
    cortex/tests/unit/test_overlay_animation.py -q
# 26 passed
```

Manual QA: start daemon, open onboarding, click each "Why?" chevron,
confirm rationale appears. Plug in an iPhone, reopen onboarding, see
the Continuity callout on the Camera card. Trigger an overlay, watch
the headline scale-in then the causal row fade-in. Toggle System
Settings → Accessibility → Display → Reduce motion, trigger again,
confirm both elements appear without tweens.

---

The pre-Phase-J audit work (Phase A through Phase H) is tracked in
`audit/findings.md` (the 56-finding ledger) and
`audit/execution-log.md` (the per-commit log). This CHANGELOG starts
with Phase J because that is the release boundary at which the audit
closes out.
