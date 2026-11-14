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
import re
import shlex
import shutil
import subprocess
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
    "wechat": ("wechat", "WeChat"),
    "设置": ("ms-settings:", "settings", "设置"),
    "settings": ("ms-settings:", "settings"),
    "系统设置": ("ms-settings:",),
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

# Windows settings pages have no executable; they are opened through a URI.
_URI_APPS = {
    "设置": "ms-settings:",
    "settings": "ms-settings:",
    "系统设置": "ms-settings:",
    "system settings": "ms-settings:",
}

# ``system_launch_app`` must not become a back door around ``allow_shell``:
# launching a shell or script host and then typing into it is equivalent to
# running an arbitrary command.
#
# These names match both the exact requested name and individual words of a
# human-readable name, so "Windows PowerShell" and "Git Bash" are caught.
_BLOCKED_LAUNCH_NAMES = {
    "alacritty",
    "bash",
    "bun",
    "busybox",
    "cmd",
    "cmder",
    "conemu",
    "conhost",
    "csh",
    "csi",
    "cscript",
    "dash",
    "deno",
    "dotnet-script",
    "elvish",
    "fish",
    "ghci",
    "ipython",
    "java",
    "javaw",
    "jjs",
    "jrunscript",
    "jshell",
    "jupyter",
    "ksh",
    "lua",
    "mintty",
    "mshta",
    "msbuild",
    "node",
    "nodejs",
    "nu",
    "npm",
    "npx",
    "perl",
    "php",
    "pnpm",
    "powershell",
    "powershell_ise",
    "pwsh",
    "py",
    "python",
    "pythonw",
    "pyw",
    "runghc",
    "rscript",
    "ruby",
    "sh",
    "tclsh",
    "tcsh",
    "terminal",
    "wish",
    "wmic",
    "wscript",
    "wsl",
    "wslconfig",
    "wt",
    "windowsterminal",
    "xonsh",
    "yarn",
    "zsh",
}
# Words safe to match anywhere in a display name.  Short or generic names
# (``sh``, ``py``, ``wt``, ``ftp``) stay out of this set: they are matched
# exactly against the requested name and against shortcut targets, so ordinary
# GUI apps such as "FileZilla FTP Client" are not rejected.
_BLOCKED_LAUNCH_TOKENS = {
    "alacritty",
    "bash",
    "busybox",
    "cmder",
    "conemu",
    "conhost",
    "cscript",
    "deno",
    "dotnet-script",
    "elvish",
    "fish",
    "ghci",
    "ipython",
    "jrunscript",
    "jshell",
    "jupyter",
    "mintty",
    "mshta",
    "msbuild",
    "node",
    "nodejs",
    "npm",
    "npx",
    "perl",
    "php",
    "pnpm",
    "powershell",
    "pwsh",
    "python",
    "pythonw",
    "runghc",
    "rscript",
    "ruby",
    "tclsh",
    "tcsh",
    "terminal",
    "windowsterminal",
    "wish",
    "wmic",
    "wscript",
    "wslconfig",
    "xonsh",
    "yarn",
    "zsh",
}
# Network/download/diagnostic utilities: dangerous when they are the requested
# executable or a shortcut target, but not when the word merely appears in a
# GUI application's display name (e.g. "FileZilla FTP Client").
_BLOCKED_LAUNCH_EXACT = {
    "bitsadmin",
    "certutil",
    "curl",
    "cygwin",
    "ftp",
    "msiexec",
    "nc",
    "ncat",
    "netcat",
    "plink",
    "putty",
    "regasm",
    "regedit",
    "regsvr32",
    "rundll32",
    "scp",
    "sftp",
    "ssh",
    "tftp",
    "telnet",
    "wget",
    "winpty",
}
# Versioned host names (``python3.12``, ``tclsh86``, ``node18``, ``javaw``)
# are shell/script hosts too, but a plain prefix match would also reject
# ordinary GUI applications such as PhpStorm or RubyMine.  Build the pattern
# from the blocked names themselves and allow only a version suffix.
_VERSIONED_LAUNCH_NAMES = tuple(
    sorted(
        (
            name
            for name in _BLOCKED_LAUNCH_NAMES | _BLOCKED_LAUNCH_TOKENS
            if re.fullmatch(r"[a-z0-9_$]+", name)
        ),
        key=len,
        reverse=True,
    )
)
_BLOCKED_LAUNCH_VERSIONED_RE = re.compile(
    "(?:"
    + "|".join(re.escape(name) for name in _VERSIONED_LAUNCH_NAMES)
    + r")(?:\d+(?:[._-]\d+)*)?(?:-preview)?(?:\.exe)?",
    re.IGNORECASE,
)
# Human-readable Start-Menu names that do not contain a blocked executable
# token but are still shell hosts.
_BLOCKED_LAUNCH_PHRASES = (
    "anaconda prompt",
    "command prompt",
    "developer command prompt",
    "git bash",
)
_BLOCKED_LAUNCH_SUFFIXES = (
    ".bat",
    ".cmd",
    ".com",
    ".hta",
    ".jar",
    ".js",
    ".jse",
    ".msi",
    ".pif",
    ".ps1",
    ".py",
    ".pyw",
    ".scr",
    ".vbe",
    ".vbs",
    ".wsf",
    ".wsh",
)

