#!/usr/bin/env bash
# =============================================================================
# Cortex macOS Build Pipeline
#
# Produces dist/Cortex.dmg from the project source.
# Steps: build extensions → generate .env → icns → PyInstaller → sign → DMG
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

# Activate venv if present
if [ -f "${ROOT_DIR}/.venv/bin/activate" ]; then
    source "${ROOT_DIR}/.venv/bin/activate"
fi

# Non-interactive bash launched from GUI tools often lacks Homebrew/NVM paths.
# Add the common macOS Node locations before building bundled extensions.
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"
if ! command -v npm &>/dev/null && [ -s "${HOME}/.nvm/nvm.sh" ]; then
    # shellcheck disable=SC1090
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
        if command -v pnpm &>/dev/null; then
            pnpm install
            pnpm exec plasmo build
            pnpm exec plasmo build --target=edge-mv3
        elif command -v corepack &>/dev/null; then
            corepack pnpm install
            corepack pnpm exec plasmo build
            corepack pnpm exec plasmo build --target=edge-mv3
        elif command -v npm &>/dev/null; then
            npm install
            npx plasmo build
            npx plasmo build --target=edge-mv3
        else
            echo "ERROR: pnpm/corepack/npm not installed; cannot build browser extension" >&2
            exit 1
        fi
    )
fi

# ── Step 2: Build VS Code extension ────────────────────────────────────────
VSIX="${CORTEX_DIR}/apps/vscode_extension/cortex-somatic-0.2.1.vsix"
VSCODE_EXT_DIR="${CORTEX_DIR}/apps/vscode_extension"
if [ "${CORTEX_SKIP_VSCODE_EXT_BUILD:-0}" = "1" ]; then
    echo "→ Skipping VS Code extension build (CORTEX_SKIP_VSCODE_EXT_BUILD=1)"
else
    echo "→ Building VS Code extension..."
    (
        cd "${VSCODE_EXT_DIR}"
        if command -v npm &>/dev/null; then
            npm install
            npm run compile
            npx --yes @vscode/vsce package --out "${VSIX}"
        else
            echo "ERROR: npm not installed; cannot build VS Code extension" >&2
            exit 1
        fi
    )
fi

# ── Step 3: Verify VSIX ────────────────────────────────────────────────────
if [ ! -f "${VSIX}" ]; then
    echo "ERROR: VSIX not found at ${VSIX}" >&2
    echo "Build it with: cd cortex/apps/vscode_extension && npx @vscode/vsce package --out cortex-somatic-0.2.1.vsix" >&2
    exit 1
fi
echo "→ VSIX found"

