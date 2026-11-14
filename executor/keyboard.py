"""Keyboard tools.

``keyboard_type`` types arbitrary unicode text; ``keyboard_press`` presses one
key or a chord such as ``ctrl+s``; ``keyboard_key`` supports key-down/key-up
when a sustained modifier is needed.  As with the mouse tools, simulation mode
routes input to the synthetic desktop instead of the real session.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from executor.base import (
    FunctionTool,
    Tool,
    ToolContext,
    ToolParameter,
    ToolResult,
    load_pyautogui,
)

logger = logging.getLogger("visionpilot.executor.keyboard")

# PyAutoGUI spelling of special keys.
_SPECIAL_KEYS = {
    "enter": "enter",
    "return": "enter",
    "tab": "tab",
    "space": "space",
    "esc": "esc",
    "escape": "esc",
    "backspace": "backspace",
    "delete": "delete",
    "insert": "insert",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "pagedown": "pagedown",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "win": "win",
    "ctrl": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "capslock": "capslock",
    "f1": "f1",
    "f2": "f2",
    "f3": "f3",
    "f4": "f4",
    "f5": "f5",
    "f6": "f6",
    "f7": "f7",
    "f8": "f8",
    "f9": "f9",
    "f10": "f10",
    "f11": "f11",
    "f12": "f12",
}


def _desktop(context: ToolContext) -> Any | None:
    return getattr(context, "desktop", None)


def _normalise_keys(raw: list[str]) -> list[str]:
    keys: list[str] = []
    for item in raw:
        for part in str(item).replace("+", " ").split():
            key = part.strip().lower()
            if key:
                keys.append(_SPECIAL_KEYS.get(key, key))
    return keys


async def _type_text(context: ToolContext, text: str, interval: float | None = None) -> ToolResult:
    if not isinstance(text, str) or text == "":
        return ToolResult(tool="keyboard_type", ok=False, error="text must be a non-empty string")

    desktop = _desktop(context)
    if context.dry_run:
        if desktop is not None:
            description = desktop.type_text(text)
            return ToolResult(
                tool="keyboard_type",
                ok=True,
                simulated=True,
                output=description or f"typed {len(text)} characters",
                data={"text": text, "length": len(text)},
            )
        return ToolResult(
            tool="keyboard_type",
            ok=True,
            simulated=True,
            output=f"would type {len(text)} characters",
            data={"text": text, "length": len(text)},
        )

    gui = load_pyautogui()
    if gui is None:
        return ToolResult(
            tool="keyboard_type",
            ok=False,
            error="no live input backend is available; install pyautogui or use simulate mode",
        )

    writer = gui
    if not text.isascii() and not type(gui).__module__.startswith("executor.native_input"):
        # PyAutoGUI's write() silently drops non-ASCII characters. Use the
        # Windows SendInput backend, which types Unicode explicitly.
        from executor.native_input import load_native_gui

        writer = load_native_gui()
        if writer is None:
            return ToolResult(
                tool="keyboard_type",
                ok=False,
                error=(
                    "non-ASCII text requires the Windows SendInput backend; "
                    "install pyautogui on Windows or type ASCII text"
                ),
            )

    interval = float(interval if interval is not None else 0.02)
    try:
        await asyncio.to_thread(writer.write, text, interval)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool="keyboard_type", ok=False, error=f"typing failed: {exc}")
    return ToolResult(
        tool="keyboard_type",
        ok=True,
        output=f"typed {len(text)} characters",
        data={
            "text": text,
            "length": len(text),
            "backend": "native" if type(writer).__module__.startswith("executor.native_input") else "pyautogui",
        },
    )


async def _press_keys(context: ToolContext, keys: list[str], repeats: int = 1) -> ToolResult:
    normalised = _normalise_keys(keys)
    if not normalised:
        return ToolResult(tool="keyboard_press", ok=False, error="no keys provided")
    repeats = max(1, min(10, int(repeats)))

    desktop = _desktop(context)
    if context.dry_run:
        if desktop is not None:
            description = desktop.press(normalised, repeats=repeats)
            return ToolResult(
                tool="keyboard_press",
                ok=True,
                simulated=True,
                output=description or f"pressed {'+'.join(normalised)} x{repeats}",
                data={"keys": normalised, "repeats": repeats},
            )
        return ToolResult(
            tool="keyboard_press",
            ok=True,
            simulated=True,
            output=f"would press {'+'.join(normalised)} x{repeats}",
            data={"keys": normalised, "repeats": repeats},
        )

    gui = load_pyautogui()
    if gui is None:
        return ToolResult(
            tool="keyboard_press",
            ok=False,
            error="no live input backend is available; install pyautogui or use simulate mode",
        )
    try:
        for _ in range(repeats):
            if len(normalised) == 1:
                await asyncio.to_thread(gui.press, normalised[0])
            else:
                await asyncio.to_thread(gui.hotkey, *normalised)
            await asyncio.sleep(0.03)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool="keyboard_press", ok=False, error=f"key press failed: {exc}")
    combo = "+".join(normalised)
    return ToolResult(
        tool="keyboard_press",
        ok=True,
        output=f"pressed {combo} x{repeats}",
        data={"keys": normalised, "repeats": repeats},
    )


async def _key_state(context: ToolContext, key: str, state: str) -> ToolResult:
    normalised = _normalise_keys([key])
    if len(normalised) != 1:
        return ToolResult(tool="keyboard_key", ok=False, error="exactly one key is required")

    desktop = _desktop(context)
    if context.dry_run:
        if desktop is not None:
            return ToolResult(
                tool="keyboard_key",
                ok=True,
                simulated=True,
                output=f"key {normalised[0]} {state}",
                data={"key": normalised[0], "state": state},
            )
        return ToolResult(
            tool="keyboard_key",
            ok=True,
            simulated=True,
            output=f"would hold {normalised[0]} {state}",
            data={"key": normalised[0], "state": state},
        )

    gui = load_pyautogui()
    if gui is None:
        return ToolResult(
            tool="keyboard_key",
            ok=False,
            error="no live input backend is available; install pyautogui or use simulate mode",
        )
    try:
        await asyncio.to_thread(gui.keyDown if state == "down" else gui.keyUp, normalised[0])
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool="keyboard_key", ok=False, error=f"key state failed: {exc}")
    return ToolResult(tool="keyboard_key", ok=True, output=f"key {normalised[0]} {state}")


def build_keyboard_tools() -> list[Tool]:
    """Factory returning every keyboard tool."""
    return [
        FunctionTool(
            name="keyboard_type",
            description="Type text into the focused window (supports Unicode).",
            category="keyboard",
            danger="caution",
            parameters=[
                ToolParameter(name="text", type="string", description="Text to type", required=True),
                ToolParameter(name="interval", type="number", description="Delay between characters"),
            ],
            handler=_type_text,
        ),
        FunctionTool(
            name="keyboard_press",
            description="Press one key or a chord such as 'ctrl+s' or 'alt+f4'.",
            category="keyboard",
            danger="caution",
            parameters=[
                ToolParameter(
                    name="keys",
                    type="array",
                    description="Keys pressed together, e.g. ['ctrl','s']",
                    required=True,
                ),
                ToolParameter(name="repeats", type="integer", description="Repeat count (1-10)", default=1),
            ],
            handler=_press_keys,
        ),
        FunctionTool(
            name="keyboard_key",
            description="Hold or release a single key (for drag-like modifier usage).",
            category="keyboard",
            danger="caution",
            parameters=[
                ToolParameter(name="key", type="string", description="Key name", required=True),
                ToolParameter(
                    name="state",
                    type="string",
                    description="down or up",
                    required=True,
                    enum=["down", "up"],
                ),
            ],
            handler=_key_state,
        ),
    ]
