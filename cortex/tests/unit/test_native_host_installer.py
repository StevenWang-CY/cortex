"""Release-grade native messaging installer and executable contracts."""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from cortex.scripts import install_native_host as installer
from cortex.scripts import native_host


def test_native_host_imports_do_not_eagerly_load_application_graph() -> None:
    """Short-lived browser hosts must not pay for unrelated app modules."""

    probe = """
import json
import sys
import cortex.libs.config.ports
import cortex.libs.schemas.native_messaging
print(json.dumps({
    name: name in sys.modules
    for name in (
        "cortex.libs.config.settings",
        "cortex.libs.schemas.api",
        "cortex.libs.schemas.intervention_transaction",
        "numpy",
        "matplotlib",
    )
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert json.loads(completed.stdout) == {
        "cortex.libs.config.settings": False,
        "cortex.libs.schemas.api": False,
        "cortex.libs.schemas.intervention_transaction": False,
        "numpy": False,
        "matplotlib": False,
    }


def test_lazy_package_exports_preserve_public_imports() -> None:
    """Latency optimization must not change the established package API."""

    from cortex.libs.config import CortexConfig, get_config, ports, settings
    from cortex.libs.schemas import NativeMessage, StateEstimate

    assert CortexConfig.__name__ == "CortexConfig"
    assert callable(get_config)
    assert settings.__name__ == "cortex.libs.config.settings"
    assert ports.WEBSOCKET_PORT == native_host.WEBSOCKET_PORT
    assert NativeMessage is not None
    assert StateEstimate.__name__ == "StateEstimate"


def _write_protocol_host(path: Path, *, response: dict[str, object]) -> None:
    encoded_response = json.dumps(response).encode("utf-8")
    script = f"""#!{sys.executable}
import sys
request_length = int.from_bytes(sys.stdin.buffer.read(4), "little")
sys.stdin.buffer.read(request_length)
response = {encoded_response!r}
sys.stdout.buffer.write(len(response).to_bytes(4, "little") + response)
sys.stdout.buffer.flush()
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def test_probe_native_host_requires_real_framed_status(tmp_path: Path) -> None:
    host = tmp_path / "CortexNativeHost"
    _write_protocol_host(
        host,
        response={"command": "status", "status": "stopped"},
    )

    assert installer._probe_native_host(str(host)) == {
        "command": "status",
        "status": "stopped",
    }


def test_probe_native_host_rejects_malformed_response(tmp_path: Path) -> None:
    host = tmp_path / "CortexNativeHost"
    _write_protocol_host(host, response={"status": "ok"})

    with pytest.raises(RuntimeError, match="invalid status response"):
        installer._probe_native_host(str(host))


def test_packaged_mode_uses_self_contained_host_not_external_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tmp_path / "Cortex.app"
    host = app / "Contents" / "MacOS" / installer.PACKAGED_HOST_EXECUTABLE
    _write_protocol_host(
        host,
        response={"command": "status", "status": "stopped"},
    )
    monkeypatch.setattr(
        installer,
        "_find_python",
        lambda **_kwargs: pytest.fail("packaged install must not resolve external Python"),
    )

    prepared = installer._prepare_host_script(
        str(tmp_path / "does-not-need-to-exist.py"),
        project_root=str(app),
    )

    assert prepared == str(host.resolve())


def test_targeted_install_creates_only_requested_browser_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chrome_root = tmp_path / "Google" / "Chrome"
    edge_root = tmp_path / "Microsoft Edge"
    host = tmp_path / "CortexNativeHost"
    _write_protocol_host(
        host,
        response={"command": "status", "status": "stopped"},
    )
    monkeypatch.setattr(
        installer,
        "BROWSER_PROFILES",
        {
            "Google Chrome": str(chrome_root),
            "Microsoft Edge": str(edge_root),
        },
    )
    monkeypatch.setattr(
        installer,
        "_prepare_host_script",
        lambda *_args, **_kwargs: str(host),
    )

    assert installer.install(target_browsers=("Chrome",)) is True

    chrome_manifest = (
        chrome_root / "NativeMessagingHosts" / f"{installer.HOST_NAME}.json"
    )
    edge_manifest = edge_root / "NativeMessagingHosts" / f"{installer.HOST_NAME}.json"
    assert chrome_manifest.is_file()
    assert not edge_manifest.exists()
    payload = json.loads(chrome_manifest.read_text(encoding="utf-8"))
    assert payload["path"] == str(host)
    assert payload["allowed_origins"] == [
        f"chrome-extension://{installer.FIXED_EXTENSION_ID}/"
    ]
    assert stat_mode(chrome_manifest) == 0o644