# ── Step 4: Generate key-free .env for bundling (allowlist, not denylist) ──
# Only non-secret pointers ship inside the DMG. Secrets live in macOS Keychain
# (cortex.bedrock / bearer_token). Anything not matching ALLOWED_KEYS is dropped.
echo "→ Generating bundled .env (allowlist scrub)..."
ALLOWED_KEYS='^(CORTEX_API__HOST|CORTEX_API__PORT|CORTEX_API__WS_PORT|CORTEX_LLM__PROVIDER|CORTEX_LLM__BEDROCK__AWS_REGION|CORTEX_LLM__USE_KEYCHAIN|CORTEX_LLM__MODEL_DEFAULT|CORTEX_LLM__MODEL_FAST|CORTEX_LLM__MODEL_DEEP|CORTEX_STORAGE__BASE_DIR)='
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
# Defence-in-depth: blow up the build if a secret slipped through.
if grep -qiE "AWS_BEARER_TOKEN_BEDROCK|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|api_key=|sk-ant-|openai\.com|franklink|gwhiz|cis\.upenn" "${ROOT_DIR}/.env.bundled"; then
    echo "ERROR: bundled .env contains a forbidden pattern; aborting build." >&2
    head -50 "${ROOT_DIR}/.env.bundled" >&2
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

    # Render SVG to PNG at various sizes using sips (requires rsvg-convert or qlmanage)
    # Fallback: use qlmanage which is always available on macOS
    TEMP_PNG="/tmp/cortex_icon_1024.png"
    if command -v rsvg-convert &>/dev/null; then
        rsvg-convert -w 1024 -h 1024 "${ICON_SVG}" -o "${TEMP_PNG}"
    else
        qlmanage -t -s 1024 -o /tmp "${ICON_SVG}" 2>/dev/null || true
        # qlmanage outputs to a different name
        QLOUT="/tmp/logo.svg.png"
        if [ -f "${QLOUT}" ]; then
            mv "${QLOUT}" "${TEMP_PNG}"
        else
            echo "WARNING: Could not convert SVG to PNG. Using default icon."
            TEMP_PNG=""
        fi
    fi

    if [ -n "${TEMP_PNG}" ] && [ -f "${TEMP_PNG}" ]; then
        for SIZE in 16 32 64 128 256 512 1024; do
            sips -z ${SIZE} ${SIZE} "${TEMP_PNG}" --out "${ICONSET_DIR}/icon_${SIZE}x${SIZE}.png" 2>/dev/null
            HALF=$((SIZE / 2))
            if [ ${HALF} -ge 16 ]; then
                cp "${ICONSET_DIR}/icon_${SIZE}x${SIZE}.png" "${ICONSET_DIR}/icon_${HALF}x${HALF}@2x.png"
            fi
        done
        iconutil -c icns "${ICONSET_DIR}" -o "${ICON_ICNS}" 2>/dev/null || true
        rm -rf "${ICONSET_DIR}" "${TEMP_PNG}"
    fi
else
    echo "→ .icns already exists"
fi

# ── Step 6: Run PyInstaller ────────────────────────────────────────────────
echo "→ Running PyInstaller..."
export CORTEX_ROOT="${ROOT_DIR}"
pyinstaller "${SPEC_FILE}" --noconfirm --clean --distpath "${DIST_DIR}" --workpath "${ROOT_DIR}/build/pyinstaller"

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
# Check for Developer ID certificate
SIGN_IDENTITY="${CORTEX_SIGN_IDENTITY:--}"  # Default to ad-hoc ("-")

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

# ── Step 8: Create DMG ────────────────────────────────────────────────────
DMG_PATH="${DIST_DIR}/Cortex.dmg"
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
        hdiutil create -volname "Cortex" -srcfolder "${DMG_STAGE_DIR}" -ov -format UDZO "${DMG_PATH}"
    fi
else
    # Fallback to hdiutil
    hdiutil create -volname "Cortex" -srcfolder "${DMG_STAGE_DIR}" -ov -format UDZO "${DMG_PATH}"
fi

if [ ! -f "${DMG_PATH}" ]; then
    echo "ERROR: DMG was not generated at ${DMG_PATH}" >&2
    exit 1
fi

# ── Step 9: Notarize (if credentials available) ───────────────────────────
if [ "${SIGN_IDENTITY}" != "-" ] && [ -n "${CORTEX_NOTARIZE_PROFILE:-}" ]; then
    echo "→ Notarizing DMG..."
    xcrun notarytool submit "${DMG_PATH}" \
        --keychain-profile "${CORTEX_NOTARIZE_PROFILE}" \
        --wait
    xcrun stapler staple "${DMG_PATH}"
    echo "→ Notarization complete"
else
    echo "→ Skipping notarization (no Developer ID or CORTEX_NOTARIZE_PROFILE not set)"
    echo "  For production: set CORTEX_SIGN_IDENTITY and CORTEX_NOTARIZE_PROFILE"
fi

# ── Step 10: Verify ───────────────────────────────────────────────────────
echo ""
echo "=== Build Complete ==="
echo "  App:  ${APP_PATH}"
echo "  DMG:  ${DMG_PATH}"
echo ""
echo "To test: open ${DMG_PATH}"
