"""Tool execution package."""

from __future__ import annotations

from typing import Any

from executor.base import (
    GROUNDING_KEYS,
    FunctionTool,
    Tool,
    ToolArgumentError,
    ToolContext,
    ToolParameter,
    ToolPermissionError,
    ToolRegistry,
    ToolResult,
    enable_dpi_awareness,
    get_screen_size,
    load_pyautogui,
)


def build_default_registry(settings: Any = None) -> ToolRegistry:
    """Build the registry with every tool the platform can offer.

    Optional backends (browser) stay registered so the catalog can explain why
    they are unavailable; :meth:`ToolRegistry.available` filters them for the
    planner.
    """
    if settings is None:
        from config.settings import load_settings

        settings = load_settings()

    from executor.browser import BrowserManager, build_browser_tools
    from executor.file_system import build_filesystem_tools
    from executor.keyboard import build_keyboard_tools
    from executor.mouse import build_mouse_tools
    from executor.system import build_system_tools

    tools: list[Tool] = []
    tools.extend(build_mouse_tools())
    tools.extend(build_keyboard_tools())
    tools.extend(build_filesystem_tools())
    tools.extend(build_system_tools(settings))
    manager = BrowserManager(headless=getattr(settings, "browser_headless", True))
    tools.extend(build_browser_tools(manager))

    registry = ToolRegistry(tools)
    # Kept for a clean shutdown from the API layer.
    registry.browser_manager = manager  # type: ignore[attr-defined]
    return registry


__all__ = [
    "GROUNDING_KEYS",
    "FunctionTool",
    "Tool",
    "ToolArgumentError",
    "ToolContext",
    "ToolParameter",
    "ToolPermissionError",
    "ToolRegistry",
    "ToolResult",
    "build_default_registry",
    "enable_dpi_awareness",
    "get_screen_size",
    "load_pyautogui",
]
