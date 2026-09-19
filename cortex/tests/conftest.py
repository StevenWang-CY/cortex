"""Shared pytest fixtures.

Currently exposes :func:`mock_pyside6` — a reusable helper for tests that
need to import desktop_shell modules without the heavy PySide6 dependency.
Apply via ``mock_pyside6(monkeypatch)`` from any test (transferred from the
Swift-Testing "shared fixture" pattern; swift-testing-pro / dependency
injection rule).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Session-wide HOME sandbox
# ---------------------------------------------------------------------------
# Every platform path helper (``cortex.libs.utils.platform``), the desktop
# controller's ``_APP_SUPPORT`` constant, the native host's ``_LOG_DIRECTORY``,
# the goal store, the cost ledger, and onboarding state resolve through
# ``Path.home()``. Several suites exercised those code paths without patching
# them and therefore rewrote the signed-in developer's real
# ``~/Library/Application Support/Cortex`` and ``~/Library/Logs/Cortex``
# contents on every ``pytest`` run. Redirecting ``HOME`` (and the XDG
# equivalents) before any Cortex module is imported makes that structurally
# impossible: module-level constants are computed after this file loads, and
# ``Path.home()``/``os.path.expanduser`` read the variable at call time.
#
# Set ``CORTEX_TEST_REAL_HOME=1`` only for a deliberate local experiment that
# must observe the real profile; CI never does.
_SANDBOX_HOME: Path | None = None
_REAL_HOME = os.environ.get("HOME")


def _install_home_sandbox() -> None:
    global _SANDBOX_HOME
    if os.environ.get("CORTEX_TEST_REAL_HOME") == "1":
        return
    sandbox = Path(tempfile.mkdtemp(prefix="cortex-test-home-"))
    # Keep the macOS/XDG layouts the production helpers expect so tests that
    # assert on ``~/Library/...`` structure still see a realistic tree.
    (sandbox / "Library" / "Application Support").mkdir(parents=True, exist_ok=True)
    (sandbox / "Library" / "Logs").mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(sandbox)
    os.environ["XDG_CONFIG_HOME"] = str(sandbox / ".config")
    os.environ["XDG_DATA_HOME"] = str(sandbox / ".local" / "share")
    os.environ["XDG_STATE_HOME"] = str(sandbox / ".local" / "state")
    os.environ["XDG_CACHE_HOME"] = str(sandbox / ".cache")
    _SANDBOX_HOME = sandbox


_install_home_sandbox()


def pytest_unconfigure(config: pytest.Config) -> None:
    """Remove the sandbox home once the session is over."""

    if _SANDBOX_HOME is not None and _SANDBOX_HOME.is_dir():
        shutil.rmtree(_SANDBOX_HOME, ignore_errors=True)


@pytest.fixture(scope="session")
def sandbox_home() -> Path:
    """The per-session fake ``$HOME`` every Cortex path helper resolves into."""

    assert _SANDBOX_HOME is not None, "HOME sandbox disabled via CORTEX_TEST_REAL_HOME=1"
    return _SANDBOX_HOME


@pytest.fixture(scope="session", autouse=True)
def _sandbox_qsettings_store() -> Any:
    """Keep ``QSettings`` reads inside the HOME sandbox.

    The ``HOME`` redirection above does not fully contain ``QSettings`` on
    macOS. ``QSettings.fileName()`` *does* follow ``$HOME`` into the sandbox,
    which makes the leak easy to miss -- but the native backend is
    CFPreferences, which answers reads from ``cfprefsd`` over XPC keyed on the
    domain (``com.cortex.Desktop``), not from that file. Measured directly: in
    a fresh sandbox where the plist does not exist, ``value("webcam_enabled")``
    still returned the signed-in developer's real ``False``.

    ``SettingsDialog.__init__`` opens ``QSettings("Cortex", "Desktop")`` and
    real-Qt suites (``test_a11y_coverage``, ``test_settings_apply_race``)
    construct that dialog, so the suite was reading the developer's own
    preferences -- the leak CLAUDE.md rule 36 exists to prevent. It also made
    results machine-dependent: ``test_get_default_settings`` asserts
    ``webcam_enabled is True`` and fails wherever the real preference is off.

    ``QSettings.setDefaultFormat`` does **not** fix this: the two-argument
    constructor was measured still reporting ``NativeFormat`` afterwards. Only
    the explicit ``QSettings(format, scope, org, app)`` form escapes
    CFPreferences, so the module symbol is swapped for a factory that uses it.
    The organisation is renamed too, so nothing can resolve to the real domain
    even if a backend ignores the requested format.

    Best-effort by design: the legacy stubbed Qt suite replaces ``PySide6``
    with doubles, and a non-Qt environment has no PySide6 -- neither can reach
    the real preference store in the first place.
    """
    module = sys.modules.get("cortex.apps.desktop_shell.settings")
    if _SANDBOX_HOME is None or module is None:
        yield
        return
    try:
        from PySide6.QtCore import QSettings as _RealQSettings

        ini = _RealQSettings.Format.IniFormat
        scope = _RealQSettings.Scope.UserScope
    except Exception:
        yield
        return
    if getattr(module, "QSettings", None) is not _RealQSettings:
        # Stubbed or already replaced; nothing here can reach cfprefsd.
        yield
        return

    target = _SANDBOX_HOME / "Library" / "Preferences"
    target.mkdir(parents=True, exist_ok=True)
    _RealQSettings.setPath(ini, scope, str(target))

    class _SandboxedQSettings:
        """Callable stand-in that also proxies the real class's attributes.

        ``settings.py`` uses the module symbol two ways: as a constructor, and
        as a namespace for the enums (``QSettings.Status.NoError``,
        ``QSettings.Format``). A bare function covers only the first, and the
        resulting ``AttributeError`` is swallowed by the defensive ``except
        Exception`` around the status check -- which silently disabled the
        save-failure signal that ``test_settings_sync_failure`` exists to pin.
        Proxying attribute access keeps both uses intact.
        """

        def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
            return _RealQSettings(ini, scope, "CortexTestSandbox", "Desktop")

        def __getattr__(self, name: str) -> Any:
            return getattr(_RealQSettings, name)

    module.QSettings = _SandboxedQSettings()
    try:
        yield
    finally:
        module.QSettings = _RealQSettings

# ``test_desktop_shell.py`` is a legacy, deliberately stubbed Qt suite. Its
# stubs replace process-global PySide6 modules and therefore must never be
# collected in the same interpreter as the real-Qt tests. A dedicated wrapper
# runs it in a child process with this opt-in flag.
if os.environ.get("CORTEX_LEGACY_QT_ISOLATED") != "1":
    collect_ignore = ["unit/test_desktop_shell.py"]


@pytest.fixture(autouse=True)
def _isolate_trigger_policy_storage(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Never let a test read or mutate the signed-in user's policy state."""

    test_key = hashlib.sha256(request.node.nodeid.encode("utf-8")).hexdigest()[:16]
    config_dir = Path(tmp_path_factory.getbasetemp()) / "cortex-config" / test_key
    monkeypatch.setattr(
        "cortex.services.state_engine.trigger_policy.get_config_dir",
        lambda: config_dir,
    )


