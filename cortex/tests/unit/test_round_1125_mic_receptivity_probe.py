"""The microphone receptivity probe must answer on macOS.

``is_microphone_in_use`` reads ``CoreAudio.kAudioHardwarePropertyProcessIsRunning``.
The pyobjc CoreAudio binding Cortex ships does not export that name, so the
attribute lookup raises, the broad ``except`` returns ``None``, and the
trigger policy treats ``None`` as "microphone off". The documented
``receptivity_block_if_mic_active`` gate and the guided-break audio mute
after a call therefore never engage: Cortex interrupts during every call.

The device-level property ``kAudioDevicePropertyDeviceIsRunningSomewhere`` on
the default input device is the one that answers "is any process capturing
from the microphone".
"""

from __future__ import annotations

import struct
import sys
import types

import pytest

from cortex.libs.utils import receptivity


@pytest.mark.skipif(sys.platform != "darwin", reason="CoreAudio exists only on macOS")
def test_real_coreaudio_binding_yields_a_microphone_answer() -> None:
    pytest.importorskip("CoreAudio")
    assert receptivity.is_microphone_in_use() in (True, False)


def _fake_coreaudio(*, default_input: int, running_somewhere: int) -> types.ModuleType:
    """Stand-in exposing only names the real pyobjc binding exports.

    ``AudioObjectGetPropertyData`` follows pyobjc's convention for the
    ``(object, address, qualifier_size, qualifier, io_size, out)`` signature:
    it returns ``(status, size, bytes)``.
    """

    module = types.ModuleType("CoreAudio")
    module.kAudioObjectSystemObject = 1  # type: ignore[attr-defined]
    module.kAudioObjectPropertyScopeGlobal = 0x676C6F62  # type: ignore[attr-defined]
    module.kAudioObjectPropertyElementMain = 0  # type: ignore[attr-defined]
    module.kAudioHardwarePropertyDefaultInputDevice = 0x64496E20  # type: ignore[attr-defined]
    module.kAudioDevicePropertyDeviceIsRunningSomewhere = 0x676F6E65  # type: ignore[attr-defined]

    def address(selector: int, scope: int, element: int) -> tuple[int, int, int]:
        return (selector, scope, element)

    def get_property_data(obj, addr, qsize, qualifier, io_size, out):  # type: ignore[no-untyped-def]
        selector = addr[0]
        if obj == 1 and selector == module.kAudioHardwarePropertyDefaultInputDevice:
            return (0, 4, struct.pack("<I", default_input))
        if obj == default_input and selector == module.kAudioDevicePropertyDeviceIsRunningSomewhere:
            return (0, 4, struct.pack("<I", running_somewhere))
        return (0x77686F3F, 0, None)  # 'who?' unknown property

    module.AudioObjectPropertyAddress = address  # type: ignore[attr-defined]
    module.AudioObjectGetPropertyData = get_property_data  # type: ignore[attr-defined]
    return module


@pytest.mark.parametrize("running", [1, 0])
def test_probe_reports_capture_on_the_default_input_device(
    monkeypatch: pytest.MonkeyPatch, running: int
) -> None:
    monkeypatch.setattr(receptivity, "_is_macos", lambda: True)
    monkeypatch.setitem(
        sys.modules, "CoreAudio", _fake_coreaudio(default_input=71, running_somewhere=running)
    )
    assert receptivity.is_microphone_in_use() is bool(running)