# Shortcut targets are stored as UTF-16 strings inside ``.lnk`` files.  They
# are inspected because a shortcut's *display name* can be innocuous while its
# target is a shell or script host.
_SHORTCUT_TARGET_RE = re.compile(
    r"[A-Za-z0-9_.\-\\/: ]+\.(?:exe|com|bat|cmd|ps1|py|pyw|vbs|vbe|js|jse|wsf|wsh|jar|hta|scr|msi|pif)",
    re.IGNORECASE,
)
_TOKEN_SPLIT_RE = re.compile(r"[\s._\-/\\]+")


def _blocked_launch_name(name: str) -> bool:
    normalized = str(name or "").strip().strip('"').casefold()
    if not normalized:
        return False
    if any(phrase in normalized for phrase in _BLOCKED_LAUNCH_PHRASES):
        return True
    if normalized.endswith((".exe", ".com", ".bat", ".cmd")):
        normalized = normalized.rsplit(".", 1)[0]
    if normalized in _BLOCKED_LAUNCH_NAMES or normalized in _BLOCKED_LAUNCH_EXACT:
        return True
    tokens = [token for token in _TOKEN_SPLIT_RE.split(normalized) if token]
    return any(
        token in _BLOCKED_LAUNCH_TOKENS
        or _BLOCKED_LAUNCH_VERSIONED_RE.fullmatch(token) is not None
        for token in tokens
    )


def _blocked_launch_path(path: Path) -> bool:
    if _blocked_launch_name(path.name):
        return True
    return path.suffix.casefold() in _BLOCKED_LAUNCH_SUFFIXES


def _blocked_launch_shortcut(shortcut: Path) -> bool:
    """True when a Start-Menu shortcut resolves to a shell/script host."""
    if _blocked_launch_name(shortcut.stem):
        return True
    try:
        raw = shortcut.read_bytes()
    except OSError:
        return False
    for encoding in ("utf-16-le", "cp1252"):
        text = raw.decode(encoding, errors="ignore")
        for match in _SHORTCUT_TARGET_RE.finditer(text):
            target = match.group(0).strip()
            if target and _blocked_launch_path(Path(target)):
                return True
    return False


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
        if _blocked_launch_name(stem):
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
    if best is None:
        return None
    shortcut = best[2]
    if _blocked_launch_shortcut(shortcut):
        logger.warning("refusing to use blocked shortcut: %s", shortcut)
        return None
    return shortcut


