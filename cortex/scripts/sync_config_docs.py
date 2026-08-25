"""Generate the complete runtime configuration reference and safe env template.

``CortexConfig`` is the source of truth.  The generated ``.env.example`` keeps
every setting commented, so copying it does not silently override model/YAML
defaults.  Use ``--check`` in CI and ``--apply`` after changing settings.
"""

from __future__ import annotations

import argparse
import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from cortex.libs.config.settings import CortexConfig

_ROOT = Path(__file__).resolve().parents[2]
_PROJECT = _ROOT / "cortex"
_DOC_OUT = _PROJECT / "docs" / "configuration-reference.md"
_ENV_OUT = _PROJECT / ".env.example"


@dataclass(frozen=True)
class ConfigEntry:
    """One leaf in the nested Pydantic configuration tree."""

    path: tuple[str, ...]
    annotation: Any
    default: Any
    description: str
    constraints: str

    @property
    def yaml_path(self) -> str:
        return ".".join(self.path)

    @property
    def env_name(self) -> str:
        return "CORTEX_" + "__".join(part.upper() for part in self.path)


@dataclass(frozen=True)
class OperationalVariable:
    name: str
    example: str
    purpose: str


OPERATIONAL_VARIABLES: tuple[OperationalVariable, ...] = (
    OperationalVariable(
        "CORTEX_ENV", "dev", "Runtime environment label; use `test` only in tests."
    ),
    OperationalVariable(
        "CORTEX_DEBUG", "0", "Enable native-host debug logging (`1` only while diagnosing)."
    ),
    OperationalVariable(
        "CORTEX_JSON2TS_CMD",
        "/opt/homebrew/bin/json2ts",
        "Override the schema generator executable.",
    ),
    OperationalVariable(
        "CORTEX_NATIVE_HOST_PROJECT_ROOTS",
        "/path/to/cortex",
        "Comma-separated roots the native host may launch.",
    ),
    OperationalVariable(
        "CORTEX_NATIVE_HOST_PYTHON",
        "/path/to/python3",
        "Interpreter written into the installed native-host shebang.",
    ),
    OperationalVariable(
        "CORTEX_PROJECT_ROOT", "/path/to/cortex", "Launcher-agent project root override."
    ),
    OperationalVariable(
        "CORTEX_PYTHON", "/path/to/python3", "Launcher-agent interpreter override."
    ),
    OperationalVariable(
        "CORTEX_SIGN_IDENTITY",
        "Developer ID Application: Example (TEAMID)",
        "Developer ID identity used for release signing.",
    ),
    OperationalVariable(
        "CORTEX_NOTARIZE_PROFILE",
        "cortex-notary",
        "`notarytool` Keychain profile used by release builds.",
    ),
    OperationalVariable(
        "CORTEX_NOTARIZE_KEYCHAIN",
        "/path/to/signing.keychain-db",
        "Non-default Keychain containing the notary profile.",
    ),
    OperationalVariable(
        "CORTEX_REQUIRE_NOTARIZATION",
        "1",
        "Fail the build unless Developer ID signing and notarization succeed.",
    ),
    OperationalVariable(
        "CORTEX_ARTIFACT_ARCH",
        "arm64",
        "Architecture label verified against the built application.",
    ),
    OperationalVariable(
        "CORTEX_RELEASE_EVIDENCE_DIR",
        "dist/evidence",
        "Directory for checksums and release-verification evidence.",
    ),
    OperationalVariable(
        "CORTEX_RELEASE_TAG", "v0.3.2", "Exact checked-out tag required by release provenance."
    ),
    OperationalVariable(
        "CORTEX_SKIP_EXT_BUILD",
        "0",
        "Skip browser bundles only when verified bundles already exist.",
    ),
    OperationalVariable(
        "CORTEX_SKIP_VSCODE_EXT_BUILD",
        "0",
        "Skip VSIX creation only when a verified VSIX already exists.",
    ),
    OperationalVariable("CORTEX_ROOT", "/path/to/cortex", "PyInstaller spec root override."),
)

SECRET_VARIABLES: tuple[OperationalVariable, ...] = (
    OperationalVariable(
        "AWS_BEARER_TOKEN_BEDROCK", "", "Bedrock bearer token; prefer macOS Keychain instead."
    ),
    OperationalVariable(
        "ANTHROPIC_API_KEY",
        "",
        "Direct Anthropic credential; used only when that provider is selected.",
    ),
    OperationalVariable(
        "GOOGLE_APPLICATION_CREDENTIALS",
        "/path/to/credentials.json",
        "Vertex ADC file; keep outside the repository.",
    ),
)


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    candidates = (annotation,) if get_origin(annotation) is None else get_args(annotation)
    for candidate in candidates:
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            return candidate
    return None


def _default(field: FieldInfo) -> Any:
    value = field.get_default(call_default_factory=True)
    return None if value is PydanticUndefined else value


def _constraint_text(field: FieldInfo) -> str:
    values: list[str] = []
    for metadata in field.metadata:
        for key, symbol in (
            ("gt", ">"),
            ("ge", "≥"),
            ("lt", "<"),
            ("le", "≤"),
            ("min_length", "length ≥"),
            ("max_length", "length ≤"),
            ("pattern", "pattern"),
        ):
            value = getattr(metadata, key, None)
            if value is not None:
                values.append(f"{symbol} {value}")
    return "; ".join(values) or "—"


