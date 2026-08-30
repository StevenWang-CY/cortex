# Changelog

All notable changes to Cortex. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v0.3.12] — 2026-08-29

This patch is a fresh release candidate built on v0.3.11. It focuses on the
remaining privacy-sensitive camera-selection boundary, cross-surface UI/UX
contracts, and a stricter tag-to-publication chain. It does not relabel or
reuse an older artifact.

### Fixed

* macOS camera selection no longer asks a local LLM to infer which device is
  built in. Cortex now consumes AVFoundation's explicit
  `isContinuityCamera`, `isConnected`, `deviceType`, and `uniqueID`
  properties from the exact device array indexed by OpenCV.
* The raw AVFoundation unique ID is never logged or retained. A one-way
  SHA-256 device key gives calibration and observation boundaries a stable
  physical identity even when an iPhone connection reshuffles indices.
* Automatic and configured camera paths exclude Continuity Camera,
  disconnected, unnamed, and unverified devices before OpenCV. The mandatory
  post-warm-up enumeration rejects a device that changed identity while the
  handle opened.
* Onboarding uses the same explicit Continuity descriptor as capture instead
  of a weaker localized-name heuristic.
* The browser lockout is a complete keyboard modal: it has a labelled timer,
  deterministic initial focus, Tab containment, Escape, and focus restoration.
  Non-modal intervention and coach surfaces expose region semantics without
  stealing the user's editing focus. Its countdown now updates the real
  labelled timer node instead of a nonexistent legacy ID.
* Pulse Room glass controls honor `prefers-reduced-transparency`; recent
  activity cards expose list/list-item/link semantics; popup transitions name
  `background-color` rather than animating a shorthand; React tests use the
  supported `act` API. Its ordinary canvas now cancels while the tab is hidden
  and resumes through one idempotent frame owner.
* Desktop and VS Code breathing pacers now honor Reduce Motion with a static,
  countdown-free `Breathe at your pace` frame. The editor owns one cancelable
  animation-frame loop and stops it while its webview is hidden; a
  screen-share-suppressed desktop intervention hides any prior private card
  and owns no hidden presentation timer.
* The full-screen breathing break no longer withholds its exit for 60 seconds.
  `End early` is visible on the first frame, Escape always exits, and the
  overlay plus exit control expose assistive-technology descriptions.
* The desktop intervention footer no longer clips all three escape controls at
  its shipped 460 px width. Concise equal-width labels keep full meanings and
  shortcuts in accessible descriptions/tooltips, with an offscreen text-fit
  regression.
* Desktop documentation now matches the crash-safe implementation: Qt's
  content view is retained and receives a native system tint. It no longer
  claims that an `NSVisualEffectView` is installed after that approach was
  shown to orphan packaged windows.
* Cache-only SHA-1 use was replaced with SHA-256. Legacy persisted MD5 key
  derivations are explicitly marked `usedforsecurity=False`, eliminating
  ambiguous high-severity static-analysis findings without breaking stored
  identities.
* The real Python native-host contract tests have bounded subprocess cleanup
  and a realistic per-test budget, avoiding load-dependent false timeouts.

### Release integrity

* A release tag must resolve to a commit reachable from `origin/main`.
* The tag gate now reruns browser TypeScript, all browser tests, Chrome and
  Edge production bundles, VS Code lint/compile/tests/package, and all three
  dependency-audit policies before any signing job can start.
* GitHub attestations now cover each standalone architecture checksum manifest
  as well as both DMGs and evidence bundles. Public promotion independently
  verifies all six reviewed subjects against the exact tag commit.
* Process-global legacy Qt stubs remain isolated from the parent pytest
  process in both canonical Make targets.

### Verification boundary

* Camera safety is tested with synthetic AVFoundation descriptors and mocked
  OpenCV handles; the refinement does not open a local physical camera.
* The packaged headless probe now returns before onboarding can enumerate
  AVFoundation devices, and the release verifier fails on any camera-discovery
  or camera-open marker. Camera-disabled evidence therefore means zero device
  discovery as well as zero capture.
