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

MediaPipe 0.10.21 also caps Intel Protobuf below 5. The resulting
`PYSEC-2026-1805` finding concerns `google.protobuf.json_format.ParseDict` on
attacker-controlled nested `Any` dictionaries. Neither Cortex nor the installed
MediaPipe Python sources import that boundary, and the product accepts no
Protobuf/JSON model graphs from clients. The reviewed Intel-only exception was
re-reviewed on 2026-09-04 (no patched Intel wheel exists), expires on
2026-10-15, is revalidated on every architecture build, and is included in
release evidence alongside the raw audit. Each renewal must record the review
date and the upstream check in the exception itself; a compatible patched
backend or retirement of the Intel artifact to CI-only status is the decision
due before that date. Silently extending the exception is not permitted.

The locks make resolution repeatable. Apple signing/notarization timestamps and
DMG metadata mean the final archive is not promised to be byte-for-byte
reproducible. It is traceable to locked inputs, a commit/tag, builder identity,
artifact hashes, signatures, notarization, SBOMs, and GitHub attestations.

## Credential boundary

The protected GitHub `production-release` environment must provide a Developer
ID Application certificate, its password, a temporary-Keychain password, exact
`CORTEX_SIGN_IDENTITY`, and exactly one complete Apple notarization credential
set:

- App Store Connect API key (`APPLE_NOTARY_KEY_P8_BASE64`, key ID, and issuer
  ID); or
- Apple ID (`APPLE_ID_USERNAME`, app-specific password, and team ID).

Partial or mixed credential sets fail before a Keychain is created. Secrets are
decoded only into the runner temporary directory, imported into a temporary
Keychain, removed after import even when credential validation fails, and the
Keychain is deleted in an `always()` cleanup step.

The scrubbed key-free environment consumed by PyInstaller and any developer
environment backup are also staged in the runner temporary directory, outside
the Git checkout. Only the ignored `.env` projection exists briefly at the
path expected by the bundle spec. This keeps the repository genuinely clean
when provenance is generated before shell exit cleanup; the clean-tree gate is
not weakened with an ignore or exception for build-created inputs.

The separate protected `production-publish` environment controls public
promotion. Its approval is recorded as a maintainer approval of the declared
assurance tier; it does not claim independence from the builder. Possession of
Apple credentials (the release environment) and authority to publish (the
publish environment) remain separate environments.

`CORTEX_REQUIRE_NOTARIZATION=1` makes the build fail before compilation if an
identity or notary profile is missing. Ad-hoc signing never enables hardened
runtime; Developer ID release signing does.

## Automated release path

The container order follows Apple's
[macOS distribution-packaging guidance](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution):
sign code and each signable nested container from the inside out, then notarize
the outermost artifact that users receive.

For each architecture, the tag workflow:

1. checks tag/version, generated schemas/docs/tokens, links, config keys, and
   the full Python/browser/editor gates from committed locks;
2. builds Chrome, Edge, VSIX, then PyInstaller `Cortex.app` with the dedicated
   console-capable `CortexNativeHost` entry point;
3. signs every nested library and framework with the hardened runtime and no
   entitlements, signs the native host explicitly, then seals the outer app
   with the camera/automation/input-monitoring entitlements; verifies nested
   and outer signatures; submits the zipped bundle to Apple, requires
   `Accepted`, and staples the application itself so a dragged-out copy
   launches offline;
4. creates `Cortex-<version>-macos-<arch>.dmg` from the stapled bundle, signs
   that outer disk image with the same Developer ID Application identity and a
   secure timestamp, and verifies the DMG signature before upload;
5. submits the signed DMG to Apple with `notarytool --wait --timeout`, requires
   `Accepted`, captures the request log, staples, and validates the ticket;
6. verifies DMG integrity, mounts read-only, validates bundle ID/version/minimum
   OS/single architecture/signature, rejects a background-only or UI-element
   GUI bundle, validates the stapled ticket on the bundle inside the image,
   scans generic credential forms only in text-like members, scans exact
   exported secrets and actual non-generic build home roots across all bytes,
   and runs a framed native-host `status` exchange and `Cortex --release-smoke`
   under an isolated `HOME` before any UI/network/camera;
7. generates application SPDX and locked-Python CycloneDX SBOMs,
   architecture-specific `SHA256SUMS-<arch>`, `release-metadata.json`, and
   command evidence;
8. creates GitHub SLSA provenance and SBOM attestations for the DMG;
9. packages each evidence directory as an architecture-specific ZIP and stages
   both DMGs, checksum files, and evidence bundles in a GitHub draft release
   only after both matrix jobs pass.

The mounted app's authoritative deep signature verification has a bounded
five-minute command budget. Intel bundles contain thousands of nested native
members and can exceed the generic 60-second subprocess budget on hosted
runners; a timeout is not treated as signature success, but a valid slow check
is given enough time to complete within the enclosing 90-minute job deadline.

Run the same artifact verifier locally:

```bash
uv run --project cortex --locked python -m cortex.scripts.verify_macos_release \
  dist/Cortex-<version>-macos-arm64.dmg \
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
gh attestation verify Cortex-<version>-macos-arm64.dmg \
  --repo StevenWang-CY/cortex
codesign --verify --strict --verbose=2 Cortex-<version>-macos-arm64.dmg
codesign -dv --verbose=4 Cortex-<version>-macos-arm64.dmg
xcrun stapler validate Cortex-<version>-macos-arm64.dmg
spctl -a -vv --type open --context context:primary-signature \
  Cortex-<version>-macos-arm64.dmg
```

The checksum file covers the DMG, metadata, SBOMs, verifier output, and command
evidence. Verify only the DMG line when you intentionally did not download the
full evidence bundle.

## Required real-device release-candidate pass

Automation cannot grant TCC permissions, judge onboarding copy, fully restart a
real browser profile, or prove camera ownership after a crash. Before public
promotion, execute [manual-release-validation.md](manual-release-validation.md)
on at least one clean supported Mac using the exact signed/notarized candidate
staged by CI, and upload a `manual-release-evidence-<arch>.json` record plus
every non-sensitive evidence file it references to the same draft release.

Publication carries an explicit **assurance tier**
([ADR 0007](../adr/0007-tiered-release-assurance.md)) that the promotion
workflow validates and renders into the release notes:

| Tier | Human evidence required | How it is labelled |
| --- | --- | --- |
| `self-attested` | One or two clean-profile records signed by the maintainer. The core cases (`artifact.identity`, `install.launch`, `browser.chrome_native`, `runtime.lifecycle_camera_tcc`, `uninstall.cleanup`) must pass on hardware; every other case is `passed` or `not_run` with a reason. | "Tier: self-attested"; architectures without a record are marked *CI-verified only*; the notes list every case not run. |
| `independently-reviewed` | Records for both architectures, every case passed, builder and independent-reviewer identities disjoint across records. | "Tier: independently reviewed". |

Both tiers require the complete automated chain above. An unexecuted template,
a `failed` case, a `passed` case without an uploaded evidence asset, or a
missing core case blocks promotion in every tier.

Dispatch **Publish validated macOS release** with the tier the records declare.
The promotion workflow revalidates the tag, commit, version, artifact names and
hashes, the fixed 14-case catalog, evidence-file existence, the tier rules,
checksum coverage, and GitHub provenance attestations for both DMGs and both
evidence bundles. It then appends an *Assurance* section to the release notes,
uploads `release-promotion-validation.json`, and converts the draft into the
latest public release. The `production-publish` environment approval is a
maintainer approval and is not represented as independent review.
