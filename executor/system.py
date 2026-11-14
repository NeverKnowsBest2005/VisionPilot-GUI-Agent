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
import time
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

# How long ``system_launch_app`` waits for a window by default.  Electron
# shells (WeChat, 抖音, VS Code, …) need 2-5s for their first frame even on a
# warm start, so the old 1.5s default timed out on perfectly healthy launches.
_DEFAULT_LAUNCH_WAIT = 8.0
# Launcher-style executables hand the window to an already running instance and
# exit immediately; keep polling briefly instead of reporting a failed launch.
_LAUNCH_EXIT_GRACE_SECONDS = 3.0
# Filler words people append to app names ("抖音 app", "微信客户端").
_APP_NAME_NOISE_WORDS = ("app", "application", "程序", "应用", "软件", "客户端")
# Sandboxed (Low integrity) agents cannot start most desktop apps directly:
# their child process inherits the restricted job, cannot write the app's
# profile directory and exits within a few hundred milliseconds.  Explorer runs
# at Medium integrity, so asking the shell to open the same target works.  The
# retry gets its own, shorter budget.
_SHELL_RETRY_WAIT = 5.0
# A direct spawn that dies faster than this never reached its own UI: the shell
# (`ShellExecute`, i.e. a double click) is then worth one attempt.
_SHELL_RETRY_EXIT_WINDOW = 2.5
_LOW_INTEGRITY_HINT = (
    "this agent process runs at Low integrity (it was started from a sandboxed "
    "shell), so applications that write their profile directory or need desktop "
    "access quit immediately; start VisionPilot from a normal terminal"
)
# A UAC consent prompt lives on the secure ("Winlogon") desktop: this process can
# neither screenshot it nor click it, and the window it guards never appears.
# ``requireAdministrator`` is refused up front with a message the user can act on,
# instead of waiting 8s + 5s for a window that cannot come.  ``highestAvailable``
# depends on the caller (a standard user starts it unelevated), so that one is
# left to ``CreateProcess``: it fails fast with ERROR_ELEVATION_REQUIRED (740).
_ELEVATED_EXECUTION_LEVELS = frozenset({"requireAdministrator"})
_EXECUTION_LEVEL_NAMES = {
    "asinvoker": "asInvoker",
    "highestavailable": "highestAvailable",
    "requireadministrator": "requireAdministrator",
}
_MANIFEST_LEVEL_RE = re.compile(
    r"requestedExecutionLevel\b[^>]*?\blevel\s*=\s*[\"']"
    r"(asInvoker|highestAvailable|requireAdministrator)[\"']",
    re.IGNORECASE,
)
# Embedded manifests are tiny; only the non-PE fallback scans the file itself.
_MANIFEST_SCAN_LIMIT = 4 * 1024 * 1024
_ERROR_ELEVATION_REQUIRED = 740

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


def _squash_name(name: str) -> str:
    """Casefold a display name and drop the punctuation people add for looks."""
    return re.sub(r"[\s._\-()（）\[\]，,、·]+", "", str(name or "").casefold())


def _app_name_variants(name: str) -> list[str]:
    """Casefolded spellings of an app name that may match a shortcut stem."""
    base = " ".join(str(name or "").split()).casefold()
    if not base:
        return []
    variants = [base]
    trimmed = base
    for noise in _APP_NAME_NOISE_WORDS:
        if trimmed.endswith(noise) and len(trimmed) > len(noise):
            trimmed = trimmed[: -len(noise)].strip()
    if trimmed and trimmed not in variants:
        variants.append(trimmed)
    if base.endswith(".exe") and len(base) > 4 and base[:-4] not in variants:
        variants.append(base[:-4])
    for source in (trimmed, base):
        squashed = _squash_name(source)
        if squashed and squashed not in variants:
            variants.append(squashed)
    return [variant for variant in variants if len(variant) >= 2]


def _name_match_score(stem: str, needles: list[str]) -> int | None:
    """Rank a shortcut stem against the requested name (``None`` = no match).

    Lower is better: 0 exact, 1 prefix, 2 the stem contains the name, 3 the
    name contains the stem ("抖音短视频" -> "抖音.lnk").
    """
    text = str(stem or "").casefold()
    squashed_stem = _squash_name(text)
    if not squashed_stem:
        return None
    best: int | None = None
    for needle in needles:
        squashed_needle = _squash_name(needle)
        if not squashed_needle:
            continue
        if squashed_stem == squashed_needle:
            score = 0
        elif squashed_stem.startswith(squashed_needle):
            score = 1
        elif squashed_needle in squashed_stem:
            score = 2
        elif squashed_stem in squashed_needle:
            score = 3
        else:
            continue
        if best is None or score < best:
            best = score
    return best


