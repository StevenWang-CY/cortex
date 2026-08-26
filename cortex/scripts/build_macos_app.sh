#!/usr/bin/env bash
# =============================================================================
# Cortex macOS Build Pipeline
#
# Produces dist/Cortex.dmg from the project source.
# Steps: extensions → key-free env → PyInstaller → sign app → DMG → sign → notarize → verify
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CORTEX_DIR="${ROOT_DIR}/cortex"
DIST_DIR="${ROOT_DIR}/dist"
SPEC_FILE="${CORTEX_DIR}/scripts/cortex.spec"
ENTITLEMENTS="${CORTEX_DIR}/scripts/cortex_entitlements.plist"

echo "=== Cortex macOS Build ==="
echo "Root: ${ROOT_DIR}"

cd "${ROOT_DIR}"

# A release build must use the environment created from cortex/uv.lock. Do not
# fall back to a shell/system interpreter whose dependency graph is unknown.
PYTHON_BIN="${CORTEX_DIR}/.venv/bin/python"
if [ ! -x "${PYTHON_BIN}" ]; then
    echo "[FATAL] Locked project interpreter is missing at ${PYTHON_BIN}. Run uv sync --project cortex --locked --all-extras." >&2
    exit 1
fi

check_node_version() {
    if ! command -v node &>/dev/null; then
        echo "[FATAL] Node is required to build bundled integrations" >&2
        return 1
    fi
    local expected actual
    expected=$(<"${ROOT_DIR}/.node-version")
    actual=$(node -p 'process.versions.node')
    if [ "${actual}" != "${expected}" ]; then
        echo "[FATAL] Node ${actual} does not match repository pin ${expected}" >&2
        return 1
    fi
}

# pyproject.toml is the sole version source. Refuse to package stale generated
# Python/extension surfaces before spending minutes on native builds.
"${PYTHON_BIN}" -m cortex.scripts.sync_versions --check
CORTEX_VERSION=$("${PYTHON_BIN}" -m cortex.scripts.sync_versions --print)
ARTIFACT_ARCH="${CORTEX_ARTIFACT_ARCH:-$(uname -m)}"
REQUIRE_NOTARIZATION="${CORTEX_REQUIRE_NOTARIZATION:-0}"
SIGN_IDENTITY="${CORTEX_SIGN_IDENTITY:--}"
EVIDENCE_DIR="${CORTEX_RELEASE_EVIDENCE_DIR:-${DIST_DIR}/evidence-${ARTIFACT_ARCH}}"

case "${ARTIFACT_ARCH}" in
    arm64|x86_64) ;;
    *)
        echo "[FATAL] Unsupported CORTEX_ARTIFACT_ARCH=${ARTIFACT_ARCH}; expected arm64 or x86_64" >&2
        exit 1
        ;;
esac
if [ "${REQUIRE_NOTARIZATION}" = "1" ]; then
    if [ "${SIGN_IDENTITY}" = "-" ] || [ -z "${CORTEX_NOTARIZE_PROFILE:-}" ]; then
        echo "[FATAL] Production release requires CORTEX_SIGN_IDENTITY and CORTEX_NOTARIZE_PROFILE" >&2
        exit 1
    fi
fi
mkdir -p "${EVIDENCE_DIR}"
export CORTEX_ARTIFACT_ARCH="${ARTIFACT_ARCH}"

# Non-interactive bash launched from GUI tools often lacks Homebrew/NVM paths.
# Keep a toolchain already selected by the caller (for example setup-node in
# release CI) authoritative, then append common macOS fallback locations.
# Prepending these directories can silently replace the pinned Node binary with
# an unrelated Homebrew version installed on the runner.
export PATH="${PATH}:/opt/homebrew/bin:/usr/local/bin"
if ! command -v npm &>/dev/null && [ -s "${HOME}/.nvm/nvm.sh" ]; then
    # shellcheck disable=SC1090,SC1091
    source "${HOME}/.nvm/nvm.sh"
    nvm use --silent default >/dev/null 2>&1 || true
