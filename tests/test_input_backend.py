"""Live input backend tests.

These do not send real mouse/keyboard events; they only assert that a backend
is selectable and that key names map to Windows virtual-key codes.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from executor import ToolContext
from executor import keyboard as keyboard_tools
from executor.base import load_pyautogui
from executor.native_input import available as native_available
from executor.native_input import _shift_required, _vk_for

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


def test_shifted_keys_report_their_shift_state():
    assert _shift_required("!") is True
    assert _shift_required("@") is True
    assert _shift_required("a") is False
    assert _shift_required("1") is False
    assert _shift_required("enter") is False


class _FakeGui:
    def __init__(self) -> None:
        self.written: list[tuple[str, float]] = []

    def write(self, text: str, interval: float) -> None:
        self.written.append((text, interval))


def _live_context(settings) -> ToolContext:
    live = settings.model_copy(update={"execution_mode": "live", "simulate_desktop": False})
    return ToolContext(settings=live)


def test_ascii_text_uses_pyautogui(settings, monkeypatch):
    gui = _FakeGui()
    monkeypatch.setattr(keyboard_tools, "load_pyautogui", lambda: gui)

    result = asyncio.run(keyboard_tools._type_text(_live_context(settings), "hello"))

    assert result.ok is True
    assert [text for text, _ in gui.written] == ["hello"]
    assert result.data["backend"] == "pyautogui"


def test_non_ascii_text_uses_the_native_unicode_backend(settings, monkeypatch):
    gui = _FakeGui()
    native = _FakeGui()
    monkeypatch.setattr(keyboard_tools, "load_pyautogui", lambda: gui)
    monkeypatch.setattr("executor.native_input.load_native_gui", lambda: native)

    result = asyncio.run(keyboard_tools._type_text(_live_context(settings), "你好，世界"))

    assert result.ok is True
    assert gui.written == []
    assert [text for text, _ in native.written] == ["你好，世界"]


def test_non_ascii_text_fails_cleanly_without_the_native_backend(settings, monkeypatch):
    gui = _FakeGui()
    monkeypatch.setattr(keyboard_tools, "load_pyautogui", lambda: gui)
    monkeypatch.setattr("executor.native_input.load_native_gui", lambda: None)

    result = asyncio.run(keyboard_tools._type_text(_live_context(settings), "中文"))

    assert result.ok is False
    assert "SendInput" in (result.error or "")
    assert gui.written == []