#: Score used when only the shortcut's *target executable* matches the name.
_TARGET_STEM_SCORE = 5


def _shortcut_target_matches(shortcut: Path, needles: list[str]) -> bool:
    """True when the shortcut points at an executable whose stem matches."""
    for target in _shortcut_targets(shortcut):
        if target.suffix.casefold() != ".exe":
            continue
        stem = target.name.rsplit(".", 1)[0].casefold()
        if stem in needles and not _blocked_launch_name(stem):
            return True
    return False


def _shortcut_for(name: str) -> Path | None:
    """Find the Start-Menu shortcut that best matches ``name``.

    Matching is tiered so a friendlier name still hits: exact stem, stem
    prefix, stem contains the name, name contains the stem, and — as a last
    resort — the executable the shortcut launches ("Douyin" -> 抖音.lnk, whose
    target is ``douyin.exe``).  The last tier needs no vendor-specific aliases.
    """
    needles = _app_name_variants(name)
    if not needles:
        return None
    loose: list[Path] = []
    best: tuple[int, int, Path] | None = None
    for shortcut in _start_menu_shortcuts():
        stem = shortcut.stem.casefold()
        if any(token in stem for token in _START_MENU_SKIP):
            continue
        if _blocked_launch_name(stem):
            continue
        score = _name_match_score(stem, needles)
        if score is None:
            loose.append(shortcut)
            continue
        candidate = (score, len(shortcut.stem), shortcut)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    if best is None:
        for shortcut in loose:
            if _shortcut_target_matches(shortcut, needles):
                candidate = (_TARGET_STEM_SCORE, len(shortcut.stem), shortcut)
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


def _is_shell_alias(path: Path) -> bool:
    """True for Store-app execution aliases, which only the shell can activate.

    Packaged apps expose a stub under ``.../Microsoft/WindowsApps``; running that
    stub through ``CreateProcess`` (even detached) dies immediately with
    ``0xC0000409`` because the activation context is missing.  ``ShellExecute``
    routes it through the app-model broker, which does start the app.
    """
    return any(part.casefold() == "windowsapps" for part in path.parts)


_INTEGRITY_LABELS = {
    "S-1-16-0": "Untrusted",
    "S-1-16-4096": "Low",
    "S-1-16-8192": "Medium",
    "S-1-16-8448": "MediumPlus",
    "S-1-16-12288": "High",
    "S-1-16-16384": "System",
}


def _integrity_label() -> str | None:
    """Windows integrity label of this process ("Low", "Medium", …), if known."""
    if os.name != "nt":
        return None
    try:  # pragma: no cover - Windows-only
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32
        token = ctypes.c_void_p()
        if not advapi32.OpenProcessToken(
            ctypes.c_void_p(kernel32.GetCurrentProcess()), 0x0008, ctypes.byref(token)
        ):
            return None
        try:
            size = wintypes.DWORD(0)
            advapi32.GetTokenInformation(token, 25, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(max(size.value, 64))
            if not advapi32.GetTokenInformation(
                token, 25, buffer, size.value or 64, ctypes.byref(size)
            ):
                return None
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
            text = ctypes.c_wchar_p()
            if not advapi32.ConvertSidToStringSidW(ctypes.c_void_p(sid), ctypes.byref(text)):
                return None
            return _INTEGRITY_LABELS.get(text.value or "")
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(token.value or 0))
    except Exception:  # noqa: BLE001 - diagnostics only
        return None