def _under_roots(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _resolve_app(name: str, workspace: Path | None = None) -> tuple[str, Path] | None:
    """Resolve an application name to an executable or Start-menu shortcut.

    Executables found in the current directory or workspace are ignored: those
    directories are writable by the agent, so letting ``shutil.which`` pick a
    planted executable would turn app launching into arbitrary code execution.
    """
    if _blocked_launch_name(name):
        logger.warning("refusing to launch blocked shell/script host: %s", name)
        return None
    roots = [Path.cwd()]
    if workspace is not None:
        roots.append(Path(workspace))
    for candidate in _app_candidates(name):
        # Aliases must be checked too: "终端" maps to "wt" and "命令提示符"
        # maps to "cmd", and both would otherwise resolve to a shell host.
        if _blocked_launch_name(candidate):
            logger.warning("refusing to resolve blocked shell/script host: %s", candidate)
            continue
        located = shutil.which(candidate) or shutil.which(f"{candidate}.exe")
        if located:
            resolved = Path(located)
            if _blocked_launch_path(resolved):
                # Do not give up on the candidate: "code" is a .cmd shim on
                # PATH, while App Paths / the Start-Menu shortcut points at
                # the safe GUI executable.
                logger.warning("refusing to launch blocked executable: %s", resolved)
            elif not _under_roots(resolved, roots):
                return "executable", resolved
        registered = _registry_app_path(candidate)
        if registered is not None:
            if _blocked_launch_path(registered):
                logger.warning("refusing to launch blocked registered app: %s", registered)
            else:
                return "registered", registered
        shortcut = _shortcut_for(candidate)
        if shortcut is not None:
            if _blocked_launch_shortcut(shortcut):
                logger.warning("refusing to launch blocked shortcut: %s", shortcut)
            else:
                return "shortcut", shortcut
    return None



def _forbidden(command: str) -> str | None:
    for token in _FORBIDDEN_TOKENS:
        if token in command:
            return token
    return None


_WINDOWS_SWITCH_RE = re.compile(r"/([A-Za-z][A-Za-z0-9]*)(?::(.*))?$", re.DOTALL)
# ``/c:text`` is a literal search string, not a filesystem path (findstr).
_LITERAL_SWITCH_OPTIONS = frozenset({"c"})


def _argument_escapes_workspace(text: str, workspace: Path, *, is_value: bool = False) -> bool:
    """True when a command argument references a path outside the workspace.

    Plain search terms (``12:30``), flag switches (``/s``, ``/i``) and
    workspace-relative paths (``sub\\notes.txt``) are allowed; drive/UNC/rooted
    paths and ``..`` traversal are not.  The value of a ``/opt:value`` switch
    is checked as well: ``findstr /d:..`` selects the parent directory and
    ``/d:/Windows`` a root-level directory.  Inside a value a leading slash is
    always a rooted path, never another switch.
    """
    if not text:
        return False
    if os.name == "nt" and len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        return True
    if os.name == "nt" and text.startswith("\\"):
        # Rooted ("\Windows") and UNC ("\\server\share") paths.  Checking the
        # original text matters: normalization would turn "\Windows" into
        # "/Windows", which looks like a switch.
        return True
    normalized = text.replace("\\", "/")
    if normalized.startswith("//"):
        return True
    if normalized.startswith("/"):
        if os.name != "nt" or is_value:
            # On POSIX any leading slash is an absolute path.  In a switch
            # value Windows resolves it against the current drive root.
            return True
        switch = _WINDOWS_SWITCH_RE.fullmatch(normalized)
        if switch is None:
            # "/etc/passwd" and "/Windows/win.ini" look like paths, not
            # switches.
            return True
        option = switch.group(1).casefold()
        value = switch.group(2)
        if value is None or option in _LITERAL_SWITCH_OPTIONS:
            return False
        return any(
            _argument_escapes_workspace(part.strip(), workspace, is_value=True)
            for part in re.split(r"[;,]", value)
            if part.strip()
        )
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if ".." in parts:
        return True
    try:
        candidate = workspace / Path(*parts)
        if "/" not in normalized and not (
            candidate.exists() or candidate.is_symlink()
        ):
            # A lone search term such as ``12:30`` or ``*.txt`` is not a
            # path.  Still resolve it when it exists, so a symlink in the
            # workspace cannot point an allowlisted tool at a file outside.
            return False
        candidate = candidate.resolve()
        candidate.relative_to(workspace.resolve())
    except (OSError, ValueError):
        return True
    return False


def _findstr_reads_file_list(arguments: list[str]) -> bool:
    """True when findstr is asked to read its file list from ``/f:<file>``.

    The list contents decide which files are read, and the file can be
    replaced between validation and execution (the child re-opens it), so the
    list cannot be validated atomically.  The option is refused instead.
    """
    return any(
        re.fullmatch(r"/[Ff]:.+", str(argument).strip()) is not None
        for argument in arguments
    )


_REPARSE_POINT_ATTRIBUTE = 0x400


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & _REPARSE_POINT_ATTRIBUTE)


