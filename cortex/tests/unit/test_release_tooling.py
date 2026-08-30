"""Release evidence and frozen-resource contract tests."""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from cortex import __version__
from cortex.scripts.create_macos_dmg import DmgCreationError, create_macos_dmg
from cortex.scripts.generate_release_evidence import (
    ReleaseEvidenceError,
    _command,
    generate,
    sha256_file,
)
from cortex.scripts.generate_support_model_identity import (
    check as check_support_model_identity,
)
from cortex.scripts.release_smoke import inspect_release_resources
from cortex.scripts.select_notary_auth import (
    API_KEY_VARIABLES,
    APPLE_ID_VARIABLES,
    NotaryAuthConfigurationError,
    select_notary_auth_mode,
)
from cortex.scripts.validate_release_records import (
    REQUIRED_CASE_IDS,
    ReleaseRecordError,
    validate_release_records,
)
from cortex.scripts.verify_macos_release import (
    ReleaseVerificationError,
    _default_personal_roots,
    _detach_mounted_dmg,
    _headless_camera_activity_markers,
    _mounted_app_signature_verification,
    _notarized_container_verification_commands,
    _probe_native_host_executable,
    _remove_detached_mountpoint,
    _scan_forbidden,
    _verify_installer_layout,
)
from cortex.scripts.verify_repository_contracts import (
    _dependency_audit_calls_missing_summary,
)

_ROOT = Path(__file__).resolve().parents[3]


def _credential_environment(names: tuple[str, ...]) -> dict[str, str]:
    return {name: f"fixture-for-{name.lower()}" for name in names}


def test_headless_release_probe_rejects_camera_discovery_and_opening() -> None:
    diagnostics = "\n".join(
        (
            "Enumerated 2 camera(s): built_in=1 other=0 continuity=1",
            "Trying camera candidate: device_id=0",
            "Opened camera device 0 (builtin_mac_camera)",
        )
    )
    assert _headless_camera_activity_markers(diagnostics) == [
        "Enumerated ",
        "Trying camera candidate:",
        "Opened camera device ",
    ]
    assert _headless_camera_activity_markers("WebcamCapture stopped") == []


def test_notary_auth_selector_accepts_each_complete_mode() -> None:
    assert select_notary_auth_mode(_credential_environment(API_KEY_VARIABLES)) == "api-key"
    assert select_notary_auth_mode(_credential_environment(APPLE_ID_VARIABLES)) == "apple-id"


@pytest.mark.parametrize("group", [API_KEY_VARIABLES, APPLE_ID_VARIABLES])
def test_notary_auth_selector_rejects_every_partial_mode(group: tuple[str, ...]) -> None:
    for omitted in group:
        environment = _credential_environment(group)
        environment.pop(omitted)
        with pytest.raises(NotaryAuthConfigurationError, match=omitted):
            select_notary_auth_mode(environment)


def test_notary_auth_selector_rejects_missing_or_mixed_modes() -> None:
    with pytest.raises(NotaryAuthConfigurationError, match="missing notarization"):
        select_notary_auth_mode({})

    both = _credential_environment((*API_KEY_VARIABLES, *APPLE_ID_VARIABLES))
    with pytest.raises(NotaryAuthConfigurationError, match="exactly one"):
        select_notary_auth_mode(both)