fi

ENV_BACKUP_PATH=""
BUNDLED_ENV_ACTIVE="0"
DMG_STAGE_DIR=""

cleanup() {
    # Always remove temporary bundled env.
    rm -f "${ROOT_DIR}/.env.bundled"

    # Restore developer .env if we temporarily replaced it.
    if [ "${BUNDLED_ENV_ACTIVE}" = "1" ]; then
        rm -f "${ROOT_DIR}/.env"
    fi
    if [ -n "${ENV_BACKUP_PATH}" ] && [ -f "${ENV_BACKUP_PATH}" ]; then
        mv "${ENV_BACKUP_PATH}" "${ROOT_DIR}/.env"
    fi

    # Clean up temporary DMG staging directory.
    if [ -n "${DMG_STAGE_DIR}" ] && [ -d "${DMG_STAGE_DIR}" ]; then
        rm -rf "${DMG_STAGE_DIR}"
    fi
}

trap cleanup EXIT

# ── Step 1: Build Chrome extension ──────────────────────────────────────────
EXT_DIR="${CORTEX_DIR}/apps/browser_extension"
if [ "${CORTEX_SKIP_EXT_BUILD:-0}" = "1" ]; then
    echo "→ Skipping browser extension build (CORTEX_SKIP_EXT_BUILD=1)"
else
    echo "→ Building Chrome and Edge extensions..."
    (
        cd "${EXT_DIR}"
        check_node_version
        EXPECTED_PNPM=$(node -p 'require("./package.json").packageManager.replace(/^pnpm@/, "")')
        if command -v corepack &>/dev/null; then
            PNPM_CMD=(corepack pnpm)
        elif command -v pnpm &>/dev/null; then
            PNPM_CMD=(pnpm)
        else
            echo "ERROR: pnpm/corepack not installed; cannot consume pnpm-lock.yaml" >&2
            exit 1
        fi
        ACTUAL_PNPM=$("${PNPM_CMD[@]}" --version)
        if [ "${ACTUAL_PNPM}" != "${EXPECTED_PNPM}" ]; then
            echo "[FATAL] pnpm ${ACTUAL_PNPM} does not match packageManager pin ${EXPECTED_PNPM}" >&2
            exit 1
        fi
        "${PNPM_CMD[@]}" install --frozen-lockfile
        "${PNPM_CMD[@]}" exec plasmo build
        "${PNPM_CMD[@]}" exec plasmo build --target=edge-mv3
    )
fi

# ── Step 2: Build VS Code extension ────────────────────────────────────────
# The version-consistency gate above proves the VS Code manifest agrees with
# the canonical project version, so both the expected VSIX and bundled spec
# resolve the same immutable artifact name.
VSIX="${CORTEX_DIR}/apps/vscode_extension/cortex-somatic-${CORTEX_VERSION}.vsix"
VSCODE_EXT_DIR="${CORTEX_DIR}/apps/vscode_extension"
if [ "${CORTEX_SKIP_VSCODE_EXT_BUILD:-0}" = "1" ]; then
    echo "→ Skipping VS Code extension build (CORTEX_SKIP_VSCODE_EXT_BUILD=1)"
else
    echo "→ Building VS Code extension..."
    (
        cd "${VSCODE_EXT_DIR}"
        check_node_version
        if command -v npm &>/dev/null; then
            npm ci
            npm run compile
            npm exec -- vsce package --out "${VSIX}"
        else
            echo "ERROR: npm not installed; cannot build VS Code extension" >&2
            exit 1
        fi
    )
fi

# ── Step 3: Verify VSIX ────────────────────────────────────────────────────
if [ ! -f "${VSIX}" ]; then
    echo "ERROR: VSIX not found at ${VSIX}" >&2
    echo "Build it with: cd cortex/apps/vscode_extension && npm ci && npm exec -- vsce package --out cortex-somatic-${CORTEX_VERSION}.vsix" >&2
    exit 1
