"""App launching: name resolution and window polling.

``system_launch_app`` used to wait a fixed 2s for a window and to accept only a
byte-identical name, which failed on healthy launches: 抖音 paints its first
screen after ~2.1s (more on a cold start) and its shortcut is called "抖音"
while people also type "Douyin".  These tests pin the polling contract and the
name resolution without starting a real application.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from agent import Planner, ReasoningEngine
from executor import ToolContext
from executor import system as system_tools

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows-only launch path")


def live_context(settings) -> ToolContext:
    """A context that takes the real launch path instead of the dry run."""
    configured = settings.model_copy(
        update={"execution_mode": "live", "simulate_desktop": False}
    )
    return ToolContext(settings=configured)


# ------------------------------------------------------------- matching
def test_window_match_prefers_the_launched_executable():
    score = system_tools._window_match_score(
        "douyin.exe", 42, "抖音", {"douyin.exe"}, "抖音", {42}, self_pid=1
    )
    assert score == 0


def test_window_match_accepts_a_child_process_window():
    # Electron shells paint their UI from a spawned helper process.
    score = system_tools._window_match_score(
        "douyin.exe", 84, "抖音", {"launcher.exe"}, "", {42, 84}, self_pid=1
    )
    assert score == 1


def test_window_match_falls_back_to_the_title():
    score = system_tools._window_match_score(
        "helper.exe", 84, "抖音 - 推荐", {"launcher.exe"}, "抖音", set(), self_pid=1
    )
    assert score == 2


def test_window_match_ignores_unrelated_and_own_windows():
    unrelated = system_tools._window_match_score(
        "explorer.exe", 7, "Documents", {"douyin.exe"}, "抖音", set(), self_pid=1
    )
    own_window = system_tools._window_match_score(
        "douyin.exe", 1, "抖音", {"douyin.exe"}, "抖音", {1}, self_pid=1
    )
    one_letter_hint = system_tools._window_match_score(
        "helper.exe", 84, "抖音", {"launcher.exe"}, "抖", set(), self_pid=1
    )
    assert unrelated is None
    assert own_window is None
    assert one_letter_hint is None


def test_name_variants_strip_display_noise():
    variants = system_tools._app_name_variants("抖音 App")
    assert "抖音" in variants
    assert system_tools._name_match_score("抖音", variants) == 0


def test_name_score_accepts_a_longer_spoken_name():
    assert system_tools._name_match_score("抖音", ["抖音短视频"]) == 3
    assert system_tools._name_match_score("抖音", ["douyin"]) is None


def test_shortcut_lookup_matches_the_target_executable(tmp_path, monkeypatch):
    shortcut = tmp_path / "抖音.lnk"
    shortcut.write_bytes("C:\\douyin\\douyin.exe".encode("utf-16-le"))
    monkeypatch.setattr(system_tools, "_start_menu_shortcuts", lambda: [shortcut])

    assert system_tools._shortcut_for("抖音") == shortcut
    assert system_tools._shortcut_for("Douyin") == shortcut  # romanised name
    assert system_tools._shortcut_for("douyin.exe") == shortcut
    assert system_tools._shortcut_for("spotify") is None


def test_explorer_aliases_reach_explorer_exe():
    for spoken in ("文件资源管理器", "文件管理器", "资源管理器", "此电脑", "我的电脑"):
        candidates = system_tools._app_candidates(spoken)
        assert any(Path(candidate).stem.casefold() == "explorer" for candidate in candidates), spoken


@pytest.mark.skipif(os.name != "nt", reason="resolves Start Menu entries")
def test_explorer_aliases_resolve_on_windows():
    for spoken in ("文件资源管理器", "此电脑"):
        resolved = system_tools._resolve_app(spoken)
        assert resolved is not None, spoken
        kind, path = resolved
        assert kind in {"executable", "shortcut"}
        assert Path(path).name.casefold() == "explorer.exe" or Path(path).stem.casefold() == "explorer"


# --------------------------------------------------------------- polling
FAKE_EXE = Path("C:/nonexistent-visionpilot/douyin.exe")


def shell_only(monkeypatch) -> None:
    """Force the ``os.startfile`` branch so no real process is ever spawned."""
    monkeypatch.setattr(
        system_tools.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())
    )
    monkeypatch.setattr(system_tools.os, "startfile", lambda path: None, raising=False)


def test_launch_polls_until_the_window_appears(settings, monkeypatch):
    polls: list[float] = []

    def fake_window(executable, title_hint=None, aliases=None, launch_pid=None):
        polls.append(1.0)
        if len(polls) < 3:
            return None
        return {"title": "抖音", "pid": 292912, "hwnd": 592186}

    monkeypatch.setattr(
        system_tools,
        "_resolve_app",
        lambda name, workspace=None: ("executable", FAKE_EXE),
    )
    shell_only(monkeypatch)
    monkeypatch.setattr(system_tools, "_activate_process_window", fake_window)

    result = asyncio.run(system_tools._launch_app(live_context(settings), "抖音", 2.0))

    assert result.ok is True, result.error
    assert result.data["window_found"] is True
    assert result.data["title"] == "抖音"
    assert len(polls) == 3


def test_launch_reports_a_missing_window(settings, monkeypatch):
    monkeypatch.setattr(
        system_tools,
        "_resolve_app",
        lambda name, workspace=None: ("executable", FAKE_EXE),
    )
    shell_only(monkeypatch)
    monkeypatch.setattr(system_tools, "_activate_process_window", lambda *a, **k: None)

    result = asyncio.run(system_tools._launch_app(live_context(settings), "抖音", 0.6))

    assert result.ok is False
    assert "through the Windows shell" in (result.error or "")
    assert "no visible 抖音 window appeared" in (result.error or "")
    assert result.data["window_found"] is False


def test_launch_reports_a_window_that_cannot_be_focused(settings, monkeypatch):
    monkeypatch.setattr(
        system_tools,
        "_resolve_app",
        lambda name, workspace=None: ("executable", FAKE_EXE),
    )
    shell_only(monkeypatch)
    monkeypatch.setattr(
        system_tools,
        "_activate_process_window",
        lambda *a, **k: {"title": "抖音", "pid": 292912, "hwnd": 592186, "focused": False},
    )
    monkeypatch.setattr(system_tools, "_foreground_window_title", lambda: "VisionPilot")

    result = asyncio.run(system_tools._launch_app(live_context(settings), "抖音", 0.6))

    assert result.ok is False
    assert "would not bring it to the foreground" in (result.error or "")
    assert "VisionPilot" in (result.error or "")
    assert result.data["window_found"] is True
    assert result.data["focused"] is False


def test_launch_keeps_polling_after_the_launcher_exits(settings, monkeypatch):
    class FakeProcess:
        """A launcher that hands the window to another instance and exits."""

        pid = 4242

        def __init__(self) -> None:
            self.polls = 0

        def poll(self):
            self.polls += 1
            return 0 if self.polls > 1 else None

    process = FakeProcess()
    seen: list[int | None] = []

    def fake_window(executable, title_hint=None, aliases=None, launch_pid=None):
        seen.append(launch_pid)
        if len(seen) < 2:
            return None
        return {"title": "抖音", "pid": 292912, "hwnd": 592186}

    monkeypatch.setattr(
        system_tools,
        "_resolve_app",
        lambda name, workspace=None: ("shortcut", Path("C:/x/抖音.lnk")),
    )
    monkeypatch.setattr(
        system_tools, "_shortcut_executable_path", lambda shortcut: Path("C:/douyin/douyin.exe")
    )
    monkeypatch.setattr(system_tools.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(system_tools, "_activate_process_window", fake_window)

    result = asyncio.run(system_tools._launch_app(live_context(settings), "抖音", 5.0))

    assert result.ok is True, result.error
    assert seen == [4242, 4242]
    assert result.data["window_found"] is True


def test_launch_default_wait_covers_slow_first_screens():
    # 抖音 needs ~2.1s warm and up to ~10s cold for its first frame.
    assert system_tools._DEFAULT_LAUNCH_WAIT >= 5.0


def test_shell_alias_detection():
    assert system_tools._is_shell_alias(
        Path(r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\mspaint.EXE")
    )
    assert not system_tools._is_shell_alias(Path(r"C:\douyin\douyin.exe"))


def test_launch_sends_store_aliases_through_the_shell(settings, monkeypatch):
    # CreateProcess on a WindowsApps stub dies with 0xC0000409; the shell brokers it.
    alias = Path(r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\mspaint.EXE")
    started: list[str] = []
    monkeypatch.setattr(
        system_tools, "_resolve_app", lambda name, workspace=None: ("executable", alias)
    )
    monkeypatch.setattr(system_tools.os, "startfile", started.append, raising=False)
    monkeypatch.setattr(
        system_tools.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn the alias")
    )
    monkeypatch.setattr(
        system_tools,
        "_activate_process_window",
        lambda *a, **k: {"title": "画图", "pid": 1, "hwnd": 2, "focused": True},
    )

    result = asyncio.run(system_tools._launch_app(live_context(settings), "画图", 1.0))

    assert result.ok is True, result.error
    assert started == [str(alias)]


def test_launch_retries_through_the_shell_after_an_instant_exit(settings, monkeypatch):
    # Notepad/Paint stubs and launcher shims die at once without a window; a
    # double click (ShellExecute) is what actually starts them.
    class DeadProcess:
        pid = 111

        def poll(self):
            return 0

    started: list[str] = []
    calls: list[int | None] = []

    def fake_window(executable, title_hint=None, aliases=None, launch_pid=None):
        calls.append(launch_pid)
        if launch_pid is not None:
            return None
        return {"title": "抖音", "pid": 7, "hwnd": 8, "focused": True}

    monkeypatch.setattr(
        system_tools, "_resolve_app", lambda name, workspace=None: ("executable", FAKE_EXE)
    )
    monkeypatch.setattr(system_tools.subprocess, "Popen", lambda *a, **k: DeadProcess())
    monkeypatch.setattr(system_tools.os, "startfile", started.append, raising=False)
    monkeypatch.setattr(system_tools, "_activate_process_window", fake_window)

    result = asyncio.run(system_tools._launch_app(live_context(settings), "抖音", 0.6))

    assert result.ok is True, result.error
    assert started == [str(FAKE_EXE)]
    assert calls[0] == 111
    assert calls[-1] is None


def test_failed_launch_explains_a_sandboxed_agent(settings, monkeypatch):
    class DeadProcess:
        pid = 111

        def poll(self):
            return 0

    def denied(path):
        raise OSError("access denied")

    monkeypatch.setattr(
        system_tools, "_resolve_app", lambda name, workspace=None: ("executable", FAKE_EXE)
    )
    monkeypatch.setattr(system_tools.subprocess, "Popen", lambda *a, **k: DeadProcess())
    monkeypatch.setattr(system_tools.os, "startfile", denied, raising=False)
    monkeypatch.setattr(system_tools, "_integrity_label", lambda: "Low")
    monkeypatch.setattr(system_tools, "_activate_process_window", lambda *a, **k: None)

    result = asyncio.run(system_tools._launch_app(live_context(settings), "抖音", 0.6))

    assert result.ok is False
    assert "Low integrity" in (result.error or "")
    assert result.data["integrity"] == "Low"


def test_manifest_of_the_target_is_read(tmp_path):
    elevated = tmp_path / "mmc.exe"
    elevated.write_text(
        '<assembly><trustInfo><security><requestedPrivileges>'
        '<requestedExecutionLevel level="requireAdministrator" uiAccess="false"/>'
        "</requestedPrivileges></security></trustInfo></assembly>",
        encoding="utf-8",
    )
    normal = tmp_path / "notepad.exe"
    normal.write_text(
        '<requestedExecutionLevel level="asInvoker" uiAccess="false"/>', encoding="utf-8"
    )

    assert system_tools._requested_execution_level(elevated) == "requireAdministrator"
    assert system_tools._requested_execution_level(normal) == "asInvoker"
    assert system_tools._requested_execution_level(tmp_path / "script.bat") is None
    assert system_tools._requested_execution_level(None) is None


def test_highest_available_is_left_to_windows(settings):
    # A standard user can start it without elevation; only CreateProcess knows
    # whether a prompt would be needed, and that failure (740) is handled.
    assert "highestAvailable" not in system_tools._ELEVATED_EXECUTION_LEVELS
    assert "requireAdministrator" in system_tools._ELEVATED_EXECUTION_LEVELS


def test_elevated_launch_is_refused_without_spawning(settings, monkeypatch):
    spawned: list[object] = []
    started: list[str] = []

    monkeypatch.setattr(
        system_tools, "_resolve_app", lambda name, workspace=None: ("executable", FAKE_EXE)
    )
    monkeypatch.setattr(
        system_tools, "_requested_execution_level", lambda path: "requireAdministrator"
    )
    monkeypatch.setattr(system_tools.subprocess, "Popen", lambda *a, **k: spawned.append(a[0]))
    monkeypatch.setattr(system_tools.os, "startfile", started.append, raising=False)

    result = asyncio.run(system_tools._launch_app(live_context(settings), "计算机管理", 0.5))

    assert result.ok is False
    assert "needs administrator rights" in (result.error or "")
    assert "UAC consent prompt" in (result.error or "")
    assert result.data["needs_admin"] is True
    assert result.data["requested_execution_level"] == "requireAdministrator"
    assert spawned == []
    assert started == []


def test_winerror_740_is_reported_as_needs_admin(settings, monkeypatch):
    started: list[str] = []

    def elevated(*args, **kwargs):
        raise OSError(0, "This operation requires elevation.", None, 740)

    monkeypatch.setattr(
        system_tools, "_resolve_app", lambda name, workspace=None: ("executable", FAKE_EXE)
    )
    monkeypatch.setattr(system_tools, "_requested_execution_level", lambda path: None)
    monkeypatch.setattr(system_tools.subprocess, "Popen", elevated)
    monkeypatch.setattr(system_tools.os, "startfile", started.append, raising=False)

    result = asyncio.run(system_tools._launch_app(live_context(settings), "计算机管理", 0.5))

    assert result.ok is False
    assert result.data["needs_admin"] is True
    # No UAC prompt is left hanging on the desktop for the user to find later.
    assert started == []


def test_planner_leaves_the_wait_to_the_tool(settings, registry):
    planner = Planner(settings, ReasoningEngine(settings), registry)

    plan = asyncio.run(planner.create_plan("打开抖音", None))

    assert plan.steps and plan.steps[0].tool == "system_launch_app"
    assert plan.steps[0].arguments == {"name": "抖音"}
    assert plan.steps[0].expect == {"window": "抖音"}
