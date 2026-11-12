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


def resolve_workspace_path(context: ToolContext, raw: str | Path) -> Path:
    """Resolve ``raw`` inside the workspace or raise :class:`ToolPermissionError`."""
    workspace = Path(context.settings.workspace_dir).expanduser().resolve()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ToolPermissionError(
            f"path '{raw}' escapes the workspace sandbox ({workspace})"
        ) from exc
    return resolved


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

    def scan() -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        iterator = target.rglob("*") if recursive else target.iterdir()
        for entry in iterator:
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
    if len(encoded) > _max_bytes(context):
        return ToolResult(
            tool="fs_write",
            ok=False,
            error=f"content too large ({len(encoded)} bytes > {_max_bytes(context)})",
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
    src = resolve_workspace_path(context, source)
    dst = resolve_workspace_path(context, destination)
    if not src.exists():
        return ToolResult(tool="fs_move", ok=False, error=f"source not found: {source}")
    if dst.exists() and not overwrite:
        return ToolResult(
            tool="fs_move", ok=False, error=f"destination exists: {destination} (set overwrite=true)"
        )

    def move() -> None:
        _ensure_parent(dst)
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
    src = resolve_workspace_path(context, source)
    dst = resolve_workspace_path(context, destination)
    if not src.exists():
        return ToolResult(tool="fs_copy", ok=False, error=f"source not found: {source}")
    if dst.exists() and not overwrite:
        return ToolResult(
            tool="fs_copy", ok=False, error=f"destination exists: {destination} (set overwrite=true)"
        )

    def copy() -> None:
        _ensure_parent(dst)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=overwrite)
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
    target = resolve_workspace_path(context, path)
    workspace = Path(context.settings.workspace_dir).resolve()
    if target == workspace:
        return ToolResult(tool="fs_delete", ok=False, error="refusing to delete the workspace root")
    if not target.exists():
        return ToolResult(tool="fs_delete", ok=False, error=f"path not found: {path}")

    def remove() -> None:
        if target.is_dir():
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
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for filename in files:
                if filename.startswith("."):
                    continue
                if fnmatch.fnmatch(filename.lower(), pattern.lower()):
                    full = Path(current) / filename
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
                ToolParameter(name="overwrite", type="boolean", description="Replace destination", default=False),
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
                ToolParameter(name="overwrite", type="boolean", description="Replace destination", default=False),
            ],
            handler=_copy,
        ),
        FunctionTool(
            name="fs_delete",
            description="Permanently delete a file or directory inside the workspace.",
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
