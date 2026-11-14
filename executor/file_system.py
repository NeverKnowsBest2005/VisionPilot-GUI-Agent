"""Filesystem tools sandboxed to ``settings.workspace_dir``.

The agent can read, write, list and search inside its workspace and nowhere
else: every path is resolved and checked against the workspace root, so
``..\\..\\Windows`` or an absolute ``C:\\`` path is rejected before touching the
disk.  Deletion is classified as a dangerous operation and is therefore gated by
the registry's confirmation policy.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import shutil
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
)

logger = logging.getLogger("visionpilot.executor.files")

_TEXT_ENCODING = "utf-8"
_REPARSE_ATTRIBUTE = 0x400

# Win32 resolves these names (and ``NAME.ext`` variants) to devices even when
# they are prefixed with a workspace path, so a "write" can silently succeed
# without creating a file.  See "Naming Files, Paths, and Namespaces".
_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "conin$", "conout$", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
    | {"com¹", "com²", "com³", "lpt¹", "lpt²", "lpt³"}
)


def _check_win32_names(raw: str | Path, candidate: Path, workspace: Path) -> None:
    """Reject Win32 path aliases that bypass the workspace namespace.

    Alternate data streams (``file:stream``), trailing spaces/dots and
    reserved device names (``NUL``, ``CON.txt``, ``COM1``) all name something
    other than the directory entry the agent asked for.  Only the components
    the caller supplied are inspected: the workspace prefix is trusted, so a
    workspace such as ``C:\\tmp\\aux\\workspace`` keeps working.
    """
    if os.name != "nt":
        return
    try:
        supplied = candidate.relative_to(workspace)
    except ValueError:
        # Outside the workspace: containment is rejected after this check, but
        # scan the caller's raw path in the meantime.
        supplied = Path(raw).expanduser()
    for part in supplied.parts:
        if part in {"", supplied.anchor, ".", ".."}:
            continue
        # Win32 trims trailing spaces/dots but preserves leading (and Unicode)
        # whitespace, so only rstrip here; `` NUL`` is an ordinary filename.
        trimmed = part.rstrip(" .")
        stem = trimmed.split(".", 1)[0].casefold()
        if stem in _RESERVED_DEVICE_NAMES:
            raise ToolPermissionError(
                f"path '{raw}' names the reserved Windows device '{part}'"
            )
        if part != trimmed:
            # Every component is normalized by Win32, not only the last one:
            # ``sub. /file.txt`` aliases ``sub/file.txt``.
            raise ToolPermissionError(
                f"path '{raw}' has a component with trailing spaces or dots, "
                "which Windows aliases to another entry"
            )
    name = candidate.name
    if ":" in name:
        # ``jump:leak`` names an alternate data stream of ``jump``.  The
        # parent resolves inside the workspace and the stream suffix hides the
        # junction from ``_is_reparse_point``/``exists``.
        raise ToolPermissionError(
            f"path '{raw}' must name a file or directory, not an alternate data stream"
        )
    if name != name.rstrip(" ."):
        # Win32 strips trailing spaces and dots when opening a file, so
        # ``f.txt.`` aliases ``f.txt`` while Path equality does not.  Move
        # could otherwise unlink the source before shutil.move fails.
        raise ToolPermissionError(
            f"path '{raw}' has trailing spaces or dots, which Windows aliases "
            "to another entry"
        )


def resolve_workspace_path(context: ToolContext, raw: str | Path) -> Path:
    """Resolve ``raw`` inside the workspace or raise :class:`ToolPermissionError`.

    Symbolic links and junctions are followed here, so a link that points
    outside the workspace cannot be used as a read/write escape hatch.
    """
    workspace = Path(context.settings.workspace_dir).expanduser().resolve()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    _check_win32_names(raw, candidate, workspace)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ToolPermissionError(
            f"path '{raw}' escapes the workspace sandbox ({workspace})"
        ) from exc
    return resolved


def resolve_workspace_entry(context: ToolContext, raw: str | Path) -> Path:
    """Resolve a path without following the final component.

    Used for delete/move/copy so operating on a link affects the link itself
    rather than silently acting on a target elsewhere.  The parent directory is
    still resolved and checked against the sandbox.
    """
    workspace = Path(context.settings.workspace_dir).expanduser().resolve()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    _check_win32_names(raw, candidate, workspace)
    parent = candidate.parent.resolve()
    try:
        parent.relative_to(workspace)
    except ValueError as exc:
        raise ToolPermissionError(
            f"path '{raw}' escapes the workspace sandbox ({workspace})"
        ) from exc
    name = candidate.name
    if name == "..":
        # ``Path('..').name == '..'``; allowing it would map the path to the
        # workspace root (or a parent inside it) and defeat the root guard in
        # ``_delete``.  A destination must name a concrete entry.
        raise ToolPermissionError(
            f"path '{raw}' must name an entry inside the workspace; '..' is not allowed"
        )
    if name in {"", "."}:
        return parent
    return parent / name


def _same_entry(first: Path, second: Path) -> bool:
    """True when two paths name the same directory entry (including aliases)."""
    try:
        if os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second)):
            return True
        if first.exists() and second.exists():
            return os.path.samefile(first, second)
    except OSError:
        return False
    return False


def _is_reparse_point(path: Path) -> bool:
    """True for symlinks, junctions and other Windows reparse points."""
    try:
        if path.is_symlink():
            return True
        if os.name != "nt":
            return False
        is_junction = getattr(os.path, "isjunction", None)
        if is_junction is not None:
            try:
                if is_junction(path):
                    return True
            except OSError:
                pass
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & _REPARSE_ATTRIBUTE)
    except OSError:
        return False


def _ignore_reparse(directory: str, names: list[str]) -> set[str]:
    base = Path(directory)
    return {name for name in names if _is_reparse_point(base / name)}


def _find_reparse_point(root: Path) -> Path | None:
    """Return the first link/junction at or below ``root``, if any."""
    if _is_reparse_point(root):
        return root
    for current, dirs, files in os.walk(root):
        base = Path(current)
        for name in list(dirs) + files:
            child = base / name
            if _is_reparse_point(child):
                return child
    return None


def _is_inside(path: Path, root: Path) -> bool:
    """Case-aware containment check for already-resolved Windows paths."""
    try:
        common = os.path.commonpath([str(path), str(root)])
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(str(root))


def _display(context: ToolContext, path: Path) -> str:
    workspace = Path(context.settings.workspace_dir).resolve()
    try:
        relative = path.relative_to(workspace)
    except ValueError:
        return str(path)
    return relative.as_posix() or "."


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _is_probably_binary(data: bytes) -> bool:
    if b"\x00" in data[:4096]:
        return True
    try:
        data[:4096].decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


def _max_bytes(context: ToolContext) -> int:
    return int(context.settings.max_file_bytes)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
async def _list_dir(context: ToolContext, path: str = ".", recursive: bool = False) -> ToolResult:
    target = resolve_workspace_path(context, path)
    if not target.exists():
        return ToolResult(tool="fs_list", ok=False, error=f"directory not found: {path}")
    if not target.is_dir():
        return ToolResult(tool="fs_list", ok=False, error=f"not a directory: {path}")

    def iter_entries():
        if not recursive:
            yield from target.iterdir()
            return
        for current, dirs, files in os.walk(target):
            base = Path(current)
            kept: list[str] = []
            for name in dirs:
                if name.startswith("."):
                    continue
                child = base / name
                # Never descend through junctions/symlinks: they can point
                # outside the workspace even though their name is inside it.
                if _is_reparse_point(child):
                    continue
                kept.append(name)
                yield child
            dirs[:] = kept
            for name in files:
                if name.startswith("."):
                    continue
                child = base / name
                if _is_reparse_point(child):
                    continue
                yield child

    def scan() -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for entry in iter_entries():
            if entry.name.startswith(".") and entry.name not in {".env.example"}:
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            entries.append(
                {
                    "name": entry.name,
                    "path": _display(context, entry),
                    "type": "dir" if entry.is_dir() else "file",
                    "size": stat.st_size if entry.is_file() else None,
                    "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                }
            )
            if len(entries) >= 400:
                break
        entries.sort(key=lambda item: (item["type"] != "dir", item["name"].lower()))
        return entries

    entries = await asyncio.to_thread(scan)
    listing = "\n".join(
        f"{item['type']}  {item['path']}" + (f"  ({item['size']} B)" if item["size"] is not None else "")
        for item in entries
    ) or "(empty)"
    return ToolResult(
        tool="fs_list",
        ok=True,
        output=f"contents of {_display(context, target)}:\n{listing}",
        data={"path": _display(context, target), "entries": entries, "count": len(entries)},
    )


async def _read_file(context: ToolContext, path: str, max_chars: int = 8000) -> ToolResult:
    max_chars = max(1, min(200_000, int(max_chars)))
    target = resolve_workspace_path(context, path)
    if not target.exists():
        return ToolResult(tool="fs_read", ok=False, error=f"file not found: {path}")
    if target.is_dir():
        return ToolResult(tool="fs_read", ok=False, error=f"'{path}' is a directory; use fs_list")
    size = target.stat().st_size
    if size > _max_bytes(context):
        return ToolResult(
            tool="fs_read",
            ok=False,
            error=f"file too large ({size} bytes > {_max_bytes(context)}); increase max_file_bytes",
        )

    def read() -> str:
        raw = target.read_bytes()
        if _is_probably_binary(raw):
            raise ValueError("binary file")
        return raw.decode(_TEXT_ENCODING, errors="replace")

    try:
        text = await asyncio.to_thread(read)
    except ValueError:
        return ToolResult(tool="fs_read", ok=False, error=f"'{path}' looks like a binary file")
    except OSError as exc:
        return ToolResult(tool="fs_read", ok=False, error=f"read failed: {exc}")

    truncated = len(text) > max_chars
    snippet = text[:max_chars]
    return ToolResult(
        tool="fs_read",
        ok=True,
        output=snippet + ("\n...[truncated]" if truncated else ""),
        data={
            "path": _display(context, target),
            "size": size,
            "truncated": truncated,
            "content": snippet,
        },
    )


async def _write_file(
    context: ToolContext,
    path: str,
    content: str,
    append: bool = False,
) -> ToolResult:
    target = resolve_workspace_path(context, path)
    if target.exists() and target.is_dir():
        return ToolResult(tool="fs_write", ok=False, error=f"'{path}' is a directory")
    encoded = content.encode(_TEXT_ENCODING)
    existing = 0
    if append and target.exists():
        try:
            existing = target.stat().st_size
        except OSError:
            existing = 0
    total = existing + len(encoded)
    if total > _max_bytes(context):
        return ToolResult(
            tool="fs_write",
            ok=False,
            error=(
                f"file would be {total} bytes (existing {existing} + new {len(encoded)}), "
                f"over the {_max_bytes(context)}-byte limit"
            ),
        )

    def write() -> int:
        _ensure_parent(target)
        mode = "ab" if append else "wb"
        with target.open(mode) as handle:
            handle.write(encoded)
        return target.stat().st_size

    try:
        size = await asyncio.to_thread(write)
    except OSError as exc:
        return ToolResult(tool="fs_write", ok=False, error=f"write failed: {exc}")

    verb = "appended to" if append else "wrote"
    return ToolResult(
        tool="fs_write",
        ok=True,
        output=f"{verb} {_display(context, target)} ({size} bytes)",
        data={"path": _display(context, target), "size": size, "append": append},
    )


async def _mkdir(context: ToolContext, path: str) -> ToolResult:
    target = resolve_workspace_path(context, path)

    def create() -> None:
        target.mkdir(parents=True, exist_ok=True)

    try:
        await asyncio.to_thread(create)
    except OSError as exc:
        return ToolResult(tool="fs_mkdir", ok=False, error=f"mkdir failed: {exc}")
    return ToolResult(
        tool="fs_mkdir", ok=True, output=f"directory ready: {_display(context, target)}"
    )


async def _move(context: ToolContext, source: str, destination: str, overwrite: bool = False) -> ToolResult:
    src = resolve_workspace_entry(context, source)
    dst = resolve_workspace_entry(context, destination)
    if _same_entry(src, dst):
        return ToolResult(
            tool="fs_move",
            ok=False,
            error="source and destination are the same path",
        )
    if _is_reparse_point(src):
        return ToolResult(
            tool="fs_move",
            ok=False,
            error=f"refusing to move a link or junction: {source}",
        )
    if not src.exists():
        return ToolResult(tool="fs_move", ok=False, error=f"source not found: {source}")
    if _is_reparse_point(dst):
        return ToolResult(
            tool="fs_move", ok=False, error=f"destination is a link or junction: {destination}"
        )
    if dst.exists():
        if not overwrite:
            return ToolResult(
                tool="fs_move", ok=False, error=f"destination exists: {destination} (set overwrite=true)"
            )
        if src.is_dir() != dst.is_dir():
            return ToolResult(
                tool="fs_move",
                ok=False,
                error=f"cannot replace a {'directory' if dst.is_dir() else 'file'} with a "
                f"{'directory' if src.is_dir() else 'file'}",
            )
        if dst.is_dir():
            return ToolResult(
                tool="fs_move",
                ok=False,
                error="overwriting an existing directory with fs_move is not supported; "
                "move into it or delete it first",
            )

    def move() -> None:
        _ensure_parent(dst)
        if overwrite and dst.exists():
            dst.unlink()
        shutil.move(str(src), str(dst))

    try:
        await asyncio.to_thread(move)
    except OSError as exc:
        return ToolResult(tool="fs_move", ok=False, error=f"move failed: {exc}")
    return ToolResult(
        tool="fs_move",
        ok=True,
        output=f"moved {_display(context, src)} -> {_display(context, dst)}",
        data={"source": _display(context, src), "destination": _display(context, dst)},
    )


async def _copy(context: ToolContext, source: str, destination: str, overwrite: bool = False) -> ToolResult:
    src = resolve_workspace_entry(context, source)
    dst = resolve_workspace_entry(context, destination)
    if _same_entry(src, dst):
        return ToolResult(
            tool="fs_copy",
            ok=False,
            error="source and destination are the same path",
        )
    if _is_reparse_point(src):
        return ToolResult(
            tool="fs_copy",
            ok=False,
            error=f"refusing to copy a link or junction: {source}",
        )
    if not src.exists():
        return ToolResult(tool="fs_copy", ok=False, error=f"source not found: {source}")
    if src.is_dir() and _is_inside(dst.parent.resolve() / dst.name, src.resolve()):
        return ToolResult(
            tool="fs_copy",
            ok=False,
            error="cannot copy a directory into itself",
        )
    if _is_reparse_point(dst):
        return ToolResult(
            tool="fs_copy", ok=False, error=f"destination is a link or junction: {destination}"
        )
    if dst.exists():
        if not overwrite:
            return ToolResult(
                tool="fs_copy", ok=False, error=f"destination exists: {destination} (set overwrite=true)"
            )
        if src.is_dir() != dst.is_dir():
            return ToolResult(
                tool="fs_copy",
                ok=False,
                error=f"cannot replace a {'directory' if dst.is_dir() else 'file'} with a "
                f"{'directory' if src.is_dir() else 'file'}",
            )
        if dst.is_dir():
            # copytree's dirs_exist_ok merge would follow a junction already
            # present in the destination and write through it.
            linked = _find_reparse_point(dst)
            if linked is not None:
                return ToolResult(
                    tool="fs_copy",
                    ok=False,
                    error=(
                        "destination contains a link or junction "
                        f"({_display(context, linked)}); refusing to merge"
                    ),
                )

    def copy() -> None:
        _ensure_parent(dst)
        if src.is_dir():
            shutil.copytree(
                src,
                dst,
                dirs_exist_ok=overwrite,
                symlinks=True,
                ignore=_ignore_reparse,
            )
        else:
            shutil.copy2(src, dst)

    try:
        await asyncio.to_thread(copy)
    except OSError as exc:
        return ToolResult(tool="fs_copy", ok=False, error=f"copy failed: {exc}")
    return ToolResult(
        tool="fs_copy",
        ok=True,
        output=f"copied {_display(context, src)} -> {_display(context, dst)}",
    )


async def _delete(context: ToolContext, path: str) -> ToolResult:
    workspace = Path(context.settings.workspace_dir).resolve()
    raw_path = Path(path).expanduser()
    if not raw_path.is_absolute():
        raw_path = workspace / raw_path
    # Compare lexically: ``resolve()`` would follow a junction inside the
    # workspace that points back at the root, and deleting that junction only
    # removes the link.  ``abspath`` normalizes ``.``/``..`` without following
    # reparse points.
    if os.path.normcase(os.path.abspath(raw_path)) == os.path.normcase(
        os.path.abspath(workspace)
    ):
        return ToolResult(tool="fs_delete", ok=False, error="refusing to delete the workspace root")

    target = resolve_workspace_entry(context, path)
    if target == workspace:
        return ToolResult(tool="fs_delete", ok=False, error="refusing to delete the workspace root")
    # A junction whose target has been removed reports exists() == False and
    # is_symlink() == False, but the link itself is still present.
    if not (target.exists() or target.is_symlink() or _is_reparse_point(target)):
        return ToolResult(tool="fs_delete", ok=False, error=f"path not found: {path}")

    def remove() -> None:
        if _is_reparse_point(target):
            # Delete the link itself, never the directory/file it points at.
            # Directory links/junctions must be removed with rmdir; unlink
            # raises on a directory symlink on Windows.
            if target.is_dir():
                target.rmdir()
            else:
                try:
                    target.unlink()
                except OSError:
                    # A dangling directory symlink is not reported as a dir by
                    # is_dir(); rmdir is what Windows accepts for it.
                    target.rmdir()
        elif target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

    try:
        await asyncio.to_thread(remove)
    except OSError as exc:
        return ToolResult(tool="fs_delete", ok=False, error=f"delete failed: {exc}")
    return ToolResult(tool="fs_delete", ok=True, output=f"deleted {_display(context, target)}")


async def _search(context: ToolContext, pattern: str = "*", path: str = ".", limit: int = 50) -> ToolResult:
    root = resolve_workspace_path(context, path)
    if not root.exists():
        return ToolResult(tool="fs_search", ok=False, error=f"path not found: {path}")
    limit = max(1, min(200, int(limit)))

    def find() -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for current, dirs, files in os.walk(root):
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".") and not _is_reparse_point(Path(current) / d)
            ]
            for filename in files:
                if filename.startswith("."):
                    continue
                full = Path(current) / filename
                if _is_reparse_point(full):
                    continue
                if fnmatch.fnmatch(filename.lower(), pattern.lower()):
                    try:
                        size = full.stat().st_size
                    except OSError:
                        size = 0
                    matches.append({"path": _display(context, full), "size": size})
                    if len(matches) >= limit:
                        return matches
        return matches

    matches = await asyncio.to_thread(find)
    listing = "\n".join(f"{item['path']}  ({item['size']} B)" for item in matches) or "(no matches)"
    return ToolResult(
        tool="fs_search",
        ok=True,
        output=f"matches for {pattern!r}:\n{listing}",
        data={"pattern": pattern, "matches": matches, "count": len(matches)},
    )


async def _info(context: ToolContext, path: str) -> ToolResult:
    target = resolve_workspace_path(context, path)
    if not target.exists():
        return ToolResult(
            tool="fs_info", ok=True, output=f"{_display(context, target)} does not exist",
            data={"path": _display(context, target), "exists": False},
        )
    stat = target.stat()
    info = {
        "path": _display(context, target),
        "exists": True,
        "type": "dir" if target.is_dir() else "file",
        "size": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }
    return ToolResult(
        tool="fs_info",
        ok=True,
        output=f"{info['type']} {info['path']} ({info['size']} B, modified {info['modified']})",
        data=info,
    )


def build_filesystem_tools() -> list[Tool]:
    """Factory returning every filesystem tool."""
    return [
        FunctionTool(
            name="fs_list",
            description="List files and directories inside the workspace.",
            category="filesystem",
            parameters=[
                ToolParameter(name="path", type="string", description="Relative directory", default="."),
                ToolParameter(name="recursive", type="boolean", description="List recursively", default=False),
            ],
            handler=_list_dir,
        ),
        FunctionTool(
            name="fs_read",
            description="Read a UTF-8 text file from the workspace.",
            category="filesystem",
            parameters=[
                ToolParameter(name="path", type="string", description="Relative file path", required=True),
                ToolParameter(name="max_chars", type="integer", description="Character limit", default=8000),
            ],
            handler=_read_file,
        ),
        FunctionTool(
            name="fs_write",
            description="Create or overwrite (or append to) a text file in the workspace.",
            category="filesystem",
            danger="caution",
            parameters=[
                ToolParameter(name="path", type="string", description="Relative file path", required=True),
                ToolParameter(name="content", type="string", description="Text to write", required=True),
                ToolParameter(name="append", type="boolean", description="Append instead of overwrite", default=False),
            ],
            handler=_write_file,
        ),
        FunctionTool(
            name="fs_mkdir",
            description="Create a directory (including parents) inside the workspace.",
            category="filesystem",
            danger="caution",
            parameters=[
                ToolParameter(name="path", type="string", description="Relative directory path", required=True)
            ],
            handler=_mkdir,
        ),
        FunctionTool(
            name="fs_move",
            description="Move or rename a file/directory inside the workspace.",
            category="filesystem",
            danger="caution",
            parameters=[
                ToolParameter(name="source", type="string", description="Current path", required=True),
                ToolParameter(name="destination", type="string", description="New path", required=True),
                ToolParameter(
                    name="overwrite",
                    type="boolean",
                    description="Replace an existing destination file (directories are not replaced)",
                    default=False,
                ),
            ],
            handler=_move,
        ),
        FunctionTool(
            name="fs_copy",
            description="Copy a file/directory inside the workspace.",
            category="filesystem",
            danger="caution",
            parameters=[
                ToolParameter(name="source", type="string", description="Source path", required=True),
                ToolParameter(name="destination", type="string", description="Target path", required=True),
                ToolParameter(
                    name="overwrite",
                    type="boolean",
                    description="Overwrite files / merge into an existing directory",
                    default=False,
                ),
            ],
            handler=_copy,
        ),
        FunctionTool(
            name="fs_delete",
            description=(
                "Permanently delete a file or directory inside the workspace. "
                "Links are removed without deleting their targets."
            ),
            category="filesystem",
            danger="dangerous",
            parameters=[
                ToolParameter(name="path", type="string", description="Path to delete", required=True)
            ],
            handler=_delete,
        ),
        FunctionTool(
            name="fs_search",
            description="Find files by name pattern (glob) inside the workspace.",
            category="filesystem",
            parameters=[
                ToolParameter(name="pattern", type="string", description="Glob, e.g. *.txt", default="*"),
                ToolParameter(name="path", type="string", description="Directory to search", default="."),
                ToolParameter(name="limit", type="integer", description="Max results", default=50),
            ],
            handler=_search,
        ),
        FunctionTool(
            name="fs_info",
            description="Report whether a path exists, plus size and modification time.",
            category="filesystem",
            parameters=[
                ToolParameter(name="path", type="string", description="Relative path", required=True)
            ],
            handler=_info,
        ),
    ]
