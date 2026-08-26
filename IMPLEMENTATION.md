# Cortex: Rigorous Algorithm, Architecture, and Implementation Plan

**Status:** release-relevant software implementation complete through WP-11;
credentialed release-candidate, reference-sensor, participant, and independent-review
gates remain external

**Historical audit snapshot:** `fac5db965b0568a73ea64d78fbb6eb594080073c`
on `main`, reviewed 2026-08-24

**Implementation record:** `implementation-hardening`, implemented and verified
through 2026-08-26; immutable WP commits are listed in
[`audit/execution-log.md`](audit/execution-log.md)

**Scope:** macOS application, in-process daemon, HTTP/WebSocket gateways, webcam physiology and kinematics, state inference, intervention planning/execution/restoration, adaptive policy evaluation, Chrome/Edge extension, optional VS Code extension, persistence, packaging, tests, documentation, privacy, and supply chain

**Audience:** maintainers implementing the next correctness and architecture milestone

---

## 0. Document state and completion boundary

This is both the original repository audit and the implementation record that
closed it. Sections 1–11 preserve the as-built evidence and design reasoning at
the historical snapshot above; present-tense defect descriptions in those
sections must not be read as claims about the hardened branch. Sections 12,
15, and 18 record implementation and closure. Current operational truth lives
in the [architecture](Architecture.md), [limitations](docs/limitations.md),
[data-flow](docs/data-flow.md), and [release evidence](docs/release/README.md)
documents.

The high-impact software boundary is complete. Cortex is now a local modular
monolith with explicit application coordinators, evidence-aware deterministic
support inference, exact authorization and receipt-backed restoration,
transactional SQLite authority, a privacy context broker, deterministic product
policy, separately governed research evaluation, and a reproducible release
pipeline. “Complete” here does not mean clinically validated, independently
audited, or already shipped as a signed/notarized artifact.

| Boundary | Implemented result | Remaining evidence, if any |
| --- | --- | --- |
| User authority | Inert proposal → exact manifest authorization → apply receipt → postcondition verification → idempotent restore | Per-release installed-artifact exercise remains mandatory |
| Signals and inference | Explicit missingness/quality, unique beat timeline, time-correct kinematics, deterministic abstaining support estimate; unsupported medical/physiology claims are unavailable | Reference-sensor and participant-held-out data are required before adding physiological accuracy claims |
| Architecture and storage | Typed kernel/coordinators, named task ownership, one desktop domain path, bounded browser modules, transactional SQLite authority and recovery | None at the source-code boundary |
| Policy and evidence | Deterministic production policy and exactly-once outcomes; consented fixed MRT/OPE research path with immutable exports and diagnostics | Independent statistical review and any human-subject approvals remain external |
| Privacy and UI | Exact redacted preview, one-time external send, optional permissions, semantic tokens, keyboard/accessibility/reduced-motion contracts, restrained feedback and interruption behavior | Real-user usability/accessibility studies remain external |
| Supply chain and release | Universal `uv.lock`, exact Python/Node/pnpm/uv pins, frozen installs, audited dependency exceptions, dual-architecture workflow, SBOMs, attestations, signing/notarization/stapling verification, mounted smoke, draft staging, and fail-closed evidence promotion | Apple credentials, protected release/publish environments, x86_64 runner, independent reviewers, and the physical TCC/device matrix are required per candidate |
| Documentation and governance | Tracked finding ledger/ADRs, generated 203-setting reference and safe env template, version/schema/design-token/config/link/action-pin contracts | External links and audit exceptions continue to be scheduled, time-varying gates |