def _read_embedded_manifest(executable: Path) -> bytes | None:
    """Raw ``RT_MANIFEST`` resource of ``executable`` (Windows only)."""
    if os.name != "nt":  # pragma: no cover - Windows only
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    load_library = kernel32.LoadLibraryExW
    load_library.restype = wintypes.HMODULE
    load_library.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    find_resource = kernel32.FindResourceW
    find_resource.restype = ctypes.c_void_p
    find_resource.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p]
    size_of_resource = kernel32.SizeofResource
    size_of_resource.restype = wintypes.DWORD
    size_of_resource.argtypes = [wintypes.HMODULE, ctypes.c_void_p]
    load_resource = kernel32.LoadResource
    load_resource.restype = ctypes.c_void_p
    load_resource.argtypes = [wintypes.HMODULE, ctypes.c_void_p]
    lock_resource = kernel32.LockResource
    lock_resource.restype = ctypes.c_void_p
    lock_resource.argtypes = [ctypes.c_void_p]
    free_library = kernel32.FreeLibrary
    free_library.argtypes = [wintypes.HMODULE]

    load_library_as_datafile = 0x00000002
    manifest_id = 1  # CREATEPROCESS_MANIFEST_RESOURCE_ID
    manifest_type = 24  # RT_MANIFEST
    module = load_library(str(executable), None, load_library_as_datafile)
    if not module:
        return None
    try:
        resource = find_resource(
            module, ctypes.c_void_p(manifest_id), ctypes.c_void_p(manifest_type)
        )
        if not resource:
            return None
        size = int(size_of_resource(module, resource))
        handle = load_resource(module, resource)
        if not size or not handle:
            return None
        pointer = lock_resource(handle)
        if not pointer:
            return None
        return ctypes.string_at(pointer, size)
    finally:
        free_library(module)


def _requested_execution_level(executable: Path | None) -> str | None:
    """``requestedExecutionLevel`` declared by ``executable``'s manifest."""
    if executable is None or executable.suffix.casefold() != ".exe":
        return None
    manifest = _read_embedded_manifest(executable)
    if manifest is None:
        try:
            manifest = executable.read_bytes()[:_MANIFEST_SCAN_LIMIT]
        except OSError:
            return None
    for encoding in ("utf-8", "utf-16-le"):
        match = _MANIFEST_LEVEL_RE.search(manifest.decode(encoding, errors="ignore"))
        if match:
            return _EXECUTION_LEVEL_NAMES[match.group(1).casefold()]
    return None


def _needs_admin_result(
    name: str, target: Path | None, kind: str, level: str | None
) -> ToolResult:
    """Refuse an elevated target with a message the user can act on."""
    requested = f" (requestedExecutionLevel={level})" if level else ""
    return ToolResult(
        tool="system_launch_app",
        ok=False,
        output=f"launch target: {target}",
        error=(
            f"{name} needs administrator rights{requested}: Windows would show a UAC "
            "consent prompt on the secure desktop, which this agent cannot see or "
            "click; ask the user to launch and approve it, then continue the task"
        ),
        data={
            "name": name,
            "target": str(target) if target is not None else None,
            "kind": kind,
            "needs_admin": True,
            "requested_execution_level": level,
        },
    )


def _shell_launch(target: Path) -> bool:
    """Open ``target`` exactly like a double click in Explorer would."""
    if os.name != "nt" or not hasattr(os, "startfile"):
        return False
    try:  # pragma: no cover - Windows-only
        os.startfile(str(target))  # type: ignore[attr-defined]  # noqa: S606 - user target
        return True
    except OSError:
        return False


