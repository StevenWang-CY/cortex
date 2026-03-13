#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
BUILD_DIR="${ROOT_DIR}/build"

cd "${ROOT_DIR}"

python3 -m pip install -e ".[dev]"

pyinstaller \
  --noconfirm \
  --clean \
  --name Cortex \
  --windowed \
  --paths "${ROOT_DIR}/.." \
  --add-data "${ROOT_DIR}/apps/vscode_extension/media:cortex/apps/vscode_extension/media" \
  cortex/apps/desktop_shell/main.py

APP_PATH="${DIST_DIR}/Cortex.app"
if [[ ! -d "${APP_PATH}" ]]; then
  echo "Expected app bundle not found at ${APP_PATH}" >&2
  exit 1
fi

DMG_PATH="${DIST_DIR}/Cortex.dmg"
hdiutil create -volname "Cortex" -srcfolder "${APP_PATH}" -ov -format UDZO "${DMG_PATH}"

echo "Built ${APP_PATH}"
echo "Built ${DMG_PATH}"