The UI implementation applies the interaction principles from
[Emil Kowalski’s design-engineering skills](https://github.com/emilkowalski/skills):
motion communicates state rather than decorates it; transitions remain short,
interruptible, and reduced-motion aware; destructive or authority-bearing
actions are explicit; visual hierarchy uses semantic tokens; and every polished
surface retains keyboard, focus, contrast, and assistive-name contracts. WP-9
contains the implementation evidence rather than treating the reference as a
style-only checklist.

### 0.1 Verification snapshot

The release-relevant source at `59e5d17a5d7fd4747395117f58a0c787cc6309c6`
passed all seven jobs in the
[final dual-architecture CI run](https://github.com/StevenWang-CY/cortex/actions/runs/32866469753)
with locked dependencies:

- Native arm64/Python 3.11.15 and x86_64/Python 3.12.13: exact interpreter and
  CPU assertions, architecture-specific dependency audits, Ruff, strict mypy
  over 511 source files, a verified 280-file wheel, 2,560 non-Qt tests passed
  with 3 declared skips, and 62 isolated Qt tests passed on each architecture.
- Reproducible artifact surface: local arm64, local Rosetta x86_64, hosted
  arm64, and hosted Intel builds all produced the same 280 runtime members;
  both hosted native builders produced the byte-identical wheel SHA-256
  `eee56574ab13837cad507719e7a6f38c015ee2131f553c7501a3d73b60154a97`.
  Wheel selection respects VCS ignores and explicit sensitive-artifact
  exclusions, while the independent archive verifier rejects environment
  files, logs, databases, interpreter debris, and credential/key containers.
- Browser: a clean pnpm 9.15.9 frozen install, TypeScript check, 248 Vitest
  tests, and Chrome/Edge MV3 production builds.
- VS Code: clean `npm ci`, TypeScript compile, 30 Jest tests, zero npm audit
  findings, and a packaged 0.2.2 VSIX.
- Contracts/evaluation: generated Python→TypeScript schemas, design tokens,
  versions, configuration docs, 203-setting reachability, local Markdown links,
  workflow action/tool pins, and all four committed replay-regression metrics.
- Dependency policy: the arm64 graph has zero known Python findings and VS Code
  has zero findings; 11 browser build/test-chain advisories are path-constrained,
  expiry-bounded, reviewed exceptions after patchable critical/high transitive
  versions were lifted. The Intel-only MediaPipe 0.10.21/NumPy 1.26.4 graph has
  one separately reviewed Protobuf 4.25.9 `ParseDict` denial-of-service exception
  through 2026-09-22; Cortex and the installed MediaPipe Python sources do not
  import that JSON parser, and CI loads and runs the real bundled model before
  accepting the exception.

A later exact-head validation, [run 32869260778](https://github.com/StevenWang-CY/cortex/actions/runs/32869260778),
passed six of seven jobs and exposed one Intel-only defect in the *test
measurement*: the synthetic capture timer included about 922 MB of random-frame
generation that production capture does not perform. Commit `7ffb97f` moves
fixture acquisition and dispatch warm-up outside timing, uses median batches,
and adds a deterministic production call-graph assertion requiring exactly one
RGB and one grayscale conversion per frame. The exact Rosetta x86_64/Python
3.12.13 target passed all four capture tests, and the full native arm64 gate
passed 2,561 non-Qt tests plus 62 Qt tests; the distributable wheel and its hash
were unchanged. The diagnostic and remediation are retained in
[`audit/execution-log.md`](audit/execution-log.md) rather than hiding the failed
run.

Credentialed release execution subsequently exercised the software boundary
without converting partial success into a publication claim. The immutable
v0.3.3 arm64 and Intel candidates were accepted by Apple's notarization
service and stapled, but the final disk-image Gatekeeper assessment found no
usable outer-container signature. v0.3.4 corrected the inside-out signature
order: its arm64 DMG was Developer ID signed, accepted, stapled, accepted by
Gatekeeper, mounted, and passed the frozen smoke before evidence generation
correctly rejected the checkout as dirty. Root-cause analysis identified the
untracked `.env.bundled` staging file, which remained present until shell exit
while provenance necessarily ran before exit. The v0.3.4 Intel DMG was also
signed, accepted, and stapled, but its mounted deep signature verification
exceeded the verifier's generic 60-second subprocess limit after the earlier
unbounded deep verification had passed.

v0.3.5 keeps both the scrubbed bundle environment and any developer `.env`
backup in the runner temporary directory outside the checkout; only the
ignored `.env` projection expected by PyInstaller exists briefly. The
fail-closed clean-tree check is retained unchanged. Mounted deep signature
verification has a command-specific, bounded five-minute budget for the much
larger Intel verification surface. Its source validation on
native arm64/Python 3.11.15 passed Ruff, strict mypy over 512 files, a verified
281-file wheel, 2,606 non-Qt tests with three declared dataset/platform skips,
62 isolated Qt tests, 248 browser tests plus Chrome/Edge production builds,
and 30 VS Code tests plus a zero-vulnerability 0.3.5 VSIX.

No physical camera/TCC matrix, reference-sensor dataset, participant study,
penetration test, independent statistical review, or public release was
fabricated. The repository makes those gates executable and evidence-bearing;
the parties with the required hardware, data authority, or independence must
execute them against the exact staged candidate artifacts.

## 1. Executive determination

Cortex has a credible product shape and several good engineering foundations: local-first capture, no raw-frame persistence in the normal path, explicit signal-quality objects, a consent vocabulary, reversible adapters, localhost capability-token authentication, schema generation for much of the daemon/browser boundary, extensive Python and browser tests, clean static analysis, and thoughtful macOS packaging work.

At the historical audit snapshot, the implementation did **not** support
several of its strongest behavioral and physiological claims. Four classes of
issue were release-critical:

1. **A real native-messaging authentication contract mismatch prevents the browser from accepting the token returned by the Python host.** Python emits `token`; TypeScript accepts only `auth_token`. Each side's isolated tests encode its own incompatible shape.
2. **Consent downgrade is treated as permission to execute the original higher-level action, and mutations occur before the plan is presented.** Both browser and VS Code clients also apply workspace changes as soon as `INTERVENTION_TRIGGER` arrives. The implemented path is therefore not a true preview/approval transaction.
3. **The physiological feature path can manufacture or duplicate evidence.** Overlapping rPPG windows repeatedly insert the same inter-beat intervals; global beat order is wrong; missing face samples are interpolated without a window missingness limit; the successful face-tracking path makes measured nose displacement zero; and the respiration band cannot produce the rate that the code labels as an apnea condition.
4. **The adaptive-policy report is descriptive, not causal.** Policy matrices are not durably restored, a decision can receive multiple reward updates, the no-action arm is not followed to an outcome, and the current IPS/SNIPS and excursion calculations do not define or estimate a defensible target-policy or proximal causal estimand.

The immediate product posture at that snapshot was therefore:

- default to **suggest-only** behavior;
- require a new authorization record for every workspace mutation;
- suppress external HRV, LF/HF, “apnea,” and physiology-driven break claims until the signal pipeline passes reference-sensor validation;
- describe state outputs as **support-need estimates** or **overwhelm-support scores**, not measurements of a medical or neurological condition;
- freeze online policy learning outside an explicitly consented research mode;
- retain the current modular-monolith deployment and replace the large orchestrators incrementally, not with network microservices.

This was containment, not abandonment. WP-0 through WP-11 implemented the
repair without discarding the existing UI, capture adapters, schemas, or most
domain services.

### 1.1 Release-blocking gates

| Gate                             | Observed evidence                                                                                                       | Required exit condition                                                                                                              |
| -------------------------------- | ----------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| G0: native auth                  | `native_host.py` returns `{status, token}` while `lib/auth.ts` reads `auth_token`                                       | One generated response schema, one canonical field, Python↔TypeScript golden contract test, installed-host smoke test                |
| G1: consent exactness            | `ConsentDecision.allowed` may be true after downgrade; daemon and executor ignore `effective_level`                     | A decision is `PERMIT`, `DOWNGRADE`, or `DENY`; only an exact permit or a newly materialized lower-level plan may execute            |
| G2: presentation before mutation | daemon calls `executor.apply()` before `send_intervention()`; browser and VS Code mutate on trigger                     | `PROPOSED → PRESENTED → AUTHORIZED → APPLYING`; trigger/presentation handlers are side-effect free except rendering                  |
| G3: reliable undo                | reversals call adapters with `{}` rather than a recorded action receipt; outcome start time is written at end           | Every applied action emits a durable receipt containing exact inverse data; restart-safe, idempotent restore is verified             |
| G4: clock contract               | public schemas say UNIX epoch while the state loop constructs feature/state objects with `time.monotonic()`             | Separate wall and monotonic fields; generated contracts reject ambiguous `timestamp`; fake-clock tests cover restart and long uptime |
| G5: signal validity              | duplicated/reordered IBI history, missingness interpolation, dead motion measure, unreachable low-respiration condition | Continuous beat timeline, explicit observation masks, time-based kinematics, valid respiration design, replay/reference validation   |
| G6: release integrity            | VS Code lockfile is ignored although CI runs `npm ci`; its Jest command is invalid; runtime `ws` has a high advisory    | Tracked lockfile, tests run in CI, fixed `ws`, frozen installs, Python audit/SBOM/provenance gates                                   |

No autonomous workspace mutation or physiology-triggered break should ship while G0–G5 are open.

---

## 2. Audit method and confidence labels

This review used a repository-wide structural, dependency, configuration, documentation, and call-site scan, followed by deep end-to-end tracing of production-critical flows:

```text
camera → face/ROI/quality → pulse/respiration/kinematics
       → feature fusion → rule scores → temporal smoother → trigger
       → context collection → LLM plan → validation/consent
       → daemon/browser/editor mutation → outcome/reward/restore
```

It also covered both desktop runtime modes, native messaging, HTTP and WebSocket authentication, schema generation, build scripts, packaging configuration, persistent stores, tests, CI workflows, ignored files, repository remotes, published links, dependency audits, and relevant primary research or platform documentation.

Statements use these meanings:

- **Observed:** directly established from the reviewed source, configuration, repository state, or executed check.
- **Inferred:** a consequence of observed code that needs a hardware or installed-package reproduction for final confirmation.
- **Proposed:** a target design or engineering gate; it is not represented as current behavior or an external standard.
- **Research basis:** a primary paper, official platform document, or official standard used to constrain a proposal.

The audit is comprehensive at the repository and production-path level, but it is not a clinical validation, a penetration test, a notarized-DMG installation study, or a human-subject experiment. Camera accuracy, permission behavior, and intervention efficacy still require the validation program in Section 13.

---

## 3. Repository, Git, links, and quality baseline

### 3.1 Repository state

At the start of review:

- the worktree was clean;
- local `main`, its `cortex/main` tracking ref, and live `refs/heads/main` at the `cortex` remote all resolved to `fac5db965b0568a73ea64d78fbb6eb594080073c`;
- the code remote was [StevenWang-CY/cortex](https://github.com/StevenWang-CY/cortex);
- `origin` pointed to the separate Cortex wiki repository;
- there were 586 tracked files and approximately 181,895 tracked lines when generated and non-text artifacts were included;
- the largest authored orchestrators were `runtime_daemon.py` (~6,300 lines), browser `background.ts` (~5,475), browser `popup.tsx` (~3,572), desktop `dashboard.py` (~3,532), WebSocket server (~2,573), desktop history tab (~2,440), desktop controller (~2,376), and VS Code panel provider (~1,091).

The clean starting state matters: the findings below describe the reviewed
commit, not an unknown mixture of uncommitted source changes. The original
audit first introduced this document; the bounded WP commits in the execution
log then implemented it incrementally.

The local workspace is much larger than the tracked project because it contains ignored environments, caches, builds, and generated assets. Those should remain untracked. In particular, `.plasmo/`, `build/`, `dist/`, application bundles, VSIX files, and editor metadata must continue to be treated as generated artifacts.

### 3.2 Configuration and version drift

The ignored `.env` was inspected by key name only; no secret values were read into this report. It does not set `CORTEX_CAPTURE__DEVICE_ID`, so the smart camera-selection path is not bypassed in the reviewed environment.

Observed drift:

- project metadata and release artifacts identify v0.2.2, including the [v0.2.2 release](https://github.com/StevenWang-CY/cortex/releases/tag/v0.2.2);
- `cortex.__version__`, the FastAPI application version, and the installed editable distribution report 0.1.0;
- the local environment uses Python 3.11.9, Node 22, and pnpm 10, while workflow/tool versions are not completely aligned;
- dependency declarations use broad minimum ranges rather than a reproducible Python resolution.

**Implementation:** define the version once in `pyproject.toml`; read it through `importlib.metadata` at runtime; generate extension manifests and release metadata from that source; fail CI if any published version differs.

### 3.3 Executed verification baseline

| Check                         |                                             Result | Interpretation                                                                   |
| ----------------------------- | -------------------------------------------------: | -------------------------------------------------------------------------------- |
| `ruff check cortex/`          |                                               pass | Python lint baseline is clean                                                    |
| strict mypy over `cortex/`    |                             pass, 428 source files | Strong static baseline; dynamic wire casts still need contract tests             |
| schema codegen `--check`      |                                               pass | Existing Pydantic→TypeScript catalog is synchronized                             |
| design-token check            |                                               pass | Token generation is synchronized                                                 |
| main Python suite             | 2,302 passed, 5 failed, 5 skipped; 2,312 collected | Broad coverage, but suite is not green                                           |
| desktop-shell suite           |                                          57 passed | Isolated desktop tests are green                                                 |
| browser TypeScript            |                                               pass | Current source type-checks                                                       |
| browser Vitest                |                             171 passed in 41 files | Broad unit baseline; warnings reveal test-harness debt                           |
| VS Code compile               |                                               pass | Extension source compiles                                                        |
| VS Code Jest                  |                         command fails before tests | Jest 30 renamed `--testPathPattern`; the script uses the removed form            |
| evaluation regression harness |                                               pass | Only validates synthetic preclassified state sequences, not sensing or inference |

The five Python failures reveal two test-design defects:

1. Four timestamp tests compare UNIX seconds with `time.monotonic() * 1000`; that assertion becomes false on a machine with sufficiently long uptime and also mixes units.
2. The cost-persistence test injects a historical `record(now=...)` time but reload pruning uses the real `date.today()`. A test that claims hermetic time control does not inject the load-time clock.

These failures should not be fixed by widening tolerances. Introduce the clock ports in Section 8 and make temporal semantics explicit.

The browser test run logged deprecated React `act` usage, an unimplemented canvas context, and one update outside `act`. These did not fail the run but should be converted into clean harness adapters so warnings remain actionable.

### 3.4 Dependency and release-chain status

- The browser production audit reported 26 advisories: 13 high, 11 moderate, and 2 low. The reported paths are predominantly through the Plasmo/Parcel/image build chain. They are principally build/supply-chain exposure unless bundle inspection proves a package is shipped and reachable at runtime; they still require remediation or documented risk acceptance.
- The VS Code production audit reported one high-severity issue in `ws` 8.0.0–8.20.1, with a fix available. The client is intended for localhost, which lowers remote reachability but does not make a malicious or compromised local endpoint harmless.
- `pip-audit` is not installed and there is no equivalent Python vulnerability gate.
- `package-lock.json` under the VS Code extension is explicitly ignored, but its CI job invokes `npm ci` and caches that path. A fresh checkout cannot satisfy that workflow reliably.
- release scripts use non-frozen installation in parts of the build and Python has no committed cross-platform resolution/constraints set.

**Implementation:** track the VS Code lockfile, use `npm ci` and `pnpm install --frozen-lockfile` everywhere, lock Python build/runtime inputs by supported architecture, run OSV/pip and Node audits, generate CycloneDX or SPDX SBOMs, archive audit results, and sign provenance. Follow the risk-based practices in the [NIST Secure Software Development Framework](https://csrc.nist.gov/pubs/sp/800/218/final); do not make “zero advisories” the only policy, because reachability and compensating controls still matter.

### 3.5 Documentation and link integrity

The public code repository and wiki are reachable, and wiki-style pages such as [How It Works](https://github.com/StevenWang-CY/cortex/wiki/How-It-Works) resolve in the wiki. The checked-in documentation nevertheless has portability and truthfulness problems:

- README and CONTRIBUTING link to `audit/findings.md` and `audit/state.md`, but no tracked `audit/` directory exists;
- README links to `CLAUDE.md`, but that file exists only as ignored local state and is absent from a clean checkout;
- extensionless wiki links work on the wiki but are ambiguous or broken when the same Markdown is read inside the code repository;
- test-count claims are stale;
- the README describes mypy as informational although CI treats it as blocking;
- adapter documentation contradicts its own protocol and example (`None`, `{}`, and attribute access are mixed);
- smoother dwell values, signal dimensionality, API version, and data-sharing descriptions do not match implementation;
- “all 56 audit findings closed” cannot be substantiated from tracked evidence.

**Implementation:** choose one canonical documentation source. If wiki pages remain authored in the code repository, publish them automatically and check every Markdown link in CI. Replace historical audit-count claims with a tracked decision/finding ledger. Generate configuration tables, schema catalogs, version strings, and test counts where practical.

---

## 4. As-built system and algorithm

### 4.1 Runtime topology

```mermaid
flowchart LR
    Camera["AVFoundation camera"] --> Capture["WebcamCapture + FaceTracker + frame quality"]
    Capture --> Physio["ROI → POS/CHROM/GREEN → pulse, HRV, respiration"]
    Capture --> Kin["blink, head pose, posture proxies"]
    Input["mouse, keyboard, app/window/browser/editor telemetry"] --> Telemetry["feature aggregation + focus graph"]
    Physio --> Fusion["FeatureFusion"]
    Kin --> Fusion
    Telemetry --> Fusion
    Fusion --> Rules["RuleScorer"]
    Rules --> Smooth["EMA + dwell/hysteresis"]
    Smooth --> Trigger["TriggerPolicy + special detectors"]
    Trigger --> Policy["Deterministic product policy or separately consented fixed MRT"]
    Policy --> Context["browser/editor context collection"]
    Context --> LLM["Anthropic planner + parser + validator"]
    LLM --> Consent["ConsentLadder"]
    Consent --> Execute["Executor + browser/editor adapters"]
    Execute --> Restore["RestoreManager"]
    Execute --> Outcomes["helpfulness, reward, policy reports"]
    Daemon["RuntimeDaemon"] --- Capture
    Daemon --- Trigger
    Daemon --- Execute
    Gateway["FastAPI :9472 + WebSocket :9473"] --- Daemon
    Browser["Chrome/Edge MV3 extension"] <--> Gateway
    VSCode["VS Code extension"] <--> Gateway
    Desktop["PySide6 desktop shell"] <--> Daemon
```

Deployment is already a **local modular monolith**, even though its logical design is described in layers. That is the right deployment boundary for camera ownership, latency, offline behavior, privacy, and macOS permissions. The problem is not too few processes; it is weak internal boundaries and several parallel implementations.

Two desktop execution paths exist:

- WebSocket mode: the shell is an external client of the daemon.
- In-process mode: Qt and the asyncio daemon are bridged through callbacks in `controller.py`.

The modes have already diverged in safety-relevant behavior: calibration simulation fallback can be rejected by the WebSocket desktop path but saved by the in-process controller. The target architecture must preserve one application service and make transport a replaceable adapter.

#### As-built component map

| Area                      | Primary implementation                                                         | Current responsibility/observation                                                                                  |
| ------------------------- | ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| canonical schemas         | `cortex/libs/schemas/`                                                         | Pydantic models and generated browser definitions; strong base, incomplete across native/VS Code/desktop boundaries |
| configuration             | `cortex/libs/config/` and `.env` loading                                       | layered settings with environment override; some unused keys and version/documentation drift                        |
| webcam capture            | `cortex/services/capture_service/webcam.py`                                    | AVFoundation selection, Continuity Camera exclusion, open/warm-up/release                                           |
| face and frame quality    | `capture_service/face_tracker.py`, `quality.py`, `pipeline.py`                 | MediaPipe landmarks, ROI prerequisites, load shedding; time/motion/missingness defects in Section 5                 |
| calibration               | `capture_service/calibration_runner.py`, desktop onboarding/controller         | independent feature collection and profile persistence; behavior diverges by transport                              |
| physiology                | `cortex/services/physio_engine/`                                               | POS/CHROM/GREEN/TS-CAN abstraction, ROI/color signal, peaks, HR/HRV/respiration and SQI                             |
| kinematics                | `cortex/services/kinematics_engine/`                                           | blinks/PERCLOS, head pose/freeze, posture proxy                                                                     |
| device telemetry          | `cortex/services/telemetry_engine/`, `activity_tracker/`                       | input rates, focus graph, window/app activity, feature aggregation                                                  |
| context                   | `cortex/services/context_engine/`                                              | browser/editor/task context, complexity, relevance and LLM-facing excerpts                                          |
| state                     | `cortex/services/state_engine/`                                                | feature fusion, rule scoring, smoothing, special detectors, stress accumulator, trigger policy                      |
| planning                  | `cortex/services/llm_engine/`                                                  | Anthropic request, prompt/cache/cost, parse/repair, bounded plan output                                             |
| consent                   | `cortex/services/consent/`                                                     | per-action escalation and policy ceiling; downgrade result currently misconsumed                                    |
| intervention              | `cortex/services/intervention_engine/`                                         | plan validation/mapping, adapter execution, snapshot, restore, special intervention flows                           |
| adaptation/evaluation     | `cortex/services/eval/`                                                        | deterministic product policy, one-window/one-reward lifecycle, descriptive diagnostics, separately governed MRT/OPE tooling |
| HTTP/WS gateway           | `cortex/services/api_gateway/`                                                 | FastAPI on 9472, WebSocket on 9473, token auth, client callbacks and broadcasts                                     |
| application orchestration | `cortex/services/runtime_daemon.py`                                            | composition, loops, callbacks, most cross-domain workflows; principal Python change hotspot                         |
| desktop                   | `cortex/apps/desktop_shell/`                                                   | PySide6 lifecycle, onboarding, settings, history/dashboard, in-process and WS transports                            |
| browser                   | `cortex/apps/browser_extension/`                                               | Plasmo/React MV3 UI, telemetry/context, native launch/stop, workspace actions; large background/popup modules       |
| editor                    | `cortex/apps/vscode_extension/`                                                | context, panel, folds/restoration, daemon WS client; compile gate but broken local Jest command                     |
| native/install            | `cortex/scripts/native_host.py`, `install_native_host.py`, `launcher_agent.py` | browser→app launch/stop/token bridge and manifest installation; auth shape mismatch                                 |
| packaging                 | `cortex/scripts/cortex.spec`, `build_macos_app.sh`, entitlements               | PyInstaller app, nested signing, extension packages, DMG; dependency reproducibility gap                            |
| verification              | `cortex/tests/`, client test directories, `.github/workflows/`                 | extensive unit/integration baseline, but limited true cross-language/hardware/full-pipeline coverage                |

The optional launcher listens on 9471. It is a launch convenience, not a second control plane; mutation and private-data APIs should remain behind the authenticated 9472/9473 gateways. Health, metrics, native messaging, and shutdown behavior need an explicit endpoint threat model because “localhost” is a network trust boundary, not an identity.

### 4.2 Current algorithm, precisely stated

The implemented state pipeline is a personalized heuristic system, not a trained cognitive-state estimator:

1. Camera frames are face-landmarked and reduced to color and geometry features.
2. Configured rPPG extracts a pulse waveform from sliding windows. Peak intervals are used for HRV-like metrics; a low-frequency band is used for respiration.
3. Blink, head-pose, posture-proxy, mouse, keyboard, application-switching, tab, and complexity features are aggregated.
4. A `FeatureVector` is scored by rules for `HYPER`, `HYPO`, `FLOW`, and `RECOVERY` using personalized baselines where available.
5. Scores are EMA-smoothed and passed through entry/exit thresholds and dwell timers.
6. Trigger policy applies confidence, context, cooldown, dismissal, quiet-mode, and special-state gates.
7. A contextual policy chooses an intervention arm; the planner gathers workspace context and may ask Anthropic for a structured plan.
8. Validation maps the plan to adapter commands; consent is checked; commands are applied and later reversed.
9. user actions and downstream state are mapped to rewards and descriptive policy reports.

Current defaults include a 10-second rPPG window with a one-second stride, a 0.7–3.5 Hz pulse band, 60-second HRV history, state entry/exit thresholds of 0.85/0.70, and state dwell defaults of 30/60/120 seconds. The main `HYPER` rule weights sum to one, but missing inputs are zero-filled rather than renormalized, so the score meaning changes with channel availability.

### 4.3 Strong foundations worth retaining

- macOS camera discovery includes Continuity Camera filtering, live post-open verification, and warm-up retries.
- camera release and daemon shutdown have multiple cleanup paths designed around real TCC and orphan-process behavior.
- capture, physiology, kinematics, telemetry, state, intervention, and adapter concerns already have recognizable modules.
- schemas cover many daemon/browser messages and a drift gate exists.
- localhost HTTP and WebSocket access are protected with a file-backed capability token; token-file permissions, CORS allowlists, rate limiting, AUTH-first WebSocket behavior, and native-host origin restrictions are sound starting controls.
- quality, missing values, plan warnings, mutation success, and restore errors are represented in data models even where current semantics need repair.
- planners are parsed and validated before adapter mapping; LLM output is not simply executed as arbitrary code.
- the extension persists some state needed across MV3 worker restarts, consistent with Chrome's warning that extension service workers are ephemeral and global memory is not durable ([Chrome service-worker guidance](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle)).
- ruff, strict mypy, schema drift checks, and thousands of tests provide a strong platform for incremental correction.

---

## 5. Rigorous findings

Severity meanings:

- **P0:** violates an authorization, data-integrity, or central product-safety invariant; contain before release.
- **P1:** makes a major output unreliable or a normal workflow non-reproducible.
- **P2:** material architecture, operability, or maintainability debt.
- **P3:** documentation or polish debt with limited direct risk.

### 5.1 Trust boundaries, consent, and restoration

#### F-001 — Native auth response is cross-language incompatible (P0, observed)

`cortex/scripts/native_host.py::_get_auth_token_response()` returns:

```json
{ "status": "ok", "token": "..." }
```

`cortex/apps/browser_extension/lib/auth.ts::fetchFromNativeHost()` accepts only:

```json
{ "auth_token": "..." }
```

Python tests assert `token`; browser mocks return `auth_token`. This is exactly the failure mode schema code generation is meant to eliminate, but native messages are outside the generated catalog.

**Impact:** a clean browser/native-host installation can fail authentication even though both component suites pass.

**Fix:** add `NativeHostRequest` and `NativeHostResponse` discriminated unions to the canonical Python schema package; generate TypeScript; use one field (`auth_token` is already used elsewhere); add a subprocess framing test that runs the actual Python host and validates it with the TypeScript decoder.

#### F-002 — Downgraded consent executes the requested level (P0, observed)

When `requested_level > max_allowed`, `ConsentLadder.check()` returns an `effective_level` below the request but sets `allowed=True` whenever that effective level is at least the action's minimum. Both the daemon's plan gate and the executor callback reduce that object to its Boolean `allowed`. Commands are still dispatched from the original plan with the original requested consent level.

**Impact:** the consent ladder communicates “downgrade” but the caller treats it as “permit unchanged.”

**Fix:** replace ambiguous Boolean semantics with a closed outcome:

```text
PERMIT(exact_level, authorization_id)
DOWNGRADE(max_level, reason)
DENY(reason)
```

A downgrade must return to planning and produce a new action manifest whose effects are valid at that lower level. The executor accepts only an unexpired authorization whose action-manifest hash, intervention ID, consent level, and user/auto-authorized source exactly match the commands.

#### F-003 — Mutation precedes presentation (P0, observed)

The daemon calls `executor.apply(plan, commands)` before broadcasting the intervention. On receipt, the browser injects the overlay and hides tabs immediately; VS Code folds code immediately. A user-facing trigger is therefore also a mutation command.

**Impact:** “preview” and “suggest” do not form a meaningful non-mutating state, and the user can see the workspace change before having an opportunity to approve it.

**Fix:** use the state machine in Section 10. The proposal event may render content only. Mutations require a subsequent `INTERVENTION_AUTHORIZE` command generated by a user gesture or an exact, pre-existing autonomous authorization.

#### F-004 — Restore is not receipt-driven or restart-safe (P0, observed)

The executor records the forward parameters but calls the reverse action with `{}`. Whether this works depends on undocumented mutable state inside a connected adapter. Active mutations are in memory, so a daemon or extension restart can sever the only undo path. `InterventionOutcome.started_at` is created with `datetime.now()` when the intervention ends, effectively making it equal to `ended_at`; duration comes from a separate monotonic clock.

**Impact:** the system can report restoration without proving the original state was reconstructed, and audit timestamps do not describe the event.

**Fix:** every adapter must return an `ActionReceipt` with before-state, after-state fingerprint, exact inverse command, and idempotency key. Persist the receipt before considering an action applied. Restore from receipts in reverse order, verify postconditions, and retain failed receipts for retry after reconnection.

### 5.2 Time, identity, and schema contracts

#### F-005 — Public timestamps violate their declared epoch contract (P0, observed)

Schema descriptions for `FeatureVector` and `StateEstimate` require UNIX epoch seconds. The runtime state loop passes `time.monotonic()` into fusion and smoothing, and those values reach clients. Monotonic time is correct for elapsed-time math and wrong for a cross-process wall timestamp. Python documents precisely this distinction: `time.monotonic()` cannot go backward and has an undefined reference point, while `time.time()` is seconds since the epoch ([Python time documentation](https://docs.python.org/3.11/library/time.html)).

**Impact:** browser/client comparisons, persisted history, replay, and tests can silently mix epochs and units.

**Fix:** ban bare `timestamp` at external boundaries. Every observation/event carries:

- `observed_at_unix_ms: int` for display, persistence, and cross-process correlation;
- `observed_at_mono_ns: int` for in-process ordering and duration;
- `boot_id: UUID` so monotonic values cannot be compared across process boots;
- `sequence: int` for stream ordering.

Only wall time crosses into long-lived persistence; duration logic uses the injected monotonic clock.

#### F-006 — Message typing is incomplete at the riskiest boundaries (P1, observed)

Browser dispatch performs a cast after partial normalization; VS Code maintains handwritten message shapes; native messaging is untyped across languages; multiple daemon callbacks transport augmented dictionaries outside the generated schema path.

**Fix:** make every process/transport boundary a generated discriminated union, including native host, desktop callbacks, browser, and VS Code. Reject unknown major schema versions and preserve unknown additive fields only within an explicit compatibility envelope.

### 5.3 Capture and observation integrity

#### F-007 — Successful face tracking zeroes the motion feature (P1, observed)

The face tracker stores the current landmark array in `_prev_landmarks_px` while processing a detected face. The pipeline then calls `compute_nose_tip_displacement(current_landmarks_px)`, which compares the current nose point to that just-stored current array. Successful frames therefore produce zero displacement.

**Impact:** frame quality sees artificially perfect motion and cannot gate motion-contaminated color signals.

**Fix:** compute displacement before committing current landmarks, or return displacement as part of a single tracker update. Prefer velocity in normalized face-widths per second using actual observation time. Unit-test stationary, translated, dropped-frame, and reset sequences.

#### F-008 — Missing-face windows can become synthetic valid signals (P0, observed)

On no-landmark frames, runtime records some metadata and returns before pushing color/timestamp samples; the later face-loss branch is unreachable. Low-quality entries can be NaN, but preprocessing interpolates them without enforcing maximum missing ratio or maximum consecutive gap. A fully missing color channel can become zeros. Face presence is passed as a current-frame value instead of window coverage.

**Impact:** an insufficient-observation window can be transformed into a regular numeric signal, then assigned physiological metrics and quality.

**Fix:** store an observation on every scheduled frame with a validity mask and missing reason. Resampling may interpolate only bounded gaps; it must never convert an all-missing channel to evidence. Signal output is `UNAVAILABLE` when coverage, longest gap, motion, illumination, or face-confidence gates fail. Emit face-lost after a time threshold regardless of whether features can be computed.

#### F-009 — MediaPipe and kinematics use nominal frames rather than elapsed time (P1, observed)

Face tracking advances its video timestamp by a fixed 33 ms; blink duration assumes 30 fps; head angular velocity is effectively degrees per frame; freeze logic counts frames. Adaptive skipping, camera FPS variation, and load shedding change the physical meaning of all of them.

**Fix:** pass the capture monotonic timestamp through MediaPipe and all derivative features. Define blink duration in milliseconds, angular velocity in degrees/second, posture dwell in seconds, and missingness in elapsed exposure time.

### 5.4 rPPG, heart rate, and HRV

#### F-010 — Overlapping windows duplicate and reorder beats (P0, observed)

Every one-second stride re-detects peaks across the entire ten-second window and inserts all derived intervals into rolling history. The newest group is added to the left in within-window order ahead of older groups. The same physical beats are counted repeatedly, global order is not chronological, and pruning examines an ordering that no longer represents time.

**Impact:** readiness counts, RMSSD, SDNN, pNN50, sample entropy, and LF/HF are computed over duplicated and misordered evidence.

**Fix:** treat beats as events on one continuous timeline. Convert every detected peak to an absolute monotonic time; reconcile overlap against existing peaks using a refractory interval and prominence/quality tie-breaker; derive IBIs only between adjacent unique beats; retain provenance to the contributing windows. Window-level HR may remain independent, but HRV must consume the unique beat stream.

#### F-011 — IBI validation is incomplete (P1, observed)

`min_hr_bpm` is accepted by the peak detector but does not constrain peak separation; only the maximum-HR distance is used. There is no complete plausibility, ectopic/artifact, local-deviation, or boundary-peak policy before HRV computation.

**Fix:** introduce a versioned `BeatValidator` with configurable physiological range, local median-deviation rule, signal-quality threshold, boundary handling, correction policy, and artifact burden. Keep raw candidate beats for diagnostic replay but use only accepted unique beats for derived metrics.

#### F-012 — HRV duration and product meaning are too strong (P0, observed + research basis)

The code exposes frequency and nonlinear HRV-like measures from a nominal 60-second history; the LF/HF helper itself accepts far shorter data. Established standards and updated reporting guidance emphasize acquisition, derivation, interpretation, and transparent sample/method reporting; a study of shortened intervals found roughly three minutes viable for several time-domain measures but five minutes necessary for frequency-domain measures ([1996 Task Force standards](https://pubmed.ncbi.nlm.nih.gov/8737210/), [2024 psychophysiology guidelines](https://pmc.ncbi.nlm.nih.gov/articles/PMC11539922/), [minimal-interval study](https://pubmed.ncbi.nlm.nih.gov/8578795/)). Those results concern ECG/PPG methods under studied conditions, not proof that noisy webcam-derived beats are equivalent.

**Impact:** precise-looking metrics can be statistically unstable and physiologically uninterpretable.

**Fix:** until reference validation succeeds:

- externally publish HR only, with quality and availability;
- retain RMSSD as an explicitly experimental trend after at least three minutes of accepted beats, and require five minutes for any user-visible HRV metric;
- disable LF/HF, pNN50, and entropy externally until five-minute webcam-to-ECG validation supports each metric;
- label every value as a camera estimate, include valid duration, accepted beat count, artifact fraction, uncertainty, algorithm version, and reason when unavailable;
- do not claim diagnosis, clinical-grade stress, fatigue, or autonomic balance.

The redesigned evaluation should use subject-disjoint and cross-dataset experiments, with MAE/RMSE/correlation/SNR and Bland–Altman analysis. The open [rPPG-Toolbox](https://github.com/ubicomplab/rPPG-Toolbox) is a useful benchmark harness and explicitly supports those metrics and cross-dataset splits; it should inform the test protocol rather than become an unquestioned production dependency.

#### F-013 — Motion, illumination, and demographic robustness are not demonstrated (P1, observed + research basis)

The repository includes useful frame and signal quality heuristics but no end-to-end validation matrix across cameras, lighting, motion, glasses/facial occlusion, and skin tone. Published work shows substantial real-world variance and identifies motion and illumination as important failure conditions; demographic composition and subgroup uncertainty must be reported, not assumed away ([diverse-population rPPG evaluation](https://pmc.ncbi.nlm.nih.gov/articles/PMC8175478/), [motion-robust rPPG study](https://pmc.ncbi.nlm.nih.gov/articles/PMC5995145/)).

**Fix:** make coverage and subgroup performance first-class release artifacts as specified in Section 13. The system should abstain more often rather than fill missing evidence or hide worst-group performance in an aggregate mean.

#### F-014 — Advertised backend adaptation is not wired (P1, observed)

`QualityScorer`, which can switch POS→CHROM→GREEN, has no production call site. The TS-CAN configuration points at a model file absent from the repository; selecting it falls back to POS. Runtime passes a raw configured path that may be brittle under working-directory changes and frozen application layout.

**Fix:** either remove unimplemented backend claims and reject unavailable model selection at startup, or wire a tested backend registry with packaged-resource resolution, model checksum/license metadata, warm-up self-test, explicit fallback event, and per-backend validation. Silent algorithm substitution is not acceptable.

### 5.5 Respiration and posture

#### F-015 — The current respiration design cannot detect its low-rate condition (P0, observed)

The estimator searches 0.15–0.4 Hz, equivalent to 9–24 breaths/minute, while the code flags rates below 8 as apnea-like. The search space cannot produce the trigger. Ten-second windows contain too few respiratory cycles for stable low-frequency estimation, and the head-motion input method has no production caller.

**Fix:** separate two problems:

1. respiratory-rate trend: use at least a 30–60 second valid observation window, measured time, explicit fusion of independently available color-envelope and head/torso motion channels, uncertainty, and abstention;
2. breath-pause research feature: detect absence/attenuation of respiratory oscillation directly with labeled reference data, not by asking a band-limited rate estimator to return a value outside its band.

Remove the term “apnea” from product behavior until an appropriate clinical protocol, reference sensor, population, and regulatory review support it. A general-purpose webcam application should report “breathing trend unavailable” or “possible prolonged pause” only in an explicitly experimental mode.

#### F-016 — Shoulder posture is not measured (P1, observed)

Production runtime calls the face-based posture path and never supplies a body pose. The calibration runner records FaceMesh points 234 and 454 as `shoulder_y`; those are lateral face/ear-region proxies, and the saved value is not wired into the production posture analyzer's baseline. Forward lean based on head pitch is confounded by camera placement and gaze.

**Fix:** choose one honest product:

- **head/neck pose proxy:** rename it, calibrate camera-relative neutral pose, and avoid shoulder claims; or
- **upper-body posture estimate:** add a validated MediaPipe Pose path, face/body association, visibility and scale checks, and per-camera calibration.

The first is the recommended near-term scope. The second adds compute, privacy surface, and validation cost without being necessary for the core product.

### 5.6 Blink and calibration

#### F-017 — Blink exposure-time initialization is inconsistent (P1, observed)

Before the first blink, tracking duration is treated as a full 60 seconds, so the first valid face frame can emit a zero blink rate and maximum suppression. After the first blink, duration can collapse to roughly one second and the feature becomes unavailable. PERCLOS and blink rate inherit frame-rate assumptions.

**Fix:** record `observation_started_at_mono_ns`, valid-eye-visible exposure, and closure intervals. Emit blink rate only after a minimum valid exposure; compute PERCLOS as closed valid time divided by eye-visible valid time; reset cleanly after face loss.

#### F-018 — Calibration is a parallel, lower-fidelity pipeline (P0, observed)

Calibration reimplements capture/physiology defaults without the production quality, missingness, measured-time, stabilizer, and configuration path. Samples from overlapping windows are treated as independent. Resting mouse collection is likely unrepresentative of active work. Synthetic fallback can be rejected in one desktop mode yet saved in the in-process path, and `finish()` itself can persist simulated baselines. The running daemon loads several baseline copies at construction and has no coherent live reload after calibration.

**Impact:** “personalized” thresholds may be simulated, stale, statistically overconfident, or semantically unrelated to production features.

**Fix:** calibration must consume the same versioned observation and feature pipeline as runtime. Store a `CalibrationProfile` with feature schema version, algorithm versions, camera identity class, valid durations, sample independence/effective sample size, quality distribution, missingness, reference task, and provenance (`measured`, never `simulated`). Synthetic values may populate a demo UI but may not be persisted as a user calibration. Publish a `CalibrationUpdated` event that atomically rebuilds all dependent models.

### 5.7 State inference and workload claims

#### F-019 — Heuristic scores are labeled as calibrated probabilities (P0, observed)

Rules produce weighted scores; a softmax creates fields called `calibrated_probabilities`; the smoother comments acknowledge that softmax maxima are incompatible with the configured entry threshold and therefore gates on raw heuristic scores. No calibration model or held-out calibration assessment exists.

**Fix:** call current outputs `state_scores`. Do not expose confidence/probability terminology until a supervised calibration step has been fitted and evaluated. If a probability is introduced, report reliability plots, Brier score, expected calibration error, and participant-held-out results.

#### F-020 — Missing channels bias the classifier toward FLOW (P0, observed)

Missing `HYPER` inputs contribute zero while their weights remain in the denominator; `HYPO` and `FLOW` use means over varying available subsets. Score scale and class competition therefore change as modalities disappear. A missing camera can look calmer rather than less certain.

**Fix:** represent `(value, validity, quality, age)` for every feature. For the current rules, normalize by available absolute weight and require class-specific coverage:

```text
score_c = sum_j(mask_j * quality_j * weight_cj * transform_cj(x_j))
          / sum_j(mask_j * quality_j * abs(weight_cj))
```

Return `INSUFFICIENT_EVIDENCE` below a declared coverage threshold. Add missingness indicators only if a fitted model is allowed to learn them without turning sensor failure into a spurious state proxy.

#### F-021 — Workload/state ground truth is undefined (P0, design gap)

Heart rate, blink behavior, posture, switching, and complexity can correlate with workload, fatigue, affect, environment, or ordinary task differences; none uniquely identifies “cognitive overwhelm.” Small webcam studies are evidence that multimodal workload estimation is investigable, not that these rules identify a person's latent state in general. NASA-TLX is an established multidimensional subjective workload instrument and includes mental, physical, temporal, performance, effort, and frustration dimensions ([NASA-TLX](https://www.nasa.gov/human-systems-integration-division/nasa-task-load-index-tlx/)).

**Fix:** write a measurement specification before fitting a model:

- target: near-term self-reported support need and workload, not a medical state;
- labels: short ecological ratings plus periodic NASA-TLX/task outcomes;
- decision unit: a predeclared observation window;
- exclusions: absent/low-quality sensors, exercise, camera transition, permission loss;
- evaluation: leave-one-participant-out first, then personalized adaptation without test leakage;
- product decision: abstain, suggest, or remain quiet; no direct diagnosis.

The recommended estimator is staged: corrected quality-aware features → regularized, interpretable probabilistic model → explicit calibration → simple temporal state model/hysteresis → abstention. An end-to-end deep state classifier is not justified without a larger consented dataset and a reproducible cross-participant benefit.

#### F-022 — Optional ML state classifier is dead in production (P1, observed)

The runtime initializes the labeled-episode count to zero; no production path increments it or fits the classifier. The inference branch requires that count to cross a minimum. Tests exercise fitting, but the application does not.

**Fix:** remove the unreachable feature and claims for the near term. If reinstated, define a deliberate label collection/consent flow, dataset version, training trigger, validation split, model registry, rollback, and model card. Intervention acceptance is an outcome label for helpfulness, not ground truth for cognitive state.

#### F-023 — “Biological Pomodoro” stress integral has invalid units and evidence (P0, observed)

The implementation now integrates a standardized HRV deficit, producing dimensionless-seconds, while comments and user meaning still say `ms*s`. The threshold narrative's arithmetic is inconsistent. Load never decays during above-baseline HRV; missing data pauses it; a recovery action subtracts an arbitrary number of raw units. Its source HRV is affected by F-010–F-012.

**Fix:** disable physiology-triggered breaks. After HRV validity is established, any experimental fatigue accumulator should be a documented leaky state:

```text
L_t = exp(-dt / tau) * L_(t-1)
      + dt * quality_t * max(0, standardized_deficit_t)
```

`tau`, the observation minimum, and the action threshold must be learned or prospectively validated against the declared outcome. Until then, time-based break suggestions and user preferences are safer and more honest.

### 5.8 Adaptive interventions and causal evaluation

#### F-024 — Policy learning is not durable or one-decision/one-outcome (P0, observed)

AMIP's matrices and counts are not restored after restart. Decision objects are retained without a bounded finalization lifecycle. A rating can update the same decision and a later composite outcome can update it again; other action paths add further reward. The offline loader collapses multiple rewards differently, so online state and reports disagree.

**Fix:** one immutable decision point produces at most one finalized proximal outcome and one reward under a versioned reward function. Intermediate feedback is stored as outcome components, not separate bandit updates. Persist sufficient statistics and/or replayable finalized rows transactionally; make finalization idempotent.

**Implementation status (2026-08-25): resolved for the production path.**
SQLite schema v2 stores immutable decision points, one bounded outcome window,
idempotent intermediate observations, one composite reward per
`(decision_id, reward_version)`, and checksummed policy state. Finalization is
transactional and survives restart. The retired trainer now rejects legacy
observational helpfulness logs at every training/evaluation entry point.

#### F-025 — The no-action arm is not a valid control (P0, observed)

When `no_action` is chosen, processing exits without scheduling the same follow-up outcome window. Its decision remains unclosed. This is informative censoring: treatment arms are observed and control is missing.

**Fix:** every eligible decision point, including no-action, gets the same outcome collection schedule. Log availability, eligibility, feasible action set, all action propensities, chosen action, delivery, contamination by other interventions, censoring, and outcome completeness.

**Implementation status (2026-08-25): resolved.** Both product and research
no-action decisions receive the same durable proximal window and finalization
schedule as delivered suggestions. Missing delivery, missing outcome,
censoring, and overlapping policy deliveries are explicit rather than silently
dropped.

#### F-026 — Current “causal report” does not identify a causal estimand (P0, observed + research basis)

The report's per-arm IPS numerator is averaged over all decisions without defining a target policy; its SNIPS normalizer is based on observed-arm weights; “excursion effect” is an unadjusted treatment mean minus observed control; bootstrap resamples treatment while holding control fixed; and the “Brier-like” statistic compares chosen-action probability to reward sign rather than calibrated outcome predictions.

Micro-randomized trials establish proximal effects by randomizing intervention options at eligible decision points and defining near-term outcomes and moderation in advance ([MRT design paper](https://pmc.ncbi.nlm.nih.gov/articles/PMC4732571/)). Contextual-bandit off-policy evaluation requires a stated target policy, logged behavior propensity, overlap, and diagnostics; IPS, doubly robust, and SWITCH estimators have distinct assumptions and bias/variance behavior ([Wang, Agarwal, and Dudík](https://proceedings.mlr.press/v70/wang17a.html)).

**Fix:** rename the current output `policy_diagnostics`. A future `causal_effect_report` requires:

- an explicit proximal estimand and outcome window;
- randomized assignment among feasible actions, including no action;
- preserved propensities for all actions;
- weighted and centered least squares or another prespecified longitudinal MRT estimator;
- participant/session-clustered uncertainty;
- availability and censoring rules;
- positivity/effective-sample-size/weight-tail diagnostics;
- frozen analysis version and reproducible report dataset.

**Implementation status (2026-08-25): invalid product claims removed; research
software implemented, external review still required.** Product output is now
named `policy_diagnostics` and is explicitly descriptive. A separate,
separately consented fixed two-arm MRT path binds each decision to the exact
study-specification checksum, creates owner-only immutable exports with sidecar
digests, and analyzes them with prespecified centered WCLS and session-cluster
bootstrap uncertainty. Target-policy IPS, SNIPS, direct-method, doubly robust,
and clipped/SWITCH-style estimates reject deterministic logs and include
overlap, weight-tail, support, effective-sample-size, and exact-input hashes.
No effect or efficacy claim is made until an independent statistician reviews
the frozen protocol and real study data.

#### F-027 — Policy safeguards are mixed with learned choice (P1, observed)

Feasibility is only partially represented; capability availability and exact consent are not an action mask. A stress rule zeros the no-action probability and forces selected recovery arms, which is a behavioral mandate rather than a safety floor. Feature vectors lack an intercept and robust normalization; matrix inversion is recomputed directly; RNG/version provenance is insufficient.

**Fix:** hard safety and consent constraints produce a feasible action set before policy selection and are never learnable. Add intercept, versioned normalization, Cholesky/solve rather than explicit inverse, a reproducible CSPRNG-derived decision seed, UUID decision IDs, bounded matrices, and startup integrity checks. Production defaults to a deterministic policy; online learning is research-only until the validation and governance gates pass.

**Implementation status (2026-08-25): resolved for the supported policy
surface.** Production is a versioned deterministic policy and never emits a
research propensity. Research mode is disabled by default, requires an
explicit fixed epoch and separate consent version, is restricted to
`no_action`/`suggest_only`, randomizes only after deterministic eligibility and
feasibility exclusions, serializes reproducible draw counters, and freezes
online learning off in the public configuration.

### 5.9 LLM privacy and planner safety

#### F-028 — Disclosure understates what leaves the device (P0, observed)

`TaskContext.to_llm_context()` can include current file path and symbol, diagnostics, up to 1,500 characters of visible code, terminal errors, browser tab titles and URLs, an active-content excerpt, goals, and learned relevance. The prompt also sends inferred state, score/confidence, dwell, and complexity. Some documentation says biometrics are not sent; while raw waveforms may stay local, a biometric-derived state is still sensitive derived data and is transmitted.

**Fix:** disclose exact categories and defaults. Add a per-request preview grouped by source, local redaction, user-approved scopes, URL query/fragment removal, secret scanning, and a “no workspace content” planner mode. Do not persist prompt content in ordinary logs. Provider retention and zero-retention eligibility must be shown accurately; Anthropic's official privacy material should be linked from the product's current provider disclosure ([Anthropic retention policy](https://privacy.anthropic.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data), [zero-data-retention scope](https://privacy.anthropic.com/en/articles/8956058-i-have-a-zero-data-retention-agreement-with-anthropic-what-products-does-it-apply-to)).

#### F-029 — Sanitization is useful but not a planner security boundary (P1, observed)

The code wraps untrusted context, removes control characters, and defangs common prompt-like tags. Those reduce accidental injection but cannot prove that a model will ignore adversarial instructions in source code, web pages, errors, or tab titles.

**Fix:** the LLM may propose only a finite typed action vocabulary. A deterministic policy validator applies user settings, consent, capability, target ownership, bounds, and reversibility. No model-originated string may become a shell command, filesystem path outside an already authorized target, selector with new authority, URL navigation, or adapter verb. Treat context as tainted data throughout tracing and logs. Manage AI risks through an explicit govern/map/measure/manage process consistent with the [NIST AI RMF](https://www.nist.gov/itl/ai-risk-management-framework) and its [Generative AI Profile](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf).

#### F-030 — Browser authority is broader than the documentation suggests (P1, observed)

The extension requests `<all_urls>` although documentation describes a more limited active-tab posture. Chrome recommends minimizing required access, using `activeTab` where possible, and moving nonessential origins to optional permissions ([Chrome extension privacy guidance](https://developer.chrome.com/docs/extensions/develop/security-privacy/user-privacy)).

**Fix:** make active-tab access the default, request site scopes at a user gesture when continuous telemetry or overlays are enabled, show and revoke granted origins in Connections/Settings, and never collect incognito context by default.

### 5.10 Architecture, persistence, and operability

#### F-031 — Orchestrators are beyond a safe change radius (P1, observed)

`runtime_daemon.py`, browser `background.ts`, browser `popup.tsx`, desktop dashboard/controller, and WebSocket server combine composition, state machines, transport, persistence, feature logic, UI, retries, and historical compatibility. WebSocket behavior is configured through a large set of setter callbacks; a global service registry provides additional implicit coupling.

**Impact:** local correctness fixes can diverge across modes or bypass a gate, as calibration and consent demonstrate.

**Fix:** use the incremental modular-monolith structure in Section 7. `RuntimeDaemon` becomes a compatibility facade over a small application kernel. Typed command/event ports replace callback setters and registry lookups. Python 3.11 `TaskGroup` provides structured ownership and cancellation for related daemon tasks ([Python TaskGroup documentation](https://docs.python.org/3.11/library/asyncio-task.html#task-groups)).

#### F-032 — Persistence is fragmented and cannot support atomic attribution (P1, observed)

State is split across JSON, JSONL/WAL-like files, caches, keyring, and in-memory dictionaries. A policy decision, delivery receipt, user response, restore result, and finalized outcome cannot be committed or queried as one coherent lifecycle.

**Fix:** introduce the single-writer event store in Section 11. SQLite is appropriate for this local application, but the runtime SQLite version must be gated. SQLite documents a rare WAL-reset corruption bug affecting versions 3.7.0 through 3.51.2 under concurrent connections that write/checkpoint simultaneously, fixed in 3.51.3 and selected backports ([SQLite WAL documentation](https://sqlite.org/wal.html)). The reviewed Python runtime links SQLite 3.45.1. Start with one dedicated connection and rollback-journal mode, or bundle a fixed SQLite before enabling multi-connection WAL. Do not let extensions open the database directly.

#### F-033 — Evaluation passes without exercising the claimed system (P1, observed)

The regression harness supplies already classified `StateEstimate` objects and checks four synthetic trigger outcomes. It does not process recorded frames, dropped samples, signals, feature fusion, rule scores, calibration, transport, consent, execution, or restore. Dataset-dependent rPPG tests are skipped.

**Fix:** retain the fast synthetic suite, but add the layered verification pyramid in Section 13. A release claim must be backed by a replay or reference dataset that reaches the layer making the claim.

#### F-034 — Several configuration knobs and claims are dead or inconsistent (P2, observed)

Examples include unused reward-window and bootstrap-sample settings, a duplicate unreachable return in the YAML source, a documented 12-dimensional vector whose array has 14 entries, stale dwell values, an absent TS-CAN asset, and code comments that describe retired audit phases rather than current invariants.

**Fix:** add config-use coverage that fails when a public setting has no production read, generate settings documentation, reject unknown/dead keys during migration, and replace historical fix narratives with concise invariant comments and ADRs.

### 5.11 macOS packaging and lifecycle constraints

#### F-035 — Critical macOS controls are implemented but lack installed-artifact regression coverage (P1, observed)

The repository correctly encodes several non-obvious controls that must survive the redesign:

- the PyInstaller spec collects MediaPipe data/dynamic libraries and keyring metadata and explicitly imports the macOS keyring backend;
- ad-hoc signing signs nested binaries without hardened runtime, while Developer ID signing uses hardened runtime;
- connection/install logic resolves the canonical `/Applications/Cortex.app` location instead of a translocated executable path;
- GUI-launched editor discovery checks application-bundle binary paths rather than trusting shell `PATH`;
- development native launch uses Terminal.app so camera TCC lineage is viable;
- native shutdown searches by ports and process patterns and escalates through termination mechanisms;
- webcam shutdown attempts `cap.release()` independently of the running flag.

These are sound, platform-specific design choices. The remaining weakness is that unit/static checks do not prove they work in the final signed/notarized application under real macOS permission, translocation, browser-restart, and failure conditions.

**Fix:** preserve these behaviors as named platform invariants and add a release-candidate installation matrix: ad-hoc developer build, Developer ID/notarized build, fresh user account, app installed outside then moved into `/Applications`, project under a protected folder, camera denied/allowed/revoked, Accessibility/Input Monitoring denied/allowed, Chrome fully restarted after native-host install, browser/editor disconnected during stop, and forced daemon failure while the camera is open. Never use a global `tccutil reset Camera` in automation.

---

## 6. Product and system invariants

These invariants are the target architecture's non-negotiable contract.

### 6.1 User authority

1. Presentation is not authorization.
2. A proposal produces no workspace mutation.
3. Authorization names the exact intervention, action manifest, maximum consent level, user or policy source, expiry, and nonce.
4. A downgrade never authorizes the original action.
5. Every mutation is bounded, idempotent, attributable, and reversible unless the UI explicitly labels it irreversible and requires contemporaneous approval.
6. Restore is based on recorded receipts and verified postconditions, not a best-effort inverse verb.
7. Safety and consent constraints cannot be overridden by the LLM or learned policy.

### 6.2 Evidence and uncertainty

1. Missing, stale, interpolated, and observed values are distinguishable.
2. No feature is published without provenance, valid duration, quality, and algorithm version.
3. Insufficient evidence produces abstention, never a calm/flow default.
4. External timestamps have declared units and epochs; duration clocks never cross boot boundaries.
5. Heuristic scores are not called probabilities; descriptive comparisons are not called causal effects.
6. Models are evaluated by participant and relevant operating condition, not only pooled averages.

### 6.3 Privacy and security

1. Raw camera frames remain in memory and are not persisted or transmitted in normal operation.
2. Derived data is still treated as sensitive.
3. External LLM context is minimized, previewable, redactable, and purpose-bound.
4. Browser/editor permissions are least-privilege and revocable.
5. Every cross-process message is authenticated, typed, size-bounded, and versioned.
6. Secrets remain in Keychain or mode-0600 capability files and never enter generated bundles or logs.

### 6.4 Experiment integrity

1. Each policy decision has exactly one eligibility record, one chosen action, one delivery status, and at most one finalized reward.
2. No-action decisions receive the same outcome follow-up as action decisions.
3. Behavior propensities and feasible actions are logged before outcome observation.
4. Analysis names its estimand, target policy, exclusions, censoring, and uncertainty method.
5. Online learning is opt-in research behavior, versioned and reversible.

---

## 7. Target architecture

### 7.1 Architectural choice

Keep one local application process for the daemon and desktop product. Do **not** split capture, inference, policy, and persistence into network services. Separate them through typed in-process ports and owned tasks:

- avoids extra authentication and serialization surfaces;
- preserves the one-process camera/TCC model;
- keeps latency predictable and offline behavior simple;
- permits deterministic in-process replay tests;
- still allows browser and editor clients to remain external adapters.

The right change is from an implicit monolith to a **modular monolith with one composition root**.

```mermaid
flowchart TB
    subgraph Clients["External clients"]
        Browser2["Browser client"]
        Editor2["Editor client"]
        Native2["Native host"]
    end

    subgraph App["Cortex application process"]
        Gateway2["Authenticated typed gateway"]
        Kernel["ApplicationKernel"]
        Sensing["SensingCoordinator"]
        Inference["InferenceCoordinator"]
        Context2["PrivacyContextBroker"]
        Intervene["InterventionCoordinator"]
        Experiment["Policy + outcome service"]
        Store["Single-writer EventStore"]
        Planner2["Planner port"]

        Gateway2 --> Kernel
        Kernel --> Sensing
        Kernel --> Inference
        Kernel --> Context2
        Kernel --> Intervene
        Kernel --> Experiment
        Kernel --> Store
        Intervene --> Planner2
    end

    Camera2["Camera + input sources"] --> Sensing
    Sensing -->|"quality-aware observations"| Inference
    Inference -->|"support estimate or abstention"| Intervene
    Context2 --> Planner2
    Experiment --> Intervene
    Browser2 <--> Gateway2
    Editor2 <--> Gateway2
    Native2 <--> Gateway2
    Desktop2["PySide6 transport adapter"] <--> Kernel
```

### 7.2 Module ownership

| Module          | Owns                                                                                           | Must not own                              |
| --------------- | ---------------------------------------------------------------------------------------------- | ----------------------------------------- |
| `application`   | startup/shutdown, dependency construction, task supervision, command dispatch                  | signal math, UI widgets, storage SQL      |
| `sensing`       | camera/input source lifecycles, observations, signal estimators, quality/readiness             | intervention decisions                    |
| `inference`     | feature definitions, baseline application, state/support estimator, temporal model, abstention | browser/editor commands                   |
| `context`       | local workspace snapshotting, data classification, redaction, provider request construction    | direct mutation                           |
| `planning`      | deterministic templates and optional LLM plan proposals                                        | consent decisions, arbitrary capabilities |
| `interventions` | proposal/authorization/apply/verify/restore state machine, action manifests                    | policy reward fitting                     |
| `experiments`   | eligibility, behavior policy, outcome windows, finalized rewards, analysis exports             | consent or safety overrides               |
| `storage`       | migrations, transactions, repositories, retention/export/delete                                | domain policy                             |
| `gateway`       | authentication, rate/size limits, schema decoding, connection/session lifecycle                | domain orchestration                      |
| `clients`       | presentation and capability execution behind authorization                                     | independent inference/consent policy      |

### 7.3 Composition root and task ownership

Create `cortex/application/kernel.py` and `cortex/application/bootstrap.py`:

- `bootstrap.py` resolves config, version, paths, clocks, keychain/token, SQLite compatibility, adapters, and feature flags once;
- `ApplicationKernel` exposes typed commands and events and owns coordinator lifetimes;
- each coordinator has `start()`, `stop()`, and health/readiness contracts;
- an outer `asyncio.TaskGroup` owns capture, state, gateway, intervention timeout, storage writer, and maintenance tasks;
- a coordinator failure is classified as fatal, degradable, or retryable; the classification is visible in health;
- shutdown cancels producers, drains the event store, restores active receipts, releases the camera, closes clients, and then exits.

`RuntimeDaemon` remains temporarily as a facade that delegates to the kernel. Existing API and desktop entry points keep working while methods are moved behind ports. This “strangler” approach limits migration risk.

### 7.4 One application service, multiple transports

The WebSocket desktop and in-process Qt bridge must call the same command handlers:

```text
Desktop UI gesture
  → DesktopTransport.decode(command)
  → ApplicationKernel.handle(command)
  → typed result/event
  → DesktopTransport.render(event)
```

No calibration, consent, settings, or intervention behavior may be implemented in a transport adapter. Delete the duplicate path only after a parity test runs the same command fixture through direct and WebSocket transports and compares domain events.

### 7.5 Typed command/event bus

Use a small in-process dispatcher, not a general event-bus framework. Commands have one handler and return a result; events may have multiple subscribers.

Representative commands:

```text
StartSession
StopSession
BeginCalibration
ApproveCalibration
ProposeIntervention
AuthorizeIntervention
RestoreIntervention
RecordUserResponse
UpdateSettings
DeleteLocalData
```

Representative events:

```text
ObservationReadinessChanged
SignalEstimatePublished
SupportEstimatePublished
CalibrationUpdated
InterventionPresented
InterventionApplied
InterventionRestoreFailed
PolicyOutcomeFinalized
HealthChanged
```

Every externally emitted event has a generated schema, event ID, schema version, wall time, boot ID, monotonic time, sequence, and correlation/causation IDs.

### 7.6 Design options considered

| Concern             | Rejected near-term option                | Recommended option                                                     | Reason                                                      |
| ------------------- | ---------------------------------------- | ---------------------------------------------------------------------- | ----------------------------------------------------------- |
| deployment          | network microservices                    | local modular monolith                                                 | TCC, latency, privacy, and operational simplicity           |
| state model         | end-to-end multimodal deep network       | corrected features + interpretable regularized model + temporal filter | data volume and ground truth do not justify deep complexity |
| physiology backend  | silent dynamic fallback                  | explicit validated backend registry and abstention                     | makes algorithm identity and failure observable             |
| policy              | always-on online contextual bandit       | deterministic production policy; consented MRT/research mode           | current rewards and causal evidence are not adequate        |
| storage             | scattered files or shared DB connections | one repository API and single-writer SQLite actor                      | atomic lifecycle records and predictable concurrency        |
| desktop integration | two domain implementations               | one kernel with two transport adapters during migration                | prevents safety divergence                                  |
| LLM role            | agent chooses and applies capabilities   | LLM proposes typed bounded plans; deterministic coordinator authorizes | preserves user authority and limits prompt-injection impact |

---

## 8. Canonical data and time contracts

### 8.1 Clock port

Add `cortex/application/clock.py`:

```python
class Clock(Protocol):
    def unix_ms(self) -> int: ...
    def monotonic_ns(self) -> int: ...
    def today_utc(self) -> date: ...
    @property
    def boot_id(self) -> UUID: ...
```

Production uses `SystemClock`; tests use `FakeClock` with independently controlled wall and monotonic values. No domain constructor calls `time.time()`, `time.monotonic()`, `datetime.now()`, or `date.today()` directly after migration. Rate-limit middleware may retain a private monotonic function injection.

Clock rules:

- elapsed-time comparisons use `monotonic_ns` from the same `boot_id`;
- persisted expiry uses wall time plus a stored duration or is recomputed safely on restart;
- wall-clock rollback never lengthens an authorization or cooldown beyond its original duration;
- wire times end in `_unix_ms`; monotonic values end in `_mono_ns`;
- duration fields end in `_ms` or `_seconds`; units are never implicit.

### 8.2 Observation envelope

All sensor paths emit a record even when no value is available:

```python
class ObservationEnvelope[T](BaseModel):
    source: Literal["camera", "mouse", "keyboard", "browser", "editor", "window"]
    source_instance_id: UUID
    sequence: int
    observed_at_unix_ms: int
    observed_at_mono_ns: int
    boot_id: UUID
    value: T | None
    validity: Literal["valid", "missing", "rejected", "stale"]
    missing_reason: MissingReason | None
    quality: float                 # [0, 1], meaningful only with documented components
    quality_components: dict[str, float]
    algorithm_version: str
```

`MissingReason` is a closed enum: `NO_FACE`, `LOW_LIGHT`, `SATURATED`, `MOTION`, `OCCLUDED`, `CAMERA_WARMUP`, `FRAME_DROPPED`, `PERMISSION`, `SOURCE_DISCONNECTED`, `INSUFFICIENT_WINDOW`, `ARTIFACT`, and `UNKNOWN`.

The capture scheduler, not each successful estimator, owns sequence and observation time. This preserves exposure and gap information.

### 8.3 Signal estimate contract

```python
class SignalEstimate(BaseModel):
    metric: SignalMetric
    status: Literal["ready", "warming_up", "unavailable", "experimental"]
    value: float | None
    unit: str
    window_start_unix_ms: int
    window_end_unix_ms: int
    valid_duration_ms: int
    valid_fraction: float
    sample_count: int
    artifact_fraction: float
    quality: float
    uncertainty: Uncertainty | None
    algorithm: AlgorithmIdentity
    unavailable_reasons: list[MissingReason]
```

The UI renders status and quality before value. A stale value is not carried forward as if current; if displaying the last value is useful, label it `last_valid_value` and show its age.

### 8.4 Feature contract

Replace a flat optional-float vector with named measurements:

```python
class FeatureValue(BaseModel):
    value: float | None
    valid: bool
    quality: float
    age_ms: int
    source_window_ms: int
    algorithm_version: str
    missing_reason: MissingReason | None

class FeatureVector(BaseModel):
    schema_version: str
    observed_at_unix_ms: int
    observed_at_mono_ns: int
    boot_id: UUID
    features: dict[FeatureName, FeatureValue]
```

If an array is needed for a model, `FeatureSchema` owns a frozen ordered catalog and transformation metadata. It validates exact model input dimension. Documentation is generated from that catalog, removing the current 12-versus-14 mismatch.

### 8.5 Support estimate contract

```python
class SupportEstimate(BaseModel):
    estimate_id: UUID
    state: Literal["support_likely", "under_engaged", "flow_like", "recovering", "unknown"]
    status: Literal["estimated", "insufficient_evidence", "warming_up"]
    scores: dict[str, float]       # heuristic/model scores
    probabilities: dict[str, float] | None  # only from a registered calibrated model
    evidence_coverage: float
    contributing_features: list[FeatureContribution]
    exclusions: list[str]
    model: AlgorithmIdentity
    observed_at_unix_ms: int
    observed_at_mono_ns: int
    boot_id: UUID
```

Keep compatibility aliases for `HYPER`, `HYPO`, `FLOW`, and `RECOVERY` on the wire during one deprecation cycle, but change explanatory product copy first. The target names deliberately describe an application decision, not a diagnosis.

### 8.6 Schema governance

- Pydantic remains the canonical wire source.
- Add native messages, VS Code messages, desktop callback payloads, error frames, receipts, and authorization objects to generation.
- Every generated file carries schema package/version and a source hash.
- Breaking changes increment a major protocol version and use a negotiation handshake.
- Golden fixtures are generated in Python and decoded/encoded by browser and VS Code tests.
- A catalog coverage test finds every literal passed to `send_message`/native output and proves it belongs to the union.
- Handwritten `Record<string, unknown>` is permitted only at the initial decoder boundary; no domain handler receives it.

---

## 9. Signal and inference implementation

### 9.1 Capture pipeline

Preserve the existing macOS camera-selection rules, including live Continuity Camera re-enumeration and warm-up. Refactor frame processing into this order:

1. capture a frame and scheduler timestamp;
2. emit frame observation metadata even on failure;
3. run face tracking with the actual capture timestamp in milliseconds, as the MediaPipe video/live-stream interfaces require frame timestamps ([official Face Landmarker guide](https://developers.google.com/edge/mediapipe/solutions/vision/face_landmarker/python));
4. compute geometry/motion against the previously committed valid landmarks;
5. compute illumination, sharpness, pose, occlusion, and ROI coverage;
6. decide frame validity with reason codes;
7. extract per-ROI robust color summaries only for valid skin regions;
8. commit current landmarks after derivative computation;
9. append the observation, including missing/rejected entries, to bounded time-series buffers;
10. publish readiness and health independently of feature values.

Frame quality must be empirically tied to downstream error. Until then, call it `quality_score`, not a probability of correctness.

Suggested bounded interpolation policy, to be validated:

- preserve a uniform target time grid derived from measured capture times;
- allow interpolation only between valid endpoints;
- never bridge a gap longer than 250 ms for pulse preprocessing;
- reject a pulse window below 80% valid coverage or above 10% motion-rejected coverage;
- expose these as proposed configuration defaults with validation plots, not hidden constants.

### 9.2 Continuous rPPG pipeline

Implement these components behind `cortex/services/physio_engine/v2/` while keeping the old estimator available for replay comparison:

```text
ROI observations
  → coverage/gap gate
  → measured-time resampler
  → detrend + per-channel normalization
  → explicit POS/CHROM/GREEN backend
  → overlap-add signal timeline
  → spectral HR candidate + harmonic/prior check
  → absolute-time peak candidates
  → overlap reconciler / artifact validator
  → unique BeatEvent stream
  → metric-specific window aggregators
```

Key data structures:

```text
PulseWindowResult(window_id, waveform segment, HR candidate, SNR, quality, exclusions)
BeatCandidate(absolute_mono_ns, prominence, source_window_id, quality)
BeatEvent(beat_id, absolute_mono_ns, status, rejection_reason, provenance)
IBI(left_beat_id, right_beat_id, duration_ms, status, correction)
```

Implementation rules:

- no estimator appends anonymous interval values directly to HRV history;
- overlapping windows merge into a signal/beat timeline using absolute times;
- boundary peaks are provisional until the next window confirms them;
- reconciliation is deterministic and idempotent under replay;
- HR spectral selection includes prior continuity but may break the prior when evidence supports a real step change;
- harmonics are explicitly tested rather than accepted as the dominant peak;
- every rejection is counted by reason;
- a backend change resets state and emits an algorithm-change event;
- backend fallback is explicit and visible to the user/health endpoint.

### 9.3 HR and HRV publication policy

| Metric         | Near-term status                | Minimum evidence before display                                 | Validation reference                     |
| -------------- | ------------------------------- | --------------------------------------------------------------- | ---------------------------------------- |
| heart rate     | supported estimate              | valid pulse window and quality gate                             | ECG or validated contact pulse reference |
| RMSSD          | experimental, hidden by default | ≥3 minutes accepted unique beats; prefer 5 minutes externally   | ECG R–R intervals                        |
| SDNN           | experimental, hidden by default | ≥5 minutes accepted unique beats                                | ECG R–R intervals                        |
| pNN50          | disabled                        | no release until dedicated validation                           | ECG R–R intervals                        |
| LF/HF          | disabled                        | ≥5 minutes plus stationarity/respiration context and validation | ECG + respiration reference              |
| sample entropy | disabled                        | metric-specific stability and repeatability study               | ECG R–R intervals                        |

All limits are conservative product gates, not a claim that a webcam becomes equivalent to ECG after the interval elapses. Report coverage and error across operating conditions.

### 9.4 Respiration v2

Create independent channel estimators:

- `ColorEnvelopeRespiration`: amplitude/baseline modulation of a quality-gated pulse/skin signal;
- `HeadMotionRespiration`: face-scale-normalized vertical trajectory derived from time-correct landmarks;
- optionally later, `TorsoMotionRespiration` if the upper-body feature is intentionally added.

Each returns candidate rate, periodicity, quality, and phase on a shared measured-time window. `RespirationFusion` combines only available, agreeing channels and otherwise abstains. Use at least 30–60 seconds for a trend; choose filter limits based on the target population and evaluate slow breathing explicitly.

Breath-pause detection, if researched, is a separate detector over signal amplitude/periodicity with labeled reference belts or capnography and a declared minimum duration. It must never be called apnea in ordinary product copy.

### 9.5 Time-correct kinematics

- `BlinkTracker` consumes `(eye_ratio, visible, mono_ns)` and yields closed intervals plus valid exposure.
- `HeadPoseTracker` unwraps angles and derives degrees/second with robust finite differences.
- `MotionTracker` derives face-widths/second and a window motion distribution.
- `PostureProxy` uses calibrated neutral head pose, face scale, and camera configuration, and returns `unavailable` after repositioning.
- camera switch, resolution change, large face-scale jump, or long face loss invalidates the relevant calibration.

### 9.6 Calibration v2

Use a short protocol with explicit intent:

1. **camera/quality check:** validate lighting, face size, pose, and steady capture;
2. **physiological rest:** collect a long enough valid interval for only the metrics actually supported;
3. **normal work sample:** collect mouse/keyboard/window behavior during representative work, not seated rest;
4. **optional labeled workload task:** collect a task plus immediate subjective label for research/personalization;
5. **review:** show valid duration, unavailable metrics, quality, and what will be stored;
6. **commit:** save only after explicit approval.

The runtime and calibration pipeline share the same estimator instances/factories and configuration. Calibration never invents data. A demo mode stores `demo_profile=true` in a separate namespace and cannot enable physiology-driven product behavior.

On `CalibrationUpdated(profile_id)`:

- pause inference publication;
- validate schema/algorithm compatibility;
- rebuild scorer, blink, posture, stress, and model baselines from one immutable profile;
- clear incompatible temporal state;
- publish readiness;
- resume without daemon restart.

### 9.7 State estimation v2

Implement in three maturity levels:

#### Level A — corrected deterministic score

- quality- and availability-normalized rules;
- explicit minimum evidence per state;
- `UNKNOWN` on insufficient evidence;
- scores only, never probabilities;
- versioned transforms and weights;
- explanation includes positive, negative, and missing contributions.

This is the first production target because it is explainable and does not pretend a training set exists.

#### Level B — validated probabilistic model

- preregister label and feature windows;
- fit a regularized multinomial/logistic model or similarly interpretable generalized model;
- split by participant before any tuning;
- perform a separate calibration fit;
- compare against Level A and simple base-rate/last-state baselines;
- ship only if it improves participant-held-out decision utility and calibration without unacceptable subgroup regression.

#### Level C — personalized update

- start from the validated population model;
- adapt only with explicit, semantically valid labels;
- constrain update magnitude and retain a rollback model;
- never equate accepting an intervention with “the HYPER label was correct”;
- show model age/data sufficiency and allow reset.

Temporal smoothing can remain EMA/hysteresis initially, but all dwell uses monotonic durations. A later hidden Markov/state-space model is justified only if replay evidence shows a consistent advantage.

### 9.8 Trigger design

Split `TriggerPolicy` into composable decisions:

```text
EligibilityGate       — session active, evidence ready, context present
ReceptivityGate       — quiet mode, cooldown, recent dismissal, meeting/focus state
SafetyGate            — feature flags, capability health, allowed action classes
InterventionNeed      — support estimate and dwell
ActionFeasibility     — connected clients, consent ceiling, reversible capabilities
PolicySelector        — deterministic or research behavior policy
```

Each returns a typed reason. No-context or missing-signal conditions become visible eligibility outcomes rather than silent early returns. A trigger trace can explain exactly why the system acted or stayed quiet without logging private content.

### 9.9 Break recommendations

Replace the current stress-integral trigger with this staged policy:

- production: elapsed focused-work duration plus user-configured cadence, input fatigue proxies, and dismiss/snooze history; physiology can be shown as an experimental trend but cannot force a break;
- research: quality-gated leaky accumulator with a frozen equation and outcome protocol;
- validated future: only promote if it improves a prespecified user-reported or performance outcome over time-only reminders without increasing interruption burden.

---

## 10. Intervention transaction and consent implementation

### 10.1 Lifecycle

```mermaid
stateDiagram-v2
    [*] --> Drafted
    Drafted --> Rejected: validation or safety failure
    Drafted --> Presented: proposal broadcast
    Presented --> Dismissed: user dismisses
    Presented --> Authorized: exact user approval
    Presented --> Authorized: matching earned auto-authorization
    Authorized --> Applying: durable intent recorded
    Applying --> Applied: all required receipts verified
    Applying --> PartiallyApplied: mixed receipts
    Applying --> Failed: no intended effect applied
    Applied --> Restoring: timeout, recovery, user undo, shutdown
    PartiallyApplied --> Restoring: immediate compensation
    Restoring --> Restored: postconditions verified
    Restoring --> RestoreFailed: retryable receipts retained
    Restored --> Closed
    Failed --> Closed
    Dismissed --> Closed
    Rejected --> Closed
```

The lifecycle is persisted. Events may be delivered more than once; handlers must be idempotent.

### 10.2 Immutable action manifest

Before presentation, validation creates:

```text
ActionManifest
  intervention_id: UUID
  manifest_version: str
  actions: ordered list[BoundedAction]
  required_consent_level: exact enum
  capability_requirements: set[Capability]
  reversible: bool per action
  max_targets: bounded integer
  expires_at_unix_ms
  content_digest: SHA-256 over canonical representation
```

`BoundedAction` contains a canonical action enum and validated typed parameters. Unknown actions fail closed. The UI displays the consequences derived from this manifest, not an LLM-authored assurance.

### 10.3 Authorization record

An authorization contains:

```text
authorization_id, intervention_id, manifest_digest,
authorized_level, source(user_gesture | earned_policy),
issued_at, expires_at, nonce, settings_version, consent_state_version
```

Execution verifies all fields and consumes the nonce atomically. Authorization expires after a short proposed default of 60 seconds and is invalidated by manifest, settings, connection, or consent-state change. A user gesture can authorize only what was visibly presented.

Earned autonomy is action- and scope-specific. Five approvals must not automatically grant an action on every site/file or at unlimited cardinality. Track action type, target scope, reversible behavior, recent rejection/undo, and expiry. A user can lower the global ceiling immediately.

### 10.4 Client protocol

Replace overloaded `INTERVENTION_TRIGGER` with:

```text
INTERVENTION_PROPOSED       render only
INTERVENTION_AUTHORIZE      client → daemon, user gesture
INTERVENTION_APPLY          daemon → named capable client, includes authorization + manifest
INTERVENTION_ACTION_RESULT  per action receipt/failure
INTERVENTION_RESTORE        daemon → client, includes stored inverse receipts
INTERVENTION_RESTORE_RESULT per receipt + verified final state
INTERVENTION_CLOSED         clear presentation
```

Browser and VS Code keep presentation handlers pure. Capability handlers validate:

- schema and size;
- authenticated daemon session;
- message/intervention/authorization IDs;
- manifest digest and expiry;
- target ownership and current connection identity;
- idempotency key;
- local user permission/site scope;
- action-specific bounds.

### 10.5 Action receipts

Every adapter returns:

```text
ActionReceipt
  receipt_id
  action_id
  client_instance_id
  attempted_at / completed_at
  result: applied | already_applied | failed
  before_state: typed minimal inverse state
  after_fingerprint
  inverse_action: typed command
  error_code / retryable
```

Examples:

- hide tabs: exact hidden tab IDs, window IDs, prior active tab, and tabs already hidden before Cortex;
- fold code: document URI/version, selections, and Cortex-owned folded ranges rather than `unfoldAll`;
- focus file: prior active editor/document and whether focus actually changed;
- overlay: host tab/frame and injected element instance ID.

Restoration changes only Cortex-owned effects and tolerates user changes after apply. It must not reveal every hidden tab or unfold user-created folds indiscriminately.

### 10.6 Failure and shutdown semantics

- Required action failure triggers compensation of already-applied actions.
- Optional action failure yields `PartiallyApplied` and a truthful UI description.
- Client disconnect retains pending receipt and retries on the same authenticated client identity when safe.
- Shutdown stops new proposals, cancels unstarted authorizations, requests restore, waits a bounded interval, persists unresolved receipts, then performs existing process cleanup.
- On next startup, unresolved interventions enter recovery mode before new mutations are allowed.
- A manual “Restore everything Cortex changed” control is always available and does not require LLM or policy access.

---

## 11. Persistence, policy, and evaluation design

### 11.1 Event store

Create `cortex/storage/` with:

```text
database.py        connection ownership and compatibility check
migrations/        numbered forward migrations
writer.py          bounded async queue, one owning task/connection
repositories.py    typed transaction operations
retention.py       delete/export/compaction policy
```

The storage writer owns one SQLite connection. Domain services submit typed operations and receive commit acknowledgement. Large raw frames, waveform samples, visible code, terminal text, and full URLs are not stored in the ordinary database.

Initial journal mode should be rollback journal with `synchronous=FULL` or another explicitly benchmarked durable setting because the reviewed SQLite is in the documented WAL-reset affected range. If the packaged runtime is upgraded to a fixed SQLite, WAL may be enabled after a concurrent checkpoint/write stress test. The browser/editor access state through authenticated APIs, never direct database connections.

### 11.2 Core relational model

The exact SQL may vary, but these logical records and constraints must exist:

| Table                 | Essential fields and constraints                                                                                                                                    |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema_migration`    | version PK, checksum, applied_at                                                                                                                                    |
| `session`             | session UUID PK, boot ID, start/end wall time, app version, config version                                                                                          |
| `calibration_profile` | profile UUID, schema/algorithm versions, provenance, quality summary, created/approved times, active flag                                                           |
| `support_estimate`    | estimate UUID, session FK, wall time, state/status, scores, coverage, model version; no raw content                                                                 |
| `intervention`        | UUID PK, proposal/manifest digest, lifecycle state, policy decision FK, exact consent level, timestamps, row version                                                |
| `authorization`       | UUID PK, intervention FK, manifest digest, source, level, expiry, consumed_at, nonce UNIQUE                                                                         |
| `action_receipt`      | UUID PK, intervention/action FK, client instance, typed inverse payload, status, error, timestamps, idempotency key UNIQUE                                          |
| `intervention_event`  | event UUID PK, intervention FK, sequence UNIQUE per intervention, type, minimal payload, timestamp                                                                  |
| `policy_decision`     | UUID PK, session, eligibility, availability reason, context schema/model/policy version, feasible actions, all propensities, chosen action, seed/counter, timestamp |
| `outcome_window`      | UUID PK, decision FK UNIQUE per reward version, scheduled interval, component observations, censoring/contamination, finalized_at                                   |
| `policy_reward`       | decision FK + reward_version UNIQUE, bounded reward, component mapping, finalized_at                                                                                |
| `consent_state`       | action/scope key PK, level, approvals/rejections, version, updated_at                                                                                               |
| `audit_event`         | event UUID, category, code, correlation IDs, redacted metadata, wall time                                                                                           |

Use foreign keys, check constraints for bounded scores/propensities/rewards, and optimistic row versions for lifecycle transitions. A transaction that finalizes an outcome also records its single reward and updates policy sufficient statistics, or none of those changes commit.

### 11.3 Retention and user control

- default retention is purpose-specific, documented, and bounded;
- store aggregates needed for personalization, not raw workspace content;
- allow separate retention toggles for product personalization and consented research export;
- export uses documented JSON/CSV schemas with provenance and redaction;
- delete is comprehensive across database, calibration, caches, logs, and Keychain entries, with an explicit confirmation and receipt;
- log rotation and database compaction never retain deleted content in ordinary backups indefinitely;
- file ownership is current-user only and permissions are checked on startup.

### 11.4 Production policy

Ship a versioned deterministic policy first:

```text
if not eligible or no feasible action: no_action
elif recent repeated dismissal: no_action
elif user selected a preferred low-friction template: that template
else: suggest_only micro-plan
```

The policy returns a full distribution for logging only when a genuine randomized research mode is active. In deterministic mode, reports must not pretend propensities support off-policy causal comparisons.

### 11.5 Research AMIP v2

If online adaptation is retained:

1. enroll through separate informed consent;
2. freeze an action catalog, context schema, reward version, and safety mask for a study epoch;
3. normalize features from training data and add an intercept;
4. log eligibility and all feasible-arm propensities before action;
5. use a reproducible random generator and store seed/counter or draw ID;
6. persist model sufficient statistics with checksum and policy version;
7. bound/regularize updates and solve linear systems stably;
8. finalize one reward after the prespecified proximal window;
9. include no-action in identical outcome collection;
10. monitor probability floors, effective sample size, reward missingness, intervention burden, and safety events;
11. stop or roll back automatically on a prespecified guardrail breach.

Do not let the policy choose consent level, new capabilities, target scope, or whether a safety rule applies.

### 11.6 Reward definition

Define reward as a versioned function of prespecified components, for example:

```text
r = w1 * user_helpfulness_rating
  + w2 * proximal_support_improvement
  - w3 * dismissal
  - w4 * restore_failure
  - w5 * interruption_cost
```

Before using it:

- define every component's scale and missing-data rule;
- prevent the same user action from serving as both an intermediate update and the finalized reward;
- keep raw components so future reward versions can be recomputed without rewriting history;
- do not use a state estimate as both treatment trigger and uncritical outcome, which would create circular self-validation;
- prioritize direct user feedback and task-level measures over a downstream score produced by the same heuristic.

### 11.7 Causal evaluation

The first defensible study should resemble a micro-randomized trial:

- decision points occur only when availability/eligibility criteria are met;
- randomize among no-action and one or more low-risk suggestion options;
- specify a proximal outcome window, such as a brief self-report or behavioral measure within a fixed interval;
- cap daily burden independently of randomization;
- use weighted and centered least squares for proximal effects and time-varying moderation, with participant/session clustering;
- state exclusions and missing-outcome handling before analysis.

For offline policy evaluation, report the exact target policy and use multiple estimators (direct, IPS/SNIPS where justified, doubly robust, and a clipped/SWITCH sensitivity analysis), together with overlap, weight tails, effective sample size, and confidence intervals. Agreement among estimators is a diagnostic, not proof. The HeartSteps trial is a useful concrete example of eligible decision points, randomization, prespecified proximal outcomes, and weighted/centered analysis ([HeartSteps MRT](https://pmc.ncbi.nlm.nih.gov/articles/PMC6401341/)).

---

## 12. File-level implementation work packages

Each package is independently reviewable. “Exit” means the package is not complete until that evidence exists. Sizes are relative (`S`, `M`, `L`, `XL`), not calendar promises.

### WP-0 — Containment and release integrity (`M`, blocks all mutation releases)

**Files:**

- `cortex/scripts/native_host.py`
- `cortex/apps/browser_extension/lib/auth.ts`
- `cortex/libs/schemas/` and `cortex/scripts/generate_ts_schemas.py`
- `cortex/services/runtime_daemon.py`
- `cortex/services/intervention_engine/executor.py`
- `cortex/apps/browser_extension/background.ts`
- `cortex/apps/vscode_extension/src/extension.ts`
- extension package manifests and CI workflows
- `cortex/__init__.py`, API construction, build/release scripts

**Changes:**

1. Add the native request/response union and change both implementations to canonical `auth_token`.
2. Add a proposed `intervention.execution_mode = suggest_only | authorized | research_autonomous` setting. Default existing users and fresh installs to `suggest_only`; migration must never silently increase authority.
3. In suggest-only mode, strip all mutating actions after validation and present them as descriptions; clients reject apply messages entirely.
4. Change consent checking so only `requested_level <= max_allowed` is a permit. Treat current downgrade as non-executable until WP-6 introduces a materialized lower-level plan.
5. Stop browser tab hiding and VS Code folding from `INTERVENTION_TRIGGER`; retain presentation only.
6. Track the VS Code lockfile, repair the Jest script for the installed major, add it to CI, and update `ws`.
7. Add Python dependency audit tooling and fix or formally record browser audit paths.
8. Generate all version surfaces from `pyproject.toml` and fail on divergence.

**Tests:**

- actual native-host stdin/stdout framing is decoded by TypeScript;
- a response using `token` instead of `auth_token` fails the contract fixture;
- every consent combination `(requested, earned, global max, minimum)` is table-tested;
- `INTERVENTION_PROPOSED`/legacy trigger causes zero Chrome tab and VS Code editor mutation;
- clean-checkout VS Code `npm ci`, compile, and Jest run;
- dependency scans emit machine-readable CI artifacts;
- version consistency check spans Python, FastAPI, package manifests, and release name.

**Exit:** G0, the immediate portion of G1/G2, and G6 are closed; a fresh app can authenticate; all suites are green; the default product cannot mutate a workspace.

### WP-1 — Clock and schema foundation (`L`)

**Implementation status (2026-08-24): complete.** The runtime, REST, and
WebSocket composition paths now share an injected dual clock; v2 event
metadata carries wall time, monotonic time, boot identity, event identity,
ordering, and causality while v1 decoding preserves its original provenance.
Cooldown persistence is reboot-safe and bounded under wall-clock rollback.
Both browser and VS Code declarations are generated from every Pydantic schema
and checked as identical consumers, with negotiated protocol compatibility and
duplicate/reordered-event defenses. Deterministic tests cover clock jumps,
reboot, long uptime, DST/date rollover, time zero, legacy/new protocol minors,
major rejection, and wall-independent elapsed decisions. Completion evidence:
2,350 non-Qt Python tests plus 57 isolated Qt tests, strict mypy across 441
files, Ruff, schema drift, 178 browser tests/build, and 12 VS Code tests/package.

**Files:**

- new `cortex/application/clock.py`
- `cortex/libs/schemas/__init__.py`, `features.py`, `state.py`, `ws_message.py`, `realtime.py`
- `cortex/services/state_engine/feature_fusion.py`, `smoother.py`
- `cortex/services/runtime_daemon.py`
- `cortex/services/api_gateway/routes.py`, `websocket_server.py`
- cost, trigger, helpfulness, restore, snapshot, focus/window, and calibration time consumers

**Changes:**

1. Add `Clock`, `SystemClock`, and `FakeClock`; inject into domain services.
2. Introduce dual-clock event metadata and `boot_id`.
3. Replace public `timestamp` fields through a versioned schema migration; supply compatibility decoding for one release.
4. Replace truthiness fallbacks such as `now = current_time or ...` with explicit `is None`, so test time zero works.
5. Store cooldown/expiry state as wall expiry plus bounded remaining duration; reconstruct safely after reboot.
6. Extend code generation to all client boundaries and establish protocol negotiation.
7. Repair timestamp and cost-persistence tests with one fake clock rather than patched globals.

**Tests:** wall clock jumps backward/forward while monotonic advances; long host uptime; process reboot with new boot ID; daylight-saving and UTC date rollover; time zero; reordered and duplicate messages; old-minor/new-minor compatibility; major-version rejection.

**Exit:** no external schema contains an ambiguous time; `rg` finds no direct clock call in migrated domain packages; replay produces identical duration decisions independent of wall clock.

### WP-2 — Observation and capture integrity (`L`)

**Implementation status (2026-08-24): complete.** The camera scheduler now
emits a dual-clock, sequenced observation for every attempt, including warm-up
and disconnected reads, queue evictions, adaptive skips, no-face results,
detector timestamp artifacts, and quality rejections. Motion is computed
before landmark commit in face-widths per second; MediaPipe receives the
actual monotonic capture instant; face-loss hysteresis is elapsed-time based.
Runtime and calibration use one bounded observation buffer with explicit
coverage, repeated-time, motion-fraction, and 250 ms maximum-gap gates before
bounded interpolation. All-missing windows stay unavailable and never reach
the pulse estimator. Live post-open camera identity is index-reorder safe;
physical changes reset camera-dependent state and invalidate calibration while
the TCC, Continuity Camera, post-open re-verification, and four-read warm-up
rules remain intact. Completion evidence: 98 focused capture/runtime/
calibration tests; 2,182 non-PySide tests passed with five dataset/platform
skips; every PySide-bearing module passed in an isolated process; strict mypy
across 444 files, Ruff, both generated-client schema drift gates, 178 browser
tests plus Chrome build, and 12 VS Code tests plus VSIX packaging.

**Files:**

- `cortex/services/capture_service/webcam.py`
- `face_tracker.py`, `quality.py`, `pipeline.py`, `calibration_runner.py`
- new `cortex/libs/schemas/observations.py`
- `runtime_daemon.py` capture loop

**Changes:**

1. Add `ObservationEnvelope` and missing-reason catalog.
2. Emit scheduled observations for failed reads, no face, low quality, and intentional frame skips.
3. Compute motion before committing landmarks and use physical-time units.
4. Pass actual capture time into MediaPipe.
5. Replace parallel arrays with a bounded time-indexed observation buffer.
6. Implement explicit gap/coverage checks and bounded interpolation.
7. Make face-loss transitions reachable and time-based.
8. Expose camera identity changes and invalidate dependent calibration.
9. Retain all project-specific TCC and Continuity Camera behavior.

**Tests:** deterministic frame traces for face present/lost/reacquired, all missing, single long gap, repeated timestamps, variable FPS, adaptive skip, camera reorder, Continuity Camera wake, warm-up, low light, and motion. A property test asserts that removing valid observations never increases readiness or quality.

**Exit:** no all-missing or insufficient-coverage trace produces a ready physiological window; motion changes under translated landmarks; capture stop always releases the camera in injected failure states.

### WP-3 — rPPG, beat stream, HRV, and respiration v2 (`XL`)

**Implementation status (2026-08-24): complete for the production signal
architecture; external evidence promotion remains gated by WP-11.** A fixed,
checksum-verifiable POS/CHROM/GREEN backend registry now feeds a measured-time
pipeline with exact code/configuration provenance, explicit quality and
uncertainty, and fail-closed publication states. TS-CAN and its nonexistent
checkpoint path were removed instead of silently falling back. Absolute beat
candidates are reconciled across overlapping windows into an idempotent,
chronological ledger; IBIs are derived only from named accepted beats and
retain explicit refractory, boundary, plausibility, and local-artifact
rejections. HRV readiness is metric-specific, fingerprints its own
implementation and upstream dependency, and remains unavailable by default.
Respiration uses independent color-envelope and face-normalized motion channels
over 30–60 second windows, requires quality plus cross-channel agreement, and
has no apnea or diagnostic behavior. It remains unavailable by default.
Heart-rate results remain labelled experimental—not supported—until the
preregistered simultaneous-reference gate is passed. Completion evidence:
2,194 non-PySide Python tests passed with five dataset/platform skips; every
PySide-bearing module passed in isolation; focused v2/observation/runtime tests,
strict mypy on the touched signal/runtime surface, Ruff, generated-schema drift,
178 browser tests plus TypeScript/Chrome build, and 12 VS Code tests plus VSIX
packaging all pass. Optional UBFC/PURE replays are checksum- and
subject-split-enforced and skip when licensed datasets are not locally supplied.

**Files:**

- `cortex/services/physio_engine/rppg.py`
- `pulse_estimator.py`, `respiration.py`, `quality_scorer.py`, `roi_extractor.py`
- new `cortex/services/physio_engine/v2/`
- physiology schemas/configuration and runtime wiring
- dataset/replay tests

**Changes:**

1. Implement measured-time resampling and explicit backend identity.
2. Create absolute `BeatCandidate`, `BeatEvent`, and `IBI` streams with overlap reconciliation.
3. Add complete beat plausibility/artifact handling and metric-specific readiness.
4. Remove direct repeated-window IBI insertion.
5. Make backend registry validate required assets/checksums at startup; remove or package TS-CAN honestly.
6. Decide whether dynamic POS/CHROM/GREEN selection is retained based on validation; wire it only if it beats a fixed backend on held-out data.
7. Implement respiration channels/fusion on 30–60 second windows; remove unreachable apnea logic and medical naming.
8. Add signal algorithm/version/quality/uncertainty to all outputs.
9. Default-disable unvalidated HRV/nonlinear/frequency and breath-pause metrics.

**Tests:**

- synthetic sinusoids with irregular sampling, drift, harmonics, step changes, motion, missing blocks, and known beats;
- overlap idempotence: processing the same window twice creates no new beat;
- chronological and unique beat/IBI invariants;
- artifact injection and boundary-peak reconciliation;
- backend absent/corrupt/fallback behavior;
- recorded public-dataset replay with subject-disjoint splits;
- simultaneous reference-sensor study described in WP-11/Section 13.

**Exit:** G5's signal portion closes; every published metric meets its evidence contract and preregistered reference gate; otherwise it remains unavailable/experimental.

### WP-4 — Kinematics and calibration unification (`L`)

**Implementation status (2026-08-24): complete.** Blink rate, duration,
PERCLOS, EAR variance, head angular velocity/jitter/freeze, and neutral-pose
dwell now use monotonic elapsed time and valid exposure rather than nominal
frame counts. The former shoulder/posture surface is an explicitly
camera-relative head/neck flexion proxy; no shoulder landmark or diagnostic
posture value is fabricated. Calibration now produces immutable, checksummed,
versioned profiles whose metric evidence records task context, maturity,
distribution, effective sample size, valid duration, missingness, quality,
camera identity/geometry, and exact code/configuration identity. Measured
profiles alone can be approved. Simulations remain demo artifacts and can
never become active.

The live and WebSocket paths now converge on one atomic activation command:
the daemon validates and constructs the entire replacement graph before
committing the active pointer, then swaps the graph synchronously and emits a
correlated `CalibrationUpdated` event. Persistence, checksum, schema,
algorithm, and camera failures preserve the prior graph; disconnect and
timeout paths reconcile a lost acknowledgement against the checksum-bound
authoritative pointer before reporting an outcome. Camera identity or
resolution changes invalidate camera-bound neutral pose and reapply it only
when the exact measured camera geometry returns. Completion evidence: 87
focused kinematics/calibration tests; 2,089 non-PySide tests with five
dataset/platform skips;
all 38 PySide-bearing test files passed in isolated processes; strict mypy
across 461 files; Ruff and schema-drift gates; 178 browser tests plus Chrome
build; and 12 VS Code tests, TypeScript compile, and VSIX packaging.

**Files:**

- blink, head-pose, posture services under `cortex/services/kinematics_engine/`
- `cortex/services/capture_service/calibration_runner.py`
- `cortex/apps/desktop_shell/onboarding.py`, `controller.py`, relevant dashboard settings
- baseline store/config models

**Changes:**

1. Convert blink, PERCLOS, angular velocity, freeze, and posture dwell to elapsed-time calculations with valid exposure.
2. Rename shoulder/posture claims to head/neck pose proxy; remove fake shoulder landmark storage.
3. Create immutable versioned `CalibrationProfile` with provenance.
4. Route calibration through the production feature factories and quality gates.
5. Separate rest physiology from representative-work behavioral calibration.
6. prevent synthetic fallback from writing an active profile in every transport and CLI mode.
7. Implement atomic `CalibrationUpdated` reload and dependent-state reset.

**Tests:** 15/24/30/60 FPS traces produce equivalent time metrics; dropped frames do not shorten blink duration; no-event cold start remains warming up; camera movement invalidates posture baseline; in-process and WebSocket calibration commands produce identical domain events; simulation can never become active calibration.

**Exit:** one calibrated profile has one provenance and is applied live by every dependent service; user-visible posture language matches the measured proxy.

### WP-5 — Evidence-aware support inference (`XL`)

**Implementation status (2026-08-25): complete and subsequently pruned.** Shipped as
`deterministic-support@2.1.0` with `support-features-v2.1.0` and an operational
`safety_null` rollback. The production path is behavior-only, fixed-denominator,
quality/availability bounded, explicitly abstaining, and probability-free.
Research HRV stress and the unregistered classifier are not instantiated or
registered. Model cards, participant-held-out/calibration split scaffolding,
the preregisterable study protocol, cross-surface unknown-state rendering, and
adversarial missingness/monotonicity/replay tests are tracked in-repo. Window
tracking has explicit source availability and a bounded event-identity ledger,
so repeated reads of its sliding window cannot inflate tab-switch or thrashing
evidence. WP-11 then deleted the unregistered ML classifier, invalid stress
integral, obsolete settings, and causal/bandit product code rather than leaving
disabled mechanisms available for accidental resurrection. Current aggregate
verification evidence is in Section 0.1.

**Files:**

- `cortex/services/state_engine/feature_fusion.py`
- `rule_scorer.py`, `smoother.py`, trigger policy, schema and UI presentation files
- new `cortex/services/state_engine/feature_schema.py` and model registry
- deleted `stress_integral.py`, the optional classifier, and their dead settings/tests

**Changes:**

1. Introduce named `FeatureValue` objects and a frozen ordered feature schema.
2. Implement quality/availability-normalized deterministic rules and coverage gates.
3. Rename scores/probabilities and emit `UNKNOWN`/insufficient evidence.
4. Convert dwell and recovery logic to injected monotonic durations.
5. Delete the stress integral; use elapsed focus time and explicit user preference for guided-break recommendations.
6. Delete the dead optional classifier; require a new validated lifecycle before any learned model can be introduced.
7. Define state/support target, labels, exclusions, and study protocol before Level B modeling.
8. Generate model cards and retain model rollback.

**Tests:** exhaustive missing-channel combinations; metamorphic tests that lower evidence cannot increase certainty; participant-held-out evaluation; probability calibration only when applicable; hysteresis under jitter; deterministic replay across machines; UI explicitly renders unknown/warming-up.

**Exit:** no score is called a calibrated probability without a calibration artifact; missing camera cannot imply flow; model and product claims match the measurement protocol.

### WP-6 — Transactional interventions and exact consent (`XL`)

**Implementation status (2026-08-25): complete at the WP-6 boundary.** The
production path now separates inert presentation from authority and enforces a
content-addressed `ActionManifest → ActionAuthorization → ActionReceipt`
chain. Authorization is one-time, exact-subset, consent-revision-bound,
wall/monotonic-expiring, and durably bound to the selected stable browser or
editor client before transport. Browser and VS Code adapters use bounded
write-ahead journals and receipt outboxes, verify postconditions, compensate
ambiguous partial effects, preserve user supersession, and recover idempotently
after process/service-worker restart. The executable catalog is deliberately
limited to ownership-safe `open_url`, `search_error`, `highlight_tab`, and
`resume_last_active_file`; destructive close/group/fold proposals remain
visible but inert. Desktop Undo is derived only from verified transaction
state, while shutdown, reset, and emergency restore share the exact inverse
path. The daemon journal remains the explicitly temporary atomic-JSON store
that WP-7 replaces with SQLite. Completion evidence: 2,472 non-Qt Python tests
with five declared dataset/platform skips; 62 Qt desktop-shell tests; strict
mypy across 472 source files; Ruff and schema drift gates; 208 browser tests,
Chrome/Edge builds, and a measured 234 KB gzip production file sum after
removing unused font assets; plus 30 VS Code tests, TypeScript compile, and
VSIX packaging.

**Files:**

- `cortex/services/consent/ladder.py`, `policy.py`
- `cortex/services/intervention_engine/executor.py`, `restore.py`, `snapshot.py`, validator/mapper
- runtime and WebSocket command routing
- browser `background.ts`, popup/content overlay
- VS Code `extension.ts`, fold controller, WebSocket client
- intervention/consent schemas

**Changes:**

1. Add lifecycle, manifest, authorization, receipt, and result schemas.
2. Replace Boolean consent with permit/downgrade/deny.
3. Make proposal delivery non-mutating.
4. Require exact manifest-bound authorization for apply.
5. Split browser/editor presentation from capability execution.
6. Return typed action receipts with inverse data and verification.
7. Persist lifecycle/receipts and recover unfinished work after restart.
8. Record true start/end wall times and monotonic duration.
9. Add compensation for partial apply and retry for restore failure.
10. Keep the user's global emergency restore and autonomy ceiling local and deterministic.

**Tests:** model-based state-machine testing, duplicate/reordered message property tests, authorization replay/expiry/mismatch, concurrent reset and apply, client disconnect at every transition, daemon/browser/editor restart, user changes after apply, partial adapter failure, reverse idempotence, and a fault-injection matrix. Assert at the adapter boundary that no call occurs without a matching authorization record.

**Exit:** G1–G3 close; every workspace effect has a causally prior exact authorization and a verified/retryable receipt.

### WP-7 — Transactional local storage (`L`)

**Implementation status (2026-08-25): complete at the WP-7 boundary.** One
current-user-only SQLite database now owns durable consent, intervention
authority, calibration identity, minimal session aggregates, policy records,
and bounded derived analytics. A single dedicated connection/thread enforces
foreign keys, `STRICT` tables, `DELETE` rollback journaling,
`synchronous=FULL`, checksummed forward migrations, full startup integrity and
write probes, and fail-closed future-schema/corruption handling. Critical
transactions await commit or rollback even under cancellation; best-effort
analytics use a bounded non-blocking queue with explicit drop/failure counts.
Legacy JSON/JSONL sources are byte-for-byte backed up, checksummed, imported
idempotently, and retained as compatibility projections; malformed
non-authority history is audibly skipped while malformed intervention
authority fails closed. Authenticated status/export/confirmed-delete APIs,
purpose-specific retention, active-effect deletion guards, exact projection
and migration-backup cleanup, calibration reload, and restart recovery are
wired through the runtime. Secrets remain in Keychain and opaque legacy keys
are never migrated. The Python wheel and PyInstaller spec both ship the SQL
migration; a new artifact gate prevents the former metadata-only wheel from
recurring. Completion evidence: forced-termination hot-journal recovery,
disk-full/read-only/corruption/permission/migration/backpressure tests; 2,501
non-desktop Python tests with three declared skips and 62 isolated desktop
tests; strict mypy across 484 files; Ruff, schema/version/design drift gates;
208 browser tests plus Chrome/Edge builds; 30 VS Code tests, compile, and VSIX
packaging; and an isolated built-wheel schema-creation smoke test.

**Files:** new `cortex/storage/`; migrations from existing state files; health and settings endpoints; build spec/package resources

**Changes:**

1. Implement startup SQLite version check, single connection, writer task, repositories, migrations, backup/export/delete, and retention.
2. Use rollback journal until a fixed packaged SQLite and WAL stress gate exist.
3. Migrate consent, intervention lifecycle, policy decisions/outcomes, calibration, and minimal session aggregates.
4. Leave Keychain secrets in Keychain; do not copy tokens/API keys into SQLite.
5. Keep a reversible migration backup and validate counts/checksums.
6. Add bounded queue behavior and backpressure; never block capture on analytics logging.

**Tests:** migration from every supported old shape, power-loss/fault injection at transaction boundaries, duplicate command idempotence, disk-full/read-only/corrupt DB behavior, retention/delete/export, permissions, queue saturation, and restart recovery.

**Exit:** decision→delivery→outcome→reward is atomically queryable; active mutation receipts survive restart; no unsupported SQLite concurrency mode is enabled.

### WP-8 — Policy and causal-evaluation correction (`XL`, depends on WP-6/7)

**Implementation status (2026-08-25): complete at the software boundary; the
independent research-method review remains an external promotion gate.** The
runtime now composes a deterministic non-learning production policy. SQLite
schema v2 records exact availability, eligibility, feasible arms, delivery,
the common proximal window, bounded idempotent observations, contamination,
censoring, and one versioned reward. No-action follows the identical lifecycle;
restart recovery and concurrent research draws are deterministic. Legacy
AMIP/bandit execution is disconnected from composition and its offline trainer
fails closed. Reports are descriptive policy diagnostics. The separately
consented research path freezes a two-arm MRT specification and analysis seed,
binds the specification checksum into policy state and every randomized
decision, persists reproducible HMAC-derived draw identifiers/counters, creates
exclusive owner-only immutable exports, and verifies exact input digests.
Centered WCLS with session-cluster bootstrap and target-policy OPE estimators
carry explicit support/positivity/weight/ESS diagnostics and reject production
records. The detailed estimand, inclusion rules, sensitivity analyses, and
promotion boundaries are tracked in
`cortex/docs/research/policy-evaluation-protocol.md`. Completion evidence
includes the real v1→v2 backup migration, deterministic replay, 40-way draw
serialization, one-reward/idempotency and no-action restart tests, contamination
timing, synthetic known-effect recovery, extreme-weight diagnostics, immutable
export checks, legacy-name migration, hard payload/date bounds, and 74 focused
runtime/evaluation/auth/artifact tests. The phase-closing matrix also passed
2,209 non-Qt Python tests with five declared skips, all 38 Qt-bearing test
modules in isolated processes, strict mypy across 493 files, Ruff and all
schema/design/version drift gates, 208 browser tests plus Chrome and Edge
builds, 30 VS Code tests plus compile/VSIX packaging, and a verified 264-file
wheel containing schema migration v2. This closes product correctness; it does
not substitute for preregistration, ethics review where applicable, or an
independent statistical review of a real study.

**Files:** AMIP/bandit/causal services under `cortex/services/eval/`; helpfulness and runtime reward paths; configuration; report UI/export

**Changes:**

1. Default to deterministic production policy.
2. Introduce exact decision-point, availability, feasibility, propensity, delivery, outcome-window, contamination, and reward records.
3. Ensure one finalized reward/version per decision and delete/pop transient decision state after finalization.
4. Follow no-action arms identically.
5. Persist and checksum policy state; use stable linear algebra, intercept, normalized features, UUIDs, and reproducible randomness.
6. Rename current report to diagnostics.
7. Implement a prespecified MRT export/analysis pipeline separately from runtime policy logic.
8. Add OPE estimators only with target-policy definition and complete diagnostics.
9. Wire `reward_window_seconds` and `bootstrap_samples` or remove them.

**Tests:** deterministic replay from decision log, one-reward uniqueness under repeated events, no-action follow-up, propensity sum/support, feasibility masking, persistence/reload equivalence, synthetic known-effect simulations, extreme-weight diagnostics, cluster bootstrap/WCLS reference comparison, and legacy-report naming migration.

**Exit:** production makes no causal claim; research reports are reproducible from an immutable export and pass a statistical-method review.

### WP-9 — Privacy context broker and planner hardening (`L`)

**Implementation status (2026-08-25): complete at the software and disclosure
boundary.** Networked planning is off by default (`no_llm`), with a stricter
`no_content` mode and a revision-bound `external_redacted` mode. Every external
request now passes through an exhaustive `TaskContext` leaf catalog,
default-false per-source selection, bounded Unicode/path/URL/secret
minimization, an exact user-visible prompt/context preview, conservative
provider-retention disclosure, and a random one-time handle that expires in at
most 60 seconds. Confirmation requires an exact phrase and burns the handle
before the provider await; cancellation, changing sources, going back, close,
expiry, wrong confirmation, or replay also burn it. The raw Anthropic transport
independently refuses requests without a broker-generated disclosure manifest.

Browser page-body context is exact-origin and requires both browser permission
and an explicit Cortex consent record, so required learning-site content-script
access cannot masquerade as an opt-in. Incognito fails closed at content-script
and background boundaries. Periodic activity telemetry is metadata-only;
untrusted records are shape-allowlisted, bounded, URL/secret-minimized, and raw
page/source excerpts are not persisted. Revocation scrubs legacy content fields
from local activity records. Model output remains an untrusted proposal and
cannot mint workspace capability; the WP-6 manifest/authorization/receipt
boundary remains separate.

Truthful privacy, security, provider-retention, API, adapter, setup, and UI
contracts are tracked in `cortex/docs/`. The cross-surface design refinement is
also complete against the pinned `emilkowalski/skills` review baseline, with
one generated token vocabulary, contrast-safe state/accent text, named
sub-300 ms functional motion, reduced-motion behavior, interruptible overlay
updates, restrained ambient work, and consistent press/focus/status semantics.
Completion evidence: 140 focused planner/privacy/API/provider tests; 35
desktop/UI test modules passing in isolated Qt processes; Ruff and schema/design
drift gates; all 241 browser tests plus TypeScript and Chrome/Edge production
builds; and 30 VS Code tests plus TypeScript compilation. Provider policy text
was checked against primary AWS, Google Cloud, and Anthropic documentation on
2026-08-25. Account/contract verification remains the deployer's
responsibility and Cortex never asserts zero retention.

**Files:** task-context schemas/collectors, LLM planner/prompts/parser/cache, browser/editor context providers, onboarding/settings/privacy/security docs

**Changes:**

1. Define a data-classification catalog for every context field.
2. Add per-source opt-in, visible request preview, and provider/retention disclosure.
3. Strip URL query/fragment, minimize paths, cap snippets, and run local secret detection/redaction.
4. Add deterministic no-content and no-LLM planners.
5. Treat all collected text as tainted; preserve origin metadata through the prompt builder.
6. Constrain model output to the generated action vocabulary and validate locally.
7. Move `<all_urls>` to user-granted optional host permissions where functionality allows; exclude incognito by default.
8. Ensure caches/logs never store raw context beyond the declared lifetime.

**Tests:** secret corpus, adversarial prompt-injection corpus, URL/path redaction, Unicode/control/bidi cases, size bounds, cache/log inspection, permission upgrade/revoke, incognito, no-network mode, and planner-output capability fuzzing.

**Exit:** a user can inspect what will leave the device; documentation and actual payload categories match; no LLM output grants authority.

### WP-10 — Orchestrator decomposition (`XL`, incremental across packages)

**Implementation status (2026-08-25): complete at the bounded-application
boundary.** `ApplicationKernel` is now the process composition root for an
instance-scoped service compatibility container, typed runtime-data and
runtime-health ports, synchronous typed event streams, and a named
`TaskSupervisor`. Sensing, inference/publication, intervention command
handling, experiment diagnostics/outcome collection, and operational
maintenance each have a coordinator with explicit lifecycle ownership.
Critical child exit requests fail-closed shutdown; partial coordinator startup
rolls back the started prefix; cancellation drains each named group. The
daemon no longer creates bare tasks or imports the process-global API registry.
The WebSocket gateway receives its ports through construction and binds one
immutable command bundle; individual callback setters and a registry fallback
remain only as compatibility facades for isolated legacy consumers.

The browser worker now delegates connection/replay, durable session state,
privacy-bounded context collection, intervention presentation, authorized
capability execution, focus sessions, telemetry, and activity persistence to
bounded modules. Popup and desktop dashboard formatting use pure view models.
Both desktop transport modes share the same message router and outbound event
stream instead of patching transport methods. During this extraction a real
goal-aware classifier defect was corrected: relevant video tabs now use the
canonical video/social/entertainment categories and are not incorrectly
treated as distractions.

Completion evidence: the full Python matrix passes (2,583 passed, 5 skipped),
the Qt-isolated desktop matrix passes (62/62), strict mypy passes across 514
source files, Ruff and generated-schema drift checks pass, all 250 browser
tests and TypeScript checks pass, and Chrome/Edge MV3 production builds
complete. Dedicated contracts cover instance isolation, immutable command
binding, event isolation/unsubscription, named task failure and drain,
idempotent coordinator start, partial-start rollback, sensing/inference
ownership, browser module boundaries, and shared desktop routing/view models.

**Files:** `runtime_daemon.py`, WebSocket server, desktop controller, browser background/popup, global registry; new `cortex/application/` coordinators

**Changes:**

1. Establish kernel/composition root without behavioral change.
2. Move sensing, inference, intervention, and experiment flows into coordinators in that order.
3. Replace callback setters with command handlers/event subscribers.
4. Replace service registry use with constructor-injected typed ports.
5. Collapse desktop behavioral duplication.
6. Split browser background into connection/auth, persisted session, context collector, presentation, capability executor, focus session, and telemetry modules.
7. Split popup/dashboard views from state/controller models.
8. Keep compatibility facades and remove them only after parity coverage.

**Tests:** characterization fixtures before each extraction, transport parity, structured cancellation, startup partial failure, repeated start/stop, camera release, queue shutdown, and import-boundary checks.

**Exit:** no single application orchestrator owns domain algorithms; coordinators have bounded public APIs; shutdown ownership is evident from the task tree.

### WP-11 — Validation, documentation, and reproducible release (`L`, continuous)

**Implementation status (2026-08-26): complete at the repository/software
boundary.** Credentialed signing, notarization, stapling, Gatekeeper, mounted
verification, and failure preservation have been exercised. The remaining
work for any public candidate is evidence that cannot truthfully be produced
from source alone: complete staged artifacts on both architectures, the
physical permission/device matrix, participant/reference-sensor data, and
independent review.

**Files:** CI/release workflows, `uv.lock`, toolchain pins, build scripts/spec,
release/dataset validators, repository-contract generators, tests, docs,
README/wiki pages, finding ledger, ADRs, model cards, and release templates

**Changes:**

1. Committed a universal 184-package `uv.lock`; constrained supported Python
   to 3.11/3.12 and pinned Python 3.11.15, Node 22.23.2, pnpm 9.15.9, and uv
   0.10.12 at repository and workflow boundaries. Required wheel environments
   include both macOS architectures; the Intel branch pins MediaPipe 0.10.21
   and its NumPy 1.x-compatible graph because later MediaPipe releases publish
   no macOS x86_64 wheel, while arm64/Linux retain the maintained line. The
   graph installs only the capped `opencv-contrib-python` provider required by
   MediaPipe; a repository contract rejects a second `opencv-python` wheel
   that could overwrite the same `cv2` package and bypass the major-version cap.
   MediaPipe's last Intel wheel also holds Protobuf below the upstream fixed
   major; its single unreachable-API advisory is architecture-scoped,
   evidence-bearing, expiry-gated, and backed by a real model/runtime smoke.
2. Added one canonical Python gate for Ruff, strict mypy, wheel inspection,
   the complete non-Qt suite, and isolated Qt suite. CI exercises identical
   arm64/3.11.15 and x86_64/3.12.13 matrices, exports `UV_PYTHON` so
   `.python-version` cannot mask the compatibility row, and asserts exact
   interpreter plus CPU architecture.
3. Made the browser lock reproducible under the pinned pnpm version by placing
   overrides in the pnpm-9-compatible package manifest location. Patchable
   transitive advisories are lifted; remaining dormant builder paths require
   narrow, expiring, package-chain-verified exceptions. VS Code uses a tracked
   lock, frozen install, real tests, and package gate. CI activates pnpm through
   exact Corepack selection rather than `pnpm/action-setup` v6, whose current
   version shim can execute a newer multi-document-lockfile implementation.
4. Added a schema-1.1 dataset manifest with path containment, checksums,
   participant-disjoint split enforcement, license/source/citation, explicit
   participant-data policy, reference sensor, clock alignment, condition
   reports, and declared aggregate/p95 error fields. A JSON Schema, safe
   example, validator, and replay tests make the contract executable without
   committing prohibited data.
5. Added a source/frozen release smoke and a mounted-DMG verifier that checks
   version/architecture identity, bundle plist, single-architecture Mach-O,
   nested signing, exact exported secrets in every byte stream, complete
   high-confidence credential forms in text-like members, actual non-generic
   build-home identifiers, frozen resources, frozen inference construction,
   full packaged-process health and bounded termination, the outer DMG's
   Developer ID signature, notarization/stapling, and Gatekeeper when requested. Secret
   scanning is streaming, fails closed on
   unreadable bundle members, rejects official provider fixtures and native
   parser-marker collisions, and catches patterns spanning read boundaries.
6. Hardened the release workflow around a protected environment, temporary
   Keychain, inside-out Developer ID signing of the hardened-runtime app and
   outer DMG, accepted Apple notary log, stapling, per-architecture
   names/checksums/evidence, application and Python
   SBOMs, GitHub attestations, clean-tag evidence hashing, and a two-architecture
   draft-staging guard. A separately protected promotion workflow refuses an
   already-public target, verifies both provenance attestations, and publishes
   only after machine-validating the complete physical-test record. All
   third-party actions are full-commit pinned. Scrubbed bundle configuration
   and developer environment backups remain outside the checkout so the
   pre-exit provenance check observes a genuinely clean tag rather than an
   ignore-list exception for build-created inputs.
7. Added a release-record JSON Schema and promotion validator whose `release`
   decision is impossible unless all 14 fixed cases passed on clean arm64 and
   Intel profiles, artifact/tag/commit/hash bindings agree, every dedicated
   architecture-specific manual-evidence asset exists and is non-empty, and no
   independent reviewer is also a builder. The committed template remains
   `blocked` by design; each candidate must create its own truthful records.
8. Generated a 203-setting configuration reference and fully commented safe
   env template directly from `CortexConfig`. Repository contracts check local
   Markdown links, canonical config keys/reachability, dead message types,
   generated schema/design/version/config surfaces, exact tool versions,
   Python matrices, and workflow action pins. Lychee checks external links.
9. Added the tracked 56-finding historical ledger, execution log, six ADRs,
   limitations, data flow, release guidance, and corrected architecture,
   setup, API, privacy, calibration, browser, and product-language docs.
10. Deleted unregistered/invalid product mechanisms and their dead controls:
    optional ML classifier, stress integral, production AMIP/bandit/causal
    report, obsolete message types, unused settings, and synthetic
    physiology-backed break semantics. The replacement guided break is driven
    by user choice and elapsed focus time.

**Verification:** Section 0.1 records the complete local matrix. The committed
regression harness also preserves exact baseline values for oscillation rate,
sustained-overwhelm pass rate, flow false-trigger rate, and deterministic
policy replay mismatch. Release JSON and dataset JSON examples are validated
against their published schemas in the Python gate.

**Exit:** achieved for source, dependency, CI, validation tooling, and release
automation. A particular release satisfies the full exit only after a clean
tag produces both credentialed architecture artifacts and independently
reviewed `release` records with attached installed-artifact, hardware,
notarization, SBOM, checksum, attestation, and claim evidence. Source completion
does not pre-approve a future artifact.

---

## 13. Verification and validation program

### 13.1 Verification layers

| Layer                   | Purpose                                                         | Required artifacts                                             |
| ----------------------- | --------------------------------------------------------------- | -------------------------------------------------------------- |
| unit/property           | equations, bounds, missingness, time, state transitions         | deterministic tests and seeded property failures               |
| schema/contract         | Python/browser/editor/native compatibility                      | generated types, golden fixtures, consumer-driven tests        |
| component replay        | recorded observation→signal or feature→estimate behavior        | versioned trace format, checksums, expected outputs            |
| process integration     | daemon/gateway/native/client lifecycle                          | subprocess tests with real framing/auth/reconnect              |
| fault injection         | cancellation, disconnect, disk, partial mutation, clock changes | scenario matrix and recovery assertions                        |
| dataset validation      | signal accuracy/generalization                                  | participant-disjoint reports and subgroup/condition breakdowns |
| hardware validation     | camera/TCC/reference sensors/install                            | device matrix and signed artifact test record                  |
| human-factor validation | support-state meaning, interruption efficacy/burden             | approved protocol, consent, analysis plan, results             |

### 13.2 Physiological reference protocol

Before user-visible physiological claims:

1. preregister supported cameras, frame rates, distance, lighting, movement, skin-tone measurement method, exclusions, and target population;
2. synchronously record webcam and a validated ECG/contact reference with clock-alignment checks;
3. include stationary, natural typing, head motion, lighting change, glasses/occlusion, and face-loss conditions;
4. split participants, not windows, across development and evaluation;
5. report coverage/abstention as well as error—an accurate estimate on 10% of users is not a complete product result;
6. report MAE, RMSE, bias, 95% limits of agreement, correlation, error distribution, and per-condition/subgroup results with uncertainty;
7. publish algorithm/config/dataset versions and all exclusions;
8. compare POS, CHROM, GREEN, and any neural backend under the same protocol.

**Provisional internal HR gate:** in declared supported stationary/natural-work conditions, at least 90% eligible-window coverage, pooled MAE ≤5 bpm, absolute bias ≤3 bpm, and 95th-percentile absolute error ≤10 bpm, with no predeclared subgroup/condition silently omitted. These are proposed product thresholds, not medical standards; they must be reviewed against the intended use and final dataset before becoming release criteria.

For HRV and respiration, do not borrow HR thresholds. Define metric-specific utility margins before data collection. External publication requires that the entire confidence interval for bias/agreement stays within that margin and that artifact/coverage limits are met. LF/HF and breath-pause features remain off until separately validated.

### 13.3 State/support validation

Build a consented dataset with:

- short momentary support-need/workload ratings at sampled times;
- periodic NASA-TLX after defined tasks;
- task performance/error/latency where meaningful;
- contextual factors such as task type and voluntary breaks;
- sensor readiness and missingness;
- intervention exposure separated from pre-trigger label windows.

Evaluation must include:

- leave-one-participant-out primary results;
- personalized adaptation evaluated only on future held-out episodes;
- base-rate, telemetry-only, physiology-only, and full-model ablations;
- discrimination, Brier score, calibration error/reliability curves, coverage-risk curves, and decision utility;
- false intervention rate during self-reported flow;
- subgroup and operating-condition uncertainty;
- sensitivity to missing modalities;
- comparison with “always quiet” and time-only reminder policies.

**Proposed promotion gate:** a model is promoted only when its participant-held-out confidence interval improves the prespecified decision utility over Level A and telemetry-only baselines, calibration is acceptable under the preregistered bound, and no safety/coverage subgroup crosses a predeclared harm margin. Do not choose those bounds after seeing the test set.

### 13.4 Intervention correctness matrix

Every action type must pass all applicable rows:

| Scenario                                    | Required result                                                         |
| ------------------------------------------- | ----------------------------------------------------------------------- |
| proposal only                               | zero mutating adapter calls                                             |
| exact user approval                         | only displayed manifest executes                                        |
| downgraded consent                          | original manifest does not execute                                      |
| expired/replayed authorization              | fail closed, no mutation                                                |
| settings/consent changed after presentation | authorization invalidated                                               |
| duplicate apply                             | one effect, `already_applied` receipt                                   |
| crash after durable intent/before apply     | recover as unapplied or safely retry                                    |
| crash after apply/before acknowledgement    | reconcile from idempotency/fingerprint                                  |
| client disconnect                           | retain retryable restore receipt; do not claim restored                 |
| partial apply                               | compensate successes or truthfully mark partial                         |
| user changes workspace after apply          | undo only Cortex-owned changes                                          |
| repeated restore                            | no additional effect                                                    |
| app shutdown/update                         | restore attempted before teardown; unresolved work recovered next start |

### 13.5 Security and privacy verification

- threat-model native host, localhost ports, extension compromise, malicious local process, prompt injection, log leakage, update/build chain, and database theft;
- fuzz every external decoder with size/depth/string limits;
- test token-file symlink/permission/ownership handling;
- rotate capability token safely and invalidate existing sessions;
- require authentication for any operational endpoint whose content is considered sensitive; reassess unauthenticated `/metrics`;
- inspect release bundles for API keys, user paths, development certificates, source maps, raw datasets, and unneeded model assets;
- run static secret scanning, dependency audit, SBOM verification, and signature/notarization checks;
- verify that “delete local data” removes all documented stores and that logs contain no workspace excerpts;
- test optional Chrome permission grant and revocation against real browser profiles.

### 13.6 Performance and degradation gates

Measure on every supported Mac class:

- capture FPS and jitter;
- end-to-end observation-to-state latency;
- CPU/GPU/energy and memory distributions over at least a workday trace;
- queue depths and dropped observations;
- LLM request latency/cost/cache hit rate without logging private prompt text;
- browser service-worker restart frequency and recovery;
- shutdown/restore/camera-release latency.

Proposed product budgets must be set from supported hardware. When overloaded, degrade in this order: pause optional model inference → reduce display/update frequency → reduce quality-safe capture processing → mark signals stale/unavailable. Never preserve a value by hiding missingness or skipping authorization/restore work.

### 13.7 CI gate layout

Recommended required jobs:

```text
python-lint-type-schema
python-unit-integration
desktop-tests
browser-install-type-test-build
vscode-install-compile-test-package
cross-language-contracts
recorded-trace-regression
dependency-vulnerability-policy
sbom-and-secret-scan
docs-links-config-version
macos-app-build-smoke           # release branches / scheduled
signed-dmg-install-e2e          # release candidate
```

CI should use the same lockfiles and major tool versions as the release builder. A job described as a test gate must actually execute tests; compile-only cannot substitute for the VS Code suite.

---

## 14. Migration sequence and dependency graph

```mermaid
flowchart LR
    P0["Phase 0: contain + auth/release fixes"] --> P1["Phase 1: clocks + schemas"]
    P1 --> P2["Phase 2: observations + capture"]
    P1 --> P6["Phase 3: transactional interventions"]
    P1 --> P7["Phase 3: event store"]
    P2 --> P3["Phase 4: physiology + kinematics/calibration"]
    P3 --> P5["Phase 5: support inference"]
    P6 --> P8["Phase 6: policy/evaluation"]
    P7 --> P8
    P5 --> P8
    P0 --> P9["Phase 2+: privacy/planner hardening"]
    P1 --> P10["Incremental orchestrator extraction"]
    P6 --> P10
    P11["Validation + release evidence"] --- P2
    P11 --- P3
    P11 --- P5
    P11 --- P6
    P11 --- P8
```

### Phase 0 — Make current behavior safe and buildable

Deliver WP-0 as small patches. Do not combine it with architectural extraction. Correct native auth, default to suggestion-only, prevent trigger-time mutation, make consent exact-deny for downgrades, repair VS Code CI/lock/dependency state, and unify versioning.

**Rollback:** revert individual compatibility patches; suggestion-only remains the safe fallback.

### Phase 1 — Establish contracts before algorithm changes

Deliver WP-1. Dual-write old and new timestamps/events for one compatibility interval, with clients preferring v2. Add golden fixtures before removing legacy fields.

**Rollback:** retain v1 encoding from the compatibility adapter; never reinterpret monotonic data as epoch.

### Phase 2 — Make missingness and privacy truthful

Deliver observation envelopes/gates and the privacy context broker in parallel where ownership permits. These changes establish valid inputs for every later algorithm and make LLM behavior accurately disclosed.

**Rollback:** feature flag v2 estimators, but retain explicit missingness and privacy reductions.

### Phase 3 — Make mutation transactional and persistence atomic

Deliver WP-6 and WP-7 behind `authorized` execution mode. Run shadow lifecycle recording while still suggestion-only. Enable one reversible action at a time after its fault matrix passes.

**Rollback:** switch to suggestion-only; retain receipts for restore and audit.

### Phase 4 — Replace physiological/kinematic algorithms in shadow mode

Run v1 and v2 from the same observations without using v2 to trigger actions. Compare readiness, HR, beats, HRV, respiration, and kinematics in privacy-safe aggregate traces. Do not store raw video unless in a separate consented validation study.

**Rollback:** hide v2 output; neither invalid v1 nor unvalidated v2 may drive physiology-triggered mutations.

### Phase 5 — Replace state semantics

Ship Level A evidence-aware scores and unknown state first. Update UI and docs. Collect consented labels for Level B; model work is a later promotion, not a prerequisite for honest deterministic behavior.

**Rollback:** deterministic suggestion rules remain; probability language stays disabled.

### Phase 6 — Rebuild policy evaluation

Start with deterministic policy and complete outcome logging. Add research randomization only under a study configuration and separate consent. Preserve old AMIP data as a legacy diagnostic export rather than migrating it into a misleading causal dataset.

**Rollback:** deterministic no-learning policy.

### Continuous — Extract orchestrators and validate

Move one cohesive flow at a time behind the kernel after characterization tests. Avoid a rewrite branch. Every phase produces updated docs, a finding-state entry, and evidence artifact.

---

## 15. Prioritized implementation backlog

### P0 — Must precede the next mutation-capable release

- [x] P0-01 canonical native auth schema and end-to-end host test
- [x] P0-02 suggestion-only default and kill switch
- [x] P0-03 pure proposal handlers in daemon/browser/VS Code
- [x] P0-04 exact consent outcome; downgrade cannot execute
- [x] P0-05 tracked VS Code lock, fixed Jest command, tests in CI, patched `ws`
- [x] P0-06 external clock contract migration scaffolding
- [x] P0-07 disable HRV/LF-HF/apnea/stress-trigger claims and actions
- [x] P0-08 version single source and current documentation correction

### P1 — Correctness and durable safety

- [x] P1-01 observation envelopes and missingness gates
- [x] P1-02 motion ordering and time-correct MediaPipe/kinematics
- [x] P1-03 unique beat/IBI stream
- [x] P1-04 respiration redesign or feature removal
- [x] P1-05 calibrated profile provenance and live reload
- [x] P1-06 evidence-normalized inference with unknown state
- [x] P1-07 manifest/authorization/receipt intervention protocol
- [x] P1-08 event store and restart recovery
- [x] P1-09 deterministic production policy and one finalized reward
- [x] P1-10 privacy context preview/redaction and optional browser permissions

### P2 — Architecture and research quality

- [x] P2-01 application kernel and typed internal commands/events
- [x] P2-02 coordinator extraction from runtime daemon
- [x] P2-03 gateway callback/registry removal
- [x] P2-04 browser background/popup split
- [x] P2-05 one desktop domain path
- [ ] P2-06 reference-sensor and participant-held-out validation program —
  manifest/schema/replay/report tooling complete; real reference data,
  participants, approvals, and analysis remain external
- [ ] P2-07 MRT/OPE research pipeline and independent statistical review —
  immutable software pipeline and diagnostics complete; independent review and
  any approved study execution remain external
- [x] P2-08 reproducible dependency/build/SBOM/provenance flow

### P3 — Documentation and cleanup

- [x] P3-01 tracked finding ledger and ADRs
- [x] P3-02 generated configuration/version/schema docs
- [x] P3-03 link checker and broken-link repair
- [x] P3-04 delete dead settings/backends/classifier paths
- [ ] P3-05 replace every historical audit comment/test label with current
  invariants — intentionally non-blocking: modified production boundaries use
  current language, while a repository-wide mechanical rename would add large
  blame churn without changing behavior or evidence

---

## 16. Risk register

| Risk                                            | Likelihood / impact | Mitigation                                                                                  | Leading indicator                          |
| ----------------------------------------------- | ------------------- | ------------------------------------------------------------------------------------------- | ------------------------------------------ |
| v2 signal lowers apparent availability          | high / medium       | treat abstention as correctness; report coverage; improve acquisition, not interpolation    | unavailable fraction by reason             |
| users perceive suggest-only as regression       | medium / medium     | explain safety posture; allow only verified reversible actions back incrementally           | opt-out and complaint rate                 |
| schema migration fragments clients              | medium / high       | negotiation, generated fixtures, one compatibility release                                  | rejected-message counts by client version  |
| receipt persistence leaks workspace state       | medium / high       | store minimal typed inverse IDs/state, never content where avoidable; retention             | payload classification audit               |
| SQLite packaging mismatch                       | medium / high       | startup version gate, single connection, rollback journal, build test                       | reported SQLite version/journal mode       |
| reference dataset does not match natural work   | high / high         | combine public benchmarks with consented natural-work reference study                       | condition-specific error/coverage gap      |
| subjective state labels are noisy               | high / medium       | repeated measures, clear construct, participant-held-out design, abstention                 | inter/intra-person reliability             |
| adaptive policy creates interruption burden     | medium / high       | deterministic default, daily caps, no-action control, stop rules                            | prompts/hour, dismissals, undo, quiet mode |
| LLM provider policy changes                     | medium / high       | runtime disclosure version, no-LLM mode, provider adapter, minimize data                    | disclosure/version drift CI                |
| modularization becomes a rewrite                | medium / high       | facade/strangler, characterization tests, one flow per PR                                   | diff size and parity failures              |
| build-chain advisory cannot be upgraded quickly | high / medium       | isolate build environment, pin hashes, inspect bundle reachability, record exception expiry | open exception age                         |
| camera/TCC behavior differs in signed DMG       | medium / high       | installed-artifact hardware matrix, preserve known launch lineage rules                     | permission/camera startup telemetry        |

---

## 17. Explicit non-goals

- Do not market Cortex as diagnosing stress, apnea, fatigue, ADHD, anxiety, or any medical/cognitive condition.
- Do not add cloud video processing or raw-frame storage to make validation easier.
- Do not introduce microservices, Kafka, Redis, or a distributed database for a single-user local application.
- Do not use an LLM to bypass deterministic action validation or consent.
- Do not train a large end-to-end state model before defining labels and collecting an adequate consented dataset.
- Do not treat synthetic calibration data, synthetic state traces, or intervention acceptance as ground truth.
- Do not retrofit legacy policy logs into causal evidence when eligibility, propensities, control outcomes, or reward finalization are missing.
- Do not reset global macOS camera permissions or weaken the proven camera release/Continuity Camera safeguards.
- Do not combine the safety fixes and orchestrator rewrite into one unreviewable change.

---

## 18. Definition of done

The release-relevant redesign is complete at the software boundary. `[x]`
means enforced by committed implementation/tests; `[ ] external` means the
repository supplies a protocol and evidence format but cannot perform the gate
without credentials, hardware, participant authority, or an independent party.

### Build and contracts

- [x] clean checkout installs with frozen dependency inputs;
- [x] Python, browser, VS Code, desktop, schema, docs, audit, and build gates pass;
- [x] one version is reported everywhere;
- [x] every external/native/desktop message is generated and contract-tested;
- [x] public time fields have explicit clock and unit semantics.

### Signals and state

- [x] every scheduled camera interval becomes a valid/missing/rejected observation;
- [x] no insufficient window yields a ready signal;
- [x] beats are unique, chronological, and provenance-linked;
- [ ] **external:** any future user-visible physiological accuracy claim meets
  its reference-sensor, participant-disjoint, condition, and subgroup gate;
- [x] unsupported HRV/respiration measures are unavailable, not approximated;
- [x] missing modalities can produce unknown, never false certainty;
- [x] state wording and probability claims match the deterministic support construct.

### User authority

- [x] proposal handlers are non-mutating;
- [x] every adapter apply has a prior exact authorization;
- [x] downgrade cannot execute the original manifest;
- [x] every applied effect has a durable verified receipt or a visible restore failure;
- [x] duplicate/reordered/replayed messages are harmless;
- [x] suggestion-only and emergency restore work without LLM/policy access.

### Policy and evidence

- [x] every eligible action and no-action decision has one outcome lifecycle;
- [x] one reward/version is finalized at most once;
- [x] production learning is disabled; fixed research policy state survives restart;
- [x] diagnostic and causal reports are named according to evidence;
- [x] any research causal export requires a prespecified estimand,
  propensities, control, missingness rules, overlap diagnostics, and uncertainty;
- [ ] **external:** an independent statistician reviews the study design and
  any efficacy result before publication or product use.

### Privacy and operations

- [x] the user can preview and control external context categories;
- [x] permissions match current need and are revocable;
- [x] ordinary stores/logs contain no raw frames or workspace excerpts;
- [x] export/delete/retention behavior is tested;
- [ ] **external:** each installed signed/notarized candidate passes camera,
  TCC, auth, browser/editor, stop, restore, update, and uninstall checks on the
  declared architecture/device matrix;
- [x] health surfaces readiness, degradation, algorithm identity, storage
  compatibility, and restore failures without exposing private content.

---

## 19. Research and platform basis

This plan uses research to constrain claims and study design, not to imply that a paper's result automatically transfers to Cortex.

| Area                          | Source                                                                                                                                                                                                                                                                                                                                        | How it changes this implementation                                                                                                                     |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| short-term HRV                | [Task Force standards](https://pubmed.ncbi.nlm.nih.gov/8737210/), [2024 measurement/reporting guidelines](https://pmc.ncbi.nlm.nih.gov/articles/PMC11539922/), and [minimal interval study](https://pubmed.ncbi.nlm.nih.gov/8578795/)                                                                                                         | stop deriving strong frequency/nonlinear claims from ultra-short noisy webcam intervals; document acquisition/derivation and validate metric by metric |
| rPPG algorithm/evaluation     | [POS derivation](https://pure.tue.nl/ws/portalfiles/portal/78340965/20171023_Wang.pdf), [rPPG-Toolbox](https://github.com/ubicomplab/rPPG-Toolbox), [bias/real-world evaluation](https://pmc.ncbi.nlm.nih.gov/articles/PMC8175478/), [motion-robust study](https://pmc.ncbi.nlm.nih.gov/articles/PMC5995145/)                                 | measured-time, quality/motion gating, cross-dataset testing, Bland–Altman, condition/subgroup reporting, abstention                                    |
| workload ground truth         | [NASA-TLX official resource](https://www.nasa.gov/human-systems-integration-division/nasa-task-load-index-tlx/)                                                                                                                                                                                                                               | define and collect a multidimensional subjective construct instead of self-validating heuristic labels                                                 |
| just-in-time interventions    | [MRT design](https://pmc.ncbi.nlm.nih.gov/articles/PMC4732571/) and [HeartSteps MRT](https://pmc.ncbi.nlm.nih.gov/articles/PMC6401341/)                                                                                                                                                                                                       | log eligible decision points, randomize control in research mode, define proximal outcomes, use longitudinal analysis                                  |
| off-policy evaluation         | [Optimal and Adaptive OPE](https://proceedings.mlr.press/v70/wang17a.html)                                                                                                                                                                                                                                                                    | require target policy, propensities, overlap, variance diagnostics, and estimator sensitivity rather than ad hoc IPS labels                            |
| clocks/concurrency            | [Python clock semantics](https://docs.python.org/3.11/library/time.html) and [TaskGroup](https://docs.python.org/3.11/library/asyncio-task.html#task-groups)                                                                                                                                                                                  | separate wall/monotonic time and give background tasks explicit structured ownership                                                                   |
| face tracking                 | [MediaPipe Face Landmarker guide](https://developers.google.com/edge/mediapipe/solutions/vision/face_landmarker/python)                                                                                                                                                                                                                       | provide real frame timestamps in video/live-stream processing and treat skipped frames explicitly                                                      |
| local durability              | [SQLite transactions](https://sqlite.org/lang_transaction.html) and [WAL guidance/current bug notice](https://sqlite.org/wal.html)                                                                                                                                                                                                            | single-writer transactional lifecycle; gate SQLite version/concurrency; do not enable unsafe WAL assumptions                                           |
| browser lifecycle/permissions | [MV3 service-worker lifecycle](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle), [extension privacy guidance](https://developer.chrome.com/docs/extensions/develop/security-privacy/user-privacy), [native messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging) | persist authoritative worker state, minimize/optionally request host access, and contract-test native framing                                          |
| AI/privacy risk               | [NIST AI RMF](https://www.nist.gov/itl/ai-risk-management-framework), [NIST GenAI Profile](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf), [Anthropic retention](https://privacy.anthropic.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data)                                                                     | govern context categories, disclose provider handling, test injection/redaction, retain deterministic authority                                        |
| secure delivery               | [NIST SSDF](https://csrc.nist.gov/pubs/sp/800/218/final), [Apple macOS distribution packaging](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution), and [Apple notarization guidance](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)                                                                                   | locked inputs, vulnerability policy, SBOM/provenance, secret and release-artifact verification; sign nested macOS code/containers inside-out and notarize the outer DMG                                            |

---

## 20. Recommended first pull-request sequence

These boundaries keep urgent fixes reviewable and avoid mixing behavior repair with architectural movement:

1. **PR 1 — Native contract:** canonical `auth_token` schema, generated TS, actual-host framing fixture.
2. **PR 2 — Safe presentation:** suggestion-only default; browser/VS Code trigger handlers render without mutation; regression spies at every adapter.
3. **PR 3 — Consent exactness:** permit/downgrade/deny result and exhaustive matrix; downgrade has no executor path.
4. **PR 4 — Release integrity:** VS Code lock/Jest/CI/`ws`, Python audit job, version single source.
5. **PR 5 — Clock port:** fake/system clocks and cost/timestamp test repair; no wire change yet.
6. **PR 6 — Protocol v2 time envelope:** generated dual-clock fields and compatibility decoder.
7. **PR 7 — Observation envelope:** explicit face-loss/missing frames and coverage diagnostics.
8. **PR 8 — Motion/time correctness:** landmark commit ordering and measured-time kinematics.
9. **PR 9 — Beat-stream v2:** unique chronological beats in shadow mode, with overlap properties.
10. **PR 10 — Intervention lifecycle schemas/store:** shadow-record proposals, authorizations, and receipts while mutation remains off.
11. **PR 11 onward — one capability at a time:** overlay, tab visibility, editor folds, focus actions; each enabled only after its authorization/receipt fault matrix passes.

At the end of every PR, update the tracked finding ledger with one of `open`, `contained`, `implemented`, or `validated`. “Implemented” is not “validated”; signal accuracy, human-state meaning, and causal benefit stay open until their respective evidence gates pass.

---

## 21. v0.3.6 packaged-startup incident closure

### 21.1 Observed failure and exact root cause

The signed and notarized v0.3.5 arm64 application was launched from the mounted
release DMG. macOS created the process normally—the Dock icon bounced—then the
process exited before the Qt event loop could display a window. Unified logging
captured an unhandled `FileNotFoundError` originating from
`deterministic_support_identity()` during `CortexDaemon` construction:

```text
CortexAppController.run
  -> CortexAppController._start_daemon
  -> CortexDaemon.__init__
  -> SupportModelRegistry.__init__
  -> deterministic_support_identity
  -> Path(rule_scorer.py).read_bytes
  -> FileNotFoundError
```

This was an application defect, not a Gatekeeper, notarization, architecture,
camera-permission, or code-signing failure. The executable had already passed
Apple assessment and begun executing Python code. The missing path was inside
`Cortex.app/Contents/Frameworks/cortex/services/state_engine/`, where PyInstaller
represented an importable archived module through `__file__` but did not ship
the original `.py` file as data.

PyInstaller's runtime documentation distinguishes importable bundled modules
from separately collected data files and requires data to be added explicitly
when code intends to open it as a file: [PyInstaller run-time information](https://pyinstaller.org/en/stable/runtime-information.html),
[PyInstaller spec-file data guidance](https://pyinstaller.org/en/stable/spec-files.html#adding-data-files).
The v0.3.5 registry violated that distinction.

### 21.2 Why the previous release proof produced a false pass

The v0.3.5 `--release-smoke` checked static files and directories only:
configuration, SQL migrations, MediaPipe assets, audio, native-host scripts,
browser bundles, and the VSIX. It never constructed `SupportModelRegistry`,
`RuleScorer`, `SupportInferenceEngine`, `CortexDaemon`, or the desktop
controller. The exact failing method was therefore unreachable from the test.

The evidence accurately proved the claims it encoded—resource presence,
signature integrity, notarization, Gatekeeper acceptance, architecture, and
secret scanning—but the release decision incorrectly treated those claims as
equivalent to application startup. v0.3.6 separates the following propositions:

| Proposition | Required proof |
| --- | --- |
| Required data is present | frozen resource inspection |
| Frozen inference imports and provenance work | registry/scorer/inference construction inside the executable |
| The production composition root starts | complete packaged-process launch |
| The runtime is operational | version-matched `/health` with the registry registered `up` |
| Shutdown is bounded | process termination probe and no surviving listeners |
| A person can install and see the app | quarantined download + Finder replace/install + normal GUI launch |

No single row may stand in for another.

### 21.3 Identity-design options considered

#### Ship Python source as a data file

Adding `rule_scorer.py` to `datas` would make the old path exist, but it would
retain an undocumented runtime dependency on source layout, duplicate code
already stored in the PyInstaller archive, and allow packaging configuration to
change model identity availability. It fixes the symptom rather than the
provenance boundary and was rejected.

#### Hash the loaded bytecode or inspect the module loader

Loader/bytecode hashing couples an externally visible model identity to Python
minor version, optimization mode, marshal format, PyInstaller archive behavior,
and build toolchain details. Two semantically identical implementations could
receive different identities, while introspection failure would still occur at
runtime. It was rejected.

#### Fall back to a fixed hash only when `sys.frozen`

A frozen-only fallback creates two provenance semantics for the same release.
It can also conceal stale metadata, precisely where the release requires the
strongest evidence. It was rejected.

#### Generate a canonical implementation manifest before packaging

The selected design hashes a sorted, relative-path manifest of the reviewed
feature schema, model registry, deterministic scorer, and inference boundary.
The generator commits an ordinary Python module containing each component hash
and the aggregate SHA-256. Runtime code imports the constant; it performs no
filesystem access. Source and frozen executions therefore expose the same
identity.

The digest is a reproducibility identifier, not a claim of statistical
validation, correctness, secrecy, or artifact authenticity. Artifact
authenticity remains the job of code signatures, checksums, attestations, and
the immutable release tag.

### 21.4 Generated provenance contract

The implementation is owned by:

| File | Responsibility |
| --- | --- |
| `cortex/scripts/generate_support_model_identity.py` | canonical relative-path manifest, per-file hashes, aggregate hash, apply/check CLI |
| `cortex/services/state_engine/generated_model_identity.py` | committed frozen-safe constants; never hand edited |
| `cortex/services/state_engine/model_registry.py` | imports the generated digest and exposes model identity `2.1.1` |
| `cortex/scripts/verify_repository_contracts.py` | rejects generated drift in CI/release repository gates |
| `.pre-commit-config.yaml` | runs the drift check whenever an input, generator, or output changes |

Invariant:

```text
changed reviewed component
  => generated source differs
  => pre-commit/repository contract fails
  => contributor regenerates and reviews the new identity
  => source smoke and frozen smoke assert the same aggregate digest
```

The scoring weights, transforms, minimum evidence counts, coverage floor,
warm-up rule, abstention semantics, and fail-closed safety-null rollback are
unchanged. `2.1.1` records the repaired identity contract; it does not relabel
heuristic scores as probabilities or validated cognitive states.

### 21.5 Crash-visible desktop bootstrap

The PyInstaller executable now starts at
`cortex/apps/desktop_shell/bootstrap.py`, a standard-library-only boundary that
runs before Qt and the runtime graph are imported. It:

1. creates `~/Library/Logs/Cortex` with current-user-only permissions where the
   filesystem supports POSIX modes;
2. installs a 2 MiB rotating startup log with three backups;
3. records version, frozen/source mode, architecture, executable, and named
   startup stages without recording environment values or API keys;
4. captures top-level and unhandled-thread tracebacks;
5. writes the latest complete failure atomically to
   `last-startup-error.txt` with a short diagnostic reference;
6. shows a Qt fatal dialog explaining that sensing/workspace changes did not
   start, where the diagnostic is stored, and that mounted-DMG users should
   install the app in Applications.

The alert and AppKit-only window decoration/appearance hooks are deliberately
disabled only for the explicit headless release probe. Qt's offscreen platform
has no WindowServer-backed Cocoa application, so invoking AppKit decoration
there aborts in CoreGraphics and tests the harness rather than Cortex. The
release verifier instead captures stdout, stderr, the rotating log, and the
last-error file in its failure report; the Finder gate exercises the complete
AppKit path.

Controller milestones distinguish failures in storage-directory setup, Qt
construction, desktop-surface construction, daemon composition, daemon-thread
start, and event-loop readiness. A future report can therefore identify the
last completed boundary even when no traceback is available.

### 21.6 Layered startup verification

#### Layer A — source and frozen contract smoke

`release_smoke.py` still checks every required resource, then constructs:

- `SupportModelRegistry`;
- `RuleScorer`;
- `SupportInferenceEngine`;
- the active registered identity and its generated aggregate digest.

This phase has no camera, input hook, database, socket, or user-storage side
effects. It is the fast, deterministic regression for the v0.3.5 defect.

#### Layer B — complete packaged liveness probe

`verify_macos_release.py` mounts the exact DMG and executes its Cortex binary
with an isolated temporary HOME and storage root. It allocates loopback ports,
sets the explicit headless hardware boundary (plus an invalid camera index as
defense in depth), and requires the application to publish honest stale-camera
state without acquiring camera or input-monitoring authority. Within a bounded
window it must publish:

- HTTP health status `healthy`;
- exactly the candidate application version;
- `support_model_registry: up` in the production service registry.

The verifier then sends SIGTERM, waits up to 30 seconds for cleanup, and fails
if forced termination is required, the return code is non-zero, either port is
still listening, the daemon-start/daemon-stop markers are absent, a fatal
marker appears, a Qt stylesheet cannot be parsed, a requested font family is
missing, the deprecated Continuity Camera compatibility path is used, or a
last-startup-error file exists. The application establishes the native system
font at its root, the sensitivity-slider rules are syntax-checked, and the
bundle explicitly declares Continuity Camera device typing while automatic
selection continues to exclude phone/tablet cameras. The intended Cormorant
Garamond Roman/italic variable faces are vendored from a pinned Google Fonts
commit with their OFL license, registered before surface construction, and
treated as required resources; Menlo is the deterministic desktop mono face
because SF Mono is not guaranteed on a clean Mac. Captured evidence includes
the health payload, assigned ports, return code, process output, startup log,
and any fatal diagnostic. This runs independently on native Apple Silicon and
Intel release runners.

#### Layer C — installed, quarantined GUI exercise

Automation cannot infer human-visible success from `/health`, so the promotion
gate includes a real macOS workflow against the exact candidate digest:

1. download the architecture-correct DMG through GitHub;
2. verify its release checksum and GitHub attestation;
3. open the downloaded quarantined DMG in Finder;
4. replace any existing `/Applications/Cortex.app` via Finder and retain the
   old copy recoverably until the test passes;
5. eject the DMG so `/Volumes/Cortex/Cortex.app` cannot satisfy the launch;
6. open the installed app through Finder/LaunchServices;
7. confirm the Dock process persists and a Cortex window/onboarding surface is
   visible;
8. confirm Finder/Info.plist reports v0.3.6 and Developer ID signing,
   notarization ticket, and Gatekeeper assessment pass;
9. confirm `/health` and WebSocket listeners belong to the installed app;
10. exercise stop/quit and prove ports 9471–9473, camera ownership, and named
    daemon processes are gone;
11. relaunch once to detect first-run-only and stale-state failures.

New camera, accessibility, input-monitoring, or automation permission grants
remain user decisions. A release test may deny or leave them ungranted and must
verify honest degraded behavior; it cannot silently approve privacy-sensitive
permissions merely to turn a gate green.

### 21.7 Release immutability and quarantine policy

v0.3.5 remains an unchanged draft with its signed artifacts and evidence. It is
an incident artifact, not a distributable. No asset, tag, or checksum is
overwritten. The repair is released only under the later v0.3.6 tag.

Publication is fail-closed:

```text
source gates
  -> PR CI on both Python/macOS architectures
  -> merge commit CI
  -> annotated immutable v0.3.6 tag
  -> signed/notarized/stapled dual-architecture draft artifacts
  -> automated mounted resource + composition + liveness verification
  -> exact-digest Finder installation/open/stop/relaunch evidence
  -> public GitHub release
```

If any post-publication check fails, the release returns to draft immediately;
the assets remain preserved, and the next correction receives a higher version.

### 21.8 Residual risks and boundaries

- A headless CI runner proves process composition and degraded runtime health,
  not camera quality, TCC wording, or visual layout. Those require installed
  hardware/UI evidence.
- An arm64 machine cannot natively execute the x86_64 build without Rosetta;
  the Intel release runner therefore owns native packaged liveness for that
  artifact, while the local Finder exercise owns the host architecture.
- A visible window does not validate physiological accuracy. Reference-sensor
  and participant-held-out validation gates in sections 13 and 18 remain open.
- Startup diagnostics are local operational data. They are permission-limited,
  bounded, contain no raw camera frames by design, and must still be reviewed
  before a user attaches them to a public issue.
- Running directly from a mounted DMG is tested for crash visibility, but the
  supported installation path remains `/Applications/Cortex.app` because
  native-messaging manifests require a canonical non-translocated location.

---

## 22. v0.3.7 hardware-independent readiness and permission lifecycle

### 22.1 Release-gate observation

The v0.3.6 candidate passed its headless packaged liveness probe, signing,
notarization, stapling, Gatekeeper, checksum, and provenance gates. The required
real Finder exercise nevertheless exposed a second first-launch defect:

1. the downloaded arm64 DMG was opened through Finder;
2. `Cortex.app` was copied to `/Applications` and the DMG was ejected;
3. the installed Developer-ID-signed v0.3.6 app launched and its dashboard
   remained visible, proving the v0.3.5 frozen-source crash was fixed;
4. the dashboard immediately displayed `Connected`;
5. PID inspection showed the executable was the installed
   `/Applications/Cortex.app/Contents/MacOS/Cortex`;
6. neither port 9472 nor 9473 was listening and `/health` was unreachable;
7. the startup log stopped at `Requesting camera permission from macOS...`;
8. exactly 60 seconds later the request timed out, camera opening failed, both
   transports started, and `/health` became version-matched and healthy.

The process did not crash, but the product violated two release-critical
contracts: visible `Connected` state did not mean operational readiness, and an
optional privacy-sensitive hardware decision gated the control plane and quit
path.

### 22.2 Exact causal chain

The blocking sequence was:

```text
CortexAppController._start_daemon
  -> start Python daemon thread
  -> immediately emit connection_changed(True)
  -> CortexDaemon.start
  -> CapturePipeline.start
  -> FaceTracker.initialize
  -> WebcamCapture.start
  -> open_video_capture
  -> _request_macos_camera_permission
  -> requestAccessForMediaType_completionHandler_
  -> threading.Event.wait(timeout=60)
  -> only then start WebSocket and HTTP
```

Four independent defects compounded:

- **Thread existence was treated as readiness.** The controller emitted
  `Connected` immediately after `thread.start()`.
- **Optional hardware preceded the control plane.** WebSocket and HTTP were
  below capture in the serial startup sequence.
- **A user-time interaction was implemented as a synchronous wait.** A hidden,
  ignored, denied, or callback-starved TCC prompt held the daemon event loop.
- **Expensive work preceded capability proof.** MediaPipe and the initial font
  cache were loaded before confirming that camera authority existed.

The headless release probe intentionally sets `CORTEX_HEADLESS_STARTUP=1`, so
it correctly proved the no-hardware path but could not expose ordering inside
the real TCC branch. This is why both automated and installed-GUI gates are
required.

### 22.3 Design alternatives considered

#### Shorten the 60-second wait

A five- or ten-second synchronous timeout would reduce the symptom but retain
false connection state, an unresponsive quit path, and a race against how long
a person needs to read the permission rationale. It was rejected.

#### Start HTTP first, then block the same event loop on capture

Scheduling a server task before synchronous camera work does not make it
operational: uvicorn and WebSocket callbacks still need the blocked asyncio
loop. It would create a listener-shaped readiness illusion rather than a
responsive control plane. It was rejected.

#### Automatically bypass or grant camera permission

Cortex cannot and must not bypass macOS TCC. Silently approving a camera grant
would also violate the product's explicit-consent boundary and the release
test's privacy rules. It was rejected.

#### Remove camera startup entirely

Deferring every camera attempt until calibration would make existing users who
already granted access lose expected sensing on launch. It was rejected.

#### Selected design: independent core and optional-hardware lifecycles

Durable/core services establish a real readiness boundary. Camera acquisition
is a separately supervised, cancellable task. Runtime code only observes
current TCC state; onboarding and Settings own the non-blocking request from an
explicit user action. A newly observed grant retries capture without a
relaunch.

### 22.4 Lifecycle state model

Core and capture have deliberately separate state machines:

```text
desktop:  Starting… -> Connected -> Disconnected
                         ^
                         |
core:     composing -> transports bound -> ready -> stopping -> stopped

capture:  pending -> opening -> ready
              |         |         |
              +---------+-------> stale/unavailable
                                    |
                         explicit live grant/retry
                                    |
                                    +-> opening -> ready
```

`Connected` means the durable core graph is initialized, WebSocket has bound,
uvicorn has reported its HTTP server started, recovery has run, and runtime
coordinators are owned by the supervisor. It does **not** claim that every
optional sensor is available. Camera unavailability is represented separately
as stale capture and insufficient evidence.

### 22.5 Runtime implementation

`CortexDaemon` now exposes a read-only `is_ready` lifecycle flag. Startup:

1. configures logging;
2. opens and verifies SQLite authority and migrations;
3. provisions the local capability token;
4. registers the application service graph;
5. binds WebSocket;
6. recovers unfinished intervention transactions;
7. starts uvicorn and waits up to a bounded five seconds for its `started`
   signal, failing closed on task exit, cancellation, shutdown, or timeout;
8. starts input/window telemetry and spawns `cortex-capture-startup` as a
   non-critical supervised background task;
9. starts coordinators and the scheduler;
10. sets `is_ready=True`, logs `startup.ready name=daemon_core`, and waits for
    shutdown.

Capture success clears both the compatibility service-registry marker and the
typed runtime-status stale flag. Capture failure registers stale state,
broadcasts an explicit insufficient-evidence update, and leaves the rest of the
daemon operational. Cancellation propagates; it is not misreported as camera
failure.

Shutdown clears readiness before teardown, cancels and drains background tasks,
and still executes the existing unconditional `CapturePipeline.stop()` safety
barrier. Multiple stop callers continue to coalesce on the one supervised
lifecycle task.

The development wrapper now treats shutdown as a join over both possible
signal owners. `DevServer` waits for either its private shutdown event or the
daemon task to finish, then always enters its `finally: stop()` barrier. This
closes a bug found only during the hardware-backed source exercise: both the
wrapper and daemon registered process signal handlers on the same loop, so the
last registration could receive Ctrl+C, let the daemon task return, and leave
the wrapper waiting forever on an unrelated event. The daemon also marks the
uvicorn task as expected at signal receipt, before uvicorn can return, so a
normal lifecycle transition cannot race into a false critical-task report.

### 22.6 Camera and MediaPipe boundaries

The runtime camera helper was renamed from an imperative request to an
observational authorization check. Its semantics are:

| macOS authorization state | Runtime behavior |
| --- | --- |
| authorized | continue to deterministic camera selection |
| not determined | return unavailable immediately; onboarding/Settings owns the request |
| denied or restricted | return unavailable immediately and log the status |
| AVFoundation query unavailable | retain the prior best-effort OpenCV capability check |

`open_video_capture()` accepts a cooperative cancellation event. It checks the
event before each candidate, after each native open, between every warm-up read,
and immediately before returning a live handle. Any cancellation after a
partial open releases that local handle.

`WebcamCapture.start()` runs enumeration, native open, and warm-up in a worker
thread rather than on the daemon loop. Cancellation sets the shared event and
propagates. `stop()` sets the same event before clearing the frame-reader flag
and retains the unconditional `cap.release()` invariant.

`CapturePipeline.start()` now opens the camera before initializing MediaPipe.
When permission is absent, the product therefore avoids model loading and
first-account font-cache construction entirely. When authority exists,
MediaPipe creation, synchronous inference, and close all run off-loop on one
pipeline-owned worker. Keeping those operations on the same worker is the
portable contract across Cortex's two dependency graphs: Intel uses the older
0.10.21 `TaskRunner`, while current Apple Silicon wheels wrap C calls in a
thread-safe serial dispatcher. It also prevents per-frame native inference
from starving HTTP, WebSocket, or quit callbacks. Failure or cancellation
stops the opened webcam and releases the face tracker on its owning worker
before propagating.

No frame persistence, camera-selection priority, Continuity Camera exclusion,
post-open live-name verification, four-read MacBook warm-up policy, signal
algorithm, or intervention-authority rule changes in v0.3.7.

### 22.7 Explicit permission and live retry

The desktop permission surfaces remain the only prompt owners:

- onboarding's **Grant Access** button invokes the non-blocking AVFoundation
  request when TCC is not determined and opens System Settings only for a
  denied/restricted recovery;
- Settings' camera permission link invokes that same request from the user's
  click when TCC is not determined and deliberately keeps Cortex foregrounded
  so the native prompt is visible; already denied/restricted state instead
  opens the Privacy & Security recovery pane;
- both surfaces poll TCC every 1.5 seconds only while relevant/visible;
- only a `False -> True` transition emits `camera_permission_granted`;
- the controller converts that signal into
  `apply_settings({"webcam_enabled": True})` on the daemon loop;
- a successful retry clears stale state; a failed retry re-broadcasts stale
  state and never claims biometric availability.

Repeated permission polls cannot produce repeated camera opens because the
transition signal is edge-triggered and `CapturePipeline.start()` is serialized
by an asyncio lock.

### 22.8 Interaction refinement

| Before | After | Why |
| --- | --- | --- |
| Dashboard painted `Connected` as soon as the daemon thread existed. | Dashboard and tray show `Starting…` until core readiness is true. | Status copy must describe a user-observable capability, not an implementation detail. |
| A hidden/ignored system prompt froze backend readiness for 60 seconds. | Permission requests happen only from explicit onboarding/Settings actions; unresolved state degrades immediately. | Keeps interaction interruptible and preserves user agency over privacy-sensitive access. |
| Granting camera permission could require a relaunch. | A live grant edge retries capture in-process exactly once. | Immediate feedback closes the action/result loop and avoids making the user infer recovery steps. |
| Camera failure looked like an empty, possibly broken app during startup. | Core connects independently and stale capture is represented as insufficient evidence. | Separates service health from sensor availability and prevents misleading biometrics. |

The visual treatment intentionally reuses existing semantic tokens. No new
palette, ornamental animation, or modal interruption was introduced. The
highest-value polish here is temporal truth: state changes occur at the moment
their underlying capability becomes real.

### 22.9 Regression and release proof

New deterministic tests prove:

- a not-determined TCC state returns in under 250 ms and never invokes the
  native request method from runtime startup;
- unresolved authority skips candidate enumeration and `cv2.VideoCapture`;
- a pending camera-open worker can be cancelled without blocking the event
  loop;
- MediaPipe initialize, frame processing, and release execute on one worker
  that is distinct from the daemon event-loop thread;
- core readiness becomes true while capture is intentionally held pending;
- the desktop emits no connected signal before `daemon.is_ready`;
- Settings/onboarding emit one live-grant retry signal across repeated polls;
- the Settings camera link invokes the non-blocking request from the user's
  action;
- existing camera warm-up, index-reorder, post-open Continuity Camera,
  capture-release, and desktop empty-state tests remain green;
- the development wrapper progresses to its complete stop barrier whether the
  wrapper event or daemon task observes the process signal;
- signal-driven uvicorn completion is classified as lifecycle-owned before the
  daemon shutdown event is set.

The hardware-backed source runtime was then exercised twice consecutively on
the release host with authorized built-in camera access. In each cycle the
HTTP and WebSocket transports bound before optional capture, `/health` reported
healthy version `0.3.7`, the MacBook camera opened, MediaPipe initialized, and
one Ctrl+C completed graceful teardown in approximately one to two seconds.
The logs confirmed `WebcamCapture stopped`, `FaceTracker released`,
`CapturePipeline stopped`, WebSocket close, and `All services stopped`; follow-
up process and listener checks found neither runtime process nor ports 9471,
9472, or 9473. The second cycle reused the same durable store, covering clean
restart after a complete stop rather than only a fresh-profile launch.

Release promotion remains fail-closed. v0.3.6 is preserved as an unpublished
incident candidate. v0.3.7 must complete:

```text
clean source gates
  -> dual-architecture CI
  -> immutable annotated v0.3.7 tag
  -> signed + notarized + stapled arm64 and x86_64 DMGs
  -> mounted frozen composition and liveness on each native runner
  -> checksum + attestation verification
  -> downloaded candidate Finder install to /Applications
  -> eject DMG
  -> visible installed launch + version-matched healthy core
  -> honest camera-ready or camera-stale state (no silent permission grant)
  -> normal quit + ports/process/camera cleanup
  -> relaunch + repeated cleanup
  -> publish GitHub release
  -> unauthenticated public redownload and repeat host-architecture install/open/quit
```

### 22.10 Residual boundaries

- A no-permission release run proves honest degraded behavior, not biometric
  signal quality. Camera-quality claims still require an explicit grant and the
  reference-sensor validation program defined earlier in this document.
- Worker-thread cancellation is cooperative around vendor native calls. A
  single native `VideoCapture` constructor cannot be pre-empted safely; the
  worker checks cancellation immediately after it returns, releases the handle,
  and process exit remains the final kernel-level reclamation boundary.
- `Connected` is core readiness, not an assertion that browser/editor
  extensions, camera, Accessibility, an LLM provider, or calibration are
  configured. Each remains separately visible and fail-closed.
- The local GUI exercise owns visible behavior on the host architecture. The
  native Intel runner owns executable liveness for x86_64; Rosetta is not
  treated as equivalent evidence.

---

## 23. v0.3.8 transactional release teardown and immutable succession

### 23.1 Why v0.3.8 exists

The annotated `v0.3.7` tag points immutably at merge commit `4e8686b`. Its
locked pre-release gate passed, and both native macOS builders began from that
exact source tree. The two builders exposed independent terminal-lifecycle
defects after their artifacts had already crossed the Apple trust boundary.

The arm64 builder completed the product-critical work:

1. built the native arm64 application and DMG;
2. applied the Developer ID signature;
3. submitted the artifact to Apple and received an `Accepted` result;
4. stapled the notarization ticket and validated it;
5. mounted the DMG read-only;
6. verified the application bundle and launched the frozen executable for the
   packaged smoke/health probe;
7. attempted to detach the verification volume.

The job failed only while Python was leaving the verifier's temporary-directory
context:

```text
release verification FAILED:
[Errno 30] Read-only file system: 'CodeResources'
```

The error was emitted after the signed/notarized app checks, but before release
evidence, attestations, and draft assets could be assembled.

The native Intel builder independently completed its x86_64 build, Developer
ID signing, `hdiutil` integrity check, Apple notarization (`Accepted`), and
stapling. Its mounted frozen app then:

1. constructed the full Qt and daemon graph;
2. bound HTTP and WebSocket on isolated random ports;
3. returned a healthy `0.3.7` payload with the support-model registry up;
4. received the probe's SIGTERM;
5. stopped uvicorn, capture, WebSocket, storage, and the daemon;
6. requested Qt exit;
7. stranded two newly submitted `CortexDaemon.stop()` wrapper tasks while the
   already-stopped daemon thread closed its asyncio loop.

The verifier correctly rejected the otherwise zero-exit process because its
diagnostics contained both:

```text
RuntimeWarning: coroutine 'CortexDaemon.stop' was never awaited
Task was destroyed but it is pending!
```

The arm64 result proved a verifier resource-ownership bug. The Intel result
proved a product shutdown-coordination bug that faster timing had not exposed.
Neither is a signing or notarization failure, and neither may be waived simply
because the application reached health.

Therefore v0.3.7 is a failed release candidate, not a public release. Its tag
and Actions run are preserved as the incident record. Repointing or reusing the
tag would make the same version name describe two source trees and would
invalidate the provenance model. Both corrections advance every synchronized
product surface to `0.3.8` and require a fresh dual-architecture build.

### 23.2 arm64 DMG-cleanup causal chain

The previous verifier delegated two incompatible resources to one context:

```text
TemporaryDirectory.__enter__ creates writable host directory
  -> hdiutil attach -readonly -mountpoint <directory> Cortex.dmg
  -> verifier reads and launches Cortex.app from mounted filesystem
  -> hdiutil detach <directory> is invoked with check=False
  -> transient detach failure is ignored
  -> TemporaryDirectory.__exit__ recursively deletes <directory>
  -> recursion has crossed into the still-mounted, read-only DMG
  -> unlink/chmod reaches Cortex.app/.../_CodeSignature/CodeResources
  -> EROFS replaces the useful detach diagnostic
```

`TemporaryDirectory` owns a normal directory tree and fulfills its contract by
recursively removing that tree. Once a filesystem is mounted on the directory,
however, the path names a kernel-managed volume whose contents the temporary
directory neither created nor owns. Recursive cleanup is therefore the wrong
abstraction even if detach usually succeeds. The latent defect became visible
when deep signature traversal and packaged launch produced a transiently busy
volume on the arm64 runner.

There were two correctness failures:

- the return value of an ownership-changing operation (`hdiutil detach`) was
  recorded but not enforced;
- cleanup inferred detachment from control flow rather than checking the
  kernel-visible mount state.

The resulting `EROFS` was secondary damage. Treating it as a code-signing or
notarization failure would have sent remediation toward the wrong subsystem.

### 23.3 Intel terminal-shutdown causal chain

The in-process desktop has two threads and two event loops with different
ownership:

- the macOS main thread owns `QApplication` and all windows;
- the `cortex-daemon` thread owns an asyncio loop and `CortexDaemon.run()`.

A user/headless quit begins on the Qt thread and submits `daemon.stop()` with
`asyncio.run_coroutine_threadsafe()`. The daemon itself also coalesces every
caller onto one `_stop_task`, so its services were stopped exactly once. The
remaining race lived one layer above that coalescing boundary:

```text
SIGTERM on Qt main thread
  -> _on_user_initiated_quit (headless branch)
  -> _on_daemon_stop_requested
  -> submit wrapper coroutine A: await daemon.stop()
  -> daemon._stop_once completes
  -> wrapper A done-callback queues DaemonBridge.daemon_stopped
  -> Qt _on_daemon_stopped clears _stopping and requests GUI quit
  -> _quit closes DashboardWindow
  -> synchronous lastWindowClosed re-enters _on_user_initiated_quit
  -> cleared _stopping permits wrapper coroutine B to be submitted
  -> QApplication.quit emits aboutToQuit
  -> _shutdown_daemon sees loop.is_running() during its final iteration
  -> wrapper coroutine C is submitted and Qt blocks on C.result(timeout=20)
  -> daemon run_until_complete returns and closes the loop
  -> B and C never advance to await the already-complete daemon stop task
  -> timeout + pending-task destruction + never-awaited warning
```

There were four distinct mistakes:

- **Daemon-level coalescing was mistaken for submission-level coalescing.**
  Multiple wrapper coroutines can await one stop task; they are still separate
  tasks that must each be scheduled before the loop closes.
- **`_stopping` described an in-flight UI action, not terminal lifecycle
  identity.** Clearing it when the first future resolved reopened the submit
  path during the narrow loop-return interval.
- **Window closure was re-entrant.** Qt emits `lastWindowClosed` synchronously
  from `DashboardWindow.close()`, before `_quit()` reached
  `QApplication.quit()`.
- **`loop.is_running()` was used as a scheduling guarantee.** It is only a
  snapshot. `run_until_complete()` can be completing on the owner thread while
  another thread observes `True`; a callback enqueued at that point may never
  execute.

Intel did not expose a slower or broken daemon stop—the log proves all services
stopped in about one second. It exposed the post-stop window in which the loop
was still technically running but no longer had durable work ownership.

### 23.4 Design alternatives considered

#### Add a fixed sleep before detach

A delay can change race frequency but provides no ownership proof. A slow
runner, antivirus scan, Finder process, or delayed executable teardown could
outlive any chosen delay. It was rejected as probabilistic correctness.

#### Keep `TemporaryDirectory` and check the detach exit code

This would expose a clearer error, but context-manager unwinding would still
invoke recursive removal while the mount remained attached. Avoiding that
would require reaching into private tempfile cleanup behavior and would retain
the incorrect ownership model. It was rejected.

#### Ignore a busy detach and let the runner clean itself

That would permit leaked mounts to contaminate later steps or jobs and could
make evidence report success without completing resource teardown. Release
verification must fail closed, so it was rejected.

#### Force-detach immediately

The verifier owns a disposable read-only image, so a bounded forced detach is
safe as a final recovery step. Making it the first step would unnecessarily
interrupt ordinary filesystem quiescence and hide whether the normal path is
healthy. It was retained only after bounded normal attempts.

#### Recursively remove the path after detach

Even after a successful command, a recursive remover creates a dangerous
time-of-check/time-of-use surface if the mount state changes or the tool's
status is stale. The verifier creates only an empty mount-point directory, so
`rmdir` is both sufficient and a stronger assertion. Recursive removal was
eliminated.

#### Re-run or move v0.3.7

Rerunning an unchanged workflow could occasionally avoid the race, but would
leave the bug active. Moving the tag would violate immutable-release identity.
Both were rejected; v0.3.8 is a new candidate.

#### Rely only on `CortexDaemon.stop()` coalescing

The daemon's `_stop_task` correctly prevents duplicated service teardown, but
does not prevent creation of multiple outer coroutines or guarantee that a
closing event loop will schedule them. Keeping only the inner guard would
retain the exact Intel failure. It was rejected.

#### Leave `_stopping=True` forever

This would block the observed re-entry but overload a presentation flag with
terminal lifecycle identity and break any future in-process restart. The
selected design stores the actual cross-thread future and resets it only when a
new daemon lifecycle begins.

#### Disconnect `lastWindowClosed`

That signal is part of the product's window-close/recap interaction. Removing
it would avoid one trigger but leave `aboutToQuit`, tray quit, Cmd+Q, signals,
and future close paths able to race. The quit operation itself must be
idempotent, so disconnection was rejected.

#### Submit another stop from `aboutToQuit`

`aboutToQuit` remains a necessary safety barrier for direct quits, but blindly
submitting there creates the loop-closing race. It now reuses the controller's
existing future and submits only when no earlier stop identity exists.

#### Cancel or suppress pending-task warnings in the verifier

The warnings represented real orphaned coroutines, not harmless log noise.
Removing the marker would convert a deterministic release rejection into an
undetected cleanup defect. It was rejected.

### 23.5 Selected mount lifecycle algorithm

The verifier now models attach/detach as a small transaction with explicit
ownership:

```text
create dedicated empty mount-point directory with mkdtemp
  -> attempt read-only hdiutil attach at that exact path
  -> remember command success, but also consult os.path.ismount
  -> execute all bundle, architecture, signature, secret-scan, smoke,
     daemon-health, and shutdown checks
  -> if attach succeeded OR the path is actually mounted:
       attempt normal detach up to 3 times
       wait 0.5 seconds between normal attempts
       after continued failure, attempt one forced detach
       after every command, accept actual unmounted state even if the command
       returned a stale nonzero status
       if still mounted after force, raise ReleaseVerificationError
  -> only when the path is not mounted, remove it with Path.rmdir()
  -> never recursively traverse or delete through the mount point
```

The bounds are deliberate. Three normal attempts cover short-lived file-handle
release without making a failed release job hang indefinitely. The total retry
delay is one second, and every `hdiutil` invocation retains a 60-second process
timeout. One forced attempt is limited to the exact read-only volume created by
this verifier; no glob, `/Volumes` sweep, user volume, writable image, or broad
directory is targeted.

`os.path.ismount()` is the postcondition authority. A nonzero detach status
with an already absent mount is treated as reconciled because the resource is
gone. A zero status is accepted as the tool's successful transition; the
caller checks mount state again before directory cleanup. If the volume is
still present after the forced attempt, the verifier raises a specific error
containing the exact mount path and final stderr and deliberately leaves the
volume untouched for diagnosis.

Attach failure is also reconciled. Some system operations can return failure
after partially changing state, so the `finally` barrier detaches whenever
either the successful-attach flag is set or the path is actually a mount. This
prevents the exception path from leaking a volume.

### 23.6 Selected controller shutdown algorithm

The controller now owns the cross-thread submission identity in
`_daemon_stop_future`. Its lifecycle is:

```text
_start_daemon
  -> reset controller lifecycle flags for the new daemon identity

first stop request on Qt thread
  -> confirm daemon loop exists and is running
  -> construct exactly one daemon.stop() coroutine
  -> submit with run_coroutine_threadsafe
  -> store returned concurrent.futures.Future before installing callback
  -> callback reads the result and emits daemon_stopped through the Qt bridge

any later stop request
  -> return the same stored Future
  -> never construct or submit another wrapper coroutine

GUI quit
  -> set _quitting=True before closing the first window
  -> synchronous lastWindowClosed observes the latch and returns
  -> QApplication.quit emits aboutToQuit

aboutToQuit safety barrier
  -> execute at most once via _qt_shutdown_started
  -> release any blocking capture handle
  -> reuse stored stop Future, or submit the first one for a direct quit
  -> wait within a 20-second monotonic deadline
  -> join the original daemon owner thread using only the remaining deadline
  -> return only after the loop closes, or emit an explicit bounded error
```

The future—not `_stopping`, readiness, thread liveness, or an instantaneous
loop flag—is the identity of the cross-thread operation. `_stopping` can still
drive UI affordance state and can be cleared for presentation without reopening
submission. A future hot restart resets the future only in `_start_daemon`, at
the same boundary that constructs a new `CortexDaemon` and event loop.

There is one unavoidable check/submit race: a loop can close after
`is_running()` returns and before `run_coroutine_threadsafe()` accepts the
coroutine. The controller holds the newly created coroutine until submission
succeeds. If submission raises, it calls `coroutine.close()` before propagating
the error, preventing Python from later reporting an un-awaited coroutine.

The owner-thread join is separate from the stop future for a reason. The future
proves the submitted `daemon.stop()` wrapper returned; the thread proves
`CortexDaemon.run()` completed its own `finally: stop()`,
`run_until_complete()` returned, and the asyncio loop closed. Both transitions
must finish before interpreter teardown can be declared clean.

### 23.7 Error and cleanup invariants

The implementation enforces the following invariants:

| Invariant | Enforcement |
| --- | --- |
| No recursive deletion can cross into a DMG. | The verifier never calls `TemporaryDirectory`, `rmtree`, or an equivalent recursive remover for its mount path. |
| A transiently busy volume gets bounded recovery. | Three normal detaches precede one forced detach. |
| Tool status cannot contradict resource truth silently. | Mount state is checked after nonzero detach results and again before cleanup. |
| A mounted volume is never treated as an ordinary directory. | `_remove_detached_mountpoint()` fails immediately when `ismount` is true. |
| Cleanup proves the revealed host directory is empty. | `Path.rmdir()` is the only directory-removal operation. |
| Partial attach state is reclaimed. | The finalizer uses both the attach flag and actual mount state. |
| Permanent detach failure cannot publish an artifact. | The verifier raises before evidence/attestation/upload stages. |
| Failure diagnostics retain causal value. | The explicit detach error replaces secondary read-only traversal errors. |
| Qt submits one stop wrapper per daemon lifecycle. | `_daemon_stop_future` is retained and returned to every later caller. |
| Window closing cannot recursively start another quit. | `_quitting` is set before any top-level window is closed. |
| `aboutToQuit` cannot race a second stop onto a closing loop. | It reuses the stored future and is guarded by `_qt_shutdown_started`. |
| A rejected coroutine cannot leak a runtime warning. | The unscheduled coroutine is explicitly closed on submission failure. |
| Stop completion includes event-loop ownership teardown. | The controller joins the daemon thread within the original deadline. |

The last invariant intentionally gives cleanup ownership failure precedence if
verification and detach fail together. Leaving a mounted filesystem is a
runner-integrity condition that must be visible; the verifier does not pretend
the transaction closed successfully.

### 23.8 Deterministic regression proof

Focused unit coverage exercises the state machine without requiring privileged
mount operations:

- two `Resource busy` normal results followed by a successful forced detach
  prove the exact bounded command sequence;
- a nonzero detach result paired with `ismount == False` proves reconciliation
  uses resource state and avoids needless force;
- a failed normal attempt and failed forced attempt with `ismount == True`
  prove the operation fails closed;
- a synthetic mounted tree containing
  `Cortex.app/Contents/_CodeSignature/CodeResources` proves cleanup refuses to
  traverse the path and leaves the signed resource unchanged;
- parameter guards reject zero attempts and negative retry delays;
- the existing release-tooling suite continues to cover signature, notary,
  architecture, evidence, identity, and workflow contracts around the change.

Controller lifecycle coverage proves:

- two controller submissions return the same future and call `daemon.stop()`
  exactly once;
- a loop-closing submission failure explicitly closes its coroutine;
- `_quitting` is set before `DashboardWindow.close()` synchronously re-enters
  the `lastWindowClosed` handler;
- `aboutToQuit` reuses a completed future, never calls `daemon.stop()` again,
  joins the original owner thread, and ignores a second invocation;
- the existing daemon tests still prove all async callers coalesce onto the one
  internal `_stop_task` and await its complete teardown.

The focused release-tooling suite contains 72 passing tests after this patch.
The focused startup/readiness suite contains nine passing tests, including the
four new controller lifecycle cases. On the final pre-PR tree, Ruff passed,
strict mypy reported no issues across 521 source files, the wheel contained 285
verified files, the non-desktop Python suite passed 2,660 tests with three
intentional skips, and the separately isolated desktop suite passed 64 tests.
Schema, configuration, version, repository-contract, recorded-trace, and
diff-integrity gates also pass.

### 23.9 Real-artifact and runtime validation

The corrected verifier was exercised against the exact existing v0.3.6 arm64
DMG rather than only synthetic fixtures. That artifact is Developer-ID signed,
Apple-notarized, stapled, and structurally equivalent to the failing release
path. The canonical version check was temporarily supplied with the artifact's
own `0.3.6` version; no artifact bytes were modified.

One initial full pass and then three consecutive full passes each completed:

- `hdiutil verify`;
- read-only attach at the dedicated mount point;
- `Info.plist` and single-architecture verification;
- deep application signature verification;
- forbidden credential/personal-path scan;
- frozen `--release-smoke` composition;
- full headless packaged launch, version-matched HTTP health response, and
  graceful stop;
- clean normal detach and empty mount-point removal;
- outer DMG signature, stapled-ticket, and Gatekeeper verification.

All four passes returned success, and the three-run stress sequence observed a
zero detach return code every time. This proves the new ownership model on a
real read-only signed/notarized volume and verifies it does not regress the
application liveness path. It does not substitute for rebuilding v0.3.8 on
both native architectures.

The corrected v0.3.8 source desktop was then started in the same headless,
hardware-disabled mode used by the packaged probe, with isolated storage and
dedicated HTTP/WebSocket ports. It reached a version-matched healthy response,
received one SIGTERM, returned exit code zero, and left neither listener nor
process behind. Its output contained no pending-task, never-awaited-coroutine,
shutdown-boundary, or startup-failure marker. This validates the repaired
controller ordering on the local arm64 source runtime; the new frozen x86_64
job remains the authoritative regression gate for the timing that exposed the
incident.

A new v0.3.8 arm64 application was also frozen with the locked Python 3.11.15
graph and the repository's exact Node 22.23.2/pnpm 9.15.9 toolchain. The app was
deep ad-hoc signed for local execution and placed in an APFS compressed test
DMG whose canonical filename matched the verifier contract. One initial pass
and three consecutive stress passes each proved:

- thin arm64 executable identity and `0.3.8` bundle versions;
- deep code-signature validity and a clean credential/personal-path scan;
- successful frozen resource/composition smoke;
- a healthy `0.3.8` daemon with support-model registry up;
- normal zero-code SIGTERM exit without either Intel failure marker;
- detach return code zero and removal of the dedicated verifier mount;
- no surviving Cortex executable or verifier mount after the sequence.

The local host refused creation of a test volume named exactly `Cortex` while
an alternate unique local volume name succeeded with identical application
bytes. That host-specific disk-arbitration/TCC condition is not hidden: the
local DMG is lifecycle evidence, not the distributable installer. The tagged
workflow must still create the production `Cortex` volume on clean native
runners, use Developer ID rather than ad-hoc signing, notarize and staple the
exact bytes, and produce the Applications-link layout consumed by Finder.

### 23.10 Product and interaction impact

There is intentionally no visual or interaction change in v0.3.8. The UI work
in v0.3.7—truthful `Starting…`/`Connected` semantics, explicit permission
ownership, non-blocking capture degradation, live recovery, and visible fatal
startup diagnostics—remains the product behavior under test. Changing UI in a
packaging-only successor would expand the incident fix and invalidate the
already-reviewed behavioral evidence.

| Before | After | Why |
| --- | --- | --- |
| A successfully verified app could still fail the job while implicit tempfile cleanup entered its mounted volume. | Verification owns attach, bounded detach, and empty-directory cleanup as explicit states. | Release status now reflects product evidence rather than a cleanup abstraction mismatch. |
| The final error named a protected signature file. | Permanent failure names the detach transition and exact mount path. | Operators can act on the true subsystem and preserve useful evidence. |
| Closing Qt after a successful daemon stop could schedule two invisible stop wrappers and wait 20 seconds. | One stored future spans Stop, window close, signal quit, and `aboutToQuit`; the owner thread is joined once. | Quit is prompt, interruptible before commitment, and terminally deterministic after commitment. |
| A failed candidate could tempt an identical rerun under the same version. | Immutable v0.3.7 is preserved and the corrected source advances to v0.3.8. | Version, source, artifact, provenance, and incident history stay one-to-one. |

### 23.11 v0.3.8 release proof still required

Local verification of an older notarized artifact proves the cleanup fix, not
the new release. v0.3.8 remains fail-closed until this entire chain succeeds:

```text
clean synchronized 0.3.8 source tree
  -> pull request CI on native arm64 and x86_64
  -> merge commit and post-merge main CI
  -> new immutable annotated v0.3.8 tag at that exact merge
  -> native arm64 + x86_64 build, Developer ID sign, notarize, and staple
  -> corrected mounted verifier + frozen health/stop probe on each runner
  -> architecture-bound SBOMs, checksums, evidence, and attestations
  -> download draft assets and independently re-verify their digests/contracts
  -> Finder install of the downloaded host artifact into /Applications
  -> eject volume before launch
  -> visible installed open, version-matched healthy core, and honest sensor state
  -> normal quit, zero listeners/processes, relaunch, and repeated cleanup
  -> complete physical-machine manual records for both supported architectures
  -> independent reviewer identity and protected publication workflow
  -> unauthenticated public redownload, digest match, and repeated Finder E2E
```

The manual evidence gate is intentionally not self-attestable by the builder.
A local Apple Silicon exercise cannot stand in for a physical Intel run, and
an automated native runner cannot assert that a human saw the correct window,
permission ownership, menu behavior, or normal quit interaction. Missing
architecture evidence or independent review blocks publication rather than
being replaced with a claim.

### 23.12 Residual boundaries

- Forced detach is appropriate only because the verifier created and owns the
  exact disposable read-only mount. The helper must not be generalized to user
  disks or writable volumes without a different authority model.
- `os.path.ismount()` is a local kernel-state check. If future release runners
  move artifact verification into a container or remote mount namespace, the
  check and detach command must execute in the same namespace.
- A passing packaged health probe proves composition, transport readiness, and
  graceful lifecycle behavior. It does not prove camera signal validity or
  physiological model accuracy; those remain governed by explicit permission,
  calibration, reference-sensor, and evidence-quality programs.
- v0.3.8 must not be described as public merely because a draft release and
  downloadable authenticated assets exist. Publication and subsequent
  unauthenticated redownload are separate evidence-bearing transitions.

## 24. v0.3.9 packaged-state parity and permission recovery

### 24.1 Why the signed v0.3.8 candidate is superseded

The v0.3.8 release workflow completed its native arm64 and x86_64 jobs: both
DMGs were Developer-ID signed, accepted by Apple notarization, stapled, and
verified by the automated frozen-runtime gate. The exact arm64 draft artifact
was then downloaded by asset ID, hash-checked, mounted through Finder, copied
to `/Applications`, ejected, and launched as a normal application. Repeated
launch, navigation, in-app Stop, `Cmd+Q`, relaunch, HTTP/WebSocket health, and
process/listener cleanup all succeeded. The reported bounce-and-disappear
failure did not reproduce in the corrected candidate.

That physical exercise nevertheless exposed a release-blocking truthfulness
defect on a host whose Cortex camera authority was `not_determined`: the core
remained healthy and visible, but the consumer dashboard rendered `Reading
your pulse…` although capture could not start. The release therefore remains
unpublished. v0.3.8 is immutable evidence of the startup/teardown correction;
v0.3.9 is the first candidate eligible to include the discovered contract and
recovery fix.

### 24.2 Root cause

Cortex has two delivery paths for the same state event:

```text
StateEstimate + biometrics + RuntimeStatus
  |-- WebSocketServer._make_state_update -> browser/editor/WS desktop
  `-- RuntimeDaemon hand-built dict      -> packaged in-process desktop
```

The WebSocket builder projected `capture`, `store`, classifier provenance,
v2 timestamps, connected clients, and sequence through the typed
`StateUpdatePayload`. The in-process branch independently constructed a plain
dictionary. It had already drifted once and still omitted `capture` and
`store`. A second asymmetry existed in the failure path:
`_emit_capture_stale_broadcast()` sent the synthetic offline estimate only
through `WebSocketServer.broadcast_state()`, not through the local
`ApplicationEventHub` subscription used by the DMG.

The desktop view model intentionally treats a missing `capture` block as an
older-daemon compatibility case and chooses the benign warm-up copy. That
fallback behaved as designed; the producer violated the current transport
contract. The complete causal chain was therefore:

```text
macOS camera authority not determined
  -> optional capture startup declines to request TCC implicitly
  -> camera open returns unavailable quickly
  -> runtime marks capture stale and keeps the healthy core alive
  -> WS receives typed stale payload
  -> packaged local bridge receives no stale event / no capture block
  -> compatibility fallback says "Reading your pulse…"
```

This was not the original process-liveness failure. It was found only because
the release test checked the visible meaning of the running app after proving
its process, ports, health version, and clean shutdown.

### 24.3 Canonical payload architecture

`WebSocketServer.make_state_update_payload()` is now the single typed
projection boundary. It accepts the domain estimate, optional biometrics, and
an optional transport-owned sequence, then derives:

- abstention-safe state/support state and evidence fields;
- canonical deterministic scores, exclusions, model identity, and reasons;
- calibrated-probability fields only when their registered artifact contract
  permits them;
- classifier source and degraded/fallback status;
- legacy and v2 time provenance;
- identified client surfaces;
- fresh-frame, face-detection, stale-capture, and capture-sequence status;
- durable-store backend/health/degradation status; and
- validated optional biometrics.

`_make_state_update()` now owns only WebSocket envelope concerns: increment the
WS sequence, invoke the canonical builder, and wrap the dumped model in a
`WSMessage`. `CortexDaemon._publish_state_to_local_subscribers()` owns only
local transport concerns: increment the local sequence, invoke the same
builder, retain the private `_seq` bridge guard for one-release compatibility,
and publish through `ApplicationEventHub`.

This separation preserves transport-specific ordering without duplicating the
state contract. A schema addition can no longer silently reach the browser but
not the packaged desktop simply because a second dictionary was forgotten.

### 24.4 Capture-unavailable state transition

Optional camera startup remains deliberately outside the core-readiness
boundary. Its state machine is:

```text
CORE_READY
  -> HARDWARE_STARTING
      -> CAPTURE_FLOWING
      -> CAPTURE_UNAVAILABLE
           -> mark RuntimeStatus.capture_stale
           -> publish canonical local STATE_UPDATE
           -> broadcast canonical WS STATE_UPDATE
           -> remain healthy in telemetry-first mode
```

The local publication occurs after the runtime-status marker is planted, so
the canonical builder observes `capture.stale=True` on the first event. A
later valid frame still clears stale through the existing live-recovery path.
The event carries `UNKNOWN` plus `insufficient_evidence`; no unavailable
physiology can be converted into a supported cognitive-state claim.

Expected macOS authority states (`not_determined`, `restricted`, `denied`) are
now logged as an explicit `startup.hardware_unavailable` degradation with the
permission reason. They do not emit a misleading ERROR traceback. Exceptions
when authority is available—camera driver failure, invalid capture
composition, or MediaPipe initialization faults—retain the complete exception
diagnostic. This distinction makes support logs high-signal without hiding
real faults.

### 24.5 Recovery interaction

The consumer health banner remains compact and nonmodal. When capture is stale
it now contains a direct `Open Settings` link. The label exposes mouse and
keyboard link interaction, does not open arbitrary external URLs, and emits a
bounded `open_settings_requested` intent. `_ConsumerTab` forwards that intent
through `DashboardWindow`; both the in-process controller and WS-mode app
route it to their existing `_show_settings()` surface. The Settings dialog
already owns the explicit camera request/recovery action and live daemon retry.

This preserves authority boundaries:

- daemon startup never requests privacy permission;
- a status render never opens System Settings on its own;
- only an explicit user action enters the existing permission UI;
- a grant triggers the existing live capture retry without an app restart;
- denial leaves the core available and the recovery path visible.

| Before | After | Why |
| --- | --- | --- |
| Packaged desktop and WS clients consumed independently assembled state shapes. | Every transport consumes one typed payload projection. | Contract parity is structural, not dependent on review remembering two dictionaries. |
| Missing camera authority could appear as `Reading your pulse…`. | The first local failure event renders `Camera offline` with abstained state. | Status copy now reflects evidence that actually exists. |
| Recovery depended on discovering the menu-bar heart and its Settings menu. | The warning itself offers a keyboard-accessible `Open Settings` action. | Recovery is adjacent to the problem and requires an explicit, understandable gesture. |
| Expected TCC absence produced an ERROR traceback after an INFO explanation. | Permission absence has a named degraded-hardware warning; unexpected initialization still has a traceback. | Logs distinguish a user-controlled capability state from a software failure. |

### 24.6 Regression proof

The regression suite exercises production paths rather than recreating their
logic in a test:

- the local publisher builds a real `StateEstimate`, reads the real
  `RuntimeStatus`, emits three events, and proves monotonic `_seq` and public
  `sequence` values plus `capture.stale=True` on every payload;
- `_emit_capture_stale_broadcast()` is invoked on a constructed daemon with a
  real local subscription and must deliver exactly one typed offline event;
- existing WebSocket capture-unavailable coverage continues to require the
  same stale block;
- the consumer renders its offline status and health banner from the payload;
- activating only the `cortex-settings` link emits the bounded recovery
  signal; and
- the dashboard, controller, and WS-mode wiring retain the same Settings
  destination.

The first focused post-change run passed 100 tests across payload assembly,
capture-failure propagation, desktop view models, dashboard status rendering,
sequence guards, and API gateway behavior. The subsequent canonical local gate
also passed:

- Ruff and strict mypy across 521 source files;
- a `0.3.9` wheel containing 285 verified files;
- 2,661 non-desktop Python tests with four intentional skips;
- the separately isolated 64-test desktop-shell suite;
- browser TypeScript plus 54 files / 248 tests and Chrome + Edge MV3 builds;
- VS Code compile, 7 suites / 30 tests, and a locked `0.3.9` VSIX;
- schema codegen, 203-setting configuration sync, design-token sync, version
  sync, links/security/dependency repository contracts, and recorded-trace
  regression;
- Python and VS Code dependency audits with no findings, plus the browser
  audit with every finding covered by the repository's reviewed exception
  policy; and
- an isolated-storage source launch that returned a version-matched healthy
  `0.3.9` response with every service and durable SQLite storage up, then
  handled SIGTERM with a normal zero exit and no remaining listener/process.

The first source launch intentionally reused the repository's ignored
`./storage` override and failed closed on an old development database whose
recorded schema-2 checksum predates the now-committed immutable migration.
Inspection proved the installed App Support database matches both committed
migration hashes. The release-relevant source probe was therefore repeated
with an explicit empty storage directory; no user data was modified or
discarded. Fresh frozen artifacts remain mandatory below.

### 24.7 v0.3.9 completion and publication gate

The v0.3.9 candidate must repeat—not inherit—the release evidence chain:

```text
full source/type/schema/config/extension tests
  -> clean PR + main CI at one merge SHA
  -> immutable annotated v0.3.9 tag at that SHA
  -> native arm64 and x86_64 Developer-ID builds
  -> Apple notarization + stapling + Gatekeeper verification
  -> checksums, SBOMs, SLSA provenance, and signed evidence
  -> download the exact draft arm64 DMG and verify its digest
  -> Finder mount/copy/eject/open from /Applications
  -> visibly confirm Camera offline + Open Settings with authority absent
  -> confirm healthy 0.3.9 core and owned 9472/9473 listeners
  -> exercise Dashboard/History/Advanced, Stop, Cmd+Q, and relaunch
  -> confirm zero processes/listeners/mounts and no teardown diagnostics
  -> permission deny/grant/revoke tests only with explicit user approval
  -> corresponding physical Intel exercise and independent review
  -> exactly two complete 14-case manual evidence records
  -> protected publication workflow
  -> unauthenticated public browser download, digest match, quarantine-aware
     Finder install/open/quit repetition
```

Automated notarization and a successful local Apple Silicon Finder run cannot
substitute for the physical Intel record, independent reviewer, or explicit
privacy-permission interactions. Until those records exist, the truthful state
is a verified draft candidate—not a public release.

## 25. v0.3.10 deterministic DMG installation contract

### 25.1 The Finder exercise falsified the automated release claim

The v0.3.9 release was tagged only after PR 53, the pull-request matrix, and the
exact merge SHA's seven-job main matrix were green. Release run `32951120795`
then produced an ARM artifact at commit
`b68e3c7758f4b80949efca147be92d06714cbf72`. That artifact passed:

- Developer ID signing for the application and outer disk image;
- Apple notarization with status `Accepted`, a stapled ticket, and zero notary
  errors;
- `hdiutil verify`, deep bundle signature verification, exact ARM architecture,
  Gatekeeper assessment of the distributable DMG, frozen-resource inspection,
  and isolated packaged startup/health/shutdown;
- a complete checksum manifest, SPDX application SBOM, CycloneDX locked-Python
  graph, SLSA provenance, and signed evidence-bundle provenance; and
- a second local run of the canonical verifier against the downloaded workflow
  artifact, whose SHA-256 was
  `3c22bd4f67d33d10b036850c5a7ac02efeb5388e0d50f146ac2b873552c3d77c`.

The next required step deliberately did not execute the app from a verifier
mount. It added a quarantine attribute without changing the file digest,
opened the exact DMG in Finder, and inspected the user-visible installer. The
volume contained only `Cortex.app`; the expected Applications shortcut was
absent. A technically valid image was therefore not a complete, conventional
drag-to-install experience. The release was cancelled before its Intel job
could finish and before the draft-staging job could run. v0.3.9 is an immutable
failed candidate, not a release.

This is why operational E2E is a separate gate from build smoke tests. A test
that starts the executable can prove runtime viability while saying nothing
about whether a person can correctly install that executable.

### 25.2 Root cause

The former build graph assigned semantic installer content to an optional
presentation dependency:

```text
stage directory
  └── Cortex.app

create-dmg available
  -> --app-drop-link attempts to add Applications

create-dmg unavailable or fails
  -> hdiutil packages the stage directory exactly
  -> Cortex.app only
```

GitHub's clean macOS release runner did not have `create-dmg`. The build log
therefore showed the direct `hdiutil` `created:` path, not the tool's normal
mount/decorate/compress transcript. The fallback was intentionally checked for
command failure, artifact existence, image integrity, signature, notarization,
and packaged startup, but its input directory had no Applications link.

Three design errors combined:

1. **Branch-asymmetric payloads.** The primary and fallback image builders did
   not receive the same semantic source tree.
2. **Presentation owned structure.** A cosmetic tool option was allowed to
   create a required installer object.
3. **The verifier modeled the app, not the installation.** It required a valid
   `Cortex.app` but did not encode the volume-level user contract.

No runtime algorithm, state estimator, capture path, or shutdown path caused
this defect. The earlier Dock-bounce investigation remains addressed by the
crash-visible bootstrap, packaged state parity, and truthful hardware state;
this newly observed failure was one layer earlier in the distribution chain.

### 25.3 Corrected architecture

The source tree now owns the complete installer payload before choosing a disk
image implementation:

```text
assemble deterministic, tool-independent stage
  ├── Cortex.app
  └── Applications -> /Applications
           |
           `--> built-in hdiutil: canonical local + CI image builder
                    volume label = Cortex <release version>

mount exact output read-only
  -> require Cortex.app contract
  -> require Applications is a symlink
  -> require readlink(Applications) == /Applications
  -> continue architecture/signature/resource/startup checks
```

The first correction retained `create-dmg` as an optional view decorator. A
fresh full-app build then exposed a second branch hazard: after the exact
quarantined v0.3.9 image had been opened and ejected, macOS logged `System
Policy: copy-helper deny(1) file-write-create /Volumes/Cortex/Cortex.app`.
Both the optional builder and its alleged `hdiutil` fallback invoked the same
mounted-path copy mechanism with the same unversioned volume label, so both
failed identically. A fallback that repeats the failed mechanism is not fault
containment.

The release script consequently has one canonical image implementation:
macOS's built-in `hdiutil`, which is also the implementation present on clean
GitHub runners. The volume label is `Cortex <release version>`, preventing the
next immutable candidate from reusing a quarantined predecessor's exact mount
identity. A changed release candidate already requires a new version, so this
also aligns filesystem identity with the project's immutable-tag policy. The
presentation dependency, AppleScript/Automation side effects, and misleading
same-mechanism fallback are gone.

The artifact verifier records the accepted layout in its JSON evidence:

```json
{
  "installer_layout": {
    "applications_link": "/Applications"
  }
}
```

The check uses `is_symlink()` and `readlink()` rather than `exists()` or
resolved-path equality. That distinction is intentional:

- a real directory named `Applications` is not a drag target;
- a broken or relative link is not accepted accidentally;
- a link to `/System/Applications` or another writable destination is rejected;
- the verifier does not follow the link and mistake the host's Applications
  directory for content inside the image.

### 25.4 Failure containment

| Failure mode | Former result | v0.3.10 result |
| --- | --- | --- |
| `create-dmg` absent on a clean runner | Valid one-icon DMG passed | No optional branch exists; built-in `hdiutil` is canonical |
| Optional builder fails | Fallback repeated its copy mechanism | Optional builder and misleading fallback are removed |
| Prior Finder run reserves `/Volumes/Cortex/Cortex.app` | Both builders receive a System Policy denial | Versioned label gives each immutable candidate a distinct mount identity |
| A builder drops the link | Signing/notarization/startup could still pass | Mounted artifact verification fails before evidence can be staged |
| `Applications` is a directory | Could look superficially plausible | Exact symlink-type check fails |
| Link targets another path | No volume-level policy | Exact `/Applications` target check fails |
| Runtime starts directly from a mount | Mistaken for installation proof | Kept as a smoke layer; Finder copy/open remains a separate gate |

This preserves the useful defense layers without conflating them:

- checksum and signature answer whether bytes are intact and authentic;
- notarization answers whether Apple scanned and ticketed the submitted nested
  distribution;
- frozen startup answers whether the packaged process can initialize and stop;
- layout verification answers whether the installer has the required objects;
- Finder E2E answers whether a user can mount, copy, eject, and launch it.

Apple's current documentation recommends signing nested code inside-out while
notarizing and stapling the outermost nested distribution. The v0.3.9 accepted
notary log contained the exact Cortex app and executable CDHash, so the
standalone pre-notarization `spctl-app.txt` diagnostic was not the layout
failure. The authoritative distribution assessment is the signed, stapled DMG
plus the real Finder path. See [Packaging Mac software for
distribution](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution)
and [Customizing the notarization
workflow](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow).

### 25.5 Regression proof

The focused release-tooling suite contains 77 passing tests after this change.
New coverage proves:

- the stage copies `Cortex.app` and creates the Applications symlink before the
  sole image builder runs;
- the canonical builder uses a release-versioned volume label and no optional
  `create-dmg` or `--app-drop-link` branch remains;
- the verifier accepts exactly `Applications -> /Applications`;
- a missing entry, real directory, or wrong symlink target fails with a bounded
  actionable error; and
- the exact v0.3.9 candidate is now rejected as the negative fixture with
  `DMG does not contain the required Applications symlink`.

Two real minimal disk images initially exercised both construction mechanisms
independently of mocks. Both preserved the staged link, but the subsequent full
app exercise proved that this was insufficient: System Policy denied the
reused unversioned copy destination, and a `makehybrid` alternative introduced
forbidden Finder metadata that invalidated the mounted app's strict signature.
The selected native APFS `hdiutil -srcfolder` image with the exact versioned
`Cortex 0.3.10` label preserved the link and the full app's strict signature.
This negative testing is why neither the same-mechanism fallback nor the
signature-breaking hybrid path remains in release code.

The complete fresh ad-hoc ARM64 build then passed the canonical mounted-artifact
verifier. Its local-only SHA-256 was
`95ab7b79754a0df11fa67fff268fd7deef0f87c49a10e9f7e5cb48c66e429610`;
the evidence reports `status: passed`, exact ARM64 architecture, version
`0.3.10`, healthy non-degraded SQLite storage, every registered service `up`,
successful isolated startup and shutdown, and
`installer_layout.applications_link: /Applications`. Finder independently
opened that same image as the versioned `Cortex 0.3.10` volume and visibly
exposed both the Applications shortcut and Cortex app before clean ejection.
No Cortex process, release mount, or listener on ports 9471–9473 remained.
This is regression evidence for the construction—not the identity of the later
Developer ID-signed release asset, which must be rebuilt and reverified from
the exact merge SHA.

### 25.6 v0.3.10 completion and publication gate

v0.3.10 must be built from a fresh merge SHA and may not inherit v0.3.9's
positive evidence. The complete path is:

```text
focused release/layout tests
  -> complete local source/type/schema/config/extension gates
  -> clean PR and exact-merge main CI
  -> new immutable v0.3.10 tag
  -> fresh ARM64 + Intel signed/notarized artifacts and attestations
  -> exact draft asset download and digest/evidence verification
  -> quarantine-aware Finder mount
  -> visibly confirm both Cortex.app and Applications shortcut
  -> drag Cortex.app onto that shortcut, eject, launch from /Applications
  -> prove the app remains visible and healthy instead of bouncing away
  -> verify truthful camera-offline recovery without silently granting TCC
  -> Dashboard / History / Advanced interactions, Stop, Cmd+Q, relaunch
  -> zero residual processes, listeners, mounts, or crash reports
  -> permission deny/grant/revoke only with explicit user approval
  -> physical Intel run and globally independent review
  -> exactly two schema-valid 14-case manual evidence records
  -> protected publication workflow
  -> unauthenticated public browser redownload, digest match, and repeat E2E
```

Until both manual architecture records and independent review exist, even a
fully automated, signed, notarized, locally verified v0.3.10 artifact remains a
draft candidate. That gate is an integrity property, not release ceremony.

## 26. v0.3.11 — relaunch-safe camera ownership and bounded self-recovery

v0.3.11 supersedes the unpublished v0.3.10 candidate. The v0.3.10 ARM64 draft
artifact was correctly signed, notarized, stapled, mounted, copied through the
Finder installer surface, and launched from `/Applications`. Its desktop UI,
HTTP server, WebSocket server, state stream, authenticated shutdown, cleanup,
and Finder relaunch all remained alive. The required relaunch exercise then
revealed a more subtle failure: the second process stayed healthy while its
AVFoundation-backed OpenCV handle returned only failed reads. The UI could
therefore remain visible while sensing had silently stopped.

This finding invalidates v0.3.10 as a publishable candidate. It remains a draft
and must not be relabelled, have its assets reused, or be represented as a
successful release. v0.3.11 requires a new immutable tag, new artifacts, new
digests, and new evidence from the exact merge commit.

### 26.1 Incident evidence and scope

The observed sequence was:

1. Download the exact ARM64 v0.3.10 draft DMG and verify its digest.
2. Apply download quarantine and confirm Gatekeeper acceptance.
3. Mount the versioned `Cortex 0.3.10` volume in Finder.
4. Confirm `Cortex.app` and the exact `Applications -> /Applications` link.
5. Copy Cortex into `/Applications`, eject the DMG, and launch the installed
   app.
6. Confirm healthy UI/control-plane operation and live WebSocket state.
7. Stop through the authenticated shutdown path; confirm capture, MediaPipe,
   hooks, ports, and process are released.
8. Relaunch the same installed app through Finder.
9. Observe that the process, UI, HTTP API, and WebSocket API remain healthy,
   but `VideoCapture.read()` returns `(False, None)` approximately once per
   second.
10. Observe that the old count-only threshold requires 30 failures and thus
    takes roughly 30 seconds—not the intended one second—to declare capture
    stale.
11. Observe no release/re-enumerate/reopen transition after the stall.

There was no corresponding macOS crash report. This is therefore not the
original “Dock icon bounces and disappears” process-termination symptom. It is
a distinct acquisition-liveness defect found only because the release test
required stop, complete cleanup, and relaunch. Both defects are release
blocking because either one makes the downloaded product appear nonfunctional.

After the user requested that Cortex stop activating the phone camera, the app
was stopped and ports 9472 and 9473 were confirmed closed. All v0.3.11 source
validation described below is camera-free and patches the camera-open boundary.
No further local app launch or physical-camera exercise is authorized by this
document.

### 26.2 Root-cause chain

The defect was not one isolated bad return value. Five implementation choices
combined into a silent, non-recovering failure:

1. **Cross-thread backend ownership.** `start()` opened, warmed, and configured
   the `cv2.VideoCapture` on an `asyncio.to_thread` worker and then transferred
   the live handle to a different `cortex-webcam` thread for reads and release.
   That split lifecycle is an avoidable hazard for a stateful AVFoundation
   backend whose capture session work is expected to be serialized.
2. **Open success was treated as permanent.** A handle that was open and
   produced a warm-up frame was assumed to remain usable for the process
   lifetime. Device interruption, reconnection, index reshuffling, or backend
   invalidation after relaunch had no recovery path.
3. **Staleness was count-only.** Thirty failed reads was intended as about one
   second at 30 FPS. A blocking backend can take about one second to return a
   single failure, stretching detection to about 30 seconds.
4. **Missing observations looked fresh.** The scheduler correctly created a
   current timestamp for every failed observation, but `frames_flowing` used
   timestamp age alone. A fresh failure was therefore misreported as fresh
   pixels.
5. **Runtime state was start-biased.** The daemon projected capture availability
   during startup but did not continuously synchronize later stale/recovered
   transitions from the resource owner.

The resulting causal chain was:

```text
validated backend handle
  -> handle changes thread ownership
  -> later backend interruption or invalidation
  -> read() repeatedly returns no pixels
  -> count-only timer reacts very late
  -> no release/reopen transition exists
  -> fresh missing timestamps still imply “frames flowing”
  -> app stays alive while capture is permanently unavailable
```

OpenCV documents that `read()` returns false and an empty image when no frame
has been grabbed, that `release()` closes the device, and that `open()` first
releases an already-open capture. Those semantics require an application-level
recovery policy; they do not promise automatic device recovery. See
[OpenCV VideoCapture](https://docs.opencv.org/doc/doxygen/html/d8/dfe/classcv_1_1VideoCapture.html).
Apple likewise models capture start/stop, interruption, and runtime error as a
session lifecycle and recommends serialized session work. See
[AVCaptureSession](https://developer.apple.com/documentation/avfoundation/avcapturesession).

### 26.3 Required invariants

The v0.3.11 design is governed by the following invariants:

| ID | Invariant | Enforced by |
|---|---|---|
| C1 | One dedicated thread owns open, configuration, read, recovery, and normal release for a camera handle. | `WebcamCapture._capture_loop` |
| C2 | A successful open is provisional; recovery is complete only after the replacement handle delivers a real frame. | `_recovery_pending_frame` and recovery counters |
| C3 | Staleness is bounded by elapsed monotonic time as well as failure count. | 2-second time bound or 30 consecutive failures |
| C4 | Recovery cannot reuse a cached AVFoundation index map. | Every attempt calls `open_video_capture()` and performs live post-open enumeration |
| C5 | Continuity Camera is rejected before open when known and again after open using the live device list. | candidate filter plus post-open identity verification |
| C6 | Missing scheduler observations never count as flowing pixels. | `FrameMeta.frame_available` and WebSocket state builder |
| C7 | A new physical open is a new temporal source. | New `source_instance_id` after every reopen |
| C8 | Shutdown cancels sleep, warm-up, and recovery backoff and never waits synchronously on the UI/event-loop thread. | shared cancellation event and `asyncio.to_thread` wait |
| C9 | Stale/recovered state and recovery counts are externally observable. | runtime registry, runtime status, diagnostics, and `/health` |
| C10 | Runtime capture code does not silently request TCC access. | authorization check only; onboarding/settings owns the user gesture |

These are correctness properties, not best-effort logging goals. A future
implementation may replace OpenCV with native AVFoundation, but it must retain
the same observable guarantees.

### 26.4 Architecture before and after

Before v0.3.11:

```text
async event loop
  -> asyncio.to_thread(open + warm-up + configure)
       -> returns a live VideoCapture handle
  -> create cortex-webcam thread
       -> read forever
       -> on failure: enqueue missing observation forever
       -> release at shutdown
```

After v0.3.11:

```text
async event loop
  -> create startup Future
  -> create cortex-webcam thread
       -> enumerate + open + warm-up + configure
       -> resolve startup Future thread-safely
       -> read
          -> live frame: publish pixels and clear confirmed stale state
          -> bounded failure: publish missing observation and enter recovery
       -> release active handle
       -> cancellable capped backoff
       -> live re-enumeration + verified reopen + configure
       -> assign new camera identity/source instance
       -> require a real frame before recording recovery success
       -> release in finally
  -> stop: signal cancellation and wait off-loop
       -> emergency release only if the backend remains blocked
```

The event loop owns coordination and queues. The camera thread owns the
backend. The only normal cross-thread camera operation is the event loop asking
the owner to stop. Emergency release is deliberately exceptional and bounded:
it exists to break a backend call that ignores cooperative cancellation.

### 26.5 Recovery state machine

| State | Entry evidence | Action | Exit condition |
|---|---|---|---|
| `STARTING` | `start()` requested | Open and verify on `cortex-webcam`; apply requested format | Verified handle → `STREAMING`; error/cancel → `OFFLINE` |
| `STREAMING` | At least one real frame or verified initial open | Schedule reads and publish observations | Successful read stays; bounded failure → `STALE_BACKOFF`; stop → `STOPPING` |
| `SUSPECT` | One or more failed reads below the bound | Publish explicit missing observations; preserve scheduler time | Real frame → `STREAMING`; 30 failures or 2 seconds → `STALE_BACKOFF` |
| `STALE_BACKOFF` | Count/time bound exceeded, or handle absent | Set stale, release handle, wait 0.5/1/2/4/8/15 seconds | Cancellation → `STOPPING`; timer → `REOPENING` |
| `REOPENING` | Backoff completed | Re-enumerate devices and run full open/warm-up/post-open verification | No verified device → `STALE_BACKOFF`; verified handle → `VERIFYING` |
| `VERIFYING` | Replacement handle installed | Assign new source instance; keep stale true; request frames | First real frame → `STREAMING`; 3 failures → `STALE_BACKOFF` |
| `STOPPING` | Stop/cancel requested | Cancel waits, release active handle, signal stopped | Worker exit → `OFFLINE` |
| `OFFLINE` | Worker exited or startup failed | No camera handle retained | New explicit start → `STARTING` |

The backoff index is based on reopen attempts since the most recent real frame.
It resets only when pixels flow again. The 15-second cap avoids hot-looping a
missing camera while still allowing unattended recovery after reconnection.
Every delay uses the shared cancellation event, so quit does not wait for a
sleep interval to expire.

### 26.6 Resource ownership and startup

`WebcamCapture.start()` now initializes state and starts the one named camera
thread. The thread performs enumeration, backend construction, warm-up reads,
format configuration, acquisition, reopen, and final release. It signals
startup completion through `loop.call_soon_threadsafe`; the live backend object
never travels through the Future.

Startup remains fail-fast to callers: no verified camera results in a
`RuntimeError`, allowing the daemon's existing capture-degraded mode to keep
the desktop/API usable. Cancellation during enumeration or warm-up releases
the provisional handle before returning. The same cancellation event is passed
through every open attempt so stop can interrupt retries.

Backend format setters are advisory. A camera that rejects width, height, or
FPS remains usable because downstream code consumes the actual frame shape.
Setter exceptions are isolated and logged per property instead of discarding a
working stream.

### 26.7 Bounded stall detection and reopen

Each read records its monotonic start time. A failure becomes recovery-eligible
when any of the following is true:

- 30 consecutive reads have failed;
- failures have covered at least 2.0 monotonic seconds, including time blocked
  inside `read()`; or
- a just-reopened handle has failed three consecutive validation reads.

This dual count/time policy preserves fast detection for nonblocking failures
and a real wall-time bound for blocking failures. Failed-read logs are limited
to one per five seconds to prevent a disconnected camera from flooding the
diagnostic log. Missing observations continue to be queued so downstream
windows preserve cadence and can reason about explicit absence.

Recovery always performs these steps in order:

1. Set `capture_stale=True`.
2. Release and clear the active handle.
3. Wait on the cancellation-aware capped backoff.
4. Call `open_video_capture()` again—not a cached index or old handle.
5. Reapply format preferences.
6. Install the live, post-open camera selection and identity.
7. Generate a new `source_instance_id` so temporal windows cannot bridge two
   physical acquisition sessions.
8. Keep capture stale until a real frame arrives.
9. Increment `recovery_successes` only on that first confirmed frame.

`recovery_attempts` therefore counts physical reopen attempts;
`recovery_successes` counts frame-confirmed recoveries. The difference is an
operator-visible signal of reopen failures or replacement handles that never
delivered pixels.

### 26.8 Camera identity and Continuity Camera safety

AVFoundation discovery is dynamic: a phone waking, sleeping, connecting, or
disconnecting can change the device list and indices. Apple exposes discovery
as a current session rather than a permanent index registry; see
[AVCaptureDevice.DiscoverySession](https://developer.apple.com/documentation/avfoundation/avcapturedevice/discoverysession).
Accordingly, recovery reuses the established safety boundary:

- known iPhone, iPad, and Continuity candidates are not opened;
- fallback probes exclude indices currently identified as Continuity Camera;
- an empty discovery list fails closed without opening anonymous indices;
- an explicitly configured index is still rejected before open when its live
  identity is a Continuity Camera;
- after a candidate returns a frame, the live AVFoundation list is queried
  again;
- a device now identified as Continuity Camera is immediately released and
  rejected;
- on macOS, an identity that cannot be verified after open is rejected; and
- camera identity hashes use the verified name/source/format rather than a
  reorder-prone numeric index.

The runtime authorization check does not trigger a permission prompt. If TCC
is not determined, denied, or restricted, capture remains offline while the
rest of Cortex starts. Only the user-facing onboarding/settings action may ask
macOS for authority.

### 26.9 Truthful frame and transport semantics

The old transport could not distinguish these two observations:

```text
real camera frame, but no face detected
scheduled camera read, but no pixels arrived
```

Both had `face_detected=false` and a fresh timestamp. `FrameMeta` now adds:

- `frame_available: bool` — true only when pixels were delivered; and
- `missing_reason: MissingReason | null` — the canonical absence reason.

The model rejects impossible combinations:

- `frame_available=false` without a missing reason;
- `frame_available=true` with a missing reason; and
- `frame_available=false` with `face_detected=true`.

A real no-face frame remains `frame_available=true`. A dropped, warm-up, or
disconnected observation is `frame_available=false` with its exact reason.
The WebSocket state builder now reports `frames_flowing=true` only when the
latest observation is under two seconds old **and** contains pixels. Scheduler
liveness can no longer masquerade as camera liveness.

The generated TypeScript declarations for the browser and VS Code extensions
are regenerated from this Pydantic source of truth. No hand-written transport
type is introduced.

### 26.10 Runtime and health observability

The pipeline exposes live pass-through properties for:

- `capture_stale`;
- `camera_recovery_attempts`; and
- `camera_recovery_successes`.

Its diagnostic snapshot includes both recovery counters. The daemon compares
pipeline stale state on every processed output and projects only transitions
to the service registry and typed runtime status. This handles both directions:
a post-start stall becomes visible and the first live frame after reopen clears
the state.

`GET /health` includes the same three fields. The endpoint reads the pipeline's
live state when available rather than relying only on a potentially older
daemon snapshot. Operators can therefore distinguish:

| Health shape | Meaning |
|---|---|
| `capture_stale=false`, attempts `0` | No confirmed stall since start |
| `capture_stale=true`, attempts `0` | Stall detected; first backoff not yet completed |
| `capture_stale=true`, attempts greater than successes | Recovery is retrying or a reopened handle has not produced pixels |
| `capture_stale=false`, attempts greater than `0`, attempts equal or greater than successes | At least one recovery was frame-confirmed; earlier attempts may have failed |

These metrics contain no image data, device frames, or biometric values.

### 26.11 Responsive and idempotent shutdown

Stop sets both cooperative signals immediately. Waiting for the camera thread
is moved to `asyncio.to_thread`, so a blocking backend cannot freeze Qt, HTTP,
or WebSocket shutdown. The normal worker has two seconds to observe the signal,
cancel any backoff/warm-up wait, and release in `finally`.

If the backend remains blocked, stop clears and releases the active handle as a
bounded emergency action, then gives the worker another 0.5 seconds. Final
cleanup calls release again through an idempotent clear-first helper; only the
first caller sees a handle. Exceptions from `release()` are logged as warnings
because a leaked camera handle affects the next app launch.

This design preserves the project-wide shutdown chain while ensuring the new
recovery loop cannot make Stop or Cmd+Q wait for a 15-second backoff.

### 26.12 Camera-free regression proof

The focused v0.3.11 capture/runtime suite has 182 passing tests. Every
camera-open boundary is patched; these tests do not access a physical camera,
request TCC, or launch Cortex. New coverage proves:

- open, format configuration, read, and normal release run on one non-main
  `cortex-webcam` thread;
- a blocked read does not block the asyncio event loop during stop;
- emergency release unblocks the synthetic backend within the configured
  shutdown bound;
- a stalled handle is released, reopened, and declared recovered only after a
  replacement frame arrives;
- a failed reopen is followed by another full open boundary, representing live
  re-enumeration and post-open verification;
- elapsed time can declare a stall before the count threshold;
- a five-second recovery backoff is cancelled immediately by stop;
- empty macOS discovery performs zero blind `VideoCapture` probes;
- an explicitly configured Continuity Camera index is rejected before open;
- a phone-only automatic device list performs zero `VideoCapture` opens;
- a candidate is released when its live post-open identity cannot be resolved,
  even if a cached pre-open name was available;
- source instance and camera identity change after replacement;
- actual no-face frames remain available while synthetic missing observations
  do not;
- incoherent frame-availability combinations are schema errors;
- a fresh missing observation does not produce `frames_flowing=true`;
- stale and recovered transitions reach the runtime registry/status; and
- `/health` reports live stale state plus frame-confirmed recovery counts.

The complete parent Python suite has 2,681 passing tests and three documented
dataset/tool-availability skips. A legacy 64-test desktop-shell module formerly
installed fake `PySide6` modules during collection, contaminating subsequent
real-Qt tests and making the result depend on file order. It now runs in a
dedicated child process; the parent suite never imports its global stubs, and
all 64 child tests pass. The real-Qt accessibility, onboarding, dashboard,
overlay, and settings cluster passes 94/94 in the uncontaminated parent
process.

Strict Ruff passes across the repository and strict mypy passes across 522
source files when invoked with the canonical `cortex/pyproject.toml`. Browser
verification passes 54 suites / 248 tests, TypeScript, and Chrome/Edge MV3
builds. VS Code verification passes seven suites / 30 tests, TypeScript
compilation, locked VSIX packaging, and a new reproducible ESLint gate.

The VS Code package had declared `npm run lint` without installing ESLint or
providing configuration, so clean-checkout lint failed before inspecting any
source. v0.3.11 adopts the current official flat configuration with locked
`eslint`, `@eslint/js`, and `typescript-eslint` development dependencies. It
ignores generated schema declarations and build output, accepts intentionally
underscore-prefixed unused callback arguments, and found two real redundant
assignments in the durable editor transaction adapter. Both were removed.
Pull-request CI and the tag release gate now execute lint before compile/test;
the development config is excluded from the shipped VSIX.

### 26.13 Better long-term architecture options

The v0.3.11 design is the smallest safe correction within the existing OpenCV
architecture. Research identifies four follow-on improvements with real value:

1. **Native AVFoundation capture adapter.** A small Objective-C/Swift or PyObjC
   adapter could subscribe directly to interruption and runtime-error
   notifications, own an explicit serial session queue, select devices by
   stable unique ID, and deliver frames without probing numeric OpenCV indices.
   This removes heuristic failure detection but is a material backend rewrite
   requiring its own performance, signing, TCC, and Intel/ARM validation.
2. **Dynamic device-change observer.** Discovery-session updates could trigger
   recovery immediately when the current device disconnects and could refresh
   candidate identities before the next open. The existing live re-enumeration
   remains necessary as a race-safe final check.
3. **First-class acquisition health schema.** A versioned `CaptureHealth`
   object could combine state, last-live-frame time, failure duration, current
   backoff, selected non-sensitive device class, and recovery counters. This
   would replace several scalar fields once browser/desktop consumers need a
   richer diagnostic surface.
4. **Deterministic backend fault harness.** A fake VideoCapture backend that can
   block, throw, reorder devices, disappear, return empty frames, and recover
   on a scripted clock would expand property/state-machine coverage without
   touching TCC or a physical camera.

The native adapter has the highest eventual reliability ceiling, but it should
not be rushed into this patch: replacing the capture backend and repairing its
lifecycle simultaneously would make regression evidence less attributable.
The current single-owner recovery boundary is intentionally compatible with a
future adapter.

### 26.14 Release and publication gate

The safe v0.3.11 path is:

```text
camera-free focused tests
  -> complete source, schema, config, type, UI, extension, and release-tool gates
  -> reviewed PR and exact-merge main CI
  -> new immutable v0.3.11 tag
  -> fresh signed/notarized ARM64 and Intel artifacts on clean headless runners
  -> verify attestations, digests, signatures, notarization, staple, architecture
  -> download and inspect the exact draft DMGs without launching locally
  -> STOP: no local live launch/camera test without explicit user authorization
  -> authorized physical ARM and Intel install/launch/relaunch/cleanup tests
  -> independent reviewer and two complete schema-valid manual evidence records
  -> protected publication workflow
  -> unauthenticated public redownload and authorized repeat verification
```

Remote headless packaging can prove build provenance, bundle structure,
architecture, signing, notarization, stapling, and camera-free startup. It
cannot prove TCC prompts, a real built-in camera, phone-camera avoidance, Finder
interaction, repeated live-frame recovery, or cleanup on physical ARM and Intel
Macs. Those cases remain mandatory before publication, but the current user
instruction prohibits running them locally. The correct state is therefore a
verified draft—not a public release—until explicit authorization and the
independent physical evidence exist.
