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

# ── Step 1: Build Chrome extension ──────────────────────────────────────────
EXT_DIR="${CORTEX_DIR}/apps/browser_extension"
if [ ! -d "${EXT_DIR}/build/chrome-mv3-prod" ]; then
    echo "→ Building Chrome extension..."
    (cd "${EXT_DIR}" && pnpm install && npx plasmo build)
else
    echo "→ Chrome extension already built"
fi

# ── Step 2: Build Edge extension ────────────────────────────────────────────
if [ ! -d "${EXT_DIR}/build/edge-mv3-prod" ]; then
    echo "→ Building Edge extension..."
    (cd "${EXT_DIR}" && npx plasmo build --target=edge-mv3)
else
    echo "→ Edge extension already built"
fi

# ── Step 3: Verify VSIX ────────────────────────────────────────────────────
VSIX="${CORTEX_DIR}/apps/vscode_extension/cortex-somatic-0.1.0.vsix"
if [ ! -f "${VSIX}" ]; then
    echo "ERROR: VSIX not found at ${VSIX}" >&2
    echo "Build it with: cd cortex/apps/vscode_extension && vsce package" >&2
    exit 1
fi
echo "→ VSIX found"

# ── Step 4: Generate key-free .env for bundling ────────────────────────────
echo "→ Generating bundled .env (no secrets)..."
if [ -f "${ROOT_DIR}/.env" ]; then
    # Strip api_key lines, force use_keychain=true
    grep -v -i "api_key" "${ROOT_DIR}/.env" \
        | grep -v "^#.*api_key" \
        | sed 's/CORTEX_LLM__AZURE__USE_KEYCHAIN=.*/CORTEX_LLM__AZURE__USE_KEYCHAIN=true/' \
        > "${ROOT_DIR}/.env.bundled"
    # Ensure use_keychain is present
    if ! grep -q "USE_KEYCHAIN" "${ROOT_DIR}/.env.bundled"; then
        echo "CORTEX_LLM__AZURE__USE_KEYCHAIN=true" >> "${ROOT_DIR}/.env.bundled"
    fi
else
    cat > "${ROOT_DIR}/.env.bundled" << 'ENVEOF'
CORTEX_LLM__MODE=azure
CORTEX_LLM__AZURE__USE_KEYCHAIN=true
CORTEX_API__HOST=127.0.0.1
CORTEX_API__PORT=9472
CORTEX_API__WS_PORT=9473
ENVEOF
fi

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

if command -v create-dmg &>/dev/null; then
    create-dmg \
        --volname "Cortex" \
        --window-pos 200 120 \
        --window-size 600 400 \
        --icon-size 100 \
        --icon "Cortex.app" 175 190 \
        --app-drop-link 425 190 \
        "${DMG_PATH}" \
        "${APP_PATH}" || true  # create-dmg returns non-zero on "already exists"
else
    # Fallback to hdiutil
    rm -f "${DMG_PATH}"
    hdiutil create -volname "Cortex" -srcfolder "${APP_PATH}" -ov -format UDZO "${DMG_PATH}"
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

# Clean up bundled env
rm -f "${ROOT_DIR}/.env.bundled"
