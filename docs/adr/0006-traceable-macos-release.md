# ADR 0006: Locked, signed, notarized, attestable macOS releases

- Status: Accepted
- Date: 2026-08-25

## Context

Broad Python ranges and permissive release signing made a successful tag
insufficient evidence that users received the reviewed dependency graph or a
Gatekeeper-valid artifact.

## Decision

The committed universal `uv.lock`, pnpm lock, npm lock, Node 22.23.2, pnpm
9.15.9, Python 3.11.15/3.12.13, and uv 0.10.12 define build inputs. Release builds run on arm64 and x86_64 macOS,
require Developer ID credentials, hardened runtime, Apple notarization and
stapling, mount and smoke-test the DMG, generate SPDX/CycloneDX SBOMs,
checksums and machine-readable verification, and publish GitHub provenance and
SBOM attestations.

## Consequences

Ad-hoc builds remain developer artifacts and cannot enter the release
workflow. Code-signing timestamps prevent a promise of byte-for-byte identical
DMGs; releases are instead repeatable from locked inputs and traceable to a
commit, builder, signature, notarization result, hashes, and attestations.

Evidence: [release guide](../release/README.md), release workflow and verifier.