* Ruff, strict mypy over 523 source files, a verified 285-member wheel, and the
  complete camera-free Python suite (2,703 passed / 3 documented skips) pass.
  Browser TypeScript, 253 tests, and Chrome/Edge MV3 builds pass; VS Code lint,
  compile, 32 tests, and VSIX packaging pass. Schema/config/version/design/link
  contracts, deterministic evaluation, and all dependency policies pass.
* The final exact-commit hosted CI, dual-architecture signed/notarized artifact,
  public-redownload, and clean-machine manual gates remain mandatory before
  this entry can describe a public release.

## [v0.3.11] — 2026-08-26

This patch supersedes the unpublished v0.3.10 candidate after its required
quarantine-aware Finder install, clean shutdown, and second Finder launch
exposed an intermittent AVFoundation stream stall. The app and control plane
remained healthy, but the already-validated camera handle returned no further
frames. v0.3.10 had no reopen path, so it could remain alive while sensing was
offline.

### Fixed

* One dedicated camera thread now owns the complete backend lifecycle: live
  enumeration, open, warmup, configuration, reads, recovery, and release. An
  opened AVFoundation handle is no longer transferred between unrelated worker
  threads after startup.
* Sustained read failure is bounded by both count and elapsed time. This closes
  the real case where each failed AVFoundation read blocked for roughly one
  second and stretched a nominal 30-frame threshold to about 30 seconds.
* A stalled handle is released and reopened through cancellable capped backoff.
  Every attempt re-enumerates current devices and repeats post-open Continuity
  Camera rejection; a new source-instance identity resets downstream temporal
  buffers.
* macOS camera discovery now fails closed when no device identity can be
  enumerated. Cortex no longer probes anonymous indices that cannot pass the
  mandatory identity check and could briefly wake Continuity Camera.
* Known Continuity Camera and unnamed identities are removed before candidate
  generation. Post-open verification no longer trusts a cached pre-open name
  when the live AVFoundation identity cannot be resolved.
* Reopen success is not declared when a handle merely opens. Capture remains
  stale until the replacement source delivers a real frame, and shutdown can
  interrupt backoff/warmup without blocking the event loop.
* `FrameMeta.frame_available` distinguishes a fresh missing observation from a
  fresh pixel frame. Missing scheduler events no longer make
  `capture.frames_flowing` appear true.
* Dynamic stale and recovered transitions now reach the service registry,
  typed runtime status, desktop/WebSocket state, and `/health`. Health exposes
  `capture_stale`, reopen attempts, and frame-confirmed recovery successes.
* Failed-read logging is rate-limited while stall/recovery transitions retain
  structured diagnostics.
* The legacy process-global PySide6 stub suite now runs behind a subprocess
  boundary. Real-Qt accessibility, onboarding, dashboard, overlay, and
  settings tests are no longer dependent on collection order.
* The VS Code extension's previously non-runnable `lint` script now has a
  locked flat ESLint/TypeScript-ESLint configuration. Lint is enforced in both
  pull-request CI and the tag release gate.

### Verification

* Camera-free regression tests prove single-thread backend ownership,
  release/re-enumerate/reopen, retry after a failed verified-device lookup,
  elapsed-time detection, cancellable backoff, responsive blocked-read
  shutdown, stale-until-frame semantics, and honest state/health projection.
* The complete Python suite passes with 2,681 tests and three documented
  dataset/tool-availability skips; the isolated legacy Qt child suite passes
  all 64 tests. Ruff, strict mypy (522 files), browser tests/type/builds, and VS
  Code tests/lint/compile/package are clean.
* No additional local Cortex launch or physical-camera access is performed
  without explicit user permission. The final Finder/live-camera exercise
  remains a mandatory manual publication gate.

## [v0.3.10] — 2026-08-26

This patch supersedes the unpublished v0.3.9 candidate after its required
quarantine-aware Finder mount exposed an incomplete installer layout. The
v0.3.9 ARM artifact was correctly signed, notarized, stapled, checksummed,
attested, and capable of a healthy packaged startup, but its DMG contained only
`Cortex.app`; GitHub's clean runner used the supported `hdiutil` path, where the
optional `create-dmg --app-drop-link` side effect never runs. The candidate was
cancelled before a draft or public release could be staged.

### Fixed

