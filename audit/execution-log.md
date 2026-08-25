# Redesign execution log

This log maps the implementation work packages in
[IMPLEMENTATION.md](../IMPLEMENTATION.md) to immutable commits. Test totals are
evidence from the named commit/working tree, not permanent README promises.

| Work package | Commit | Result |
| --- | --- | --- |
| WP0 — containment and build contracts | `ca28125` | Native auth, suggest-only, consent exactness, proposal purity, version/release gates |
| WP1 — clocks and observations | `286f20c` | Dual-clock contracts, observation/missingness semantics, schema fixtures |
| WP2 — capture and kinematics | `8f28507` | Time-correct capture, explicit missingness, kinematic quality gates |
| WP3 — physiology | `be959c4` | Unique beat timeline, gated pulse pipeline, unsupported metrics unavailable |
| WP4 — support inference | `017c3f4` | Evidence-normalized deterministic inference, unknown state, model cards |
| WP5 — intervention transaction | `934ef4d` | Manifest → authorization → receipt → verify → restore |
| WP6 — local authority | `4e6a834` | Transactional intervention ownership and fault contracts |
| WP7 — persistence | `6266e12` | SQLite authority, checksummed migrations, recovery/export/delete |
| WP8 — policy/evaluation | `27c05b6` | Deterministic production policy and fixed MRT research path |
| WP9 — privacy/UI | `9fb7a9d` | Exact redacted preview, one-time send, minimized browser permissions, polished surfaces |
| WP10 — application architecture | `2e8fd93` | Kernel/coordinators, task ownership, browser/desktop decomposition |
| WP11 — validation/release/docs | `47cf7dc` | Universal lock, dataset provenance, repository contracts, draft/promotion evidence gates, SBOM/attestation/notarization tooling, ADRs and limitations |

## WP11 verification record

The `47cf7dc` source tree passed the canonical gate on Python 3.11.15 and
3.12.13: Ruff, strict mypy over 510 source files, a verified 281-file wheel,
2,548 non-Qt tests with 3 declared skips, and 62 isolated Qt tests. It also
passed a clean Node 22.23.2/pnpm 9.15.9 browser install, TypeScript, 248 Vitest
tests, Chrome and Edge MV3 builds; clean Node 22.23.2 editor install, compile,
30 Jest tests and VSIX packaging; Python/editor zero-finding audits; the
path- and expiry-bound browser advisory policy; schema/design/version/config
contracts; source release smoke; regression replay; ShellCheck; actionlint;
and 216 Markdown link checks with no errors.

Both local Python executions used arm64 hardware. The committed CI/release
matrix assigns Python 3.12.13 to `macos-15-intel` and asserts `x86_64`; that
runner result was still a remote release/CI gate at `47cf7dc`, not locally
fabricated evidence.

The signed/notarized installation matrix is intentionally not marked executed
in this source log. Each release candidate must attach its own credentialed,
hardware-specific record using the templates under
[`docs/release/`](../docs/release/README.md).

## Post-WP11 portability and evidence closure

| Commit | Result |
| --- | --- |
| `1828741` | Bound the WP11 verification record to its immutable source commit |
| `d529761` | Replaced the pnpm action version shim, made architecture-sensitive dependency/test paths portable, and kept release evidence optional-tool safe |
| `d756645` | Added a fail-closed, architecture-scoped Intel Protobuf exception policy with expiry, source-boundary checks, and a real bundled-model smoke |
| `94b9195` | Made the numerical path valid and strictly typed across NumPy 1.26 and 2.3, including SciPy trapezoidal integration and explicit OpenCV/array dtype boundaries |
| `59e5d17` | Made wheel inputs VCS-aware and deterministic, with independent rejection of local logs, state databases, environment files, interpreter debris, and credential containers |
| `7ffb97f` | Replaced an architecture-sensitive synthetic capture timer with acquisition-free median batches and an exact production call-graph conversion contract |

