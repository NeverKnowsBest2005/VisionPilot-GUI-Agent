"""Built-in Windows input backend.

PyAutoGUI is the preferred live-mode backend, but it is an optional dependency
and its ``write`` helper cannot type non-ASCII text (Chinese app names, for
example).  This module provides a small PyAutoGUI-compatible facade on top of
Win32 ``SendInput`` so that live mode still works on Windows when PyAutoGUI is
missing, and so that ``keyboard_type`` can type arbitrary Unicode.

The facade implements only the surface used by :mod:`executor.mouse` and
:mod:`executor.keyboard`: ``size``, ``position``, ``moveTo``, ``click``,
``dragTo``, ``scroll``, ``write``, ``press``, ``hotkey``, ``keyDown`` and
``keyUp``.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from typing import Any

_IS_WINDOWS = os.name == "nt"

if _IS_WINDOWS:  # pragma: no branch - import guard
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
else:  # pragma: no cover - exercised only on non-Windows hosts
    _user32 = None

_ULONG_PTR = ctypes.c_size_t

_INPUT_KEYBOARD = 1
_INPUT_MOUSE = 0

_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004

_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_ABSOLUTE = 0x8000

_WHEEL_DELTA = 120
_SM_CXSCREEN = 0
_SM_CYSCREEN = 1

_VK = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "menue": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "delete": 0x2E,
    "win": 0x5B,
    "windows": 0x5B,
    "lwin": 0x5B,
    "rwin": 0x5C,
    "app": 0x5D,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "add": 0x6B,
    "subtract": 0x6D,
    "multiply": 0x6A,
    "divide": 0x6F,
    "decimal": 0x6E,
}
for _i in range(1, 25):
    _VK[f"f{_i}"] = 0x6F + _i
for _ch in "abcdefghijklmnopqrstuvwxyz":
    _VK[_ch] = ord(_ch.upper())
for _digit in "0123456789":
    _VK[_digit] = ord(_digit)


if _IS_WINDOWS:

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        ]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        ]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [
            ("ki", _KEYBDINPUT),
            ("mi", _MOUSEINPUT),
            ("hi", _HARDWAREINPUT),
        ]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]

    _user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
    _user32.SendInput.restype = wintypes.UINT
    _user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
    _user32.GetSystemMetrics.restype = ctypes.c_int
    _user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
    _user32.GetCursorPos.restype = wintypes.BOOL


def available() -> bool:
    """True when this host can drive the real mouse/keyboard."""
    return bool(_IS_WINDOWS)


def _vk_for(key: str) -> int:
    name = str(key).strip().lower()
    if name in _VK:
        return _VK[name]
    if len(name) == 1:
        # VkKeyScanW returns the virtual key in the low byte and the required
        # shift state in the high byte.
        scan = _user32.VkKeyScanW(ord(name)) if _IS_WINDOWS else -1
        if scan != -1:
            return scan & 0xFF
        return ord(name.upper())
    raise ValueError(f"unsupported key: {key!r}")


def _shift_required(key: str) -> bool:
    """Whether typing ``key`` requires Shift (e.g. ``!``, ``@``, ``?``)."""
    name = str(key).strip()
    if not _IS_WINDOWS or len(name) != 1:
        return False
    scan = _user32.VkKeyScanW(ord(name))
    if scan == -1:
        return False
    return bool((scan >> 8) & 0x01)


class NativeGui:
    """Minimal PyAutoGUI-compatible facade backed by ``SendInput``."""

    FAILSAFE = False
    PAUSE = 0.0

    def __init__(self) -> None:
        if not _IS_WINDOWS:  # pragma: no cover - guarded by caller
            raise RuntimeError("the native input backend requires Windows")
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- helpers
    def _send(self, *inputs: Any) -> None:
        if not inputs:
            return
        array = (_INPUT * len(inputs))(*inputs)
        sent = _user32.SendInput(len(inputs), array, ctypes.sizeof(_INPUT))
        if sent != len(inputs):
            raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def _key_input(vk: int = 0, scan: int = 0, flags: int = 0) -> Any:
        item = _INPUT()
        item.type = _INPUT_KEYBOARD
        item.union.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
        return item

    @staticmethod
    def _mouse_input(dx: int = 0, dy: int = 0, data: int = 0, flags: int = 0) -> Any:
        item = _INPUT()
        item.type = _INPUT_MOUSE
        item.union.mi = _MOUSEINPUT(dx=dx, dy=dy, mouseData=data, dwFlags=flags, time=0, dwExtraInfo=0)
        return item

    def _key_down(self, key: str) -> None:
        self._send(self._key_input(vk=_vk_for(key)))

    def _key_up(self, key: str) -> None:
        self._send(self._key_input(vk=_vk_for(key), flags=_KEYEVENTF_KEYUP))

    def _tap(self, key: str) -> None:
        # Some shell hotkeys (notably the Windows key) are ignored when the
        # down/up pair arrives in a single SendInput batch.
        shift = _shift_required(key)
        if shift:
            self._send(self._key_input(vk=_VK["shift"]))
        try:
            self._send(self._key_input(vk=_vk_for(key)))
            time.sleep(0.03)
            self._send(self._key_input(vk=_vk_for(key), flags=_KEYEVENTF_KEYUP))
        finally:
            if shift:
                self._send(self._key_input(vk=_VK["shift"], flags=_KEYEVENTF_KEYUP))

    def _absolute(self, x: int, y: int) -> tuple[int, int]:
        width, height = self.size()
        max_x = max(1, width - 1)
        max_y = max(1, height - 1)
        return int(round(x * 65535 / max_x)), int(round(y * 65535 / max_y))

    def _move_absolute(self, x: int, y: int, duration: float = 0.0) -> None:
        start_x, start_y = self.position()
        steps = max(1, int(float(duration) / 0.01)) if duration else 1
        for step in range(1, steps + 1):
            px = start_x + (x - start_x) * step / steps
            py = start_y + (y - start_y) * step / steps
            dx, dy = self._absolute(int(round(px)), int(round(py)))
            self._send(self._mouse_input(dx=dx, dy=dy, flags=_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE))
            if duration:
                time.sleep(float(duration) / steps)

    # ------------------------------------------------------------------- API
    def size(self) -> tuple[int, int]:
        return int(_user32.GetSystemMetrics(_SM_CXSCREEN)), int(_user32.GetSystemMetrics(_SM_CYSCREEN))

    def position(self) -> tuple[int, int]:
        point = wintypes.POINT()
        if not _user32.GetCursorPos(ctypes.byref(point)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(point.x), int(point.y)

    def moveTo(self, x: int, y: int, duration: float = 0.0) -> None:  # noqa: N802 - PyAutoGUI name
        with self._lock:
            self._move_absolute(int(x), int(y), float(duration or 0.0))

    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        clicks: int = 1,
        interval: float = 0.0,
        button: str = "left",
    ) -> None:
        with self._lock:
            if x is not None and y is not None:
                self._move_absolute(int(x), int(y))
            flags = {
                "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
                "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
                "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
            }.get(str(button).lower(), (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP))
            for _ in range(max(1, int(clicks))):
                self._send(self._mouse_input(flags=flags[0]), self._mouse_input(flags=flags[1]))
                if interval:
                    time.sleep(float(interval))

    def dragTo(self, x: int, y: int, duration: float = 0.0, button: str = "left") -> None:  # noqa: N802
        with self._lock:
            down = _MOUSEEVENTF_LEFTDOWN if str(button).lower() == "left" else _MOUSEEVENTF_RIGHTDOWN
            up = _MOUSEEVENTF_LEFTUP if str(button).lower() == "left" else _MOUSEEVENTF_RIGHTUP
            start_x, start_y = self.position()
            self._send(self._mouse_input(flags=down))
            try:
                self._move_absolute(int(x), int(y), float(duration or 0.0))
            finally:
                self._send(self._mouse_input(flags=up))

    def scroll(self, clicks: int) -> None:
        with self._lock:
            for _ in range(abs(int(clicks))):
                self._send(
                    self._mouse_input(
                        data=_WHEEL_DELTA if int(clicks) > 0 else -_WHEEL_DELTA,
                        flags=_MOUSEEVENTF_WHEEL,
                    )
                )

    def write(self, text: str, interval: float = 0.02) -> None:
        """Type arbitrary Unicode text (PyAutoGUI itself is ASCII-only)."""
        with self._lock:
            for char in str(text):
                if char == "\n":
                    self._tap("enter")
                elif char == "\t":
                    self._tap("tab")
                else:
                    encoded = char.encode("utf-16-le")
                    for index in range(0, len(encoded), 2):
                        scan = encoded[index] | (encoded[index + 1] << 8)
                        self._send(
                            self._key_input(scan=scan, flags=_KEYEVENTF_UNICODE),
                            self._key_input(scan=scan, flags=_KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP),
                        )
                if interval:
                    time.sleep(float(interval))

    def press(self, key: str, presses: int = 1, interval: float = 0.0) -> None:
        with self._lock:
            for _ in range(max(1, int(presses))):
                self._tap(str(key))
                if interval:
                    time.sleep(float(interval))

    def hotkey(self, *keys: str) -> None:
        with self._lock:
            try:
                for key in keys:
                    self._key_down(str(key))
                    time.sleep(0.01)
            finally:
                for key in reversed(keys):
                    self._key_up(str(key))
                    time.sleep(0.01)

    def keyDown(self, key: str) -> None:  # noqa: N802
        with self._lock:
            self._key_down(str(key))

    def keyUp(self, key: str) -> None:  # noqa: N802
        with self._lock:
            self._key_up(str(key))


def load_native_gui() -> NativeGui | None:
    """Return the native backend on Windows, else ``None``."""
    if not _IS_WINDOWS:
        return None
    try:
        return NativeGui()
    except Exception:  # noqa: BLE001 - headless session without a desktop
        return None