* The DMG staging directory now deterministically contains both `Cortex.app`
  and an `Applications -> /Applications` symlink before image creation. The
  release now uses one built-in `hdiutil` builder locally and in CI; optional
  tool availability can no longer change the installer or its
  signature-preservation behavior.
* DMG volume labels now include the release version. This prevents a prior
  quarantine-aware Finder exercise from colliding with the next candidate's
  exact `/Volumes/.../Cortex.app` identity under macOS System Policy.
* The canonical macOS artifact verifier now mounts the image read-only and
  fails closed unless `Applications` is a symlink whose exact target is
  `/Applications`. A mountable, signed, notarized one-icon DMG can no longer
  pass release verification.
* Release-tooling regression coverage proves builder ordering, accepts the
  correct drag target, and rejects a missing shortcut, a directory masquerading
  as the shortcut, and a link to the wrong destination.

### Release process

* The exact v0.3.9 ARM candidate now serves as a negative regression fixture:
  the strengthened verifier rejects it with `DMG does not contain the required
  Applications symlink`.
* Native `hdiutil` and optional `create-dmg` construction were exercised with
  minimal fixtures while selecting the canonical path; only the built-in
  `hdiutil` path remains in release code. v0.3.10 must still repeat all source,
  dual-architecture signing/notarization, evidence, Finder installation,
  independent review, protected publication, and public-redownload gates.

## [v0.3.9] — 2026-08-26

This patch supersedes the unpublished v0.3.8 candidate after its required real
Finder install exposed a packaged in-process state-contract gap. The app no
longer disappears after launch, and an unavailable camera is now represented
truthfully and recoverably on the desktop surface. The deterministic support
algorithm, evidence thresholds, intervention authority, and privacy semantics
are unchanged.

### Fixed

* Desktop and WebSocket consumers now share one typed `STATE_UPDATE` payload
  builder. The in-process DMG path no longer omits `capture`, `store`, source,
  probability, timestamp, or sequence fields from a separately maintained
  hand-written projection.
* Capture-start failure now reaches in-process desktop subscribers as well as
  browser/editor WebSocket clients. A missing, denied, or restricted camera
  grant renders “Camera offline” instead of the false warm-up message “Reading
  your pulse…”.
* The camera-offline health banner includes a direct, keyboard-accessible
  “Open Settings” recovery action. Users no longer have to discover the Cortex
  menu-bar item before restoring sensing.
* Expected macOS permission states are logged as an explicit degraded hardware
  condition without a misleading exception traceback. Unexpected capture or
  MediaPipe initialization failures retain full error diagnostics.
* Regression coverage now exercises the actual local publisher, asserts
  monotonic public/private sequences, verifies capture-health parity, proves
  the synthetic stale event reaches the packaged bridge, and tests the
  contextual Settings action.

### Release process

* v0.3.8 remains an immutable unpublished candidate with its signed/notarized
  artifacts and evidence preserved. v0.3.9 receives a fresh source tag,
  dual-architecture build, notarization, attestations, Finder install/open/quit
  exercise, manual architecture records, protected publication, and public
  redownload verification; no v0.3.8 artifact is relabeled.

## [v0.3.8] — 2026-08-26

This patch supersedes the immutable v0.3.7 release candidate after its native
arm64 and Intel packaging jobs exposed two independent release-finalization
races. Both artifacts passed Developer ID signing, Apple notarization, and
stapling; the Intel app also reached a version-matched healthy core before its
terminal Qt cleanup race was detected. No v0.3.7 release was published. The
support algorithm, evidence thresholds, intervention authority, and privacy
semantics are unchanged from v0.3.7.

### Fixed

* DMG verification no longer gives a mounted read-only volume to Python's
  recursively deleting `TemporaryDirectory`. A transient `hdiutil detach`
  failure previously caused tempfile cleanup to walk into
  `Cortex.app/Contents/_CodeSignature/CodeResources`, replacing the useful
  detach condition with `[Errno 30] Read-only file system` and failing the
  release after notarization.
* Read-only verifier mounts now have an explicit lifecycle: three bounded
  normal detach attempts, one bounded forced detach for the disposable image,
  a kernel-visible mount-state check, and empty-directory-only cleanup after
  detachment. A volume that remains attached fails closed with the mount path
  and final diagnostic; release code never recursively removes or traverses it.