def test_browser_manifests_authorize_only_that_browsers_detected_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chrome_root = tmp_path / "Google" / "Chrome"
    edge_root = tmp_path / "Microsoft Edge"
    chrome_id = "a" * 32
    edge_id = "b" * 32
    for browser_root, extension_id in (
        (chrome_root, chrome_id),
        (edge_root, edge_id),
    ):
        preferences = browser_root / "Default" / "Preferences"
        preferences.parent.mkdir(parents=True)
        preferences.write_text(
            json.dumps(
                {
                    "extensions": {
                        "settings": {
                            extension_id: {
                                "manifest": {"name": "Cortex Workspace Engine"}
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    host = tmp_path / "CortexNativeHost"
    _write_protocol_host(
        host,
        response={"command": "status", "status": "stopped"},
    )
    monkeypatch.setattr(
        installer,
        "BROWSER_PROFILES",
        {
            "Google Chrome": str(chrome_root),
            "Microsoft Edge": str(edge_root),
        },
    )
    monkeypatch.setattr(
        installer,
        "_prepare_host_script",
        lambda *_args, **_kwargs: str(host),
    )

    assert installer.install(target_browsers=("Chrome", "Edge")) is True

    chrome_manifest = json.loads(
        installer._manifest_path("Chrome").read_text(encoding="utf-8")
    )
    edge_manifest = json.loads(
        installer._manifest_path("Edge").read_text(encoding="utf-8")
    )
    fixed_origin = f"chrome-extension://{installer.FIXED_EXTENSION_ID}/"
    assert set(chrome_manifest["allowed_origins"]) == {
        fixed_origin,
        f"chrome-extension://{chrome_id}/",
    }
    assert set(edge_manifest["allowed_origins"]) == {
        fixed_origin,
        f"chrome-extension://{edge_id}/",
    }


def test_scan_recognizes_fixed_id_with_localized_manifest_name(tmp_path: Path) -> None:
    preferences = tmp_path / "Default" / "Preferences"
    preferences.parent.mkdir(parents=True)
    preferences.write_text(
        json.dumps(
            {
                "extensions": {
                    "settings": {
                        installer.FIXED_EXTENSION_ID: {
                            "manifest": {"name": "__MSG_appName__"}
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert installer._scan_browser_for_cortex_ids(str(tmp_path)) == {
        installer.FIXED_EXTENSION_ID
    }


def test_verification_refuses_untrusted_manifest_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_root = tmp_path / "Chrome"
    manifest = (
        browser_root / "NativeMessagingHosts" / f"{installer.HOST_NAME}.json"
    )
    untrusted_host = tmp_path / "untrusted-host"
    _write_protocol_host(
        untrusted_host,
        response={"command": "status", "status": "stopped"},
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "name": installer.HOST_NAME,
                "description": "Cortex",
                "path": str(untrusted_host),
                "type": "stdio",
                "allowed_origins": [
                    f"chrome-extension://{installer.FIXED_EXTENSION_ID}/"
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        installer,
        "BROWSER_PROFILES",
        {"Google Chrome": str(browser_root)},
    )
    monkeypatch.setattr(
        installer,
        "_probe_native_host",
        lambda _path: pytest.fail("untrusted manifest target must not execute"),
    )

    result = installer.verify_browser_installation("Chrome")

    assert result.ok is False
    assert result.manifest_valid is False


def test_verification_rejects_manifest_without_detected_extension_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_root = tmp_path / "Chrome"
    manifest = (
        browser_root / "NativeMessagingHosts" / f"{installer.HOST_NAME}.json"
    )
    host = tmp_path / "CortexNativeHost"
    _write_protocol_host(
        host,
        response={"command": "status", "status": "stopped"},
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "name": installer.HOST_NAME,
                "description": "Cortex",
                "path": str(host),
                "type": "stdio",
                "allowed_origins": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        installer,
        "BROWSER_PROFILES",
        {"Google Chrome": str(browser_root)},
    )

    result = installer.verify_browser_installation("Chrome")

    assert result.ok is False
    assert result.manifest_valid is False
    assert result.error == "native messaging manifest has stale or invalid fields"


def test_native_host_frame_is_little_endian() -> None:
    body = b'{"command":"status"}'
    assert struct.unpack("<I", struct.pack("<I", len(body)))[0] == len(body)


def test_native_host_log_is_bounded_and_outside_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_dir = tmp_path / "Library" / "Logs" / "Cortex"
    log_path = log_dir / "native-host.log"
    monkeypatch.setattr(native_host, "_LOG_DIRECTORY", log_dir)
    monkeypatch.setattr(native_host, "LOG_FILE", log_path)
    monkeypatch.setattr(native_host, "_LOG_MAX_BYTES", 32)
    monkeypatch.setattr(native_host, "_LOG_BACKUP_COUNT", 2)

    native_host.log("first message long enough to rotate")
    native_host.log("second message triggers bounded backup")

    assert log_path.is_file()
    assert log_path.with_name("native-host.log.1").is_file()
    assert stat_mode(log_path) == 0o600
    assert str(log_path).startswith(str(tmp_path))


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
