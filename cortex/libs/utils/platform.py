"""
Cortex Platform Utilities

OS detection, platform-specific path resolution, and permission checks.
"""

from __future__ import annotations

import os
import sys
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class Platform(StrEnum):
    """Supported platforms."""

    MACOS = "macos"
    LINUX = "linux"
    WINDOWS = "windows"
    UNKNOWN = "unknown"


def get_platform() -> Platform:
    """Detect the current operating system."""
    if sys.platform == "darwin":
        return Platform.MACOS
    elif sys.platform.startswith("linux"):
        return Platform.LINUX
    elif sys.platform == "win32":
        return Platform.WINDOWS
    return Platform.UNKNOWN


def is_macos() -> bool:
    """Check if running on macOS."""
    return get_platform() == Platform.MACOS


def is_linux() -> bool:
    """Check if running on Linux."""
    return get_platform() == Platform.LINUX


def is_windows() -> bool:
    """Check if running on Windows."""
    return get_platform() == Platform.WINDOWS


def get_config_dir() -> Path:
    """
    Get the platform-appropriate configuration directory.

    Returns:
        Path to configuration directory
    """
    platform = get_platform()

    if platform == Platform.MACOS:
        return Path.home() / "Library" / "Application Support" / "Cortex"
    elif platform == Platform.LINUX:
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        if xdg_config:
            return Path(xdg_config) / "cortex"
        return Path.home() / ".config" / "cortex"
    elif platform == Platform.WINDOWS:
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Cortex"
        return Path.home() / "AppData" / "Roaming" / "Cortex"

    return Path.home() / ".cortex"


def get_data_dir() -> Path:
    """
    Get the platform-appropriate data directory.

    Returns:
        Path to data directory (sessions, cache, etc.)
    """
    platform = get_platform()

    if platform == Platform.MACOS:
        return Path.home() / "Library" / "Application Support" / "Cortex" / "Data"
    elif platform == Platform.LINUX:
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            return Path(xdg_data) / "cortex"
        return Path.home() / ".local" / "share" / "cortex"
    elif platform == Platform.WINDOWS:
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            return Path(localappdata) / "Cortex" / "Data"
        return Path.home() / "AppData" / "Local" / "Cortex" / "Data"

    return Path.home() / ".cortex" / "data"


def get_log_dir() -> Path:
    """
    Get the platform-appropriate log directory.

    Returns:
        Path to log directory
    """
    platform = get_platform()

    if platform == Platform.MACOS:
        return Path.home() / "Library" / "Logs" / "Cortex"
    elif platform == Platform.LINUX:
        xdg_state = os.environ.get("XDG_STATE_HOME")
        if xdg_state:
            return Path(xdg_state) / "cortex" / "logs"
        return Path.home() / ".local" / "state" / "cortex" / "logs"
    elif platform == Platform.WINDOWS:
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            return Path(localappdata) / "Cortex" / "Logs"
        return Path.home() / "AppData" / "Local" / "Cortex" / "Logs"

    return Path.home() / ".cortex" / "logs"


def ensure_dir(path: Path) -> Path:
    """
    Ensure a directory exists, creating it if necessary.

    Args:
        path: Directory path to ensure

    Returns:
        The same path (for chaining)
    """
    path.mkdir(parents=True, exist_ok=True)
    return path


def check_accessibility_permission() -> bool:
    """
    Check if the application has accessibility permissions (macOS).

    Required for pynput keyboard/mouse monitoring on macOS.

    Returns:
        True if permission is granted or not required
    """
    if not is_macos():
        return True

    try:
        # pyobjc is only available on macOS
        from ApplicationServices import AXIsProcessTrusted

        return AXIsProcessTrusted()
    except ImportError:
        # If pyobjc isn't installed, assume permission is granted
        return True
    except Exception:
        # On error, assume we don't have permission
        return False


def check_camera_permission() -> bool:
    """
    Check if the application has camera permissions (macOS).

    Returns:
        True if permission is granted or not required
    """
    if not is_macos():
        return True

    try:
        import AVFoundation

        status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
            AVFoundation.AVMediaTypeVideo
        )
        # 3 = AVAuthorizationStatusAuthorized
        return status == 3
    except ImportError:
        return True
    except Exception:
        return False


def request_camera_permission() -> None:
    """
    Request camera permission (macOS).

    This is a non-blocking request. The user will see a system dialog.
    """
    if not is_macos():
        return

    try:
        import AVFoundation

        AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVFoundation.AVMediaTypeVideo,
            lambda granted: None,
        )
    except ImportError:
        pass
    except Exception:
        pass


class PermissionStatus:
    """Container for permission check results."""

    def __init__(self) -> None:
        self.accessibility = check_accessibility_permission()
        self.camera = check_camera_permission()
        self.platform = get_platform()

    @property
    def all_granted(self) -> bool:
        """Check if all required permissions are granted."""
        return self.accessibility and self.camera

    @property
    def can_capture(self) -> bool:
        """Check if capture is possible."""
        return self.camera

    @property
    def can_monitor_input(self) -> bool:
        """Check if input monitoring is possible."""
        return self.accessibility

    def to_dict(self) -> dict[str, bool | str]:
        """Convert to dictionary."""
        return {
            "platform": self.platform.value,
            "accessibility": self.accessibility,
            "camera": self.camera,
            "all_granted": self.all_granted,
        }


def get_permissions() -> PermissionStatus:
    """Get current permission status."""
    return PermissionStatus()