* Partial `hdiutil attach` failures are also reconciled against actual mount
  state, preventing a failed attach command from leaking a volume.
* The in-process desktop controller now retains exactly one cross-thread daemon
  stop future. `lastWindowClosed` and `aboutToQuit` reuse that lifecycle instead
  of submitting new `CortexDaemon.stop()` wrappers while the background asyncio
  loop is returning from `run_until_complete()`.
* Qt quit is latched before any window closes, preventing the synchronous
  `lastWindowClosed` signal from re-entering the headless or user quit path.
  `aboutToQuit` waits for the original stop future and joins its owner thread
  within one shared 20-second budget. A loop that rejects submission has its
  unscheduled coroutine explicitly closed, eliminating both pending-task and
  never-awaited-coroutine teardown warnings.
* Regression tests cover transient-busy recovery, successful unmount despite a
  stale nonzero tool status, permanent forced-detach failure, and refusal to
  touch a mounted `CodeResources` fixture. Controller tests cover single stop
  submission, loop-close rejection, synchronous window-close re-entry, reuse of
  a completed stop, bounded owner-thread join, and idempotent `aboutToQuit`.
* The patched verifier completed three consecutive full passes against the
  existing signed, notarized, and stapled v0.3.6 arm64 DMG. Every pass mounted
  read-only, verified the deep signature and bundle contract, ran both frozen
  smoke paths including a version-matched healthy daemon, detached cleanly,
  and left no mounted image behind.
* The corrected source desktop completed a full isolated headless
  start/version-matched-health/SIGTERM/normal-exit exercise with zero residual
  HTTP/WebSocket listeners or processes and without pending-task,
  never-awaited-coroutine, shutdown-timeout, or startup-failure diagnostics.
* A freshly frozen, deep ad-hoc-signed v0.3.8 arm64 app then completed four
  consecutive mounted-DMG verification passes. Each pass returned healthy
  version 0.3.8, exited normally after SIGTERM, detached with status zero, and
  left no app process, listener, or verifier mount. Production Developer ID and
  notarization proof remains owned by the clean tagged release workflow.

### Release process

* v0.3.7 remains an immutable failed candidate tag for incident traceability;
  its GitHub Actions evidence is not rewritten or rerun as another release.
* v0.3.8 must independently pass the locked source gate, native arm64 and
  x86_64 signed/notarized/stapled packaged gates, provenance and checksum gates,
  real downloaded-DMG Finder install/open/quit/relaunch validation, independent
  manual evidence review, protected publication, and unauthenticated public
  redownload validation before it can be called released.

## [v0.3.7] — 2026-08-26

This patch closes the first-launch readiness defect found during the required
Finder exercise of the v0.3.6 candidate. The deterministic support algorithm,
weights, evidence gates, intervention authority, and privacy semantics are
unchanged.

### Fixed

* Camera authorization is no longer requested from the daemon's synchronous
  startup path. An unresolved macOS TCC prompt previously blocked the daemon
  loop for exactly 60 seconds, delaying HTTP, WebSocket, and graceful quit.
  Camera prompts remain explicit user gestures in onboarding or Settings.
* Core readiness is now independent of optional hardware. Durable storage,
  WebSocket, the bound HTTP server, recovery, and coordinators become ready
  before the supervised camera task begins; camera failure produces an honest
  stale-capture state while telemetry-first operation remains available.
* The desktop now displays `Starting…` until the daemon's real readiness
  boundary is crossed. It no longer reports `Connected` merely because a
  Python thread is alive.
* OpenCV enumeration/warmup and all MediaPipe create/inference/close calls run
  off the daemon event loop. MediaPipe operations share one owned worker so
  the Intel 0.10.21 TaskRunner and newer serial-dispatcher wheels have the same
  lifecycle guarantee. Camera open is cooperatively cancellable, stop always
  releases partial resources, and permission is checked before model loading.
* A camera grant observed live in onboarding or Settings now retries capture
  in-process. Both camera actions perform the native non-blocking request from
  the user's click without foregrounding another app over the first TCC prompt;
  System Settings is used only for the denied/restricted recovery path. A
  relaunch is no longer required after granting access.