async def _launch_app(
    context: ToolContext, name: str, wait: float = _DEFAULT_LAUNCH_WAIT
) -> ToolResult:
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
        # A URI launch has no window to wait for, so cap the settle time.
        wait = max(0.0, min(10.0, float(wait)))
        await asyncio.sleep(min(wait, 2.0))
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
    launch_path = _shortcut_executable_path(path) if kind == "shortcut" else path
    requested_level = await asyncio.to_thread(_requested_execution_level, launch_path)
    if requested_level in _ELEVATED_EXECUTION_LEVELS:
        # Do not let the shell raise a UAC prompt: nobody can answer it from here.
        return _needs_admin_result(name, launch_path, kind, requested_level)
    launch_process: subprocess.Popen | None = None
    launch_started = time.monotonic()
    try:
        if launch_path is not None and _is_shell_alias(launch_path):
            # Store apps (Paint, Notepad, Calculator, …) must go through the shell.
            os.startfile(str(launch_path))  # type: ignore[attr-defined]  # noqa: S606
        elif launch_path is not None and launch_path.suffix.casefold() == ".exe":
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
            try:
                launch_process = subprocess.Popen(  # noqa: S603 - resolved installed app
                    [str(launch_path)],
                    cwd=str(launch_path.parent),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                )
            except OSError as exc:
                if getattr(exc, "winerror", None) == _ERROR_ELEVATION_REQUIRED:
                    # The manifest was unreadable, said ``highestAvailable`` (a
                    # standard user could start it) or the shortcut pointed at a
                    # shim; never leave a UAC prompt hanging on the desktop.
                    return _needs_admin_result(name, launch_path, kind, requested_level)
                # Targets that cannot be spawned from here (mmc.exe, osk.exe, …)
                # may still work through the shell, exactly like a double click.
                os.startfile(str(launch_path))  # type: ignore[attr-defined]  # noqa: S606
        else:
            os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606 - resolved installed app
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool="system_launch_app", ok=False, error=f"could not launch {name}: {exc}")

    wait = max(0.0, min(10.0, float(wait)))
    launch_pid = launch_process.pid if launch_process is not None else None

    async def poll_window(pid: int | None, budget: float) -> dict[str, Any] | None:
        """Poll for the app's window until ``budget`` seconds have passed."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(budget, 0.5)
        exit_deadline: float | None = None
        found_window: dict[str, Any] | None = None
        while True:
            now = loop.time()
            if now >= deadline or (exit_deadline is not None and now >= exit_deadline):
                return found_window
            found = await asyncio.to_thread(
                _activate_process_window,
                launch_path.name if launch_path is not None else path.name,
                name if kind == "shortcut" else None,
                _shortcut_executable_names(path) if kind == "shortcut" else None,
                pid,
            )
            if found:
                found_window = found
                if found.get("focused", True):
                    return found
            elif (
                exit_deadline is None
                and launch_process is not None
                and launch_process.poll() is not None
            ):
                # Launcher-style executables hand the window to an already
                # running instance and exit; keep looking for a moment.
                exit_deadline = loop.time() + _LAUNCH_EXIT_GRACE_SECONDS
            await asyncio.sleep(0.25)

    window = await poll_window(launch_pid, wait)
    if window is None:
        # ``CreateProcess`` is not the same as a double click: packaged apps
        # (Notepad, Paint, Calculator) and launcher shims die instantly without
        # an activation context, and a sandboxed agent cannot start much at all.
        # Retry through the shell once, which is what a user click does.
        target_path = launch_path or path
        retry_target = path if kind == "shortcut" else target_path
        quick_exit = (
            launch_process is not None
            and launch_process.poll() is not None
            and time.monotonic() - launch_started < _SHELL_RETRY_EXIT_WINDOW
        )
        if launch_process is None or quick_exit:
            if _shell_launch(retry_target):
                logger.debug("retrying launch of %s through the shell", retry_target)
                proxied = await poll_window(None, min(wait, _SHELL_RETRY_WAIT))
                if proxied is not None:
                    window = proxied
    if window is not None and not window.get("focused", True):
        foreground = await asyncio.to_thread(_foreground_window_title)
        return ToolResult(
            tool="system_launch_app",
            ok=False,
            output=f"launch target: {launch_path or path}",
            error=(
                f"found the {name} window ({window['title']}) but Windows would not "
                f"bring it to the foreground; '{foreground or 'another window'}' still has focus"
            ),
            data={"name": name, "target": str(launch_path or path), "kind": kind,
                  "window_found": True, "focused": False, "foreground": foreground, **window},
        )
    if window is None:
        target_path = launch_path or path
        process_status = launch_process.poll() if launch_process is not None else None
        if launch_process is not None and process_status is not None:
            exit_code = int(process_status) & 0xFFFFFFFF
            failure = (
                f"{target_path.name} exited before a visible window appeared "
                f"(exit code {process_status}"
                + (f" / 0x{exit_code:08X}" if process_status < 0 else "")
                + ")"
            )
        elif launch_process is None:
            failure = (
                f"started {target_path.name} through the Windows shell, "
                f"but no visible {name} window appeared"
            )
        else:
            failure = f"{target_path.name} is running but no visible {name} window appeared"
        if _integrity_label() == "Low":
            failure = f"{failure}; {_LOW_INTEGRITY_HINT}"
        return ToolResult(
            tool="system_launch_app",
            ok=False,
            output=f"launch target: {target_path}",
            error=failure,
            data={"name": name, "target": str(target_path), "kind": kind,
                  "pid": launch_process.pid if launch_process else None,
                  "process_exit_code": process_status, "window_found": False,
                  "integrity": _integrity_label()},
        )
    return ToolResult(
        tool="system_launch_app",
        ok=True,
        output=f"launched and activated {name}: {window['title']}",
        data={"name": name, "target": str(launch_path or path), "kind": kind,
              "window_found": True, **window},
    )


def _shortcut_targets(shortcut: Path) -> list[Path]:
    """Absolute paths mentioned inside a Windows shortcut payload (best effort)."""
    try:
        raw = shortcut.read_bytes()
    except OSError:
        return []
    targets: list[Path] = []
    seen: set[str] = set()
    for encoding in ("utf-16-le", "cp1252"):
        for match in _SHORTCUT_TARGET_RE.finditer(raw.decode(encoding, errors="ignore")):
            text = match.group(0).strip()
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                targets.append(Path(text))
    return targets


def _shortcut_executable_names(shortcut: Path) -> set[str]:
    """Best-effort executable names extracted from a Windows shortcut file."""
    return {
        target.name.casefold()
        for target in _shortcut_targets(shortcut)
        if target.suffix.casefold() == ".exe"
    }


def _shortcut_executable_path(shortcut: Path) -> Path | None:
    """Return an existing absolute EXE target from a Start Menu shortcut."""
    for candidate in _shortcut_targets(shortcut):
        if (candidate.is_absolute() and candidate.suffix.casefold() == ".exe"
                and candidate.is_file() and not _blocked_launch_path(candidate)):
            return candidate
    return None


def _descendant_pids(root_pid: int | None) -> set[int]:
    """``root_pid`` plus every process it spawned (Windows only, best effort).

    Electron shells and launchers open their window from a child process, so
    matching on the process image alone misses windows like 抖音's.
    """
    if os.name != "nt" or not root_pid:
        return set()
    try:  # pragma: no cover - Windows-only
        import ctypes
        from ctypes import wintypes

        class ProcessEntry32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return {root_pid}
        children: dict[int, list[int]] = {}
        try:
            entry = ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(ProcessEntry32W)
            more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                children.setdefault(int(entry.th32ParentProcessID), []).append(
                    int(entry.th32ProcessID)
                )
                more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not read the process tree: %s", exc)
        return {root_pid}
    family = {root_pid}
    frontier = [root_pid]
    while frontier:
        for child in children.get(frontier.pop(), []):
            if child not in family:
                family.add(child)
                frontier.append(child)
    return family


def _window_match_score(
    image: str,
    pid: int,
    title: str,
    expected_names: set[str],
    title_match: str,
    lineage: set[int],
    self_pid: int | None = None,
) -> int | None:
    """Rank how well a window matches the launch request (``None`` = no match).

    Lower is better: 0 the expected executable owns the window, 1 a process
    spawned by the launched one owns it, 2 only the title matches.  Title
    matching is a *fallback* so ``.lnk``/launcher launches still work when the
    visible window belongs to a differently named helper process.
    """
    if self_pid is not None and pid == self_pid:
        return None
    if image in expected_names:
        return 0
    if pid in lineage:
        return 1
    hint = (title_match or "").strip().casefold()
    if len(hint) >= 2 and hint in title.casefold():
        return 2
    return None


_SHELL_WINDOW_CLASSES = frozenset({"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW"})


def _foreground_window_title() -> str:
    """Title of the window that currently owns the foreground (empty if none)."""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = wintypes.HWND(user32.GetForegroundWindow())
        if not _handle_value(hwnd):
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value.strip()
    except Exception:  # noqa: BLE001
        return ""


def _handle_value(handle: Any) -> int:
    """Normalise a Win32 handle (int or ``ctypes`` pointer) to a plain int."""
    value = getattr(handle, "value", handle)
    return int(value) if value else 0


def _top_window_handle() -> int:
    """Handle of the topmost visible, titled window (the one users see in front)."""
    if os.name != "nt":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        handles: list[int] = []
        shell_classes = _SHELL_WINDOW_CLASSES

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _lparam):
            if len(handles) > 24:
                return False
            if not (user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd)):
                return True
            buffer = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buffer, 256)
            if buffer.value in shell_classes:
                return True
            handles.append(int(hwnd))
            return True

        user32.EnumWindows(visit, 0)
        return handles[0] if handles else 0
    except Exception:  # noqa: BLE001
        return 0


def _window_is_in_front(hwnd: int) -> bool:
    """Whether ``hwnd`` is what the user sees in front.

    ``GetForegroundWindow`` is preferred, but it returns ``NULL`` for callers at
    a lower integrity level and while the foreground is another app, so the
    Z-order top window is used as the fallback — it is also exactly what
    :func:`perception.vision` reports as the active window, so a window that
    passes here is the one the agent will see next.
    """
    if os.name != "nt":
        return False
    return _foreground_handle() == hwnd or _top_window_handle() == hwnd


def _foreground_handle() -> int:
    if os.name != "nt":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        return _handle_value(user32.GetForegroundWindow())
    except Exception:  # noqa: BLE001
        return 0


def _click_title_strip(hwnd: int) -> bool:
    """Click the top centre of a window so Windows hands it the foreground.

    ``SetForegroundWindow`` only works for a process that owns the foreground or
    received the last input event, which a background agent usually did not.  A
    synthetic click is real input, so it bypasses that lock; it is aimed at the
    top centre of the window chrome, which is empty drag area in both classic and
    Electron/custom frames, and the pointer is put back afterwards.
    """
    if os.name != "nt":
        return False
    try:  # pragma: no cover - Windows-only
        import ctypes
        from ctypes import wintypes

        from executor import native_input

        gui = native_input.load_native_gui()
        if gui is None:
            return False
        rect = wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return False
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width < 120 or height < 80:
            return False
        x = rect.left + width // 2
        y = rect.top + max(6, min(14, height // 40))
        try:
            origin = gui.position()
        except Exception:  # noqa: BLE001
            origin = None
        gui.moveTo(x, y)
        time.sleep(0.15)
        gui.click()
        time.sleep(0.2)
        if origin is not None:
            try:
                gui.moveTo(*origin)
            except Exception:  # noqa: BLE001
                pass
        return _window_is_in_front(hwnd)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not click the window chrome: %s", exc)
        return False


def _raise_window(hwnd: int) -> bool:
    """Put ``hwnd`` on top of the Z-order without asking for the foreground.

    The foreground lock can refuse activation for good, but the Z-order is not
    protected: a short ``HWND_TOPMOST``/``HWND_NOTOPMOST`` cycle lifts the window
    above its siblings.  The agent derives the active window from that order, so
    this is what turns "launched but the console is still in front" into a usable
    screen, and later clicks land inside the app.
    """
    if os.name != "nt":
        return False
    try:  # pragma: no cover - Windows-only
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        swp_no_size, swp_no_move, swp_no_activate = 0x0001, 0x0002, 0x0010
        flags = swp_no_size | swp_no_move | swp_no_activate
        target = wintypes.HWND(hwnd)
        user32.SetWindowPos(target, wintypes.HWND(-1), 0, 0, 0, 0, flags)  # HWND_TOPMOST
        user32.SetWindowPos(target, wintypes.HWND(-2), 0, 0, 0, 0, flags)  # HWND_NOTOPMOST
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("raise window failed: %s", exc)
        return False


def _force_foreground(hwnd: int, attempts: int = 3) -> bool:
    """Try hard to bring ``hwnd`` in front and verify that it worked.

    ``SetForegroundWindow`` is *silently* ignored when the calling process does
    not own the foreground: a background agent that launches an app then sees
    its own console/browser still in front, reports a healthy launch, and every
    later step works on the wrong window ("启动成功但界面没变").  Attaching to
    the foreground thread, the Alt-key nudge, ``SwitchToThisWindow`` and a
    minimise/restore cycle are the standard workarounds; success is verified by
    :func:`_window_is_in_front` instead of trusting a return value.
    """
    if os.name != "nt":
        return False
    try:  # pragma: no cover - Windows-only
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        target = wintypes.HWND(hwnd)
        switch_to_this_window = getattr(user32, "SwitchToThisWindow", None)

        for attempt in range(attempts):
            if _window_is_in_front(hwnd):
                return True
            if user32.IsIconic(target):
                user32.ShowWindow(target, 9)  # SW_RESTORE
                time.sleep(0.05)
            # A bare Alt tap is enough to make this process eligible to take
            # the foreground without changing anything visible.
            user32.keybd_event(0x12, 0, 0, 0)  # VK_MENU down
            user32.keybd_event(0x12, 0, 0x0002, 0)  # VK_MENU up
            current_tid = kernel32.GetCurrentThreadId()
            attached: list[int] = []
            for owner in (_foreground_handle(), hwnd):
                tid = _handle_value(user32.GetWindowThreadProcessId(wintypes.HWND(owner), None))
                if tid and tid != current_tid and user32.AttachThreadInput(tid, current_tid, True):
                    attached.append(tid)
            try:
                user32.BringWindowToTop(target)
                user32.SetForegroundWindow(target)
                user32.SetFocus(target)
            finally:
                for tid in attached:
                    user32.AttachThreadInput(tid, current_tid, False)
            if _window_is_in_front(hwnd):
                return True
            if switch_to_this_window is not None:
                switch_to_this_window(target, True)
                if _window_is_in_front(hwnd):
                    return True
            if attempt == 0:
                # Ask for the foreground explicitly; granted only when this
                # process is allowed to hand out foreground rights.
                allow = getattr(user32, "AllowSetForegroundWindow", None)
                if allow is not None:
                    allow(wintypes.DWORD(0xFFFFFFFF))  # ASFW_ANY
            if attempt >= 1:
                # Minimise/restore makes the window manager perform the switch.
                user32.ShowWindow(target, 6)  # SW_MINIMIZE
                user32.ShowWindow(target, 9)  # SW_RESTORE
                if _window_is_in_front(hwnd):
                    return True
            _raise_window(hwnd)
            if _window_is_in_front(hwnd):
                return True
            if attempt == attempts - 1 and _click_title_strip(hwnd):
                # Last resort: a synthetic click is real user input, the only
                # thing Windows accepts once the foreground lock is engaged.
                return True
            time.sleep(0.1)
        return _window_is_in_front(hwnd)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not force the foreground window: %s", exc)
        return False


def _activate_process_window(
    executable: str,
    title_hint: str | None = None,
    executable_aliases: set[str] | None = None,
    launch_pid: int | None = None,
) -> dict[str, Any] | None:
    """Find a visible app window and request focus, including shortcut launches."""
    if os.name != "nt":
        return None
    try:  # pragma: no cover - Windows-only
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        expected = Path(executable).name.casefold()
        expected_names = {expected} | {
            str(alias).casefold() for alias in (executable_aliases or set())
        }
        title_match = (title_hint or Path(executable).stem).casefold()
        lineage = _descendant_pids(launch_pid)
        self_pid = os.getpid()
        best: tuple[int, dict[str, Any]] | None = None

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _):
            nonlocal best
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if not length:
                return True
            title_buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title_buffer, length + 1)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
            if not process:
                return True
            image_buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(image_buffer))
            try:
                if kernel32.QueryFullProcessImageNameW(process, 0, image_buffer, ctypes.byref(size)):
                    score = _window_match_score(
                        Path(image_buffer.value).name.casefold(),
                        int(pid.value),
                        title_buffer.value,
                        expected_names,
                        title_match,
                        lineage,
                        self_pid,
                    )
                    if score is not None and (best is None or score < best[0]):
                        best = (
                            score,
                            {
                                "title": title_buffer.value.strip(),
                                "pid": int(pid.value),
                                "hwnd": int(hwnd),
                            },
                        )
            finally:
                kernel32.CloseHandle(process)
            return True

        user32.EnumWindows(visit, 0)
        if best is None:
            return None
        window = best[1]
        window["focused"] = _force_foreground(window["hwnd"])
        if not window["focused"]:
            logger.warning(
                "found the %s window (%s) but Windows kept '%s' in the foreground",
                executable,
                window["title"],
                _foreground_window_title(),
            )
        return window
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not find/activate window for %s: %s", executable, exc)
        return None


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
                "Start Menu shortcuts, then waits for the app's first window and "
                "brings it to the front. Prefer this over Start-menu keystrokes."
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
                    description=(
                        "Seconds to wait for the app window (0-10). Electron apps "
                        "can need several seconds for their first screen."
                    ),
                    default=_DEFAULT_LAUNCH_WAIT,
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