def _workspace_contains_reparse_point(workspace: Path) -> bool:
    """True when any entry in the workspace is a junction or symbolic link.

    Allowlisted tools run as child processes and follow reparse points:
    ``findstr /s`` and ``where /r`` recurse through a workspace junction and
    read files outside the sandbox.  The walk itself never descends into a
    reparse point (it returns as soon as one is found).
    """
    for root, dirs, files in os.walk(workspace):
        for name in list(dirs) + files:
            if _is_reparse_point(Path(root) / name):
                return True
    return False


def _split_command(command: str) -> list[str]:
    """Split a command line the way a Windows program sees its argv.

    ``shlex`` in non-POSIX mode keeps the quote characters, so an argument
    such as ``"12:30"`` was validated without quotes but executed with them.
    ``CommandLineToArgvW`` matches the parsing used by the child process and
    also handles ``/c:"hello world"`` correctly.
    """
    if os.name != "nt":
        return shlex.split(command, posix=True)

    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    shell32.CommandLineToArgvW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_int),
    ]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    argc = ctypes.c_int()
    argv = shell32.CommandLineToArgvW(command, ctypes.byref(argc))
    if not argv:
        raise ValueError("Windows could not parse the command line")
    try:
        return [argv[index] for index in range(argc.value) if argv[index] is not None]
    finally:
        kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


