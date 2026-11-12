"""System tools: environment introspection, waiting and guarded shell access.

``system_run_command`` is deliberately conservative:

* it is a *dangerous* tool, so the registry blocks it unless the run confirms;
* it is disabled entirely unless ``VISIONPILOT_ALLOW_SHELL=true``;
* only executables on ``VISIONPILOT_SHELL_ALLOWLIST`` may run;
* shell metacharacters are rejected, so no piping/redirection/chaining;
* it always runs with ``cwd`` set to the workspace and a hard timeout.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from executor.base import (
    FunctionTool,
    Tool,
    ToolContext,
    ToolParameter,
    ToolPermissionError,
    ToolResult,
    get_screen_size,
    load_pyautogui,
)

logger = logging.getLogger("visionpilot.executor.system")

_FORBIDDEN_TOKENS = ("|", "&", ";", ">", "<", "`", "$(", "&&", "||", "%0a", "\n", "\r")

# Friendly names -> executable / Start-menu names to look for.  Keys are
# compared case-insensitively against the requested app name.
_APP_ALIASES: dict[str, tuple[str, ...]] = {
    "微信": ("wechat", "微信"),
    "wechat": ("wechat",),
    "企业微信": ("wxwork", "企业微信"),
    "wecom": ("wxwork",),
    "qq": ("qq",),
    "记事本": ("notepad", "记事本"),
    "notepad": ("notepad",),
    "计算器": ("calc", "calculator", "计算器"),
    "calc": ("calc",),
    "calculator": ("calc",),
    "画图": ("mspaint", "paint", "画图"),
    "paint": ("mspaint", "paint"),
    "命令提示符": ("cmd", "命令提示符"),
    "cmd": ("cmd",),
    "终端": ("wt", "windowsterminal", "terminal"),
    "资源管理器": ("explorer",),
    "explorer": ("explorer",),
    "edge": ("msedge", "microsoft edge"),
    "浏览器": ("msedge", "chrome", "firefox"),
    "word": ("winword", "word"),
    "excel": ("excel",),
    "powerpoint": ("powerpnt", "powerpoint"),
    "outlook": ("outlook",),
    "spotify": ("spotify",),
    "vscode": ("code", "visual studio code"),
    "code": ("code",),
}

_START_MENU_SKIP = ("uninstall", "卸载", "readme", "帮助", "help", "website", "官网")


def _app_candidates(name: str) -> list[str]:
    key = name.strip().casefold()
    candidates = [name.strip()]
    candidates.extend(_APP_ALIASES.get(key, ()))
    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        lowered = item.casefold()
        if item and lowered not in seen:
            seen.add(lowered)
            result.append(item)
    return result


def _registry_app_path(name: str) -> Path | None:
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows
        return None
    executable = name if name.lower().endswith(".exe") else f"{name}.exe"
    subkey = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{executable}"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        path = Path(str(value).strip('"'))
        if path.is_file():
            return path
    return None


def _start_menu_shortcuts() -> list[Path]:
    roots = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]
    shortcuts: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            shortcuts.extend(root.rglob("*.lnk"))
        except OSError:
            continue
    return shortcuts


def _shortcut_for(name: str) -> Path | None:
    needle = name.strip().casefold()
    if len(needle) < 2:
        return None
    best: tuple[int, int, Path] | None = None
    for shortcut in _start_menu_shortcuts():
        stem = shortcut.stem.casefold()
        if any(token in stem for token in _START_MENU_SKIP):
            continue
        if stem == needle:
            score = 0
        elif stem.startswith(needle):
            score = 1
        elif needle in stem:
            score = 2
        else:
            continue
        candidate = (score, len(stem), shortcut)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    return None if best is None else best[2]


def _resolve_app(name: str) -> tuple[str, Path] | None:
    """Resolve an application name to an executable or Start-menu shortcut."""
    for candidate in _app_candidates(name):
        located = shutil.which(candidate) or shutil.which(f"{candidate}.exe")
        if located:
            return "executable", Path(located)
        registered = _registry_app_path(candidate)
        if registered is not None:
            return "registered", registered
        shortcut = _shortcut_for(candidate)
        if shortcut is not None:
            return "shortcut", shortcut
    return None



def _forbidden(command: str) -> str | None:
    for token in _FORBIDDEN_TOKENS:
        if token in command:
            return token
    return None


async def _info(context: ToolContext) -> ToolResult:
    width, height = get_screen_size()
    data = {
        "os": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "executable": sys.executable,
        "cwd": os.getcwd(),
        "workspace": str(context.settings.workspace_dir),
        "screen": {"width": width, "height": height},
        "execution_mode": context.settings.execution_mode,
        "pyautogui": load_pyautogui() is not None,
        "playwright": shutil.which("playwright") is not None,
        "time": datetime.now(timezone.utc).isoformat(),
    }
    return ToolResult(
        tool="system_info",
        ok=True,
        output=(
            f"{data['os']} {data['release']} | python {data['python']} | "
            f"screen {width}x{height} | mode {data['execution_mode']}"
        ),
        data=data,
    )


async def _wait(context: ToolContext, seconds: float) -> ToolResult:
    seconds = max(0.0, min(30.0, float(seconds)))
    await asyncio.sleep(seconds)
    return ToolResult(
        tool="system_wait",
        ok=True,
        output=f"waited {seconds:.2f}s",
        data={"seconds": seconds},
        simulated=context.dry_run,
    )


async def _run_command(context: ToolContext, command: str, timeout: float | None = None) -> ToolResult:
    settings = context.settings
    if not settings.allow_shell:
        return ToolResult(
            tool="system_run_command",
            ok=False,
            denied=True,
            error="shell access is disabled (set VISIONPILOT_ALLOW_SHELL=true to enable)",
        )

    command = command.strip()
    if not command:
        return ToolResult(tool="system_run_command", ok=False, error="empty command")
    bad = _forbidden(command)
    if bad:
        return ToolResult(
            tool="system_run_command", ok=False, error=f"forbidden shell token {bad!r} in command"
        )

    try:
        parts = shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        return ToolResult(tool="system_run_command", ok=False, error=f"cannot parse command: {exc}")
    if not parts:
        return ToolResult(tool="system_run_command", ok=False, error="empty command")

    executable = Path(parts[0]).name.lower()
    if executable.endswith(".exe"):
        executable = executable[: -4]
    allowed = {name.lower() for name in settings.shell_allowlist}
    if executable not in allowed:
        raise ToolPermissionError(
            f"'{executable}' is not on the shell allowlist ({', '.join(sorted(allowed))})"
        )

    timeout = float(timeout if timeout is not None else settings.shell_timeout_sec)
    workspace = str(settings.workspace_dir)
    logger.info("shell: %s (cwd=%s)", command, workspace)

    def run() -> tuple[int, str, str]:
        import subprocess

        completed = subprocess.run(  # noqa: S603 - allowlisted executable, no shell
            parts,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return completed.returncode, completed.stdout, completed.stderr

    try:
        code, stdout, stderr = await asyncio.to_thread(run)
    except asyncio.TimeoutError:
        return ToolResult(tool="system_run_command", ok=False, error=f"command timed out after {timeout}s")
    except FileNotFoundError:
        return ToolResult(tool="system_run_command", ok=False, error=f"executable not found: {executable}")
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool="system_run_command", ok=False, error=f"command failed: {exc}")

    output = (stdout or "").strip()
    if stderr.strip():
        output = f"{output}\n[stderr]\n{stderr.strip()}".strip()
    if len(output) > 4000:
        output = output[:3997] + "..."
    return ToolResult(
        tool="system_run_command",
        ok=code == 0,
        output=output,
        error=None if code == 0 else f"exit code {code}",
        data={"command": command, "exit_code": code, "stdout": stdout, "stderr": stderr},
    )


async def _launch_app(context: ToolContext, name: str, wait: float = 1.5) -> ToolResult:
    """Launch an installed desktop application by its friendly name."""
    name = str(name or "").strip().strip('"')
    if not name:
        return ToolResult(tool="system_launch_app", ok=False, error="application name is required")
    if len(name) > 80:
        return ToolResult(tool="system_launch_app", ok=False, error="application name is too long")
    if any(character in name for character in '\\/:*?"<>|'):
        return ToolResult(
            tool="system_launch_app",
            ok=False,
            error="application name must be a plain name, not a path",
        )
    if context.dry_run:
        return ToolResult(
            tool="system_launch_app",
            ok=True,
            simulated=True,
            output=f"would launch {name}",
            data={"name": name},
        )
    if os.name != "nt" or not hasattr(os, "startfile"):
        return ToolResult(
            tool="system_launch_app",
            ok=False,
            error="application launching is only implemented on Windows",
        )

    resolved = await asyncio.to_thread(_resolve_app, name)
    if resolved is None:
        return ToolResult(
            tool="system_launch_app",
            ok=False,
            error=(
                f"could not find an installed application named '{name}' "
                "(checked PATH, App Paths and Start Menu shortcuts)"
            ),
        )
    kind, path = resolved
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606 - resolved installed app
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool="system_launch_app", ok=False, error=f"could not launch {name}: {exc}")

    wait = max(0.0, min(10.0, float(wait)))
    if wait:
        await asyncio.sleep(wait)
    return ToolResult(
        tool="system_launch_app",
        ok=True,
        output=f"launched {name} ({kind}: {path.name})",
        data={"name": name, "target": str(path), "kind": kind},
    )


def build_system_tools() -> list[Tool]:
    """Factory returning system-level tools."""
    return [
        FunctionTool(
            name="system_info",
            description="Report OS, Python, screen size and execution mode.",
            category="system",
            parameters=[],
            handler=_info,
        ),
        FunctionTool(
            name="system_wait",
            description="Pause for a number of seconds (useful while an app loads).",
            category="system",
            parameters=[
                ToolParameter(name="seconds", type="number", description="Seconds to wait (0-30)", required=True)
            ],
            handler=_wait,
        ),
        FunctionTool(
            name="system_launch_app",
            description=(
                "Launch an installed Windows application by friendly name, e.g. "
                "'微信', 'WeChat', 'notepad' or '设置'. Searches PATH, App Paths and "
                "Start Menu shortcuts. Prefer this over Start-menu keystrokes."
            ),
            category="system",
            danger="caution",
            available=lambda: os.name == "nt",
            unavailable_reason="application launching is only implemented on Windows",
            parameters=[
                ToolParameter(name="name", type="string", description="Application name", required=True),
                ToolParameter(
                    name="wait",
                    type="number",
                    description="Seconds to wait for the app to appear (0-10)",
                    default=1.5,
                ),
            ],
            handler=_launch_app,
        ),
        FunctionTool(
            name="system_run_command",
            description=(
                "Run an allowlisted console command inside the workspace. "
                "Disabled unless shell access is explicitly enabled."
            ),
            category="system",
            danger="dangerous",
            parameters=[
                ToolParameter(name="command", type="string", description="Command line", required=True),
                ToolParameter(name="timeout", type="number", description="Timeout in seconds"),
            ],
            handler=_run_command,
        ),
    ]
