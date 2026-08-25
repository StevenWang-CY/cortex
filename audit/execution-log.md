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
runner result remains a remote release/CI gate, not locally fabricated evidence.

The signed/notarized installation matrix is intentionally not marked executed
in this source log. Each release candidate must attach its own credentialed,
hardware-specific record using the templates under
[`docs/release/`](../docs/release/README.md).
