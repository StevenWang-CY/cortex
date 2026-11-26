"""Repository documentation and generated-surface drift gates."""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex.scripts import verify_repository_contracts
from cortex.scripts.verify_repository_contracts import all_problems


def test_repository_contracts_are_synchronized() -> None:
    problems = all_problems()
    assert problems == [], "\n".join(problems)


def test_dependency_contract_rejects_overlapping_opencv_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
dependencies = ["opencv-contrib-python>=4.9.0,<5"]
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        """
version = 1

[[package]]
name = "opencv-contrib-python"
version = "4.11.0.86"

[[package]]
name = "opencv-python"
version = "4.11.0.86"
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_repository_contracts, "_PROJECT", tmp_path)

    problems = verify_repository_contracts.check_dependency_graph_contract()

    assert any("must not co-install opencv-python" in problem for problem in problems)


def test_protobuf_json_boundary_rejects_production_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "unsafe_parser.py"
    source.write_text(
        "from google.protobuf import json_format\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_repository_contracts, "_ROOT", tmp_path)
    monkeypatch.setattr(
        verify_repository_contracts,
        "_production_sources",
        lambda: (source,),
    )

    problems = verify_repository_contracts.check_protobuf_json_boundary()

    assert any("imports the Protobuf JSON parser" in problem for problem in problems)


def _link_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, markdown: str
) -> list[str]:
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "present.md").write_text("# present\n", encoding="utf-8")
    (tmp_path / "wiki").mkdir(exist_ok=True)
    page = tmp_path / "wiki" / "Page.md"
    page.write_text(markdown, encoding="utf-8")
    monkeypatch.setattr(verify_repository_contracts, "_ROOT", tmp_path)
    monkeypatch.setattr(verify_repository_contracts, "_repository_markdown", lambda: (page,))
    return verify_repository_contracts.check_local_markdown_links()


@pytest.mark.parametrize(
    "target",
    [
        "https://github.com/StevenWang-CY/cortex/blob/main/docs/present.md",
        "https://github.com/StevenWang-CY/cortex/blob/main/docs/present.md#a-heading",
        "https://github.com/StevenWang-CY/cortex/tree/main/docs",
        "https://github.com/StevenWang-CY/cortex/tree/main/docs/",
    ],
)
def test_a_self_link_to_a_file_in_the_tree_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    assert _link_problems(tmp_path, monkeypatch, f"See [it]({target}).\n") == []


def test_a_self_link_to_a_missing_file_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The external link check no longer probes these links (github.com answers
    # CI runners with 503), so the local check is the only one that can fail.
    target = "https://github.com/StevenWang-CY/cortex/blob/main/docs/removed.md"

    problems = _link_problems(tmp_path, monkeypatch, f"See [it]({target}).\n")

    assert problems == [f"wiki/Page.md:1: missing repository link {target!r}"]


@pytest.mark.parametrize(
    "target",
    [
        "https://github.com/someone-else/cortex/blob/main/docs/removed.md",
        "https://github.com/StevenWang-CY/cortex/blob/v0.4.0/docs/removed.md",
        "https://github.com/StevenWang-CY/cortex/releases/latest",
    ],
)
def test_links_that_are_not_self_links_on_main_are_left_to_the_external_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    assert _link_problems(tmp_path, monkeypatch, f"See [it]({target}).\n") == []


def test_the_external_link_check_excludes_exactly_the_locally_checked_self_links() -> None:
    import re
    import tomllib

    config = tomllib.loads(
        (Path(verify_repository_contracts._ROOT) / ".lychee.toml").read_text(encoding="utf-8")
    )
    excludes = [re.compile(pattern) for pattern in config["exclude"]]

    def excluded(url: str) -> bool:
        return any(pattern.search(url) for pattern in excludes)

    for url in (
        "https://github.com/StevenWang-CY/cortex/blob/main/docs/limitations.md",
        "https://github.com/StevenWang-CY/cortex/tree/main/docs",
    ):
        assert excluded(url)
        assert verify_repository_contracts._SELF_LINK_RE.match(url)
    for url in (
        "https://github.com/StevenWang-CY/cortex/releases/latest",
        "https://github.com/StevenWang-CY/cortex/actions/workflows/ci.yml",
        "https://github.com/someone-else/cortex/blob/main/README.md",
    ):
        assert not excluded(url), url
