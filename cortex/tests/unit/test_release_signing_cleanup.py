"""Execute the workflow's actual shell against fake Apple tools and secrets."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[3] / ".github/workflows/release.yml"


def _step(name: str) -> str:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return next(step["run"] for step in workflow["jobs"]["build-dmg"]["steps"]
                if step.get("name") == name)


def _executable(path: Path, source: str) -> None:
    path.write_text(f"#!{sys.executable}\n" + source)
    path.chmod(0o700)


@pytest.mark.parametrize("mode", ["api-key", "apple-id"])
@pytest.mark.parametrize("failure", [
    "create-keychain", "set-keychain-settings", "unlock-keychain", "import",
    "set-key-partition-list", "list-keychains", "notary", "",
])
def test_every_signing_exit_removes_private_material(tmp_path: Path, mode: str, failure: str) -> None:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    _executable(binaries / "uv", "import os; print(os.environ['TEST_NOTARY_MODE'])\n")
    _executable(binaries / "security", """
import os, sys
from pathlib import Path
command = sys.argv[1]
path = Path(sys.argv[-1])
if command == 'create-keychain':
    path.touch(mode=0o600)
elif command == 'delete-keychain':
    path.unlink(missing_ok=True)
sys.exit(1 if command == os.environ['TEST_SIGNING_FAILURE'] else 0)
""")
    _executable(binaries / "xcrun", """
import os, sys
from pathlib import Path
for pattern in ('*.p12', '*.p8'):
    for path in Path(os.environ['RUNNER_TEMP']).glob(pattern):
        assert path.stat().st_mode & 0o077 == 0, 'private file is world/group accessible'
if os.environ['TEST_SIGNING_FAILURE'] == 'notary':
    print('HTTP 403: A required agreement is missing or has expired.', file=sys.stderr)
    sys.exit(1)
""")
    environment = {
        **os.environ,
        "PATH": f"{binaries}{os.pathsep}{os.defpath}",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_ENV": str(tmp_path / "github-env"),
        "TEST_NOTARY_MODE": mode,
        "TEST_SIGNING_FAILURE": failure,
        "APPLE_DEVELOPER_ID_P12_BASE64": base64.b64encode(b"fake certificate").decode(),
        "APPLE_DEVELOPER_ID_P12_PASSWORD": "fake-password",
        "TEMP_KEYCHAIN_PASSWORD": "fake-keychain-password",
        "APPLE_NOTARY_KEY_P8_BASE64": base64.b64encode(b"fake key").decode(),
        "APPLE_NOTARY_KEY_ID": "fake-key-id",
        "APPLE_NOTARY_ISSUER_ID": "fake-issuer",
        "APPLE_ID_USERNAME": "example@example.invalid",
        "APPLE_ID_APP_PASSWORD": "fake-app-password",
        "APPLE_TEAM_ID": "fake-team",
    }
    result = subprocess.run(
        ["bash", "-c", _step("Import Developer ID certificate and notary credentials")],
        env=environment, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == (1 if failure else 0), result.stderr
    assert not list(tmp_path.glob("*.p12"))
    assert not list(tmp_path.glob("*.p8"))
    keychain = tmp_path / "cortex-signing.keychain-db"
    assert keychain.exists() is (not failure)
    assert "fake-password" not in result.stdout + result.stderr
    if failure == "notary":
        assert "Account Holder" in result.stdout
    if not failure:
        environment["CORTEX_CI_KEYCHAIN"] = str(keychain)
        cleanup = subprocess.run(
            ["bash", "-c", _step("Remove temporary signing Keychain")],
            env=environment, capture_output=True, text=True, timeout=15,
        )
        assert cleanup.returncode == 0, cleanup.stderr
        assert not keychain.exists()