* A daemon-thread startup failure is surfaced in the existing error UI and
  startup log instead of leaving a misleading connected dashboard.
* Development-mode shutdown now joins whichever lifecycle actually receives
  SIGINT/SIGTERM. The daemon and outer wrapper previously installed competing
  asyncio signal handlers, so one Ctrl+C could stop uvicorn while stranding the
  wrapper, WebSocket, camera, MediaPipe, and database until a forced kill.
  Signal-owned uvicorn completion is also classified as expected rather than a
  false critical-service crash.
* Added regression coverage for non-blocking unresolved TCC state, skipped
  OpenCV work without authority, cancellable camera opening, core readiness
  during pending capture, truthful desktop lifecycle state, single-shot live
  permission retry, single-worker MediaPipe ownership, and
  settings/onboarding permission behavior. The source runtime has also passed
  two consecutive authorized-camera start/health/single-signal-stop cycles,
  including MediaPipe release and zero residual listeners/processes.

### Release process

* v0.3.6 remains an unchanged, unpublished draft with its signed artifacts and
  complete validation evidence preserved for incident traceability.
* v0.3.7 must pass the dual-architecture source and packaged gates, then a real
  downloaded-DMG Finder install, ejected-volume launch, visible-window and
  version-matched health check, normal quit, relaunch, and orphan/port cleanup
  check before publication. After publication, the public unauthenticated DMG
  is downloaded and the host-architecture install/open/quit check is repeated.

## [v0.3.6] — 2026-08-26

This patch closes the packaged-startup failure found in the v0.3.5 candidate.
The deterministic support scoring rules, weights, evidence gates, intervention
authority, and privacy behavior are unchanged. The model metadata version is
`2.1.1` because its implementation-identity mechanism is now an explicit
generated contract rather than a runtime source-file read.

### Fixed

* The model registry no longer reads `rule_scorer.py` from the filesystem at
  application startup. PyInstaller imports Python modules from its embedded
  archive, so the source path represented by `__file__` was not a materialized
  file in Cortex.app. The resulting `FileNotFoundError` occurred before the Qt
  event loop, making the Dock icon bounce briefly and disappear.
* Deterministic support provenance is now generated from a canonical,
  path-independent manifest of the reviewed feature schema, registry, scorer,
  and inference boundary. The committed generated module works identically in
  source and frozen execution.
* Added repository-contract, CI, and pre-commit drift gates. Editing a hashed
  algorithm component without regenerating its identity now fails closed.
* PyInstaller now enters through a minimal standard-library bootstrap. It
  installs a bounded private log at `~/Library/Logs/Cortex/startup.log`, writes
  the latest full traceback to `last-startup-error.txt`, and presents an
  actionable fatal dialog instead of silently disappearing when startup fails.
* Startup stages are logged across storage, Qt, desktop-surface construction,
  daemon composition, daemon-thread start, and Qt event-loop readiness.
* Corrected an extra closing brace in every sensitivity-slider QSS rule and
  established the native macOS system font at the application boundary. The
  packaged verifier now rejects stylesheet parse failures and missing-font
  fallbacks instead of allowing visually degraded controls to ship.
* Bundled and startup-validated the Roman and italic Cormorant Garamond 4.001
  variable faces from a pinned Google Fonts commit, including their OFL 1.1
  license and source digests. The desktop mono token now prefers macOS-shipped
  Menlo instead of assuming the optional developer font SF Mono is installed.
* Declared the modern Continuity Camera device type in the app metadata. Cortex
  continues to prefer and re-verify the built-in Mac camera, while avoiding the
  deprecated AVFoundation compatibility path during device enumeration.
* The frozen resource smoke now constructs the real support registry, scorer,
  and inference boundary and verifies the generated identity. This directly
  covers the code path v0.3.5 omitted.
* Mounted-DMG verification now launches the complete packaged app in an
  isolated HOME, forces camera-unavailable degradation, waits for a healthy
  version-matched HTTP service graph containing the support registry, and then
  verifies bounded process termination. Resource presence alone can no longer
  qualify a DMG.
* Installation guidance now explicitly requires replacing an older copy in
  `/Applications`, ejecting the DMG, and launching the installed copy, avoiding
  ambiguity between a stale Dock item and a newly downloaded mounted app.