fi
echo "→ VSIX found"

# ── Step 4: Generate key-free .env for bundling (allowlist, not denylist) ──
# Only non-secret pointers ship inside the DMG. Secrets live in macOS Keychain
# (cortex.bedrock / bearer_token). Anything not matching ALLOWED_KEYS is dropped.
echo "→ Generating bundled .env (allowlist scrub)..."
# P2-11: Removed CORTEX_STORAGE__BASE_DIR — the real key is CORTEX_STORAGE__PATH
#        and the bundled app must not pin to a developer's local path.
# P2-13: Each remaining key is kept because at least one .py file references
#        it; see cortex/libs/config/settings.py (APIConfig, LLMConfig).
#   CORTEX_API__HOST            — APIConfig.host; daemon bind address
#   CORTEX_API__PORT            — APIConfig.port; HTTP API port (9472)
#   CORTEX_API__WS_PORT         — APIConfig.ws_port; WebSocket port (9473)
#   CORTEX_LLM__PROVIDER        — LLMConfig.provider; selects Bedrock/vertex/direct
#   CORTEX_LLM__BEDROCK__AWS_REGION — BedrockConfig.aws_region; region for IAM
#   CORTEX_LLM__USE_KEYCHAIN    — LLMConfig.use_keychain; enables BYOK path
#   CORTEX_LLM__MODEL_DEFAULT   — LLMConfig.model_default; default model tier
#   CORTEX_LLM__MODEL_FAST      — LLMConfig.model_fast; fast-tier model
#   CORTEX_LLM__MODEL_DEEP      — LLMConfig.model_deep; deep-tier model
ALLOWED_KEYS='^(CORTEX_API__HOST|CORTEX_API__PORT|CORTEX_API__WS_PORT|CORTEX_LLM__PROVIDER|CORTEX_LLM__BEDROCK__AWS_REGION|CORTEX_LLM__USE_KEYCHAIN|CORTEX_LLM__MODEL_DEFAULT|CORTEX_LLM__MODEL_FAST|CORTEX_LLM__MODEL_DEEP)='
if [ -f "${ROOT_DIR}/.env" ]; then
    grep -E "${ALLOWED_KEYS}" "${ROOT_DIR}/.env" > "${ROOT_DIR}/.env.bundled" || true
else
    : > "${ROOT_DIR}/.env.bundled"
fi
# Always force these defaults in the bundled .env regardless of dev .env state.
{
    echo "CORTEX_LLM__PROVIDER=bedrock"
    echo "CORTEX_LLM__USE_KEYCHAIN=true"
    echo "CORTEX_LLM__BEDROCK__AWS_REGION=us-east-2"
    echo "CORTEX_API__HOST=127.0.0.1"
    echo "CORTEX_API__PORT=9472"
    echo "CORTEX_API__WS_PORT=9473"
} >> "${ROOT_DIR}/.env.bundled"
# Defence-in-depth: fail without echoing the suspect file into CI logs.
if grep -qiE "AWS_BEARER_TOKEN_BEDROCK|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|api_key=|sk-ant-|openai\.com|/Users/|/home/" "${ROOT_DIR}/.env.bundled"; then
    echo "ERROR: bundled .env contains a forbidden pattern; aborting build." >&2
    exit 1
fi

# Rename .env.bundled → .env so PyInstaller bundles it with the right name
# (saved back after build)
if [ -f "${ROOT_DIR}/.env" ]; then
    ENV_BACKUP_PATH="$(mktemp "${ROOT_DIR}/.env.backup.XXXXXX")"
    mv "${ROOT_DIR}/.env" "${ENV_BACKUP_PATH}"
fi
cp "${ROOT_DIR}/.env.bundled" "${ROOT_DIR}/.env"
BUNDLED_ENV_ACTIVE="1"

