# macOS release and evidence guide

Only a credentialed release workflow may produce a distributable Cortex DMG.
Local `make dmg` output is an ad-hoc development artifact unless every required
verification below is present.

## Reproducible inputs

- Python: 184-package `cortex/uv.lock`, uv 0.10.12, `uv sync --locked`.
- Browser: Node 22.23.2, pnpm 9.15.9, `pnpm-lock.yaml`, frozen install.
- VS Code: Node 22.23.2, `package-lock.json`, `npm ci`.
- Version: `cortex/pyproject.toml`, projected by `sync_versions`.
- Platforms: clean macOS arm64/Python 3.11.15 and
  x86_64/Python 3.12.13 builders.

The lock explicitly requires wheels for both macOS architectures. MediaPipe
stopped publishing Intel wheels after 0.10.21, so x86_64 uses that release and
its NumPy 1.x-compatible dependency branch; arm64 and Linux use the maintained
MediaPipe/NumPy branch. Both are subjected to the same source gate, and the
artifact metadata records the resolved architecture-specific graph. The lock
uses `opencv-contrib-python<5` as the sole `cv2` provider on both architectures;
the repository gate rejects co-installing the overlapping base OpenCV wheel.

The locks make resolution repeatable. Apple signing/notarization timestamps and
DMG metadata mean the final archive is not promised to be byte-for-byte
reproducible. It is traceable to locked inputs, a commit/tag, builder identity,
artifact hashes, signatures, notarization, SBOMs, and GitHub attestations.

## Credential boundary

The protected GitHub `production-release` environment must provide a Developer
ID Application certificate, its password, a temporary-Keychain password, App
Store Connect notary API key, key ID/issuer, and exact
`CORTEX_SIGN_IDENTITY`. Secrets are decoded only into the runner temporary
directory, imported into a temporary Keychain, removed after import, and the
Keychain is deleted in an `always()` cleanup step.

The separate protected `production-publish` environment controls public
promotion. Its required approver must inspect the real-device evidence and must
not be an artifact builder. This separates possession of Apple credentials from
authority to publish a candidate.

`CORTEX_REQUIRE_NOTARIZATION=1` makes the build fail before compilation if an
identity or notary profile is missing. Ad-hoc signing never enables hardened
runtime; Developer ID release signing does.

## Automated release path

For each architecture, the tag workflow:

1. checks tag/version, generated schemas/docs/tokens, links, config keys, and
   the full Python/browser/editor gates from committed locks;
2. builds Chrome, Edge, VSIX, then PyInstaller `Cortex.app`;
3. signs the app with hardened runtime and verifies the signature;
4. creates `Cortex-<version>-macos-<arch>.dmg`;
5. submits to Apple with `notarytool --wait`, requires `Accepted`, captures the
   request log, staples, and validates the ticket;
6. verifies DMG integrity, mounts read-only, validates bundle ID/version/minimum
   OS/single architecture/signature, scans credentials and absolute
   home-directory markers, and runs `Cortex --release-smoke` before any
   UI/network/camera;
7. generates application SPDX and locked-Python CycloneDX SBOMs,
   architecture-specific `SHA256SUMS-<arch>`, `release-metadata.json`, and
   command evidence;
8. creates GitHub SLSA provenance and SBOM attestations for the DMG;
9. packages each evidence directory as an architecture-specific ZIP and stages
   both DMGs, checksum files, and evidence bundles in a GitHub draft release
   only after both matrix jobs pass.

Run the same artifact verifier locally:

```bash
uv run --project cortex --locked python -m cortex.scripts.verify_macos_release \
  dist/Cortex-0.2.2-macos-arm64.dmg \
  --expected-arch arm64 \
  --require-notarized \
  --output dist/evidence-arm64/release-verification.json
```

## Consumer verification

After downloading an artifact, its `SHA256SUMS-<arch>`, and its
architecture-specific evidence ZIP, extract the ZIP and place the DMG beside
the checksum file before running:

```bash
shasum -a 256 -c SHA256SUMS-arm64
gh attestation verify Cortex-0.2.2-macos-arm64.dmg \
  --repo StevenWang-CY/cortex
xcrun stapler validate Cortex-0.2.2-macos-arm64.dmg
spctl -a -vv --type open --context context:primary-signature \
  Cortex-0.2.2-macos-arm64.dmg
```

The checksum file covers the DMG, metadata, SBOMs, verifier output, and command
evidence. Verify only the DMG line when you intentionally did not download the
full evidence bundle.

## Required real-device release-candidate pass

Automation cannot grant TCC permissions, judge onboarding copy, fully restart a
real browser profile, or prove camera ownership after a crash. Before public
promotion, execute [manual-release-validation.md](manual-release-validation.md)
on at least one clean supported arm64 Mac and one clean supported Intel Mac,
using the signed/notarized candidates staged by CI. Upload exactly
`manual-release-evidence-arm64.json` and
`manual-release-evidence-x86_64.json`, plus every non-sensitive evidence file
they reference, to the same draft release. An unexecuted template is not
passing evidence.

Dispatch **Publish validated macOS release** only after those uploads. The
promotion workflow revalidates the tag, commit, version, artifact names and
hashes, both host architectures, the fixed 14-case catalog, evidence-file
existence, globally disjoint builder/reviewer identities, checksum coverage,
and GitHub provenance attestations. Only then can a protected-environment
approver publish the draft.