### Release process

* v0.3.5 is retained unchanged as a quarantined draft candidate with its
  artifacts and evidence preserved for incident traceability. It must not be
  republished or have assets replaced.
* v0.3.6 requires both architecture release jobs, signing, Apple acceptance,
  stapling, Gatekeeper assessment, frozen resource/composition smoke, packaged
  health/termination probe, and a clean-profile Finder download/install/open
  exercise before publication.

## [v0.3.5] — 2026-08-26

This patch preserves clean-checkout release provenance while the macOS builder
stages its key-free bundled environment. It contains no sensing or inference-
algorithm, authority, privacy, or interaction-policy change from v0.3.0.

### Fixed

* The scrubbed environment that PyInstaller consumes is now created in the
  runner temporary directory rather than as an untracked `.env.bundled` file
  inside the checkout. The ignored `.env` build input remains the only
  temporary repository-path projection and is removed by the existing exit
  cleanup.
* A developer's original `.env`, when present, is also backed up outside the
  checkout. Production evidence generation can therefore require a genuinely
  clean tree before exit cleanup without ignoring or special-casing any
  build-created provenance input.
* Added a release-tooling contract that rejects repository-local scrubbed or
  backup environment paths and keeps the fail-closed `--require-clean` gate.
* Mounted deep signature verification now has a command-specific, bounded
  five-minute budget. The generic 60-second subprocess limit was insufficient
  for the thousands of nested members in the Intel app on hosted runners and
  incorrectly reported a timeout after signing, notarization, and stapling had
  all succeeded.

### Release process

* The immutable v0.3.4 tag is retained as an artifact-free candidate. Its
  arm64 DMG was signed, accepted by Apple, stapled, accepted by Gatekeeper, and
  passed mounted frozen-artifact verification before provenance correctly
  rejected the still-present untracked `.env.bundled` staging file. Its Intel
  DMG was signed, accepted, and stapled before mounted deep signature
  verification exceeded the generic 60-second command budget. v0.3.5
  supersedes both failures; no v0.3.4 tag or artifact is replaced.

## [v0.3.4] — 2026-08-25

This patch signs and verifies the outer macOS disk-image container before
notarization. It contains no sensing or inference-algorithm, authority,
privacy, or interaction-policy change from v0.3.0.

### Fixed

* The production builder now signs the finished DMG with the Developer ID
  Application identity, a secure Apple timestamp, and the stable
  `com.cortex.daemon.dmg` identifier before submitting that exact artifact to
  Apple. This implements Apple's inside-out container-signing guidance and
  gives Gatekeeper a usable primary signature for the disk image itself.
* DMG signature verification and signature metadata are captured before
  notarization. The mounted-artifact verifier independently checks the outer
  signature again after stapling, before ticket validation and Gatekeeper
  assessment.
* Added a release-tooling ordering contract that requires DMG integrity
  verification, production-only outer signing, secure timestamping, a stable
  identifier, signature verification, and only then notary submission.
* Release guidance now makes the outer signature an explicit producer and
  consumer invariant, including standalone `codesign` verification commands.

### Release process

* The immutable v0.3.3 tag is retained as the artifact-free candidate whose
  arm64 and Intel DMGs were independently signed at the app layer, accepted by
  Apple, stapled, and then rejected by the final Gatekeeper DMG assessment with
  `source=no usable signature`. v0.3.4 supersedes it.

## [v0.3.3] — 2026-08-25

This patch makes the post-notarization bundle scanner distinguish genuine
secrets from official provider fixtures and opaque native-library bytes, and
makes receipt acknowledgement ordering explicit. It contains no sensing or
inference-algorithm, authority, privacy, or interaction-policy change from
v0.3.0.

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
* Browser transaction-state handling now awaits durable receipt-outbox
  acknowledgement before settling and broadcasting the acknowledged state.
  Observers of that state can no longer see receipts that the daemon has
  already acknowledged.
* The WebSocket test transport exposes an awaitable delivery boundary, and the
  exact intervention apply/restore suite now synchronizes on handler
  completion instead of fixed-duration sleeps. Negative mutation assertions
  therefore execute after rejection has finished, not before a delayed effect.
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
