"""The foreground-app classifier must not mistake Xcode for VS Code.

``classify_app`` receives macOS ``NSApplicationName`` values from the window
tracker ("Code", "Google Chrome", "Xcode", ...). Its VS Code pattern matches
the bare substring ``code`` anywhere, so "Xcode" classifies as ``vscode``.
Downstream, ``ContextAssembler`` reports ``active_app="vscode"`` and
``RabbitHoleDetector.check_context`` -- which promises to "use only metadata
belonging to the foreground application" -- reads the VS Code extension's
last file as the file the user is working on while Xcode is in front.
"""

from __future__ import annotations

import pytest

from cortex.libs.schemas.context import EditorContext, TaskContext
from cortex.services.context_engine.app_classifier import classify_app
from cortex.services.state_engine.rabbit_hole import RabbitHoleDetector


@pytest.mark.parametrize(
    ("app_name", "expected"),
    [
        ("Code", "vscode"),
        ("Visual Studio Code", "vscode"),
        ("Cursor", "vscode"),
        ("Code - Insiders", "vscode"),
        ("Xcode", "other"),
        ("Microsoft Edge", "chrome"),
        ("Arc", "chrome"),
        ("Archive Utility", "other"),
    ],
)
def test_macos_app_names_classify_to_their_own_kind(app_name: str, expected: str) -> None:
    assert classify_app(app_name) == expected


def test_rabbit_hole_does_not_read_vs_code_metadata_while_xcode_is_in_front() -> None:
    detector = RabbitHoleDetector()
    seen: dict[str, str] = {}

    def spy(**kwargs: object) -> None:
        seen["current_file"] = str(kwargs.get("current_file"))
        return None

    detector.check = spy  # type: ignore[method-assign,assignment]
    context = TaskContext(
        mode="coding_debugging",
        active_app="other",
        complexity_score=0.2,
        current_goal_hint="Ship the auth refactor",
        editor_context=EditorContext(file_path="/src/auth.py", visible_range=(1, 20)),
    )

    detector.check_context(
        context, active_app="Xcode", state="FLOW", current_time=1.0, may_trigger=True,
    )

    assert seen["current_file"] == ""
