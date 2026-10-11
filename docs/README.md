# Cortex documentation

Cortex is a macOS research prototype with local behavioral support inference,
optional experimental camera estimates and separately authorized workspace
effects. The default planner is local and execution defaults to `suggest_only`.

The [0.5.1 validation record](release/0.5.1-validation.md) is the current review
entry point. The [October implementation record](implementation-2026-10.md)
preserves the preceding `0.5.0` work. Both distinguish implemented behavior
from outstanding release and research requirements. A local build is not a
published or notarized release.

## Use and develop Cortex

| Need | Guide |
| --- | --- |
| Product overview and screenshots | [Main README](../README.md) |
| Install, configure and run | [Setup](../cortex/docs/setup.md) |
| Source layout and developer commands | [Package README](../cortex/README.md), [contributing](../CONTRIBUTING.md) |
| Settings and environment fields | [Generated configuration reference](../cortex/docs/configuration-reference.md) |
| Optional external planner and credentials | [Provider setup](../cortex/docs/deploy_anthropic.md) |
| Recover from connection, camera or shutdown problems | [Troubleshooting](../wiki/Troubleshooting.md), [support](../SUPPORT.md) |

## Understand the implementation

| Area | Authoritative documentation |
| --- | --- |
| Runtime ownership, services and clients | [Architecture](../cortex/docs/architecture.md), [data flow](data-flow.md) |
| Behavioral inference and abstention | [Deterministic support model](model-cards/deterministic-support-v2.md), [safety fallback](model-cards/no-inference-safety-fallback.md) |
| Baselines and sensor calibration | [Calibration](../cortex/docs/calibration.md) |
| HTTP, WebSocket and client commands | [API reference](../cortex/docs/apis.md) |
| Effect authorization, receipts and restoration | [Adapters](../cortex/docs/adapters.md) |
| Typography, motion, review, freshness and keyboard behavior | [UI contract](../cortex/docs/ui-design.md) |
| Local data, external disclosure and threat boundaries | [Privacy](../cortex/docs/privacy.md), [security model](../cortex/docs/security.md), [reporting policy](../SECURITY.md) |

Production inference uses observed input/window behavior; camera estimates are
diagnostic-only. Missing observations must not become evidence of inactivity,
and missing time cannot accumulate inference dwell. A proposal or model response
cannot grant authority to change the workspace. The linked guides explain these
boundaries and their regression checks.

## Evaluate and release

- [Known limitations](limitations.md) separates implemented contracts from
  unestablished accuracy, usability and platform claims.
- [Support-inference study protocol](research/support-inference-study-protocol.md)
  defines prospective evaluation; [policy evaluation](../cortex/docs/research/policy-evaluation-protocol.md)
  separates deterministic production behavior from micro-randomized research.
- [Release guide](release/README.md) distinguishes local development artifacts
  from distribution assurance; [manual validation](release/manual-release-validation.md)
  defines the hardware and interaction checks that automated probes cannot prove.
- [Architecture decisions](adr/README.md) preserve accepted decisions with
  links to current implementation qualifications.

## History and documentation maintenance

The [changelog](../CHANGELOG.md), [original implementation ledger](../IMPLEMENTATION.md),
[finding ledger](../audit/findings.md), [execution log](../audit/execution-log.md)
and [plan index](../plans/README.md) retain historical evidence. Old test totals
and commit-bound results must not be read as current verification. The
[local wiki](../wiki/Home.md) is maintained alongside the canonical guides;
editing it does not publish the separate GitHub Wiki repository.

When behavior changes, update the corresponding guide, relevant model/UI/API
contract and changelog together. Regenerate configuration, schema, model-identity
or design-token outputs from their source instead of hand-editing generated
files. Run `cortex/.venv/bin/python -m cortex.scripts.verify_repository_contracts`
and `git diff --check` for documentation consistency; source changes also need
their applicable test and build gates.
