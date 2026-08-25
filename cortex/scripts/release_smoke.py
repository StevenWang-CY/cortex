"""Fast, side-effect-free verification of source or frozen release resources."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from cortex import __version__

_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_FORBIDDEN_BUNDLED_ENV = (
    b"AWS_BEARER_TOKEN_BEDROCK",
    b"AWS_ACCESS_KEY_ID",
    b"AWS_SECRET_ACCESS_KEY",
    b"ANTHROPIC_API_KEY",
    b"sk-ant-",
    b"/Users/",
)


@dataclass(frozen=True)
class ReleaseSmokeReport:
    schema_version: str
    cortex_version: str
    frozen: bool
    resource_root: str
    checks: dict[str, str]


def resource_root() -> tuple[Path, bool]:
    """Return PyInstaller's resource root or the source checkout root."""

    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        raw = getattr(sys, "_MEIPASS", None)
        if not isinstance(raw, str) or not raw:
            raise RuntimeError("frozen process has no _MEIPASS resource root")
        return Path(raw), True
    return _SOURCE_ROOT, False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_release_resources(root: Path, *, frozen: bool) -> ReleaseSmokeReport:
    """Verify all resources needed for first launch without starting Qt/daemon."""

    if frozen:
        expected_files = {
            "defaults": root / "cortex/libs/config/defaults.yaml",
            "migration_0001": root / "cortex/storage/migrations/0001_initial.sql",
            "migration_0002": root / "cortex/storage/migrations/0002.sql",
            "native_host": root / "cortex/scripts/native_host.py",
            "native_host_installer": root / "cortex/scripts/install_native_host.py",
            "face_landmarker": root / "cortex/models/face_landmarker.task",
            "audio_box": root / "cortex/assets/audio/box_4s.wav",
        }
        expected_directories = {
            "browser_chrome": root / "browser_extension_chrome",
            "browser_edge": root / "browser_extension_edge",
        }
        vsix_matches = tuple(root.glob(f"cortex-somatic-{__version__}.vsix"))
    else:
        project = root / "cortex"
        expected_files = {
            "defaults": project / "libs/config/defaults.yaml",
            "migration_0001": project / "storage/migrations/0001_initial.sql",
            "migration_0002": project / "storage/migrations/0002.sql",
            "native_host": project / "scripts/native_host.py",
            "native_host_installer": project / "scripts/install_native_host.py",
            "face_landmarker": project / "models/face_landmarker.task",
            "audio_box": project / "assets/audio/box_4s.wav",
        }
        expected_directories = {
            "browser_source": project / "apps/browser_extension",
            "vscode_source": project / "apps/vscode_extension",
        }
        vsix_matches = ()

    checks: dict[str, str] = {}
    for name, path in expected_files.items():
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"required release resource is missing or empty: {path}")
        checks[name] = _sha256(path)
    for name, path in expected_directories.items():
        if not path.is_dir() or not any(path.iterdir()):
            raise RuntimeError(f"required release resource directory is empty: {path}")
        checks[name] = "present"
    if frozen:
        if len(vsix_matches) != 1 or vsix_matches[0].stat().st_size == 0:
            raise RuntimeError(f"expected exactly one non-empty cortex-somatic-{__version__}.vsix")
        checks["vscode_vsix"] = _sha256(vsix_matches[0])
        bundled_env = root / ".env"
        if bundled_env.exists():
            contents = bundled_env.read_bytes()
            matched = [
                pattern.decode("ascii", errors="replace")
                for pattern in _FORBIDDEN_BUNDLED_ENV
                if pattern.lower() in contents.lower()
            ]
            if matched:
                raise RuntimeError(
                    "bundled .env contains forbidden credential/path names: " + ", ".join(matched)
                )
            checks["bundled_env"] = _sha256(bundled_env)
    return ReleaseSmokeReport(
        schema_version="1.0",
        cortex_version=__version__,
        frozen=frozen,
        resource_root=str(root),
        checks=checks,
    )


def run_release_smoke() -> ReleaseSmokeReport:
    root, frozen = resource_root()
    return inspect_release_resources(root, frozen=frozen)


def main() -> int:
    try:
        report = run_release_smoke()
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    print(json.dumps({"status": "passed", **asdict(report)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