def test_release_workflow_wires_both_notary_modes_through_selector() -> None:
    workflow = (_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    for variable in (*API_KEY_VARIABLES, *APPLE_ID_VARIABLES):
        assert f"{variable}: ${{{{ secrets.{variable} }}}}" in workflow
    assert "-m cortex.scripts.select_notary_auth" in workflow
    assert '--key "${NOTARY_KEY_PATH}"' in workflow
    assert '--apple-id "${APPLE_ID_USERNAME}"' in workflow
    assert '--password "${APPLE_ID_APP_PASSWORD}"' in workflow
    assert '--team-id "${APPLE_TEAM_ID}"' in workflow


def test_release_workflow_stages_versioned_changelog_notes() -> None:
    workflow = (_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert 'HEADING="## [${RELEASE_TAG}]"' in workflow
    assert "body_path: ${{ env.CORTEX_RELEASE_NOTES }}" in workflow
    assert "generate_release_notes: false" in workflow


def test_generated_release_tag_example_tracks_the_canonical_version() -> None:
    from cortex.scripts.sync_config_docs import OPERATIONAL_VARIABLES

    release_tag = next(
        variable
        for variable in OPERATIONAL_VARIABLES
        if variable.name == "CORTEX_RELEASE_TAG"
    )
    assert release_tag.example == f"v{__version__}"


def test_release_tag_gate_rechecks_all_shipped_client_surfaces() -> None:
    workflow = (_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "git merge-base --is-ancestor HEAD origin/main" in workflow
    assert "pnpm --dir cortex/apps/browser_extension exec tsc --noEmit" in workflow
    assert "pnpm --dir cortex/apps/browser_extension test" in workflow
    assert "plasmo build --target=edge-mv3" in workflow
    assert "npm --prefix cortex/apps/vscode_extension test" in workflow
    assert "npm --prefix cortex/apps/vscode_extension run package:vsix" in workflow
    verifier_segments = workflow.split("verify_dependency_audit.py")[1:]
    assert len(verifier_segments) >= 3
    assert all("--summary-out" in segment for segment in verifier_segments)


def test_dependency_audit_workflow_contract_identifies_the_exact_missing_call() -> None:
    run_block = """
    python verify_dependency_audit.py --ecosystem pip --summary-out pip.json
    python verify_dependency_audit.py --ecosystem pnpm
    python verify_dependency_audit.py --ecosystem npm --summary-out npm.json
    """

    assert _dependency_audit_calls_missing_summary(run_block) == (2,)


def test_release_workflow_attests_every_standalone_checksum_manifest() -> None:
    release_workflow = (_ROOT / ".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )
    publish_workflow = (_ROOT / ".github/workflows/publish-release.yml").read_text(
        encoding="utf-8"
    )

    assert (
        "subject-path: dist/evidence-${{ matrix.arch }}/SHA256SUMS-${{ matrix.arch }}"
        in release_workflow
    )
    assert "release-assets/SHA256SUMS-arm64" in publish_workflow
    assert "release-assets/SHA256SUMS-x86_64" in publish_workflow
    assert "release-assets/Cortex-*-macos-arm64-evidence.zip" in publish_workflow
    assert "release-assets/Cortex-*-macos-x86_64-evidence.zip" in publish_workflow


def test_make_targets_never_run_process_global_qt_stubs_in_parent() -> None:
    makefile = (_ROOT / "Makefile").read_text(encoding="utf-8")

    unsafe_invocation = "$(PYTEST) cortex/tests/unit/test_desktop_shell.py"
    assert unsafe_invocation not in makefile
    assert makefile.count("--ignore=cortex/tests/unit/test_desktop_shell.py") == 2


def test_macos_builder_preserves_caller_selected_node_before_gui_fallbacks() -> None:
    build_script = (_ROOT / "cortex/scripts/build_macos_app.sh").read_text(encoding="utf-8")
    assert 'export PATH="${PATH}:/opt/homebrew/bin:/usr/local/bin"' in build_script
    assert 'export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"' not in build_script


def test_macos_builder_keeps_temporary_env_inputs_outside_checkout() -> None:
    build_script = (_ROOT / "cortex/scripts/build_macos_app.sh").read_text(encoding="utf-8")

    assert '"${ROOT_DIR}/.env.bundled"' not in build_script
    assert (
        'BUNDLED_ENV_PATH="$(mktemp "${TMPDIR:-/tmp}/cortex-bundled-env.XXXXXX")"'
        in build_script
    )
    assert 'ENV_BACKUP_PATH="$(mktemp "${TMPDIR:-/tmp}/cortex-env-backup.XXXXXX")"' in (
        build_script
    )
    assert 'cp "${BUNDLED_ENV_PATH}" "${ROOT_DIR}/.env"' in build_script
    assert '--require-clean' in build_script


def test_macos_builder_signs_and_architecture_checks_native_host() -> None:
    build_script = (_ROOT / "cortex/scripts/build_macos_app.sh").read_text(
        encoding="utf-8"
    )

    assert 'NATIVE_HOST_PATH="${APP_PATH}/Contents/MacOS/CortexNativeHost"' in (
        build_script
    )
    assert 'if [ ! -x "${NATIVE_HOST_PATH}" ]; then' in build_script
    assert 'codesign --force --sign - "${NATIVE_HOST_PATH}"' in build_script
    assert 'codesign --verify --strict --verbose=2 "${NATIVE_HOST_PATH}"' in (
        build_script
    )
    assert 'NATIVE_HOST_ARCHS=$(lipo -archs "${NATIVE_HOST_PATH}")' in build_script
    assert "Production notarized builds cannot skip app executable probes" in (
        build_script
    )


def test_production_signing_reseals_least_privileged_native_host() -> None:
    build_script = (_ROOT / "cortex/scripts/build_macos_app.sh").read_text(
        encoding="utf-8"
    )
    verifier = (_ROOT / "cortex/scripts/verify_macos_release.py").read_text(
        encoding="utf-8"
    )
    production_start = build_script.index("else\n    # Developer ID:")
    production_end = build_script.index("\nfi\n", production_start)
    production_block = build_script[production_start:production_end]

    deep_index = production_block.index("--deep")
    helper_index = production_block.index('"${NATIVE_HOST_PATH}"', deep_index)
    final_outer_index = production_block.rindex('"${APP_PATH}"')

    assert deep_index < helper_index < final_outer_index
    assert "--deep" not in production_block[helper_index:final_outer_index]
    for entitlement in (
        "com.apple.security.device.camera",
        "com.apple.security.device.audio-input",
        "com.apple.security.automation.apple-events",
    ):
        assert entitlement in verifier


def test_production_macos_builder_signs_outer_dmg_before_notarization() -> None:
    build_script = (_ROOT / "cortex/scripts/build_macos_app.sh").read_text(encoding="utf-8")
    integrity_check = 'if ! hdiutil verify "${DMG_PATH}"; then'
    production_guard = 'if [ "${SIGN_IDENTITY}" != "-" ]; then'
    dmg_signature = 'codesign --sign "${SIGN_IDENTITY}" \\'
    secure_timestamp = '--timestamp \\'
    stable_identifier = '--identifier "com.cortex.daemon.dmg" \\'
    signature_check = 'codesign --verify --strict --verbose=2 "${DMG_PATH}"'
    notary_submission = 'submit "${DMG_PATH}"'

    integrity_index = build_script.index(integrity_check)
    production_guard_index = build_script.index(production_guard, integrity_index)
    signature_index = build_script.index(dmg_signature, production_guard_index)
    verification_index = build_script.index(signature_check, signature_index)
    notary_index = build_script.index(notary_submission, verification_index)

    assert secure_timestamp in build_script[signature_index:verification_index]
    assert stable_identifier in build_script[signature_index:verification_index]
    assert integrity_index < production_guard_index < signature_index
    assert signature_index < verification_index < notary_index


def test_macos_builder_owns_drag_to_applications_layout_before_image_creation() -> None:
    build_script = (_ROOT / "cortex/scripts/build_macos_app.sh").read_text(encoding="utf-8")
    staging_copy = 'cp -R "${APP_PATH}" "${DMG_STAGE_DIR}/Cortex.app"'
    applications_link = 'ln -s /Applications "${DMG_STAGE_DIR}/Applications"'
    versioned_volume = 'DMG_VOLUME_NAME="Cortex ${CORTEX_VERSION}"'
    canonical_builder = '"${PYTHON_BIN}" -m cortex.scripts.create_macos_dmg \\'

    volume_index = build_script.index(versioned_volume)
    copy_index = build_script.index(staging_copy)
    link_index = build_script.index(applications_link, copy_index)
    builder_index = build_script.index(canonical_builder, link_index)

    assert volume_index < copy_index < link_index < builder_index
    assert '--volume-name "${DMG_VOLUME_NAME}"' in build_script[builder_index:]
    assert '--source-dir "${DMG_STAGE_DIR}"' in build_script[builder_index:]
    assert '--output "${DMG_PATH}"' in build_script[builder_index:]
    assert '--evidence-dir "${EVIDENCE_DIR}"' in build_script[builder_index:]
    assert "create-dmg" not in build_script
    assert "--app-drop-link" not in build_script


def test_macos_dmg_creation_retries_resource_busy_and_keeps_evidence(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "stage"
    source_dir.mkdir()
    output = tmp_path / "Cortex.dmg"
    evidence_dir = tmp_path / "evidence"
    commands: list[list[str]] = []
    delays: list[float] = []

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        assert not output.exists(), "partial DMG was not removed before retry"
        commands.append(command)
        if len(commands) < 3:
            output.write_bytes(b"partial")
            transcript = (
                "DIHLDiskImageCreate() returned 16\n"
                if len(commands) == 1
                else "hdiutil: create failed - Resource busy\n"
            )
            return subprocess.CompletedProcess(
                command,
                1,
                stdout=transcript,
            )
        output.write_bytes(b"dmg")
        return subprocess.CompletedProcess(command, 0, stdout="created: Cortex.dmg\n")

    attempt = create_macos_dmg(
        volume_name=f"Cortex {__version__}",
        source_dir=source_dir,
        output=output,
        evidence_dir=evidence_dir,
        max_attempts=3,
        retry_delay_seconds=2,
        runner=fake_runner,
        sleeper=delays.append,
    )

    assert attempt == 3
    assert len(commands) == 3
    assert commands[0] == [
        "hdiutil",
        "create",
        "-verbose",
        "-volname",
        f"Cortex {__version__}",
        "-srcfolder",
        str(source_dir),
        "-ov",
        "-format",
        "UDZO",
        str(output),
    ]
    assert delays == [2, 4]
    assert output.read_bytes() == b"dmg"
    assert sorted(path.name for path in evidence_dir.iterdir()) == [
        "hdiutil-create-attempt-1.txt",
        "hdiutil-create-attempt-2.txt",
        "hdiutil-create-attempt-3.txt",
    ]


def test_macos_dmg_creation_does_not_retry_non_transient_failure(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "stage"
    source_dir.mkdir()
    output = tmp_path / "Cortex.dmg"
    calls = 0

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        output.write_bytes(b"partial")
        return subprocess.CompletedProcess(
            command,
            1,
            stdout=(
                "DIHLDiskImageCreate() returned 2\n"
                "hdiutil: create failed - localized missing-file error\n"
            ),
        )

    with pytest.raises(DmgCreationError, match="non-transient exit 1"):
        create_macos_dmg(
            volume_name=f"Cortex {__version__}",
            source_dir=source_dir,
            output=output,
            evidence_dir=tmp_path / "evidence",
            runner=fake_runner,
            sleeper=lambda _delay: pytest.fail("non-transient failure retried"),
        )

    assert calls == 1
    assert not output.exists()


def test_macos_dmg_creation_fails_after_bounded_busy_retries(tmp_path: Path) -> None:
    source_dir = tmp_path / "stage"
    source_dir.mkdir()
    output = tmp_path / "Cortex.dmg"
    calls = 0

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        output.write_bytes(b"partial")
        return subprocess.CompletedProcess(command, 1, stdout="Resource busy\n")

    with pytest.raises(DmgCreationError, match="remained busy after 2 attempts"):
        create_macos_dmg(
            volume_name=f"Cortex {__version__}",
            source_dir=source_dir,
            output=output,
            evidence_dir=tmp_path / "evidence",
            max_attempts=2,
            retry_delay_seconds=0,
            runner=fake_runner,
            sleeper=lambda _delay: None,
        )

    assert calls == 2
    assert not output.exists()


def test_notarized_dmg_verification_requires_signature_ticket_and_gatekeeper() -> None:
    artifact = Path(f"/tmp/Cortex-{__version__}-macos-arm64.dmg")

    assert _notarized_container_verification_commands(artifact) == (
        ["codesign", "--verify", "--strict", "--verbose=2", str(artifact)],
        ["codesign", "-dv", "--verbose=4", str(artifact)],
        ["xcrun", "stapler", "validate", str(artifact)],
        [
            "spctl",
            "-a",
            "-vv",
            "--type",
            "open",
            "--context",
            "context:primary-signature",
            str(artifact),
        ],
    )


def test_mounted_app_deep_signature_verification_has_bounded_intel_budget() -> None:
    app = Path("/tmp/Cortex.app")

    assert _mounted_app_signature_verification(app) == (
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)],
        300.0,
    )


def test_installer_layout_requires_applications_symlink(tmp_path: Path) -> None:
    applications_link = tmp_path / "Applications"
    applications_link.symlink_to("/Applications")

    assert _verify_installer_layout(tmp_path) == {"applications_link": "/Applications"}


@pytest.mark.parametrize(
    ("kind", "message"),
    (
        ("missing", "required Applications symlink"),
        ("directory", "required Applications symlink"),
        ("wrong-target", "expected '/Applications'"),
    ),
)
def test_installer_layout_rejects_incomplete_or_misdirected_drag_target(
    tmp_path: Path,
    kind: str,
    message: str,
) -> None:
    applications_link = tmp_path / "Applications"
    if kind == "directory":
        applications_link.mkdir()
    elif kind == "wrong-target":
        applications_link.symlink_to("/System/Applications")

    with pytest.raises(ReleaseVerificationError, match=message):
        _verify_installer_layout(tmp_path)


def test_read_only_dmg_detach_retries_then_forces_without_recursive_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from cortex.scripts import verify_macos_release as verifier

    mount = tmp_path / "mounted-image"
    mount.mkdir()
    responses = iter(
        (
            {"returncode": 16, "stdout": "", "stderr": "Resource busy"},
            {"returncode": 16, "stdout": "", "stderr": "Resource busy"},
            {"returncode": 0, "stdout": "detached", "stderr": ""},
        )
    )
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> dict[str, Any]:
        commands.append(command)
        return next(responses)

    monkeypatch.setattr(verifier, "_run", fake_run)
    monkeypatch.setattr(verifier.os.path, "ismount", lambda _path: True)

    results = _detach_mounted_dmg(mount, normal_attempts=2, retry_delay_seconds=0)

    assert [result["returncode"] for result in results] == [16, 16, 0]
    assert commands == [
        ["hdiutil", "detach", str(mount)],
        ["hdiutil", "detach", str(mount)],
        ["hdiutil", "detach", "-force", str(mount)],
    ]


def test_read_only_dmg_detach_accepts_nonzero_exit_after_volume_disappears(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from cortex.scripts import verify_macos_release as verifier

    mount = tmp_path / "mounted-image"
    mount.mkdir()
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> dict[str, Any]:
        commands.append(command)
        return {"returncode": 16, "stdout": "", "stderr": "not mounted"}

    monkeypatch.setattr(verifier, "_run", fake_run)
    monkeypatch.setattr(verifier.os.path, "ismount", lambda _path: False)

    results = _detach_mounted_dmg(mount, retry_delay_seconds=0)

    assert [result["returncode"] for result in results] == [16]
    assert commands == [["hdiutil", "detach", str(mount)]]


def test_read_only_dmg_detach_fails_closed_when_force_cannot_unmount(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from cortex.scripts import verify_macos_release as verifier

    mount = tmp_path / "mounted-image"
    mount.mkdir()
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> dict[str, Any]:
        commands.append(command)
        return {"returncode": 16, "stdout": "", "stderr": "Resource busy"}

    monkeypatch.setattr(verifier, "_run", fake_run)
    monkeypatch.setattr(verifier.os.path, "ismount", lambda _path: True)

    with pytest.raises(ReleaseVerificationError, match="one forced attempt"):
        _detach_mounted_dmg(mount, normal_attempts=1, retry_delay_seconds=0)

    assert commands == [
        ["hdiutil", "detach", str(mount)],
        ["hdiutil", "detach", "-force", str(mount)],
    ]


@pytest.mark.parametrize(
    ("normal_attempts", "retry_delay_seconds", "message"),
    (
        (0, 0.0, "normal_attempts must be positive"),
        (1, -0.1, "retry_delay_seconds must be non-negative"),
    ),
)
def test_read_only_dmg_detach_rejects_invalid_retry_policy(
    normal_attempts: int,
    retry_delay_seconds: float,
    message: str,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match=message):
        _detach_mounted_dmg(
            tmp_path / "mounted-image",
            normal_attempts=normal_attempts,
            retry_delay_seconds=retry_delay_seconds,
        )


def test_read_only_dmg_cleanup_refuses_to_walk_an_attached_mount(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from cortex.scripts import verify_macos_release as verifier

    mount = tmp_path / "mounted-image"
    mount.mkdir()
    code_resources = mount / "Cortex.app/Contents/_CodeSignature/CodeResources"
    code_resources.parent.mkdir(parents=True)
    code_resources.write_text("signed", encoding="utf-8")
    monkeypatch.setattr(verifier.os.path, "ismount", lambda _path: True)

    with pytest.raises(ReleaseVerificationError, match="still attached"):
        _remove_detached_mountpoint(mount)

    assert code_resources.read_text(encoding="utf-8") == "signed"


def test_release_verifier_does_not_delegate_mount_to_recursive_tempfile_cleanup() -> None:
    source = (_ROOT / "cortex/scripts/verify_macos_release.py").read_text(encoding="utf-8")

    assert 'with tempfile.TemporaryDirectory(prefix="cortex-release-")' not in source
    assert 'tempfile.mkdtemp(prefix="cortex-release-")' in source
    assert "mount.rmdir()" in source


def test_macos_spec_packages_only_sql_migration_resources() -> None:
    spec = (_ROOT / "cortex/scripts/cortex.spec").read_text(encoding="utf-8")
    migration_root = 'CORTEX / "storage" / "migrations"'
    assert f'{migration_root} / "*.sql"' in spec
    assert f'(str({migration_root}), "cortex/storage/migrations")' not in spec


def test_macos_spec_uses_crash_visible_bootstrap_entrypoint() -> None:
    spec = (_ROOT / "cortex/scripts/cortex.spec").read_text(encoding="utf-8")

    assert '"desktop_shell" / "bootstrap.py"' in spec
    assert '"desktop_shell" / "main.py"' not in spec


def test_macos_spec_declares_continuity_camera_device_type() -> None:
    spec = (_ROOT / "cortex/scripts/cortex.spec").read_text(encoding="utf-8")

    assert '"NSCameraUseContinuityCameraDeviceType": True' in spec


def test_macos_spec_packages_bundled_display_fonts() -> None:
    spec = (_ROOT / "cortex/scripts/cortex.spec").read_text(encoding="utf-8")

    assert 'CORTEX / "assets" / "fonts"' in spec
    assert '"cortex/assets/fonts"' in spec


def test_macos_spec_builds_console_native_host_as_a_nested_executable() -> None:
    spec = (_ROOT / "cortex/scripts/cortex.spec").read_text(encoding="utf-8")

    assert '[str(CORTEX / "scripts" / "native_host.py")]' in spec
    assert 'name="CortexNativeHost"' in spec
    native_exe = spec[spec.index("native_host_exe = EXE(") :]
    assert "console=True" in native_exe
    collect = spec[spec.index("coll = COLLECT(") :]
    assert "native_host_exe" in collect
    assert "native_host_a.binaries" in collect
    assert "native_host_a.datas" in collect


def test_release_native_host_probe_validates_real_framing(tmp_path: Path) -> None:
    host = tmp_path / "CortexNativeHost"
    host.write_text(
        f"""#!{sys.executable}
import json
import struct
import sys
size = struct.unpack('<I', sys.stdin.buffer.read(4))[0]
request = json.loads(sys.stdin.buffer.read(size))
response = json.dumps({{'command': request['command'], 'status': 'stopped'}}).encode()
sys.stdout.buffer.write(struct.pack('<I', len(response)) + response)
sys.stdout.buffer.flush()
""",
        encoding="utf-8",
    )
    host.chmod(0o755)

    evidence = _probe_native_host_executable(
        host,
        base_env={"PATH": "/usr/bin:/bin", "TMPDIR": str(tmp_path)},
    )

    assert evidence["response"] == {"command": "status", "status": "stopped"}
    assert evidence["returncode"] == 0


def test_settings_slider_stylesheet_has_balanced_rule_boundaries() -> None:
    from cortex.apps.desktop_shell.settings import _SLIDER_QSS

    assert _SLIDER_QSS.count("{") == _SLIDER_QSS.count("}")
    assert "}}" not in _SLIDER_QSS


def test_generated_support_model_identity_is_current() -> None:
    assert check_support_model_identity() == []


def _write_approved_release_record(
    root: Path,
    *,
    architecture: str,
    commit: str,
    builder_id: str = "builder-one",
    reviewer_id: str = "reviewer-one",
) -> Path:
    artifact_name = f"Cortex-{__version__}-macos-{architecture}.dmg"
    artifact = root / artifact_name
    artifact.write_bytes(f"artifact-{architecture}".encode())
    artifact_sha256 = sha256_file(artifact)
    (root / f"Cortex-{__version__}-macos-{architecture}-evidence.zip").write_bytes(
        b"automated evidence"
    )
    (root / f"SHA256SUMS-{architecture}").write_text(
        f"{artifact_sha256}  {artifact_name}\n",
        encoding="utf-8",
    )
    evidence_name = f"manual-evidence-{architecture}.zip"
    (root / evidence_name).write_bytes(b"evidence")
    record = {
        "schema_version": "1.0",
        "artifact": {
            "version": __version__,
            "filename": artifact_name,
            "sha256": artifact_sha256,
            "git_commit": commit,
            "notarized": True,
        },
        "host": {
            "architecture": architecture,
            "macos_version": "15.0",
            "device_class": "clean test fixture",
            "clean_profile": True,
        },
        "cases": [
            {
                "id": case_id,
                "result": "passed",
                "observed": f"Executed {case_id} against the exact candidate artifact.",
                "evidence_files": [evidence_name],
            }
            for case_id in sorted(REQUIRED_CASE_IDS)
        ],
        "reviewers": [
            {
                "role": "builder",
                "reviewer_id": builder_id,
                "signed_at_utc": "2026-08-25T12:00:00Z",
                "attestation": "I built and executed this candidate record.",
            },
            {
                "role": "independent_reviewer",
                "reviewer_id": reviewer_id,
                "signed_at_utc": "2026-08-25T13:00:00Z",
                "attestation": "I independently reviewed this candidate evidence.",
            },
        ],
        "decision": "release",
    }
    path = root / f"manual-release-evidence-{architecture}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_source_release_smoke_covers_critical_resources() -> None:
    report = inspect_release_resources(_ROOT, frozen=False)
    assert report.cortex_version == __version__
    assert report.checks["face_landmarker"] != ""
    assert report.checks["font_display"] != ""
    assert report.checks["font_display_italic"] != ""
    assert report.checks["font_license"] != ""
    assert report.checks["migration_0002"] != ""
    assert report.checks["browser_source"] == "present"
    assert len(report.checks["support_model_identity"]) == 64
    assert report.checks["support_model_registry"].startswith(
        "deterministic-support/"
    )
    assert report.checks["support_inference"] == "constructed"


def test_frozen_release_smoke_rejects_secret_bearing_env(tmp_path: Path) -> None:
    required = (
        "cortex/libs/config/defaults.yaml",
        "cortex/storage/migrations/0001_initial.sql",
        "cortex/storage/migrations/0002.sql",
        "cortex/scripts/native_host.py",
        "cortex/scripts/install_native_host.py",
        "cortex/models/face_landmarker.task",
        "cortex/assets/audio/box_4s.wav",
        "cortex/assets/fonts/CormorantGaramond[wght].ttf",
        "cortex/assets/fonts/CormorantGaramond-Italic[wght].ttf",
        "cortex/assets/fonts/OFL.txt",
    )
    for relative in required:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    for relative in ("browser_extension_chrome", "browser_extension_edge"):
        directory = tmp_path / relative
        directory.mkdir()
        (directory / "manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / f"cortex-somatic-{__version__}.vsix").write_bytes(b"fixture")
    (tmp_path / ".env").write_text("AWS_BEARER_TOKEN_BEDROCK=must-not-ship\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="forbidden"):
        inspect_release_resources(tmp_path, frozen=True)


def test_release_evidence_hashes_every_input(tmp_path: Path) -> None:
    artifact = tmp_path / f"Cortex-{__version__}-macos-arm64.dmg"
    sbom = tmp_path / "cortex-app.spdx.json"
    verification = tmp_path / "release-verification.json"
    artifact.write_bytes(b"immutable artifact")
    sbom.write_text('{"spdxVersion":"SPDX-2.3"}\n', encoding="utf-8")
    verification.write_text('{"status":"passed"}\n', encoding="utf-8")
    output = tmp_path / "evidence"

    metadata_path, sums_path = generate(
        artifact,
        sboms=(sbom,),
        verification=verification,
        output_dir=output,
        expected_tag=None,
        require_clean=False,
        checksum_name="SHA256SUMS-arm64",
    )

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    records = {item["path"]: item for item in metadata["inputs"]}
    assert records[artifact.name]["sha256"] == sha256_file(artifact)
    sums = sums_path.read_text(encoding="utf-8")
    assert artifact.name in sums
    assert sbom.name in sums
    assert verification.name in sums
    assert metadata_path.name in sums
    assert sums_path.name == "SHA256SUMS-arm64"


def test_release_evidence_records_missing_optional_builder_tool() -> None:
    assert _command(["cortex-builder-tool-that-does-not-exist", "--version"]) == (
        "unavailable (not found)"
    )


def test_release_evidence_rejects_checksum_paths(tmp_path: Path) -> None:
    artifact = tmp_path / f"Cortex-{__version__}-macos-arm64.dmg"
    artifact.write_bytes(b"artifact")

    with pytest.raises(ReleaseEvidenceError, match="checksum_name"):
        generate(
            artifact,
            sboms=(),
            verification=None,
            output_dir=tmp_path / "evidence",
            expected_tag=None,
            require_clean=False,
            checksum_name="../SHA256SUMS",
        )


def test_release_evidence_rejects_ambiguous_checksum_basenames(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / f"Cortex-{__version__}-macos-arm64.dmg"
    artifact.write_bytes(b"artifact")
    first = tmp_path / "one" / "sbom.json"
    second = tmp_path / "two" / "sbom.json"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("{}", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")

    with pytest.raises(ReleaseEvidenceError, match="unique basenames"):
        generate(
            artifact,
            sboms=(first, second),
            verification=None,
            output_dir=tmp_path / "evidence",
            expected_tag=None,
            require_clean=False,
        )


def test_release_bundle_scan_finds_embedded_credentials(tmp_path: Path) -> None:
    (tmp_path / "safe.txt").write_text("nothing sensitive", encoding="utf-8")
    credential = b"".join((b"sk-", b"ant-", b"abcdefghijklmnop123456"))
    (tmp_path / "unsafe.bin").write_bytes(b"prefix " + credential + b" suffix")
    findings = _scan_forbidden(tmp_path, personal_roots=())
    assert findings == ["unsafe.bin matches credential rule anthropic-api-key"]


@pytest.mark.parametrize(
    ("payload", "expected_rule"),
    [
        (
            b"AWS_SECRET_ACCESS_KEY=" + b"abcdefghijklmnopqrstuvwxyz1234567890ABCD",
            "credential-assignment",
        ),
        (
            b"-----BEGIN PRIVATE KEY-----\n"
            + b"A" * 64
            + b"\n"
            + b"B" * 64
            + b"\n-----END PRIVATE KEY-----",
            "private-key",
        ),
        (b"AKIA" + b"ABCDEFGHIJKLMNOP", "aws-access-key-id"),
        (b"ASIA" + b"ABCDEFGHIJKLMNOP", "aws-access-key-id"),
        (b"sk-" + b"proj-" + b"abcdefghijklmnopqrstuvwxyz123456", "openai-api-key"),
        (b"ghp_" + b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJ", "github-token"),
        (b"github_" + b"pat_" + b"abcdefghijklmnopqrstuv", "github-token"),
        (b"ghs_123456_" + b"header.payload.signature-material", "github-token"),
        (b"xoxb-" + b"1234567890-abcdefghijkl", "slack-token"),
        (b"xapp-" + b"1234567890-abcdefghijkl", "slack-token"),
        (b"xwfp-" + b"1234567890-abcdefghijkl", "slack-token"),
    ],
    ids=(
        "aws-secret-assignment",
        "private-key",
        "aws-long-term-access-id",
        "aws-temporary-access-id",
        "openai-project-key",
        "github-classic-pat",
        "github-fine-grained-pat",
        "github-stateless-installation-token",
        "slack-bot-token",
        "slack-app-token",
        "slack-workflow-token",
    ),
)
def test_release_bundle_scan_rejects_high_confidence_secret_forms(
    tmp_path: Path,
    payload: bytes,
    expected_rule: str,
) -> None:
    (tmp_path / "unsafe.bin").write_bytes(payload)

    assert _scan_forbidden(tmp_path, personal_roots=()) == [
        f"unsafe.bin matches credential rule {expected_rule}"
    ]


def test_release_bundle_scan_detects_pattern_across_read_boundary(tmp_path: Path) -> None:
    prefix = b"x" * (1024 * 1024 - 3)
    credential = b"".join((b"sk-", b"ant-", b"abcdefghijklmnop123456"))
    (tmp_path / "boundary.bin").write_bytes(prefix + b" " + credential)

    assert _scan_forbidden(tmp_path, personal_roots=()) == [
        "boundary.bin matches credential rule anthropic-api-key"
    ]


def test_release_bundle_scan_detects_long_token_across_read_boundary(tmp_path: Path) -> None:
    prefix = b"x" * (1024 * 1024 - 1001) + b" "
    credential = b"".join((b"ghs_123456_", b"a" * 1500))
    (tmp_path / "boundary.bin").write_bytes(prefix + credential + b" ")

    assert _scan_forbidden(tmp_path, personal_roots=()) == [
        "boundary.bin matches credential rule github-token"
    ]


def test_release_bundle_scan_ignores_detector_literals_and_generic_runner_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "detector.js").write_bytes(rb"/\bsk-ant-[A-Za-z0-9_-]{16,}\b/gu")
    (tmp_path / "dependency.bin").write_bytes(
        b"debug metadata /Users/runner/work/dependency/source.c"
    )

    assert _scan_forbidden(tmp_path, personal_roots=()) == []


@pytest.mark.parametrize(
    "placeholder",
    (
        b"AKIAIOSFODNN7EXAMPLE",
        b"AKIAI44QH8DHBEXAMPLE",
        b"AKIA111111111EXAMPLE",
        b"AKIA222222222EXAMPLE",
    ),
)
def test_release_bundle_scan_ignores_official_aws_example_ids(
    tmp_path: Path,
    placeholder: bytes,
) -> None:
    (tmp_path / "examples.json").write_bytes(b'{"AccessKeyId":"' + placeholder + b'"}')

    assert _scan_forbidden(tmp_path, personal_roots=(), sensitive_values=()) == []


def test_release_bundle_scan_ignores_parser_markers_and_token_shapes_in_opaque_binaries(
    tmp_path: Path,
) -> None:
    payload = b"".join(
        (
            b"\xcf\xfa\xed\xfe\x00\x00\x00\x00",
            b"-----BEGIN PRIVATE KEY-----",
            b"sk-",
            b"abcdefghijklmnopqrstuvwxyz1234567890",
            b"ghp_",
            b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJ",
        )
    )
    (tmp_path / "libcrypto-fixture.dylib").write_bytes(payload)

    assert _scan_forbidden(tmp_path, personal_roots=(), sensitive_values=()) == []


def test_release_bundle_scan_finds_exact_sensitive_value_in_opaque_binary(
    tmp_path: Path,
) -> None:
    secret = b"release-secret-value-1234567890"
    (tmp_path / "compiled.bin").write_bytes(b"\x00\xffprefix" + secret + b"suffix")

    assert _scan_forbidden(
        tmp_path,
        personal_roots=(),
        sensitive_values=(("TEST_RELEASE_SECRET", secret),),
    ) == ["compiled.bin contains exact sensitive value TEST_RELEASE_SECRET"]


def test_release_bundle_scan_finds_exact_sensitive_value_across_read_boundary(
    tmp_path: Path,
) -> None:
    secret = b"exact-release-secret-crossing-the-stream-boundary"
    prefix = b"\x00" + b"x" * (1024 * 1024 - 12)
    (tmp_path / "compiled.bin").write_bytes(prefix + secret + b"suffix")

    assert _scan_forbidden(
        tmp_path,
        personal_roots=(),
        sensitive_values=(("TEST_RELEASE_SECRET", secret),),
    ) == ["compiled.bin contains exact sensitive value TEST_RELEASE_SECRET"]


def test_release_bundle_scan_requires_complete_private_key_not_parser_header(
    tmp_path: Path,
) -> None:
    (tmp_path / "pem-parser.txt").write_bytes(
        b"accepted marker: -----BEGIN PRIVATE KEY-----"
    )

    assert _scan_forbidden(tmp_path, personal_roots=(), sensitive_values=()) == []


@pytest.mark.parametrize(
    "kind,preamble",
    (
        (b"PRIVATE KEY", b""),
        (b"ENCRYPTED PRIVATE KEY", b""),
        (
            b"RSA PRIVATE KEY",
            b"Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-256-CBC,0123456789ABCDEF\n\n",
        ),
    ),
)
def test_release_bundle_scan_finds_compact_and_encrypted_private_keys(
    tmp_path: Path,
    kind: bytes,
    preamble: bytes,
) -> None:
    (tmp_path / "private-key.pem").write_bytes(
        b"-----BEGIN "
        + kind
        + b"-----\n"
        + preamble
        + b"A" * 64
        + b"\n-----END "
        + kind
        + b"-----"
    )

    assert _scan_forbidden(
        tmp_path,
        personal_roots=(),
        sensitive_values=(),
    ) == ["private-key.pem matches credential rule private-key"]


@pytest.mark.parametrize(
    "declaration",
    (
        b'AWS_SECRET_ACCESS_KEY = "AWS_SECRET_ACCESS_KEY"',
        b"aws_secret_access_key=aws_secret_access_key",
        b"ANTHROPIC_API_KEY = ANTHROPIC_API_KEY",
    ),
)
def test_release_bundle_scan_ignores_same_named_sdk_constants_and_parameters(
    tmp_path: Path,
    declaration: bytes,
) -> None:
    (tmp_path / "sdk.py").write_bytes(declaration)

    assert _scan_forbidden(tmp_path, personal_roots=(), sensitive_values=()) == []


def test_release_bundle_scan_still_checks_personal_roots_in_opaque_binaries(
    tmp_path: Path,
) -> None:
    (tmp_path / "compiled.bin").write_bytes(
        b"\x00\xff debug metadata /Users/alice/private-project/source.c"
    )

    assert _scan_forbidden(
        tmp_path,
        personal_roots=("/Users/alice",),
        sensitive_values=(),
    ) == ["compiled.bin contains a non-generic local home path"]


def test_release_bundle_scan_rejects_explicit_personal_build_root(tmp_path: Path) -> None:
    (tmp_path / "compiled.bin").write_bytes(b"debug metadata /Users/alice/private-project/source.c")

    assert _scan_forbidden(tmp_path, personal_roots=("/Users/alice",)) == [
        "compiled.bin contains a non-generic local home path"
    ]


def test_release_bundle_scan_does_not_confuse_home_prefixes(tmp_path: Path) -> None:
    (tmp_path / "compiled.bin").write_bytes(b"debug metadata /Users/alice2/project/source.c")

    assert _scan_forbidden(tmp_path, personal_roots=("/Users/alice",)) == []


def test_default_personal_roots_captures_local_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/Users/alice")
    monkeypatch.delenv("USERPROFILE", raising=False)

    assert _default_personal_roots() == ("/Users/alice",)


def test_default_personal_roots_excludes_generic_build_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/Users/runner")
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\runneradmin")

    assert _default_personal_roots() == ()


def test_release_bundle_scan_fails_closed_on_unreadable_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unreadable = tmp_path / "unreadable.bin"
    unreadable.write_bytes(b"content")
    original_open = Path.open

    def deny_unreadable_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == unreadable:
            raise PermissionError("simulated unreadable bundle member")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_unreadable_open)

    assert _scan_forbidden(tmp_path, personal_roots=()) == [
        "unreadable.bin could not be scanned (PermissionError)"
    ]


def test_manual_release_template_is_valid_and_cannot_be_released() -> None:
    schema = json.loads(
        (_ROOT / "docs/release/manual-release-evidence.schema.json").read_text(encoding="utf-8")
    )
    template = json.loads(
        (_ROOT / "docs/release/manual-release-evidence.template.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert list(validator.iter_errors(template)) == []
    assert template["decision"] == "block"
    assert {item["role"] for item in template["reviewers"]} == {
        "builder",
        "independent_reviewer",
    }

    false_release = deepcopy(template)
    false_release["decision"] = "release"
    assert list(validator.iter_errors(false_release)) != []


def test_dataset_manifest_example_matches_published_schema() -> None:
    schema = json.loads(
        (_ROOT / "cortex/tests/physio/manifest.schema.json").read_text(encoding="utf-8")
    )
    example = json.loads(
        (_ROOT / "cortex/tests/physio/manifest.example.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert list(validator.iter_errors(example)) == []
    assert example["participant_data_committed"] is False


def test_release_record_validator_requires_complete_two_architecture_evidence(
    tmp_path: Path,
) -> None:
    commit = "a" * 40
    _write_approved_release_record(tmp_path, architecture="arm64", commit=commit)
    _write_approved_release_record(tmp_path, architecture="x86_64", commit=commit)

    report = validate_release_records(
        tmp_path,
        asset_dir=tmp_path,
        expected_version=__version__,
        expected_commit=commit,
    )

    assert report["status"] == "passed"
    assert report["architectures"] == ["arm64", "x86_64"]


def test_release_record_validator_rejects_reviewer_role_overlap(
    tmp_path: Path,
) -> None:
    commit = "b" * 40
    _write_approved_release_record(
        tmp_path,
        architecture="arm64",
        commit=commit,
        builder_id="same-person",
        reviewer_id="same-person",
    )
    _write_approved_release_record(tmp_path, architecture="x86_64", commit=commit)

    with pytest.raises(ReleaseRecordError, match="identities overlap"):
        validate_release_records(
            tmp_path,
            asset_dir=tmp_path,
            expected_version=__version__,
            expected_commit=commit,
        )


def test_release_record_validator_rejects_incomplete_case_catalog(
    tmp_path: Path,
) -> None:
    commit = "c" * 40
    arm_record = _write_approved_release_record(tmp_path, architecture="arm64", commit=commit)
    _write_approved_release_record(tmp_path, architecture="x86_64", commit=commit)
    payload = json.loads(arm_record.read_text(encoding="utf-8"))
    payload["cases"].pop()
    arm_record.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReleaseRecordError, match="violates schema"):
        validate_release_records(
            tmp_path,
            asset_dir=tmp_path,
            expected_version=__version__,
            expected_commit=commit,
        )


def test_release_record_validator_rejects_artifact_hash_mismatch(
    tmp_path: Path,
) -> None:
    commit = "d" * 40
    _write_approved_release_record(tmp_path, architecture="arm64", commit=commit)
    _write_approved_release_record(tmp_path, architecture="x86_64", commit=commit)
    (tmp_path / f"Cortex-{__version__}-macos-arm64.dmg").write_bytes(b"tampered")

    with pytest.raises(ReleaseRecordError, match="hash mismatch"):
        validate_release_records(
            tmp_path,
            asset_dir=tmp_path,
            expected_version=__version__,
            expected_commit=commit,
        )


def test_release_record_validator_rejects_automated_asset_as_manual_evidence(
    tmp_path: Path,
) -> None:
    commit = "e" * 40
    arm_record = _write_approved_release_record(tmp_path, architecture="arm64", commit=commit)
    _write_approved_release_record(tmp_path, architecture="x86_64", commit=commit)
    payload = json.loads(arm_record.read_text(encoding="utf-8"))
    payload["cases"][0]["evidence_files"] = [f"Cortex-{__version__}-macos-arm64.dmg"]
    arm_record.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReleaseRecordError, match="non-manual"):
        validate_release_records(
            tmp_path,
            asset_dir=tmp_path,
            expected_version=__version__,
            expected_commit=commit,
        )