# ── Step 5: Convert SVG → .icns ───────────────────────────────────────────
ICON_SVG="${CORTEX_DIR}/assets/logo.svg"
ICON_ICNS="${CORTEX_DIR}/assets/cortex.icns"

if [ ! -f "${ICON_ICNS}" ]; then
    echo "→ Converting logo.svg → cortex.icns..."
    ICONSET_DIR="/tmp/cortex.iconset"
    rm -rf "${ICONSET_DIR}"
    mkdir -p "${ICONSET_DIR}"

    # I4: do NOT silence qlmanage/sips/iconutil errors. Previously every
    # tool call had ``2>/dev/null || true`` which produced an empty
    # .icns on broken installs without any signal. Now we capture
    # failures into ICON_BUILD_FAILED so the trailing check below can
    # exit the whole build script non-zero with a clear message.
    ICON_BUILD_FAILED=0

    # Render SVG to PNG at various sizes using rsvg-convert (preferred)
    # or qlmanage (macOS fallback).
    TEMP_PNG="/tmp/cortex_icon_1024.png"
    PRIMARY_FAILED=0
    FALLBACK_FAILED=0
    if command -v rsvg-convert &>/dev/null; then
        if ! rsvg-convert -w 1024 -h 1024 "${ICON_SVG}" -o "${TEMP_PNG}"; then
            echo "WARN: rsvg-convert failed for ${ICON_SVG}" >&2
            PRIMARY_FAILED=1
            TEMP_PNG=""
        fi
    else
        if ! qlmanage -t -s 1024 -o /tmp "${ICON_SVG}"; then
            echo "WARN: qlmanage rendering failed for ${ICON_SVG}" >&2
            FALLBACK_FAILED=1
        fi
        # qlmanage outputs to a different name
        QLOUT="/tmp/logo.svg.png"
        if [ -f "${QLOUT}" ]; then
            mv "${QLOUT}" "${TEMP_PNG}"
        else
            echo "WARN: qlmanage did not produce ${QLOUT}; cannot build .icns" >&2
            FALLBACK_FAILED=1
            TEMP_PNG=""
        fi
    fi

    if [ -n "${TEMP_PNG}" ] && [ -f "${TEMP_PNG}" ]; then
        for SIZE in 16 32 64 128 256 512 1024; do
            if ! sips -z ${SIZE} ${SIZE} "${TEMP_PNG}" --out "${ICONSET_DIR}/icon_${SIZE}x${SIZE}.png"; then
                echo "WARN: sips failed for size ${SIZE}" >&2
                ICON_BUILD_FAILED=1
            fi
            HALF=$((SIZE / 2))
            if [ ${HALF} -ge 16 ] && [ -f "${ICONSET_DIR}/icon_${SIZE}x${SIZE}.png" ]; then
                cp "${ICONSET_DIR}/icon_${SIZE}x${SIZE}.png" "${ICONSET_DIR}/icon_${HALF}x${HALF}@2x.png"
            fi
        done
        if ! iconutil -c icns "${ICONSET_DIR}" -o "${ICON_ICNS}"; then
            echo "WARN: iconutil failed; .icns will not be embedded" >&2
            ICON_BUILD_FAILED=1
        fi
        rm -rf "${ICONSET_DIR}" "${TEMP_PNG}"
    fi

    # If both rendering paths failed (primary attempted + fallback
    # attempted with no successful TEMP_PNG, or iconutil refused to
    # package the iconset) we must NOT silently produce a brand-less
    # build. A user-installed Cortex.app with the generic gear icon is
    # the #1 cosmetic regression we ship; better to fail the build.
    if [ "${PRIMARY_FAILED}" = "1" ] && [ "${FALLBACK_FAILED}" = "1" ]; then
        echo "[FATAL] Could not render icon SVG via either rsvg-convert or qlmanage." >&2
        exit 1
    fi
    if [ "${ICON_BUILD_FAILED}" = "1" ] && [ ! -f "${ICON_ICNS}" ]; then
        echo "[FATAL] Icon pipeline failed to produce ${ICON_ICNS}." >&2
        exit 1
    fi