def config_entries() -> tuple[ConfigEntry, ...]:
    """Return every leaf without instantiating ``BaseSettings`` or reading env."""

    entries: list[ConfigEntry] = []

    def visit(model: type[BaseModel], prefix: tuple[str, ...]) -> None:
        for name, field in model.model_fields.items():
            annotation = field.annotation
            nested = _nested_model(annotation)
            if nested is not None:
                visit(nested, (*prefix, name))
                continue
            entries.append(
                ConfigEntry(
                    path=(*prefix, name),
                    annotation=annotation,
                    default=_default(field),
                    description=(field.description or "—").strip(),
                    constraints=_constraint_text(field),
                )
            )

    visit(CortexConfig, ())
    return tuple(entries)


def canonical_env_names() -> frozenset[str]:
    return frozenset(entry.env_name for entry in config_entries())


def canonical_yaml_paths() -> frozenset[str]:
    return frozenset(entry.yaml_path for entry in config_entries())


def operational_env_names() -> frozenset[str]:
    return frozenset(item.name for item in (*OPERATIONAL_VARIABLES, *SECRET_VARIABLES))


def _type_name(annotation: Any) -> str:
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        return " | ".join(_type_name(item) for item in get_args(annotation))
    if origin is not None:
        args = ", ".join(_type_name(item) for item in get_args(annotation))
        name = getattr(origin, "__name__", str(origin).replace("typing.", ""))
        return f"{name}[{args}]"
    return getattr(annotation, "__name__", str(annotation).replace("typing.", ""))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _display_default(value: Any) -> str:
    if value is None:
        return "`null`"
    rendered = json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True)
    return f"`{rendered.replace('|', '&#124;')}`"


def _env_default(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(_jsonable(value), separators=(",", ":"), sort_keys=True)
    return str(_jsonable(value))


def render_reference() -> str:
    rows = [
        "# Configuration reference",
        "",
        "> Generated by `python -m cortex.scripts.sync_config_docs --apply`. Do not edit by hand.",
        "",
        "`CortexConfig` is the runtime source of truth. YAML uses dotted paths below; environment overrides use the corresponding `CORTEX_…` name and double underscores. Secrets are deliberately not fields on `CortexConfig`.",
        "",
        f"This reference contains **{len(config_entries())} runtime settings**.",
        "",
        "| YAML path | Environment variable | Type | Default | Constraints | Description |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entry in config_entries():
        description = entry.description.replace("\n", " ").replace("|", "&#124;")
        rows.append(
            f"| `{entry.yaml_path}` | `{entry.env_name}` | `{_type_name(entry.annotation)}` | "
            f"{_display_default(entry.default)} | {entry.constraints} | {description} |"
        )
    rows.extend(
        [
            "",
            "## Operational and credential variables",
            "",
            "These variables are consumed by build/installer/provider entry points rather than `CortexConfig`.",
            "",
            "| Variable | Purpose |",
            "| --- | --- |",
        ]
    )
    for item in (*OPERATIONAL_VARIABLES, *SECRET_VARIABLES):
        rows.append(f"| `{item.name}` | {item.purpose} |")
    rows.extend(
        [
            "",
            "Credential values belong in macOS Keychain or the provider's supported credential store. Never commit them to `.env`.",
            "",
        ]
    )
    return "\n".join(rows)


def render_env_example() -> str:
    lines = [
        "# Cortex configuration template — generated; do not edit by hand.",
        "# Copy to .env only when you need overrides. Every line is commented so",
        "# copying this file preserves the reviewed defaults from defaults.yaml/model fields.",
        "# Full definitions: cortex/docs/configuration-reference.md",
        "# Secrets belong in macOS Keychain or provider credential stores, never git.",
        "",
        "# Runtime settings (CortexConfig)",
    ]
    current_section = ""
    for entry in config_entries():
        section = entry.path[0]
        if section != current_section:
            lines.extend(("", f"# [{section}]"))
            current_section = section
        lines.append(f"# {entry.env_name}={_env_default(entry.default)}")
    lines.extend(("", "# Build, installer, and operational variables"))
    for item in OPERATIONAL_VARIABLES:
        lines.append(f"# {item.name}={item.example}")
    lines.extend(("", "# Provider credentials (prefer Keychain/ADC)"))
    for item in SECRET_VARIABLES:
        lines.append(f"# {item.name}={item.example}")
    lines.append("")
    return "\n".join(lines)


def expected_surfaces() -> dict[Path, str]:
    return {_DOC_OUT: render_reference(), _ENV_OUT: render_env_example()}


def check() -> list[str]:
    problems: list[str] = []
    for path, expected in expected_surfaces().items():
        actual = path.read_text(encoding="utf-8") if path.exists() else ""
        if actual != expected:
            problems.append(f"{path.relative_to(_ROOT)} is stale")
    return problems


def apply() -> None:
    for path, expected in expected_surfaces().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected, encoding="utf-8")
        print(f"updated {path.relative_to(_ROOT)}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.apply:
        apply()
    problems = check()
    if problems:
        print("configuration documentation FAILED:", file=sys.stderr)
        for problem in problems:
            print(f" - {problem}", file=sys.stderr)
        print("run: python -m cortex.scripts.sync_config_docs --apply", file=sys.stderr)
        return 1
    print(f"configuration surfaces are synchronized ({len(config_entries())} settings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
