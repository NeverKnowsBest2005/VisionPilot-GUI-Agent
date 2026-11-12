"""Mouse tools.

Coordinates are physical screen pixels with ``(0, 0)`` at the top-left of the
primary monitor.  In ``live`` mode the tools drive PyAutoGUI; in ``simulate``
mode they operate on a synthetic desktop (when one is attached) or simply
report what would have happened, which keeps the agent loop runnable in CI.
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
    get_screen_size,
    load_pyautogui,
)

logger = logging.getLogger("visionpilot.executor.mouse")

_BUTTONS = ["left", "right", "middle"]


def _desktop(context: ToolContext) -> Any | None:
    return getattr(context, "desktop", None)


def _screen_size(context: ToolContext) -> tuple[int, int]:
    desktop = _desktop(context)
    size = getattr(desktop, "size", None) if desktop is not None else None
    if isinstance(size, (tuple, list)) and len(size) == 2:
        return int(size[0]), int(size[1])
    return get_screen_size()


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _position(context: ToolContext) -> tuple[int, int]:
    desktop = _desktop(context)
    if desktop is not None:
        position = getattr(desktop, "position", None)
        if callable(position):
            position = position()
        if isinstance(position, (tuple, list)) and len(position) == 2:
            return int(position[0]), int(position[1])
    gui = load_pyautogui()
    if gui is not None and not context.dry_run:
        try:
            point = gui.position()
            return int(point[0]), int(point[1])
        except Exception:  # noqa: BLE001
            pass
    return 0, 0


async def _move(context: ToolContext, x: int, y: int, duration: float | None = None) -> ToolResult:
    desktop = _desktop(context)
    if context.dry_run:
        if desktop is not None:
            desktop.move(x, y)
            return ToolResult(
                tool="mouse_move", ok=True, simulated=True, output=f"pointer moved to ({x}, {y})"
            )
        return ToolResult(
            tool="mouse_move",
            ok=True,
            simulated=True,
            output=f"would move pointer to ({x}, {y})",
            data={"x": x, "y": y},
        )

    gui = load_pyautogui()
    if gui is None:
        return ToolResult(
            tool="mouse_move",
            ok=False,
            error="no live input backend is available; install pyautogui or use simulate mode",
        )
    duration = float(context.settings.mouse_move_duration if duration is None else duration)
    try:
        await asyncio.to_thread(gui.moveTo, x, y, duration)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool="mouse_move", ok=False, error=f"mouse move failed: {exc}")
    return ToolResult(tool="mouse_move", ok=True, output=f"pointer moved to ({x}, {y})")


async def _click(
    context: ToolContext,
    x: int | None,
    y: int | None,
    button: str,
    clicks: int,
) -> ToolResult:
    screen_w, screen_h = _screen_size(context)
    if x is None or y is None:
        x, y = _position(context)
    x = _clamp(x, 0, screen_w - 1)
    y = _clamp(y, 0, screen_h - 1)

    desktop = _desktop(context)
    if context.dry_run:
        if desktop is not None:
            description = desktop.click(x, y, button=button, clicks=clicks)
            return ToolResult(
                tool="mouse_click",
                ok=True,
                simulated=True,
                output=description or f"clicked ({x}, {y}) with {button}",
                data={"x": x, "y": y, "button": button, "clicks": clicks},
            )
        return ToolResult(
            tool="mouse_click",
            ok=True,
            simulated=True,
            output=f"would click ({x}, {y}) with {button} x{clicks}",
            data={"x": x, "y": y, "button": button, "clicks": clicks},
        )

    gui = load_pyautogui()
    if gui is None:
        return ToolResult(
            tool="mouse_click",
            ok=False,
            error="no live input backend is available; install pyautogui or use simulate mode",
        )
    try:
        await asyncio.to_thread(gui.click, x, y, clicks, 0.08, button)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool="mouse_click", ok=False, error=f"click failed: {exc}")
    label = {1: "click", 2: "double-click", 3: "triple-click"}.get(clicks, f"{clicks}x click")
    return ToolResult(
        tool="mouse_click",
        ok=True,
        output=f"{label} at ({x}, {y}) with {button}",
        data={"x": x, "y": y, "button": button, "clicks": clicks},
    )


async def _drag(
    context: ToolContext,
    from_x: int,
    from_y: int,
    to_x: int,
    to_y: int,
    duration: float | None = None,
) -> ToolResult:
    screen_w, screen_h = _screen_size(context)
    from_x = _clamp(from_x, 0, screen_w - 1)
    from_y = _clamp(from_y, 0, screen_h - 1)
    to_x = _clamp(to_x, 0, screen_w - 1)
    to_y = _clamp(to_y, 0, screen_h - 1)

    desktop = _desktop(context)
    if context.dry_run:
        if desktop is not None:
            description = desktop.drag(from_x, from_y, to_x, to_y)
            return ToolResult(
                tool="mouse_drag",
                ok=True,
                simulated=True,
                output=description or f"dragged ({from_x}, {from_y}) -> ({to_x}, {to_y})",
            )
        return ToolResult(
            tool="mouse_drag",
            ok=True,
            simulated=True,
            output=f"would drag ({from_x}, {from_y}) -> ({to_x}, {to_y})",
        )

    gui = load_pyautogui()
    if gui is None:
        return ToolResult(
            tool="mouse_drag",
            ok=False,
            error="no live input backend is available; install pyautogui or use simulate mode",
        )
    duration = float(context.settings.mouse_move_duration if duration is None else duration)
    try:
        await asyncio.to_thread(gui.moveTo, from_x, from_y)
        await asyncio.to_thread(gui.dragTo, to_x, to_y, duration, button="left")
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool="mouse_drag", ok=False, error=f"drag failed: {exc}")
    return ToolResult(tool="mouse_drag", ok=True, output=f"dragged ({from_x}, {from_y}) -> ({to_x}, {to_y})")


async def _scroll(
    context: ToolContext,
    clicks: int,
    x: int | None = None,
    y: int | None = None,
) -> ToolResult:
    desktop = _desktop(context)
    if context.dry_run:
        if desktop is not None:
            description = desktop.scroll(clicks, x=x, y=y)
            return ToolResult(
                tool="mouse_scroll",
                ok=True,
                simulated=True,
                output=description or f"scrolled {clicks}",
                data={"clicks": clicks},
            )
        return ToolResult(
            tool="mouse_scroll",
            ok=True,
            simulated=True,
            output=f"would scroll {clicks}",
            data={"clicks": clicks},
        )

    gui = load_pyautogui()
    if gui is None:
        return ToolResult(
            tool="mouse_scroll",
            ok=False,
            error="no live input backend is available; install pyautogui or use simulate mode",
        )
    try:
        if x is not None and y is not None:
            await asyncio.to_thread(gui.moveTo, x, y)
        await asyncio.to_thread(gui.scroll, clicks)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool="mouse_scroll", ok=False, error=f"scroll failed: {exc}")
    return ToolResult(tool="mouse_scroll", ok=True, output=f"scrolled {clicks}", data={"clicks": clicks})


async def _position_tool(context: ToolContext) -> ToolResult:
    x, y = _position(context)
    screen_w, screen_h = _screen_size(context)
    return ToolResult(
        tool="mouse_position",
        ok=True,
        output=f"pointer at ({x}, {y}); screen is {screen_w}x{screen_h}",
        data={"x": x, "y": y, "screen_width": screen_w, "screen_height": screen_h},
        simulated=context.dry_run,
    )


def build_mouse_tools() -> list[Tool]:
    """Factory returning every mouse tool."""
    return [
        FunctionTool(
            name="mouse_move",
            description="Move the mouse pointer to absolute screen coordinates.",
            category="mouse",
            danger="caution",
            parameters=[
                ToolParameter(name="x", type="integer", description="Screen x coordinate", required=True),
                ToolParameter(name="y", type="integer", description="Screen y coordinate", required=True),
                ToolParameter(name="duration", type="number", description="Seconds for the movement", default=None),
            ],
            handler=_move,
        ),
        FunctionTool(
            name="mouse_click",
            description="Click at screen coordinates (defaults to the current pointer position).",
            category="mouse",
            danger="caution",
            parameters=[
                ToolParameter(name="x", type="integer", description="Screen x coordinate"),
                ToolParameter(name="y", type="integer", description="Screen y coordinate"),
                ToolParameter(name="button", type="string", description="Mouse button", default="left", enum=_BUTTONS),
                ToolParameter(name="clicks", type="integer", description="Click count (1-3)", default=1),
            ],
            handler=_click,
        ),
        FunctionTool(
            name="mouse_double_click",
            description="Double-click at screen coordinates.",
            category="mouse",
            danger="caution",
            parameters=[
                ToolParameter(name="x", type="integer", description="Screen x coordinate", required=True),
                ToolParameter(name="y", type="integer", description="Screen y coordinate", required=True),
            ],
            handler=lambda ctx, x, y: _click(ctx, x, y, "left", 2),
        ),
        FunctionTool(
            name="mouse_right_click",
            description="Right-click at screen coordinates (opens context menus).",
            category="mouse",
            danger="caution",
            parameters=[
                ToolParameter(name="x", type="integer", description="Screen x coordinate", required=True),
                ToolParameter(name="y", type="integer", description="Screen y coordinate", required=True),
            ],
            handler=lambda ctx, x, y: _click(ctx, x, y, "right", 1),
        ),
        FunctionTool(
            name="mouse_drag",
            description="Drag from one point to another holding the left button.",
            category="mouse",
            danger="caution",
            parameters=[
                ToolParameter(name="from_x", type="integer", description="Start x", required=True),
                ToolParameter(name="from_y", type="integer", description="Start y", required=True),
                ToolParameter(name="to_x", type="integer", description="End x", required=True),
                ToolParameter(name="to_y", type="integer", description="End y", required=True),
                ToolParameter(name="duration", type="number", description="Seconds for the drag"),
            ],
            handler=_drag,
        ),
        FunctionTool(
            name="mouse_scroll",
            description="Scroll the mouse wheel; positive scrolls up, negative scrolls down.",
            category="mouse",
            danger="caution",
            parameters=[
                ToolParameter(name="clicks", type="integer", description="Wheel notches", required=True),
                ToolParameter(name="x", type="integer", description="Optional pointer x"),
                ToolParameter(name="y", type="integer", description="Optional pointer y"),
            ],
            handler=_scroll,
        ),
        FunctionTool(
            name="mouse_position",
            description="Report the current pointer position and the screen size.",
            category="mouse",
            parameters=[],
            handler=_position_tool,
        ),
    ]