else
    echo "→ .icns already exists"
fi

# ── Step 6: Run PyInstaller ────────────────────────────────────────────────
echo "→ Running PyInstaller..."
export CORTEX_ROOT="${ROOT_DIR}"
"${PYTHON_BIN}" -m PyInstaller "${SPEC_FILE}" --noconfirm --clean --distpath "${DIST_DIR}" --workpath "${ROOT_DIR}/build/pyinstaller"

APP_PATH="${DIST_DIR}/Cortex.app"
if [ ! -d "${APP_PATH}" ]; then
    echo "ERROR: App bundle not found at ${APP_PATH}" >&2
    exit 1
fi

# Inject .icns if available
if [ -f "${ICON_ICNS}" ]; then
    cp "${ICON_ICNS}" "${APP_PATH}/Contents/Resources/cortex.icns"
    # Update Info.plist to reference the icon
    /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string cortex" "${APP_PATH}/Contents/Info.plist" 2>/dev/null \
        || /usr/libexec/PlistBuddy -c "Set :CFBundleIconFile cortex" "${APP_PATH}/Contents/Info.plist"
fi

# ── Step 7: Code sign ──────────────────────────────────────────────────────
echo "→ Code signing with: ${SIGN_IDENTITY}"

# For ad-hoc signing, we must NOT use --options runtime (hardened runtime).
# Hardened runtime enforces library validation which rejects ad-hoc-signed
# libraries with different (or no) Team IDs — breaking Python.framework loading.
# For Developer ID signing, hardened runtime is required for notarization.
if [ "${SIGN_IDENTITY}" = "-" ]; then
    # Ad-hoc: sign all nested binaries first (same identity), no hardened runtime
    echo "  (ad-hoc mode: signing nested binaries individually)"
    find "${APP_PATH}" -type f \( -name "*.dylib" -o -name "*.so" \) -exec \
        codesign --force --sign - {} \; 2>/dev/null
    # Sign Python framework explicitly
    PYTHON_FW=$(find "${APP_PATH}" -name "Python" -path "*/Python.framework/*" -type f 2>/dev/null | head -1)
    if [ -n "${PYTHON_FW}" ]; then
        codesign --force --sign - "${PYTHON_FW}"
    fi
    # Sign main executable and bundle
    codesign --force --sign - --entitlements "${ENTITLEMENTS}" "${APP_PATH}/Contents/MacOS/Cortex"
    codesign --force --sign - --entitlements "${ENTITLEMENTS}" "${APP_PATH}"
else
    # Developer ID: use --deep --options runtime for notarization
    codesign --force --options runtime --deep \
        --sign "${SIGN_IDENTITY}" \
        --entitlements "${ENTITLEMENTS}" \
        "${APP_PATH}"
fi

# Post-sign verification — catches signing regressions before the user
# sees a Gatekeeper bounce. ``codesign --verify`` is authoritative for
# both ad-hoc and Developer ID bundles; ``spctl`` is warn-only for
# ad-hoc because Gatekeeper rejects unsigned binaries by design.
if ! codesign --verify --deep --strict --verbose=2 "${APP_PATH}" \
    2>&1 | tee "${EVIDENCE_DIR}/codesign-verify-app.txt"; then
    echo "[FATAL] codesign --verify failed for ${APP_PATH}" >&2
    exit 1
fi
spctl -a -vv --type execute "${APP_PATH}" \
    > "${EVIDENCE_DIR}/spctl-app.txt" 2>&1 || true

BUILT_ARCHS=$(lipo -archs "${APP_PATH}/Contents/MacOS/Cortex")
if [ "${BUILT_ARCHS}" != "${ARTIFACT_ARCH}" ]; then
    echo "[FATAL] Built architecture ${BUILT_ARCHS} does not equal ${ARTIFACT_ARCH}" >&2
    exit 1
