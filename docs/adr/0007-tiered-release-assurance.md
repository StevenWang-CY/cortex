# ADR 0007: Tiered release assurance for a solo-maintained project

- Status: Accepted
- Date: 2026-09-04
- Supersedes: the implied publication rule in [ADR 0006](0006-traceable-macos-release.md)
  that a public release requires two clean-hardware records signed by a builder
  and a globally distinct independent reviewer.

## Context

ADR 0006 made the automated release chain strong: locked inputs, dual
architectures, Developer ID signing, notarization, stapling, mounted smoke
probes, SBOMs, checksums, and GitHub attestations. Its publication gate,
however, could only be satisfied by two people with two clean Macs. Cortex has
one maintainer. The observable result was the opposite of the intent: every
candidate from v0.3.5 to v0.3.15 remained a GitHub draft, the only "Latest"
release was published by hand outside the gate, and users kept downloading a
build that predated the native-bridge repair. A gate nobody can pass protects
no one.

Two shipped regressions (v0.3.14's non-executable native host and v0.3.15's
background-only bundle) also showed which on-device cases actually catch
problems: artifact identity, installation and launch, the browser bridge,
camera/TCC lifecycle, and clean uninstall.

## Decision

Publication carries an explicit **assurance tier** that is validated by the
promotion workflow, written into `release-promotion-validation.json`, and
rendered into the public release notes.

`self-attested`
: The maintainer exercised at least one architecture on a clean profile using
the exact CI-built, notarized DMG. The core cases (`artifact.identity`,
`install.launch`, `browser.chrome_native`, `runtime.lifecycle_camera_tcc`,
`uninstall.cleanup`) must pass on hardware. Every other catalogued case is
recorded as `passed` or `not_run` with a stated reason. Architectures without a
hardware record are labelled *CI-verified only* in the notes. This tier is
sufficient for a normal (non-prerelease) GitHub release because the release
notes say exactly what was and was not exercised.

`independently-reviewed`
: Both architectures carry a record, every case passed, and the builder and
independent-reviewer identities are disjoint. This is the ADR 0006 bar, kept
as an upgrade path and rendered as such.

The automated chain is unchanged and mandatory for both tiers. The tier is a
statement about human evidence, never a substitute for automation. A record
that claims `passed` without an uploaded evidence asset, a `failed` case, or a
missing core case still blocks promotion.

## Consequences

- The maintainer can publish honestly without pretending to be two people.
- Users can tell, from the release page alone, whether a build was exercised on
  hardware, on which architecture, and by whom.
- Protected-environment approval remains a maintainer approval and is
  described as such; it is not represented as independent review.
- The schema moves to version 1.1 (`assurance_tier`, `maintainer` role,
  `not_run` results with reasons). Version 1.0 records are not accepted because
  none were ever produced.

Evidence: `cortex/scripts/validate_release_records.py`, `.github/workflows/publish-release.yml`,
[release guide](../release/README.md), [validation protocol](../release/manual-release-validation.md).
