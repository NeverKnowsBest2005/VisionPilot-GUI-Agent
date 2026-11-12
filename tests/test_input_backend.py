"""Live input backend tests.

These do not send real mouse/keyboard events; they only assert that a backend
is selectable and that key names map to Windows virtual-key codes.
"""

from __future__ import annotations

import os

import pytest

from executor.base import load_pyautogui
from executor.native_input import available as native_available
from executor.native_input import _vk_for

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows-only input backend")


def test_windows_backend_is_available():
    gui = load_pyautogui()
    assert gui is not None
    width, height = gui.size()
    assert width > 0 and height > 0
    x, y = gui.position()
    assert isinstance(x, int) and isinstance(y, int)


def test_native_backend_reports_available():
    assert native_available() is True


def test_virtual_key_mapping():
    assert _vk_for("win") == 0x5B
    assert _vk_for("enter") == 0x0D
    assert _vk_for("ctrl") == 0x11
    assert _vk_for("s") == 0x53
    with pytest.raises(ValueError):
        _vk_for("not-a-real-key-name")