fi
echo "${BUILT_ARCHS}" > "${EVIDENCE_DIR}/architectures.txt"

# ── Step 8: Create DMG ────────────────────────────────────────────────────
DMG_PATH="${DIST_DIR}/Cortex-${CORTEX_VERSION}-macos-${ARTIFACT_ARCH}.dmg"
echo "→ Creating DMG..."
DMG_STAGE_DIR="$(mktemp -d /tmp/cortex_dmg_stage.XXXXXX)"
cp -R "${APP_PATH}" "${DMG_STAGE_DIR}/Cortex.app"
rm -f "${DMG_PATH}"

if command -v create-dmg &>/dev/null; then
    if ! create-dmg \
        --volname "Cortex" \
        --window-pos 200 120 \
        --window-size 600 400 \
        --icon-size 100 \
        --icon "Cortex.app" 175 190 \
        --app-drop-link 425 190 \
        "${DMG_PATH}" \
        "${DMG_STAGE_DIR}"; then
        echo "WARNING: create-dmg failed; falling back to hdiutil" >&2
        rm -f "${DMG_PATH}"
        # I8: previously the hdiutil fallback's exit code was discarded,
        # so a second failure left the DMG missing and the build appeared
        # green until the final ``[ ! -f "${DMG_PATH}" ]`` check fired.
        # Surface the failure at source with a single FATAL line.
        if ! hdiutil create -volname "Cortex" -srcfolder "${DMG_STAGE_DIR}" -ov -format UDZO "${DMG_PATH}"; then
            echo "[FATAL] DMG creation failed via both create-dmg and hdiutil" >&2
            exit 1
        fi
    fi
else
    # Fallback to hdiutil
    if ! hdiutil create -volname "Cortex" -srcfolder "${DMG_STAGE_DIR}" -ov -format UDZO "${DMG_PATH}"; then
        echo "[FATAL] DMG creation failed via hdiutil (create-dmg not installed)" >&2
        exit 1
    fi
fi

if [ ! -f "${DMG_PATH}" ]; then
    echo "ERROR: DMG was not generated at ${DMG_PATH}" >&2
    exit 1
fi

# Verify DMG integrity — corrupt UDZO archives bounce silently at
# install time, so block the build now if hdiutil disagrees.
if ! hdiutil verify "${DMG_PATH}"; then
    echo "[FATAL] hdiutil verify failed for ${DMG_PATH}" >&2
    exit 1
fi

# The DMG is the outermost distributable container. Apple requires each
# signable nested container to be signed from the inside out, so a Developer
# ID release must sign the finished disk image before submitting that exact
# byte sequence for notarization. A notarization ticket alone is not a DMG
# signature: Gatekeeper otherwise reports ``source=no usable signature``.
if [ "${SIGN_IDENTITY}" != "-" ]; then
    echo "→ Code signing DMG..."
    codesign --sign "${SIGN_IDENTITY}" \
        --timestamp \
        --identifier "com.cortex.daemon.dmg" \
        "${DMG_PATH}"
    if ! codesign --verify --strict --verbose=2 "${DMG_PATH}" \
        2>&1 | tee "${EVIDENCE_DIR}/codesign-verify-dmg.txt"; then
        echo "[FATAL] codesign --verify failed for ${DMG_PATH}" >&2
        exit 1
    fi
    codesign -dv --verbose=4 "${DMG_PATH}" \
        2>&1 | tee "${EVIDENCE_DIR}/codesign-display-dmg.txt"
else
    echo "→ Skipping DMG signing (ad-hoc development build)"
fi

