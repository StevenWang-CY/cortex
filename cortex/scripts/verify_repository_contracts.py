"""Verify documentation, configuration, and live wire-schema contracts."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tokenize
import tomllib
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml

from cortex.libs.schemas.ws_message_types import MessageType
from cortex.scripts import (
    generate_support_model_identity,
    sync_config_docs,
    sync_design_tokens,
    sync_versions,
)

_ROOT = Path(__file__).resolve().parents[2]
_PROJECT = _ROOT / "cortex"
_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\((?P<target><[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)")
_ENV_ASSIGNMENT_RE = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)


def _repository_markdown() -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "*.md"],
        cwd=_ROOT,
        check=True,
        capture_output=True,
    )
    return tuple(_ROOT / item.decode("utf-8") for item in result.stdout.split(b"\0") if item)


def check_local_markdown_links() -> list[str]:
    """Require every tracked local Markdown target to exist in a checkout."""

    problems: list[str] = []
    for document in _repository_markdown():
        text = document.read_text(encoding="utf-8")
        # Markdown-looking expressions in fenced source/math blocks are not
        # links. Preserve newlines while blanking their content so diagnostics
        # retain exact source line numbers.
        searchable = re.sub(
            r"^\s*(```|~~~).*?^\s*\1\s*$",
            lambda match: "\n" * match.group(0).count("\n"),
            text,
            flags=re.MULTILINE | re.DOTALL,
        )
        searchable = re.sub(
            r"`[^`\n]+`",
            lambda match: " " * len(match.group(0)),
            searchable,
        )
        for match in _MARKDOWN_LINK_RE.finditer(searchable):
            raw = match.group("target").strip("<>")
            parts = urlsplit(raw)
            if parts.scheme or parts.netloc or raw.startswith(("#", "mailto:")):
                continue
            target_text = unquote(parts.path)
            if not target_text:
                continue
            candidate = (
                (_ROOT / target_text.lstrip("/"))
                if target_text.startswith("/")
                else (document.parent / target_text)
            )
            candidates = (
                (candidate, candidate.with_suffix(".md")) if not candidate.suffix else (candidate,)
            )

            def is_repository_content(item: Path) -> bool:
                if not item.exists():
                    return False
                ignored = (
                    subprocess.run(
                        ["git", "check-ignore", "--quiet", str(item)],
                        cwd=_ROOT,
                        check=False,
                    ).returncode
                    == 0
                )
                tracked = (
                    subprocess.run(
                        ["git", "ls-files", "--error-unmatch", str(item)],
                        cwd=_ROOT,
                        check=False,
                        capture_output=True,
                    ).returncode
                    == 0
                )
                return tracked or not ignored

            if not any(is_repository_content(item) for item in candidates):
                line = searchable.count("\n", 0, match.start()) + 1
                problems.append(f"{document.relative_to(_ROOT)}:{line}: missing local link {raw!r}")
    return problems


def _flatten_yaml(value: object, prefix: tuple[str, ...] = ()) -> set[str]:
    if not isinstance(value, dict):
        return {".".join(prefix)}
    paths: set[str] = set()
    for key, nested in value.items():
        paths.update(_flatten_yaml(nested, (*prefix, str(key))))
    return paths


def check_configuration_contract() -> list[str]:
    problems = sync_config_docs.check()
    defaults_path = _PROJECT / "libs" / "config" / "defaults.yaml"
    raw_defaults = yaml.safe_load(defaults_path.read_text(encoding="utf-8")) or {}
    unknown_defaults = sorted(
        _flatten_yaml(raw_defaults) - set(sync_config_docs.canonical_yaml_paths())
    )
    problems.extend(f"defaults.yaml has unknown setting {path}" for path in unknown_defaults)

    env_text = (_PROJECT / ".env.example").read_text(encoding="utf-8")
    documented = set(_ENV_ASSIGNMENT_RE.findall(env_text))
    allowed = set(sync_config_docs.canonical_env_names()) | set(
        sync_config_docs.operational_env_names()
    )
    problems.extend(
        f".env.example has unknown variable {name}" for name in sorted(documented - allowed)
    )
    missing = sorted(set(sync_config_docs.canonical_env_names()) - documented)
    problems.extend(f".env.example omits runtime setting {name}" for name in missing)

    # This is deliberately a conservative reachability check, not a proof of
    # runtime control flow. A leaf must at least be named outside the model
    # declaration/tests/generated surfaces; otherwise it cannot influence the
    # application and belongs in a migration decoder rather than public config.
    setting_declaration = _PROJECT / "libs" / "config" / "settings.py"
    production_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in _production_sources()
        if path != setting_declaration
    )
    for entry in sync_config_docs.config_entries():
        leaf = entry.path[-1]
        if re.search(rf"\b{re.escape(leaf)}\b", production_text) is None:
            problems.append(f"runtime setting {entry.yaml_path} has no production reference")
    return problems


def _production_sources() -> tuple[Path, ...]:
    roots = (
        _PROJECT / "application",
        _PROJECT / "apps",
        _PROJECT / "libs",
        _PROJECT / "services",
        _PROJECT / "scripts",
        _PROJECT / "storage",
    )
    files: list[Path] = []
    for root in roots:
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".ts", ".tsx"}:
                continue
            relative_parts = path.relative_to(_PROJECT).parts
            if any(
                part
                in {"tests", "test", "__tests__", "generated", "node_modules", ".plasmo", "build"}
                for part in relative_parts
            ):
                continue
            if path == _PROJECT / "libs" / "schemas" / "ws_message_types.py":
                continue
            files.append(path)
    return tuple(files)


def _python_symbols(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    try:
        without_comments = tokenize.untokenize(
            token
            for token in tokenize.generate_tokens(StringIO(text).readline)
            if token.type != tokenize.COMMENT
        )
        tree = ast.parse(without_comments)
    except (SyntaxError, tokenize.TokenError):
        return set()
    symbols: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "MessageType"
        ):
            symbols.add(node.attr)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            symbols.add(node.value)
    return symbols


def _typescript_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", text)


def check_live_message_catalog() -> list[str]:
    python_symbols: set[str] = set()
    typescript = ""
    for path in _production_sources():
        if path.suffix == ".py":
            python_symbols.update(_python_symbols(path))
        else:
            typescript += "\n" + _typescript_text(path)
    problems: list[str] = []
    for member in MessageType:
        quoted = re.search(rf"(?P<quote>['\"]){re.escape(member.value)}(?P=quote)", typescript)
        if (
            member.name not in python_symbols
            and member.value not in python_symbols
            and quoted is None
        ):
            problems.append(f"MessageType.{member.name} has no production producer or consumer")
    return problems


def check_generated_surfaces() -> list[str]:
    problems: list[str] = []
    problems.extend(sync_versions.check(sync_versions.canonical_version()))
    problems.extend(generate_support_model_identity.check())
    for path, expected in sync_design_tokens.expected_outputs(sync_design_tokens._load()).items():
        actual = path.read_text(encoding="utf-8") if path.exists() else ""
        if actual != expected:
            problems.append(f"{path.relative_to(_ROOT)} design tokens are stale")
    return problems


def check_workflow_action_pins() -> list[str]:
    """Require immutable commit refs for every third-party workflow action."""

    problems: list[str] = []
    for workflow in sorted((_ROOT / ".github" / "workflows").glob("*.yml")):
        for line_number, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            match = re.search(r"\buses:\s*([^\s#]+)", line)
            if match is None:
                continue
            action = match.group(1)
            if action.startswith("./"):
                continue
            _separator, _at, reference = action.rpartition("@")
            if re.fullmatch(r"[0-9a-f]{40}", reference) is None:
                problems.append(
                    f"{workflow.relative_to(_ROOT)}:{line_number}: "
                    f"workflow action is not commit-pinned: {action}"
                )
    return problems


def check_toolchain_pins() -> list[str]:
    """Keep release and CI runtimes aligned with repository-owned pins."""

    problems: list[str] = []
    python_version = (_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    if re.fullmatch(r"\d+\.\d+\.\d+", python_version) is None:
        problems.append(".python-version must pin an exact semantic version")
    node_version = (_ROOT / ".node-version").read_text(encoding="utf-8").strip()
    if re.fullmatch(r"\d+\.\d+\.\d+", node_version) is None:
        problems.append(".node-version must pin an exact semantic version")
    browser_manifest = json.loads(
        (_PROJECT / "apps" / "browser_extension" / "package.json").read_text(encoding="utf-8")
    )
    package_manager = str(browser_manifest.get("packageManager") or "")
    match = re.fullmatch(r"pnpm@(\d+\.\d+\.\d+)", package_manager)
    if match is None:
        problems.append("browser package.json must pin packageManager to exact pnpm")
        pnpm_version = ""
    else:
        pnpm_version = match.group(1)
    with (_PROJECT / "pyproject.toml").open("rb") as handle:
        project_metadata = tomllib.load(handle)
    uv_config = project_metadata.get("tool", {}).get("uv", {})
    uv_required = str(uv_config.get("required-version", ""))
    uv_version = uv_required.removeprefix("==")
    if not uv_required.startswith("=="):
        problems.append("tool.uv.required-version must be an exact == pin")
    required_environments = set(uv_config.get("required-environments", []))
    expected_macos_environments = {
        "sys_platform == 'darwin' and platform_machine == 'arm64'",
        "sys_platform == 'darwin' and platform_machine == 'x86_64'",
    }
    if required_environments != expected_macos_environments:
        problems.append("tool.uv.required-environments must cover exact macOS arm64/x86_64 wheels")
    workflow_payloads: dict[str, dict[str, Any]] = {}
    for workflow in sorted((_ROOT / ".github" / "workflows").glob("*.yml")):
        workflow_text = workflow.read_text(encoding="utf-8")
        payload = yaml.safe_load(workflow_text) or {}
        workflow_payloads[workflow.name] = payload
        environment = payload.get("env", {}) if isinstance(payload, dict) else {}
        expected = {
            "PYTHON_PRIMARY": python_version,
            "NODE_VERSION": node_version,
            "PNPM_VERSION": pnpm_version,
            "UV_VERSION": uv_version,
        }
        for key, value in expected.items():
            if environment.get(key) != value:
                problems.append(f"{workflow.relative_to(_ROOT)} env.{key} must equal {value!r}")
        if "pnpm/action-setup@" in workflow_text:
            problems.append(f"{workflow.relative_to(_ROOT)} must not use the pnpm v6 version shim")
        for job_name, job in payload.get("jobs", {}).items():
            steps = job.get("steps", []) if isinstance(job, dict) else []
            run_blocks = [str(step.get("run") or "") for step in steps if isinstance(step, dict)]
            invokes_pnpm = any(
                re.search(r"(^|\s)pnpm(?:\s|$)", block, re.MULTILINE) for block in run_blocks
            )
            activates_exact_pnpm = any(
                'corepack prepare "pnpm@${PNPM_VERSION}" --activate' in block
                and 'test "$(pnpm --version)" = "${PNPM_VERSION}"' in block
                for block in run_blocks
            )
            if invokes_pnpm and not activates_exact_pnpm:
                problems.append(
                    f"{workflow.relative_to(_ROOT)} job {job_name} invokes pnpm "
                    "without exact Corepack activation"
                )

    matrix_jobs = (("ci.yml", "python"), ("release.yml", "build-dmg"))
    matrices: list[list[dict[str, Any]]] = []
    for workflow_name, job_name in matrix_jobs:
        job = workflow_payloads.get(workflow_name, {}).get("jobs", {}).get(job_name, {})
        if job.get("env", {}).get("UV_PYTHON") != "${{ matrix.python }}":
            problems.append(
                f"{workflow_name} job {job_name} must export UV_PYTHON from matrix.python"
            )
        include = job.get("strategy", {}).get("matrix", {}).get("include", [])
        if not isinstance(include, list) or not include:
            problems.append(f"{workflow_name} job {job_name} lacks a Python matrix")
            continue
        rows = [row for row in include if isinstance(row, dict)]
        matrices.append(rows)
        for row in rows:
            version = str(row.get("python") or "")
            if re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
                problems.append(f"{workflow_name} job {job_name} has non-exact Python {version!r}")
        audit_run = "\n".join(
            str(step.get("run") or "")
            for step in job.get("steps", [])
            if isinstance(step, dict)
            and step.get("name") == "Audit the resolved architecture-specific Python graph"
        )
        required_audit_fragments = (
            "pip-audit",
            "--format json",
            "verify_dependency_audit.py",
            "python-intel-audit-exceptions.json",
            '[[ "${{ matrix.arch }}" == "x86_64" ]]',
        )
        if any(fragment not in audit_run for fragment in required_audit_fragments):
            problems.append(
                f"{workflow_name} job {job_name} must enforce the "
                "architecture-scoped Python audit policy"
            )
    if len(matrices) == len(matrix_jobs) and matrices[0] != matrices[1]:
        problems.append("CI and release Python architecture matrices must be identical")
    if matrices and not any(row.get("python") == python_version for row in matrices[0]):
        problems.append("CI Python matrix must include the .python-version primary")
    return problems


def check_dependency_graph_contract() -> list[str]:
    """Reject mutually overwriting native providers in the locked runtime."""

    problems: list[str] = []
    with (_PROJECT / "pyproject.toml").open("rb") as handle:
        project_metadata = tomllib.load(handle)
    dependencies = [
        str(dependency).split(";", 1)[0].strip()
        for dependency in project_metadata.get("project", {}).get("dependencies", [])
    ]
    opencv_dependencies = [
        dependency
        for dependency in dependencies
        if re.match(r"^opencv-(?:contrib-)?python(?:\W|$)", dependency)
    ]
    if opencv_dependencies != ["opencv-contrib-python>=4.9.0,<5"]:
        problems.append(
            "project dependencies must use one capped opencv-contrib-python provider; "
            f"found {opencv_dependencies!r}"
        )

    with (_PROJECT / "uv.lock").open("rb") as handle:
        lock = tomllib.load(handle)
    locked_packages = [package for package in lock.get("package", []) if isinstance(package, dict)]
    base_versions = sorted(
        str(package.get("version") or "")
        for package in locked_packages
        if package.get("name") == "opencv-python"
    )
    if base_versions:
        problems.append(
            "uv.lock must not co-install opencv-python with opencv-contrib-python; "
            f"found base versions {base_versions!r}"
        )
    contrib_versions = sorted(
        str(package.get("version") or "")
        for package in locked_packages
        if package.get("name") == "opencv-contrib-python"
    )
    if not contrib_versions:
        problems.append("uv.lock is missing the canonical opencv-contrib-python provider")
    elif any(not re.fullmatch(r"[0-4](?:\.\d+)+", version) for version in contrib_versions):
        problems.append(f"uv.lock contains an uncapped OpenCV contrib major: {contrib_versions!r}")
    return problems


def check_protobuf_json_boundary() -> list[str]:
    """Keep the Intel Protobuf exception's vulnerable API out of Cortex."""

    problems: list[str] = []
    for path in _production_sources():
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        imports_boundary = any(
            (
                isinstance(node, ast.Import)
                and any(alias.name == "google.protobuf.json_format" for alias in node.names)
            )
            or (
                isinstance(node, ast.ImportFrom)
                and (
                    node.module == "google.protobuf.json_format"
                    or (
                        node.module == "google.protobuf"
                        and any(alias.name == "json_format" for alias in node.names)
                    )
                )
            )
            for node in ast.walk(tree)
        )
        if imports_boundary:
            problems.append(
                f"{path.relative_to(_ROOT)} imports the Protobuf JSON parser "
                "excluded by the Intel audit boundary"
            )
    return problems


def all_problems() -> list[str]:
    return [
        *check_local_markdown_links(),
        *check_configuration_contract(),
        *check_live_message_catalog(),
        *check_generated_surfaces(),
        *check_workflow_action_pins(),
        *check_toolchain_pins(),
        *check_dependency_graph_contract(),
        *check_protobuf_json_boundary(),
    ]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-links", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    problems = [
        *(check_local_markdown_links() if not args.skip_links else []),
        *check_configuration_contract(),
        *check_live_message_catalog(),
        *check_generated_surfaces(),
        *check_workflow_action_pins(),
        *check_toolchain_pins(),
        *check_dependency_graph_contract(),
        *check_protobuf_json_boundary(),
    ]
    if problems:
        print("repository contracts FAILED:", file=sys.stderr)
        for problem in problems:
            print(f" - {problem}", file=sys.stderr)
        return 1
    print(
        "repository contracts pass "
        "(links, config reachability, live messages, generated surfaces, "
        "action/tool pins, dependency/security boundaries)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
