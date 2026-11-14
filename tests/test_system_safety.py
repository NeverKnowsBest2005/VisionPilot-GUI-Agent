"""Shell and app-launch boundaries must not be bypassed by path tricks."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from executor import ToolContext
from executor import system as system_tools
from executor.base import ToolPermissionError
from perception import SimulatedDesktop

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows command semantics")


def context(settings, *, allow_shell: bool = True, desktop=None) -> ToolContext:
    configured = settings.model_copy(update={"allow_shell": allow_shell})
    return ToolContext(settings=configured, desktop=desktop)


@pytest.mark.parametrize(
    "command",
    [
        r"findstr . C:\Windows\win.ini",
        r"findstr . ..\secret.txt",
        "findstr . ../secret.txt",
        r"findstr . \Windows\win.ini",
        r"where /r \Windows win.ini",
        "where /r .. win.ini",
        "findstr /s /d:.. OUTSIDE *.txt",
        r"findstr /s /d:..\.. OUTSIDE *.txt",
        r"findstr /s /d:C:\Windows OUTSIDE *.txt",
        "findstr /s /d:/Windows OUTSIDE *.txt",
        r"findstr /s /d:\Windows OUTSIDE *.txt",
        "findstr /s /d:/Users OUTSIDE *.txt",
        "C:findstr . notes.txt",
    ],
)
def test_command_paths_outside_the_workspace_are_rejected(settings, monkeypatch, command):
    calls: list[object] = []
    monkeypatch.setattr(system_tools.subprocess, "run", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(ToolPermissionError):
        asyncio.run(system_tools._run_command(context(settings), command))

    assert calls == []


def test_workspace_file_arguments_are_allowed(settings, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(parts, **kwargs):
        calls.append(parts)
        return SimpleNamespace(returncode=0, stdout="notes.txt: hello", stderr="")

    monkeypatch.setattr(system_tools.subprocess, "run", fake_run)

    result = asyncio.run(system_tools._run_command(context(settings), "findstr . notes.txt"))

    assert result.ok is True
    assert calls and calls[0][0] == "findstr"


@pytest.mark.parametrize(
    "name",
    [
        "cmd", "powershell", "pwsh", "wscript", "mshta", "python", "node",
        "终端", "wt", "py", "wsl", "npm", "javaw",
        "mintty", "tclsh", "jrunscript", "wish",
        "Git Bash", "Command Prompt", "Windows PowerShell", "Windows PowerShell ISE",
    ],
)
def test_launch_app_refuses_shell_and_script_hosts(settings, name):
    desktop = SimulatedDesktop()

    result = asyncio.run(system_tools._launch_app(context(settings, desktop=desktop), name))

    assert result.ok is False
    assert "blocked" in result.error
    assert desktop.active_app == "Desktop"


def test_resolve_app_refuses_aliases_that_map_to_shell_hosts(settings, monkeypatch):
    looked_up: list[str] = []

    def fake_which(name: str):
        looked_up.append(name.casefold())
        if name.casefold() == "py":
            return "C:/Windows/py.EXE"
        if name.casefold() == "wt":
            return "C:/Users/test/AppData/Local/Microsoft/WindowsApps/wt.EXE"
        return None

    monkeypatch.setattr(system_tools.shutil, "which", fake_which)
    monkeypatch.setattr(system_tools, "_registry_app_path", lambda name: None)
    monkeypatch.setattr(system_tools, "_shortcut_for", lambda name: None)

    assert system_tools._resolve_app("py", Path(settings.workspace_dir)) is None
    assert system_tools._resolve_app("终端", Path(settings.workspace_dir)) is None
    # The alias target must be rejected before PATH lookup is attempted.
    assert "wt" not in looked_up


def test_shortcut_targets_are_inspected_for_shell_hosts(tmp_path):
    blocked = tmp_path / "Totally Safe Tool.lnk"
    blocked.write_bytes("C:\\Windows\\System32\\cmd.exe".encode("utf-16-le"))
    allowed = tmp_path / "Notepad.lnk"
    allowed.write_bytes("C:\\Windows\\System32\\notepad.exe".encode("utf-16-le"))
    mintty = tmp_path / "Innocuous.lnk"
    mintty.write_bytes("C:\\Program Files\\Git\\usr\\bin\\mintty.exe".encode("utf-16-le"))

    assert system_tools._blocked_launch_shortcut(blocked) is True
    assert system_tools._blocked_launch_shortcut(allowed) is False
    assert system_tools._blocked_launch_shortcut(mintty) is True


def test_shortcut_to_an_mmc_console_is_allowed(tmp_path):
    console = tmp_path / "Event Viewer.lnk"
    console.write_bytes("C:\\Windows\\System32\\eventvwr.msc".encode("utf-16-le"))

    assert system_tools._blocked_launch_shortcut(console) is False


@pytest.mark.parametrize("target", [r"C:\evil\payload.msi", r"C:\evil\payload.pif"])
def test_shortcut_to_an_installer_is_blocked(tmp_path, target):
    shortcut = tmp_path / "Totally Safe Tool.lnk"
    shortcut.write_bytes(target.encode("utf-16-le"))

    assert system_tools._blocked_launch_shortcut(shortcut) is True


@pytest.mark.parametrize("name", ["PhpStorm", "RubyMine", "javafx"])
def test_script_host_prefixes_do_not_block_gui_apps(name):
    assert system_tools._blocked_launch_name(name) is False


@pytest.mark.parametrize(
    "name",
    [
        "python3.12",
        "python.exe",
        "javaw",
        "javaw.exe",
        "node18",
        "ruby3.2",
        "pwsh-preview",
        "tclsh86",
        "wish86",
        "ipython3",
        "lua54",
        "conemu64",
    ],
)
def test_versioned_script_hosts_are_still_blocked(name):
    assert system_tools._blocked_launch_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "FileZilla FTP Client",
        "Xlight FTP",
        "Event Viewer",
        "Computer Management",
        "PhpStorm",
        "RubyMine",
    ],
)
def test_launch_app_allows_ordinary_gui_apps(settings, name):
    desktop = SimulatedDesktop()

    result = asyncio.run(system_tools._launch_app(context(settings, desktop=desktop), name))

    assert result.ok is True, result.error
    assert result.simulated is True


def test_resolve_app_falls_back_to_app_paths_after_a_blocked_path_shim(settings, monkeypatch):
    code_cmd = "C:/Microsoft VS Code/bin/code.CMD"
    code_exe = Path("C:/Microsoft VS Code/Code.exe")
    monkeypatch.setattr(
        system_tools.shutil,
        "which",
        lambda name: code_cmd if name.casefold() == "code" else None,
    )
    monkeypatch.setattr(
        system_tools,
        "_registry_app_path",
        lambda name: code_exe if name.casefold() == "code" else None,
    )
    monkeypatch.setattr(system_tools, "_shortcut_for", lambda name: None)

    assert system_tools._resolve_app("code", Path(settings.workspace_dir)) == (
        "registered",
        code_exe,
    )


def test_resolve_app_skips_a_start_menu_shortcut_to_a_shell(settings, monkeypatch, tmp_path):
    shortcut = tmp_path / "Totally Safe Tool.lnk"
    shortcut.write_bytes("C:\\Windows\\System32\\cmd.exe".encode("utf-16-le"))

    monkeypatch.setattr(system_tools.shutil, "which", lambda name: None)
    monkeypatch.setattr(system_tools, "_registry_app_path", lambda name: None)
    monkeypatch.setattr(system_tools, "_shortcut_for", lambda name: shortcut)

    assert system_tools._resolve_app("totally safe tool", Path(settings.workspace_dir)) is None


@pytest.mark.parametrize(
    "command",
    [
        'findstr "12:30" notes.txt',
        r"findstr . sub\notes.txt",
        "findstr . sub/notes.txt",
        'findstr /c:"hello world" notes.txt',
        "findstr /s /i pattern .",
        "findstr /s /d:. hello *.txt",
    ],
)
def test_legitimate_arguments_are_not_mistaken_for_path_escapes(settings, monkeypatch, command):
    calls: list[list[str]] = []

    def fake_run(parts, **kwargs):
        calls.append(parts)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(system_tools.subprocess, "run", fake_run)

    result = asyncio.run(system_tools._run_command(context(settings), command))

    assert result.ok is True
    assert calls and calls[0][0] == "findstr"


def test_quoted_arguments_are_executed_without_quote_characters(settings, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(parts, **kwargs):
        calls.append(parts)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(system_tools.subprocess, "run", fake_run)

    first = asyncio.run(
        system_tools._run_command(context(settings), 'findstr "12:30" notes.txt')
    )
    second = asyncio.run(
        system_tools._run_command(context(settings), 'findstr /c:"hello world" notes.txt')
    )

    assert first.ok is True and second.ok is True
    assert calls[0] == ["findstr", "12:30", "notes.txt"]
    assert calls[1] == ["findstr", "/c:hello world", "notes.txt"]


def test_findstr_file_lists_are_refused(settings, tmp_path, monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(system_tools.subprocess, "run", lambda *args, **kwargs: calls.append(args))

    workspace = Path(settings.workspace_dir)
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("TOP SECRET", encoding="utf-8")
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")
    (workspace / "inside.txt").write_text("notes.txt\n", encoding="utf-8")
    (workspace / "outside.txt").write_text(f"{outside}\n", encoding="utf-8")

    # Even an all-relative list is refused: the child re-opens the file, so
    # validation cannot be atomic.
    for list_name in ("inside.txt", "outside.txt"):
        with pytest.raises(ToolPermissionError):
            asyncio.run(
                system_tools._run_command(context(settings), f"findstr /f:{list_name} .")
            )

    assert calls == []
    assert outside.read_text(encoding="utf-8") == "TOP SECRET"


def test_run_command_refuses_a_workspace_containing_a_junction(settings, tmp_path, monkeypatch):
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "secret.txt").write_text("OUTSIDE-SECRET", encoding="utf-8")
    link = Path(settings.workspace_dir) / "jump"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip(f"cannot create a junction here: {created.stderr or created.stdout}")

    calls: list[object] = []
    monkeypatch.setattr(system_tools.subprocess, "run", lambda *args, **kwargs: calls.append(args))

    for command in ("findstr /s /c:OUTSIDE-SECRET *", "where /r . secret.txt"):
        with pytest.raises(ToolPermissionError):
            asyncio.run(system_tools._run_command(context(settings), command))

    assert calls == []


def test_resolve_app_refuses_blocked_executables_found_on_path(settings, monkeypatch):
    powershell = "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    monkeypatch.setattr(
        system_tools.shutil,
        "which",
        lambda name: powershell if name.casefold().startswith("powershell") else None,
    )
    monkeypatch.setattr(system_tools, "_registry_app_path", lambda name: None)
    monkeypatch.setattr(system_tools, "_shortcut_for", lambda name: None)

    assert system_tools._resolve_app("powershell", Path(settings.workspace_dir)) is None


def test_resolve_app_refuses_script_files_found_on_path(settings, monkeypatch):
    monkeypatch.setattr(
        system_tools.shutil,
        "which",
        lambda name: "C:/tools/evil.bat" if name.casefold().startswith("evil") else None,
    )
    monkeypatch.setattr(system_tools, "_registry_app_path", lambda name: None)
    monkeypatch.setattr(system_tools, "_shortcut_for", lambda name: None)

    assert system_tools._resolve_app("evil", Path(settings.workspace_dir)) is None


def test_resolve_app_still_accepts_a_normal_executable(settings, monkeypatch):
    notepad = "C:/Windows/System32/notepad.exe"
    monkeypatch.setattr(
        system_tools.shutil,
        "which",
        lambda name: notepad if name.casefold().startswith("notepad") else None,
    )
    monkeypatch.setattr(system_tools, "_registry_app_path", lambda name: None)
    monkeypatch.setattr(system_tools, "_shortcut_for", lambda name: None)

    resolved = system_tools._resolve_app("notepad", Path(settings.workspace_dir))

    assert resolved is not None
    assert resolved[1].name.lower() == "notepad.exe"


def test_info_reports_the_pyautogui_module_backend(settings, monkeypatch):
    gui = SimpleNamespace(__name__="pyautogui")
    monkeypatch.setattr(system_tools, "load_pyautogui", lambda: gui)

    result = asyncio.run(
        system_tools._info(ToolContext(settings=settings, desktop=SimulatedDesktop()))
    )

    assert result.ok is True
    assert result.data["input_backend"] == "pyautogui"