# ── Step 9: Notarize (if credentials available) ───────────────────────────
if [ "${SIGN_IDENTITY}" != "-" ] && [ -n "${CORTEX_NOTARIZE_PROFILE:-}" ]; then
    echo "→ Notarizing DMG..."
    NOTARY_ARGS=(
        submit "${DMG_PATH}"
        --keychain-profile "${CORTEX_NOTARIZE_PROFILE}"
        --wait
        --output-format json
    )
    if [ -n "${CORTEX_NOTARIZE_KEYCHAIN:-}" ]; then
        NOTARY_ARGS+=(--keychain "${CORTEX_NOTARIZE_KEYCHAIN}")
    fi
    xcrun notarytool "${NOTARY_ARGS[@]}" \
        | tee "${EVIDENCE_DIR}/notarytool-submit.json"
    "${PYTHON_BIN}" - "${EVIDENCE_DIR}/notarytool-submit.json" "${EVIDENCE_DIR}/notary-request-id.txt" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("status") != "Accepted":
    raise SystemExit(f"notarization was not accepted: {payload.get('status')!r}")
request_id = str(payload.get("id") or "").strip()
if not request_id:
    raise SystemExit("accepted notarization response omitted its request id")
Path(sys.argv[2]).write_text(request_id + "\n", encoding="utf-8")
PY
    IFS= read -r NOTARY_REQUEST_ID < "${EVIDENCE_DIR}/notary-request-id.txt"
    NOTARY_LOG_ARGS=(
        log "${NOTARY_REQUEST_ID}"
        --keychain-profile "${CORTEX_NOTARIZE_PROFILE}"
    )
    if [ -n "${CORTEX_NOTARIZE_KEYCHAIN:-}" ]; then
        NOTARY_LOG_ARGS+=(--keychain "${CORTEX_NOTARIZE_KEYCHAIN}")
    fi
    xcrun notarytool "${NOTARY_LOG_ARGS[@]}" \
        > "${EVIDENCE_DIR}/notarytool-log.json"
    xcrun stapler staple "${DMG_PATH}"
    xcrun stapler validate "${DMG_PATH}" \
        2>&1 | tee "${EVIDENCE_DIR}/stapler-validate-dmg.txt"
    echo "→ Notarization complete"
else
    echo "→ Skipping notarization (no Developer ID or CORTEX_NOTARIZE_PROFILE not set)"
    echo "  For production: set CORTEX_SIGN_IDENTITY and CORTEX_NOTARIZE_PROFILE"
fi

# ── Step 10: Verify the mounted artifact and emit local provenance ─────────
VERIFY_ARGS=(
    "${DMG_PATH}"
    --expected-arch "${ARTIFACT_ARCH}"
    --output "${EVIDENCE_DIR}/release-verification.json"
)
if [ "${REQUIRE_NOTARIZATION}" = "1" ]; then
    VERIFY_ARGS+=(--require-notarized)
fi
"${PYTHON_BIN}" -m cortex.scripts.verify_macos_release "${VERIFY_ARGS[@]}"

EVIDENCE_ARGS=(
    --artifact "${DMG_PATH}"
    --verification "${EVIDENCE_DIR}/release-verification.json"
    --output-dir "${EVIDENCE_DIR}"
    --checksum-name "SHA256SUMS-${ARTIFACT_ARCH}"
)
if [ "${REQUIRE_NOTARIZATION}" = "1" ]; then
    EVIDENCE_ARGS+=(--require-clean)
fi
if [ -n "${CORTEX_RELEASE_TAG:-}" ]; then
    EVIDENCE_ARGS+=(--expected-tag "${CORTEX_RELEASE_TAG}")
fi
"${PYTHON_BIN}" -m cortex.scripts.generate_release_evidence "${EVIDENCE_ARGS[@]}"

# ── Step 11: Summary ──────────────────────────────────────────────────────
echo ""
echo "=== Build Complete ==="
echo "  App:  ${APP_PATH}"
echo "  DMG:  ${DMG_PATH}"
echo "  Evidence: ${EVIDENCE_DIR}"
echo ""
echo "To test: open ${DMG_PATH}"