def _make_qt_stubs() -> dict[str, types.ModuleType]:
    """Return a dict of stubbed PySide6 submodules.

    The submodules auto-vivify any requested attribute as a ``_Stub`` class
    so new desktop_shell imports don't require updating the stub list — any
    ``from PySide6.QtWidgets import QFoo`` resolves to a no-op Stub.
    """

    class _Stub:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._args = args
            self._kwargs = kwargs

        def __call__(self, *args: Any, **kwargs: Any) -> _Stub:
            return _Stub(*args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return _Stub()

        def emit(self, *args: Any, **kwargs: Any) -> None:
            return None

        def connect(self, *args: Any, **kwargs: Any) -> None:
            return None

    def _make_attr(_name: str) -> type:
        class Inner(_Stub):
            pass

        return Inner

    class _AutoStubModule(types.ModuleType):
        def __getattr__(self, name: str) -> Any:
            # Cache so identity stays stable across repeated imports.
            attr = _make_attr(name)
            setattr(self, name, attr)
            return attr

    pyside6 = _AutoStubModule("PySide6")
    qtcore = _AutoStubModule("PySide6.QtCore")
    qtgui = _AutoStubModule("PySide6.QtGui")
    qtwidgets = _AutoStubModule("PySide6.QtWidgets")

    # Pre-populate well-known names so explicit checks for them keep
    # their historical types; everything else is auto-vivified above.
    for name in ("QObject", "QTimer", "QSettings", "Qt", "Signal", "Slot", "QRectF", "QRect", "QPointF"):
        setattr(qtcore, name, _make_attr(name))
    for name in ("QColor", "QFont", "QIcon", "QPixmap", "QPainter", "QPainterPath",
                 "QPen", "QAction"):
        setattr(qtgui, name, _make_attr(name))
    for name in (
        "QApplication", "QSystemTrayIcon", "QMenu", "QWidget", "QLabel",
        "QPushButton", "QLineEdit", "QCheckBox", "QComboBox", "QSlider",
        "QSpinBox", "QFrame", "QHBoxLayout", "QVBoxLayout", "QGridLayout",
        "QStackedWidget", "QTabWidget", "QButtonGroup", "QGraphicsDropShadowEffect",
        "QMessageBox", "QSizePolicy", "QProgressBar", "QScrollArea",
    ):
        setattr(qtwidgets, name, _make_attr(name))

    return {
        "PySide6": pyside6,
        "PySide6.QtCore": qtcore,
        "PySide6.QtGui": qtgui,
        "PySide6.QtWidgets": qtwidgets,
    }


@pytest.fixture
def mock_pyside6(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install lightweight PySide6 stubs into ``sys.modules``.

    Lets tests import :mod:`cortex.apps.desktop_shell.*` without a real Qt
    runtime. The stubs are scoped to the test function (monkeypatch undoes
    them at teardown).
    """
    stubs = _make_qt_stubs()
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)