async def _info(context: ToolContext) -> ToolResult:
    desktop = getattr(context, "desktop", None)
    if context.dry_run and desktop is not None and getattr(desktop, "size", None):
        width, height = int(desktop.size[0]), int(desktop.size[1])
    else:
        width, height = get_screen_size()
    gui = load_pyautogui()
    if gui is None:
        backend = "none"
    elif getattr(gui, "__name__", "") == "pyautogui" or type(gui).__module__.startswith("pyautogui"):
        backend = "pyautogui"
    else:
        backend = "native"
    try:
        from executor.browser import playwright_available

        playwright = playwright_available()
    except Exception:  # noqa: BLE001
        playwright = False
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
        "input_backend": backend,
        "playwright": playwright,
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
    # Stay comfortably below the registry's per-call timeout, otherwise the
    # registry cancels the tool and the wait looks like a failure.
    ceiling = max(0.0, min(25.0, float(context.settings.adaptive_step_timeout_sec) - 2.0))
    seconds = max(0.0, min(ceiling, float(seconds)))
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
        parts = _split_command(command)
    except (ValueError, OSError) as exc:
        return ToolResult(tool="system_run_command", ok=False, error=f"cannot parse command: {exc}")
    if not parts:
        return ToolResult(tool="system_run_command", ok=False, error="empty command")

    raw_executable = parts[0].strip('"').strip("'")
    separators = [sep for sep in (os.sep, os.altsep) if sep]
    if (
        ":" in raw_executable
        or any(sep in raw_executable for sep in separators)
        or raw_executable in {".", ".."}
    ):
        raise ToolPermissionError(
            "command must be a bare executable name, not a relative or absolute path"
        )
    parts[0] = raw_executable

    executable = Path(raw_executable).name.lower()
    if executable.endswith(".exe"):
        executable = executable[: -4]
    allowed = {name.lower() for name in settings.shell_allowlist}
    if executable not in allowed:
        raise ToolPermissionError(
            f"'{executable}' is not on the shell allowlist ({', '.join(sorted(allowed))})"
        )

    # cwd=workspace does not stop an argument from naming a path elsewhere.
    # Allowlisted commands such as findstr would otherwise read any file on
    # disk (e.g. ``findstr . C:\\Windows\\win.ini``).  Legitimate switch
    # syntax (``/c:pattern``) and relative paths inside the workspace are
    # still accepted.
    workspace = Path(settings.workspace_dir)
    for index, argument in enumerate(parts[1:], start=1):
        text = str(argument).strip('"').strip("'")
        if _argument_escapes_workspace(text, workspace):
            raise ToolPermissionError(
                f"argument {index} looks like a filesystem path outside the workspace; "
                "command arguments are limited to the workspace"
            )
    if executable == "findstr" and _findstr_reads_file_list(parts[1:]):
        raise ToolPermissionError(
            "findstr /f: reads a file list whose contents cannot be validated "
            "atomically; use the file tools or pass file names directly"
        )
    if _workspace_contains_reparse_point(workspace):
        raise ToolPermissionError(
            "workspace contains a junction or symbolic link that command-line "
            "tools would follow outside the sandbox; remove it with fs_delete "
            "or use the file tools"
        )

    timeout = float(timeout if timeout is not None else settings.shell_timeout_sec)
    logger.info("shell: %s (cwd=%s)", command, workspace)

    def run() -> tuple[int, str, str]:
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
    except subprocess.TimeoutExpired:
        return ToolResult(tool="system_run_command", ok=False, error=f"command timed out after {timeout}s")
    except asyncio.TimeoutError:  # pragma: no cover - defensive
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
    blocking_alias = next(
        (candidate for candidate in _app_candidates(name) if _blocked_launch_name(candidate)),
        None,
    )
    if blocking_alias is not None:
        return ToolResult(
            tool="system_launch_app",
            ok=False,
            error=(
                f"refusing to launch '{name}' ('{blocking_alias}'): shells and script "
                "hosts are blocked (use system_run_command when shell access is "
                "explicitly enabled)"
            ),
        )
    if context.dry_run:
        desktop = getattr(context, "desktop", None)
        if desktop is not None and hasattr(desktop, "open_app"):
            desktop.open_app(name)
            return ToolResult(
                tool="system_launch_app",
                ok=True,
                simulated=True,
                output=(
                    f"would launch {name} "
                    f"(demo desktop now shows {getattr(desktop, 'active_window', name)})"
                ),
                data={"name": name, "active_window": getattr(desktop, "active_window", "")},
            )
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

    uri = _URI_APPS.get(name.casefold())
    if uri:
        try:
            os.startfile(uri)  # type: ignore[attr-defined]  # noqa: S606 - fixed settings URI
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                tool="system_launch_app", ok=False, error=f"could not open {name}: {exc}"
            )
        wait = max(0.0, min(10.0, float(wait)))
        if wait:
            await asyncio.sleep(wait)
        return ToolResult(
            tool="system_launch_app",
            ok=True,
            output=f"opened {name} ({uri})",
            data={"name": name, "target": uri, "kind": "uri"},
        )

    resolved = await asyncio.to_thread(_resolve_app, name, Path(context.settings.workspace_dir))
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


def build_system_tools(settings: Any | None = None) -> list[Tool]:
    """Factory returning system-level tools."""

    def shell_enabled() -> bool:
        return bool(settings.allow_shell) if settings is not None else True

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
                ToolParameter(
                    name="seconds",
                    type="number",
                    description="Seconds to wait (clamped below the per-call timeout)",
                    required=True,
                )
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
            available=shell_enabled,
            unavailable_reason="shell access is disabled (set VISIONPILOT_ALLOW_SHELL=true to enable)",
            parameters=[
                ToolParameter(name="command", type="string", description="Command line", required=True),
                ToolParameter(name="timeout", type="number", description="Timeout in seconds"),
            ],
            handler=_run_command,
        ),
    ]