The wheel correction follows
[Hatch's documented file-selection semantics](https://hatch.pypa.io/dev/config/build/):
ordinary explicit selection respects VCS ignores and gives `exclude` precedence,
whereas forced directory inclusion recursively includes its contents. This
distinction mattered because a Git-ignored native-host debug log existed in the
developer workspace. The old force-inclusion map admitted it locally, producing
281 members while clean hosted builders produced 280. The final source rewrite
and verifier produce the same 280-member wheel locally and on both native hosted
architectures.

### Hosted diagnostic sequence

The failed runs below are retained as diagnostic evidence rather than hidden:

1. [Run 32851889271](https://github.com/StevenWang-CY/cortex/actions/runs/32851889271)
   at `1828741` exposed three clean-run assumptions: the pnpm action selected a
   newer parser incompatible with the committed pnpm-9 lock, the dependency
   graph requested a MediaPipe release without an Intel wheel, and macOS
   camera/release tests leaked host-specific optional-tool assumptions.
2. [Run 32859665250](https://github.com/StevenWang-CY/cortex/actions/runs/32859665250)
   at `d529761` passed six jobs and then correctly surfaced
   `PYSEC-2026-1805` in the last Intel-compatible Protobuf graph. It was not
   suppressed globally: `d756645` introduced an exact package/advisory/path,
   severity, architecture, reason, mitigation, and 2026-09-22 expiry contract.
3. [Run 32861740878](https://github.com/StevenWang-CY/cortex/actions/runs/32861740878)
   at `d756645` proved the scoped audit policy but exposed 33 NumPy-1.26 typing
   errors in the Intel canonical gate. Exact local Intel reproduction also
   established that the NumPy-2-only `np.trapezoid` runtime call was unavailable.
4. [Run 32864342288](https://github.com/StevenWang-CY/cortex/actions/runs/32864342288)
   at `94b9195` passed all seven jobs on both native architectures. Comparing its
   280-file hosted wheels with the 281-file local wheel then exposed the ignored
   debug-log inclusion described above.
5. [Run 32866469753](https://github.com/StevenWang-CY/cortex/actions/runs/32866469753)
   at `59e5d17a5d7fd4747395117f58a0c787cc6309c6` is the immutable runtime-source proof: all
   seven jobs passed, including dependency policy, repository/link/schema
   contracts, replay regression, browser and editor gates, and both native
   Python architecture rows.
6. [Run 32869260778](https://github.com/StevenWang-CY/cortex/actions/runs/32869260778)
   at `a58fdce7cc89ea91eb47f0a04c227402d84b47da` passed every non-Python job and
   the complete arm64 row, then exposed a test-validity defect in the Intel
   row. The synthetic capture benchmark spent its timed region generating
   1000 random 640x480 frames—about 922 MB of PRNG/allocation work that is not
   performed by the production capture pipeline. The runtime tests themselves
   reached 99%; the only failure was 5.54 seconds against a 5-second synthetic
   threshold (`2559 passed, 3 skipped, 1 failed`).

`7ffb97f` fixes the measurement rather than merely suppressing the result:
frame fixtures are generated before timing, OpenCV/NumPy dispatch is warmed,
five 100-frame samples are reduced by their median, and the synthetic stages
retain an 8 ms/frame ceiling (less than one quarter of a 30 Hz frame interval).
A separate test observes the real `CapturePipeline._process_frame` call graph
and requires exactly one BGR→RGB and one BGR→gray conversion, while the existing
subsample-invocation assertion remains independent of timing. The exact local
Rosetta x86_64/Python 3.12.13 graph passed all four targeted capture tests; the
full native arm64 gate passed Ruff, strict mypy over 511 files, the unchanged
280-file wheel, 2,561 non-Qt tests with 3 declared skips, and all 62 Qt tests.
The wheel remained byte-identical because this correction changes tests only.

### Final software evidence

- arm64/Python 3.11.15 resolved MediaPipe 0.10.35, NumPy 2.3.5, and Protobuf
  7.36.0; the architecture audit found zero known vulnerabilities.
- x86_64/Python 3.12.13 resolved MediaPipe 0.10.21, NumPy 1.26.4, and Protobuf
  4.25.9; its one finding matched only the reviewed `PYSEC-2026-1805` exception,
  and the exception verifier reported `pass` after the real model/source-boundary
  tests ran in the canonical gate.
- Each native row passed Ruff, strict mypy over 511 source files, a 280-file
  wheel inspection, 2,560 non-Qt tests with 3 declared skips, and 62 isolated
  Qt tests. Both builders produced the byte-identical wheel SHA-256
  `eee56574ab13837cad507719e7a6f38c015ee2131f553c7501a3d73b60154a97`.
- Browser TypeScript, 248 Vitest tests, Chrome/Edge MV3 production builds,
  editor compile/30 Jest tests/VSIX packaging, all four replay-regression
  metrics, schema drift, configuration/version/design-token contracts, and
  Markdown link checks passed in the same immutable run.

### Evidence that remains external

No source or hosted CI run is represented as a signed release candidate. Apple
Developer ID signing/notarization credentials, the clean-profile arm64/Intel
14-case TCC/camera/browser/editor/stop/restore matrix, independent release
review, reference-sensor/participant validation, and independent statistical
review remain mandatory external gates. The Intel Protobuf exception must be
replaced by a patched compatible backend or Intel support must be removed before
its 2026-09-22 expiry; expiry causes CI/release failure by design.
