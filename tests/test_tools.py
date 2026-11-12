"""Tool registry behaviour: sandboxing, simulation and argument validation."""

from __future__ import annotations

import asyncio

from executor import ToolContext
from perception import SimulatedDesktop


def run(coro):
    return asyncio.run(coro)


def test_registry_exposes_all_tool_groups(registry):
    names = set(registry.names())
    expected = {
        "mouse_move", "mouse_click", "mouse_double_click", "mouse_right_click",
        "mouse_drag", "mouse_scroll", "mouse_position",
        "keyboard_type", "keyboard_press", "keyboard_key",
        "fs_list", "fs_read", "fs_write", "fs_mkdir", "fs_move", "fs_copy",
        "fs_delete", "fs_search", "fs_info",
        "browser_open", "browser_click", "browser_fill", "browser_press",
        "browser_get_text", "browser_screenshot", "browser_close",
        "system_info", "system_wait", "system_run_command", "system_launch_app",
    }
    assert expected <= names
    assert len(registry.all()) == len(expected)
    # Optional backends are present but marked unavailable.
    assert registry.get("browser_open").is_available() is False
    assert registry.get("mouse_click").is_available() is True


def test_filesystem_roundtrip_inside_workspace(settings, registry):
    context = ToolContext(settings=settings, desktop=SimulatedDesktop())

    async def scenario():
        write = await registry.execute("fs_write", {"path": "hello.txt", "content": "你好 VisionPilot"}, context)
        read = await registry.execute("fs_read", {"path": "hello.txt"}, context)
        listing = await registry.execute("fs_list", {"path": "."}, context)
        return write, read, listing

    write, read, listing = run(scenario())
    assert write.ok, write.error
    assert read.ok and "VisionPilot" in read.output
    assert listing.ok and "hello.txt" in listing.output
    assert (settings.workspace_dir / "hello.txt").read_text(encoding="utf-8") == "你好 VisionPilot"


def test_filesystem_rejects_paths_outside_workspace(settings, registry):
    context = ToolContext(settings=settings, desktop=SimulatedDesktop())
    result = run(registry.execute("fs_write", {"path": "../../escape.txt", "content": "no"}, context))
    assert result.ok is False
    assert result.denied is True or "outside" in (result.error or "").lower()
    assert not (settings.workspace_dir.parent.parent / "escape.txt").exists()


def test_dangerous_tool_requires_confirmation(settings, registry):
    context = ToolContext(settings=settings, desktop=SimulatedDesktop())
    blocked = run(registry.execute("fs_delete", {"path": "ghost.txt"}, context))
    assert blocked.ok is False and blocked.denied is True

    confirmed = ToolContext(settings=settings, desktop=SimulatedDesktop(), confirmed=True)
    allowed = run(registry.execute("fs_delete", {"path": "ghost.txt"}, confirmed))
    assert allowed.ok is False  # confirmation unlocks it, but the file is missing
    assert allowed.denied is False
    assert "not found" in (allowed.error or "").lower()


def test_mouse_and_keyboard_use_simulated_desktop(settings, registry):
    desktop = SimulatedDesktop()
    context = ToolContext(settings=settings, desktop=desktop)

    async def scenario():
        click = await registry.execute("mouse_click", {"x": 90, "y": 240}, context)
        await registry.execute("mouse_double_click", {"x": 90, "y": 240}, context)
        typing = await registry.execute("keyboard_type", {"text": "hello"}, context)
        return click, typing

    click, typing = run(scenario())
    assert click.ok and click.simulated
    assert typing.ok and typing.simulated
    assert desktop.active_app == "Notes"
    assert "hello" in desktop.buffers["Notes"]


def test_argument_validation_rejects_unknown_and_coerces_types(settings, registry):
    context = ToolContext(settings=settings, desktop=SimulatedDesktop())
    bad = run(registry.execute("mouse_click", {"nonsense": 1}, context))
    assert bad.ok is False and ("unknown argument" in (bad.error or "").lower() or "unexpected" in (bad.error or "").lower())

    coerced = run(registry.execute("mouse_move", {"x": "10", "y": "20"}, context))
    assert coerced.ok, coerced.error


def test_system_command_is_disabled_by_default(settings, registry):
    context = ToolContext(settings=settings, desktop=SimulatedDesktop())
    result = run(registry.execute("system_run_command", {"command": "whoami"}, context))
    assert result.ok is False
    assert result.denied is True


def test_launch_app_simulates_in_dry_run(settings, registry):
    context = ToolContext(settings=settings, desktop=SimulatedDesktop())
    result = run(registry.execute("system_launch_app", {"name": "微信"}, context))
    assert result.ok is True
    assert result.simulated is True
    assert "would launch" in result.output or "disabled" in (result.error or "").lower()
