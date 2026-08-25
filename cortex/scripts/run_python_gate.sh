#!/usr/bin/env bash
# Canonical Python gate used by both pull-request and release workflows.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

UV_RUN=(uv run --project cortex --locked --extra dev --extra codegen)

"${UV_RUN[@]}" ruff check cortex/
"${UV_RUN[@]}" mypy --config-file cortex/pyproject.toml cortex/ --strict

WHEEL_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cortex-wheel.XXXXXX")"
cleanup() {
    rm -rf "${WHEEL_DIR}"
}
trap cleanup EXIT
"${UV_RUN[@]}" python -m pip wheel --no-deps ./cortex --wheel-dir "${WHEEL_DIR}"
"${UV_RUN[@]}" python cortex/scripts/verify_python_artifact.py "${WHEEL_DIR}"/cortex-*.whl

export QT_QPA_PLATFORM=offscreen
"${UV_RUN[@]}" pytest cortex/tests/ --ignore=cortex/tests/unit/test_desktop_shell.py
"${UV_RUN[@]}" pytest cortex/tests/unit/test_desktop_shell.py
