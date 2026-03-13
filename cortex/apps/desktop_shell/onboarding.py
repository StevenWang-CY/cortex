"""
Desktop shell onboarding for first-run setup.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from cortex.libs.config.settings import get_config
from cortex.libs.utils import check_accessibility_permission, check_camera_permission, request_camera_permission


class OnboardingWindow(QWidget):
    """Simple first-run setup surface for packaged and dev installs."""

    completed = Signal()
    open_settings_requested = Signal()
    run_calibration_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cortex Setup")
        self.setMinimumSize(560, 420)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(18)

        title = QLabel("Set Up Cortex")
        title.setStyleSheet("font-size: 28px; font-weight: 700;")
        layout.addWidget(title)

        subtitle = QLabel(
            "Finish permissions, confirm your Azure/OpenAI backend, and connect VS Code and Chrome. "
            "Cortex stays local-first and only sends workspace text context to the model."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #666; font-size: 13px;")
        layout.addWidget(subtitle)

        layout.addWidget(self._section("Permissions", self._permission_text()))
        layout.addWidget(self._section("LLM", self._llm_text()))
        layout.addWidget(
            self._section(
                "Extensions",
                "Install the VS Code sidebar extension and the Chrome extension for coding and research context. "
                "Cortex still works in reduced mode without them.",
            )
        )

        button_row = QHBoxLayout()
        request_camera = QPushButton("Request Camera Access")
        request_camera.clicked.connect(request_camera_permission)
        button_row.addWidget(request_camera)

        settings_button = QPushButton("Open Settings")
        settings_button.clicked.connect(self.open_settings_requested.emit)
        button_row.addWidget(settings_button)

        calibrate_button = QPushButton("Run Calibration")
        calibrate_button.clicked.connect(self.run_calibration_requested.emit)
        button_row.addWidget(calibrate_button)

        finish_button = QPushButton("Finish Setup")
        finish_button.clicked.connect(self.completed.emit)
        button_row.addWidget(finish_button)

        layout.addLayout(button_row)
        layout.addStretch()

    def _section(self, title: str, body: str) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame.setStyleSheet("QFrame { background: #f5f7fb; border-radius: 12px; }")
        layout = QVBoxLayout(frame)
        heading = QLabel(title)
        heading.setStyleSheet("font-size: 16px; font-weight: 600;")
        content = QLabel(body)
        content.setWordWrap(True)
        content.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(heading)
        layout.addWidget(content)
        return frame

    def _permission_text(self) -> str:
        camera = "Granted" if check_camera_permission() else "Not granted"
        accessibility = "Granted" if check_accessibility_permission() else "Not granted"
        return (
            f"Camera: {camera}\n"
            f"Accessibility/Input Monitoring: {accessibility}\n"
            "On macOS, grant Accessibility for keyboard and mouse telemetry in System Settings > Privacy & Security."
        )

    def _llm_text(self) -> str:
        config = get_config()
        endpoint = config.llm.azure.endpoint or "Not configured"
        deployment = config.llm.azure.deployment_name or "Not configured"
        return (
            f"Mode: {config.llm.mode}\n"
            f"Azure endpoint: {endpoint}\n"
            f"Deployment: {deployment}\n"
            "If Azure is unavailable, Cortex can fall back to local Ollama or built-in rule-based guidance."
        )


def onboarding_marker_path() -> Path:
    return Path(get_config().storage.path) / ".onboarding_complete"

