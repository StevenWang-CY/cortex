"""Deterministic camera-permission state mapping tests."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from cortex.libs.utils import platform as platform_mod
from cortex.libs.utils.platform import CameraPermissionState


@pytest.mark.parametrize(
    ("native_status", "expected"),
    [
        (0, CameraPermissionState.NOT_DETERMINED),
        (1, CameraPermissionState.RESTRICTED),
        (2, CameraPermissionState.DENIED),
        (3, CameraPermissionState.AUTHORIZED),
        (99, CameraPermissionState.UNKNOWN),
        (object(), CameraPermissionState.UNKNOWN),
    ],
)
def test_camera_permission_state_maps_avfoundation_without_prompting(
    native_status: object,
    expected: CameraPermissionState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_access = object()
    avfoundation = SimpleNamespace(
        AVMediaTypeVideo="vide",
        AVCaptureDevice=SimpleNamespace(
            authorizationStatusForMediaType_=lambda _media: native_status,
            requestAccessForMediaType_completionHandler_=request_access,
        ),
    )
    monkeypatch.setattr(platform_mod, "is_macos", lambda: True)
    monkeypatch.setitem(sys.modules, "AVFoundation", avfoundation)

    assert platform_mod.get_camera_permission_state() == expected


def test_camera_permission_state_is_authorized_off_macos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform_mod, "is_macos", lambda: False)

    assert (
        platform_mod.get_camera_permission_state()
        == CameraPermissionState.AUTHORIZED
    )


@pytest.mark.parametrize(
    ("state", "allowed"),
    [
        (CameraPermissionState.AUTHORIZED, True),
        (CameraPermissionState.UNAVAILABLE, True),
        (CameraPermissionState.NOT_DETERMINED, False),
        (CameraPermissionState.RESTRICTED, False),
        (CameraPermissionState.DENIED, False),
        (CameraPermissionState.UNKNOWN, False),
    ],
)
def test_boolean_permission_compatibility_is_fail_closed_when_query_succeeds(
    state: CameraPermissionState,
    allowed: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform_mod, "get_camera_permission_state", lambda: state)

    assert platform_mod.check_camera_permission() is allowed
