"""Workspace sandbox: junctions must not leak reads, writes or deletes."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from executor import ToolContext
from executor import file_system as fs
from executor.base import ToolPermissionError

pytestmark = pytest.mark.skipif(os.name != "nt", reason="junction tests are Windows-specific")


@pytest.fixture()
def outside(tmp_path: Path) -> Path:
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    return folder


def make_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"cannot create a junction here: {result.stderr or result.stdout}")


@pytest.fixture()
def junction(settings, outside: Path) -> Path:
    link = Path(settings.workspace_dir) / "jump"
    make_junction(link, outside)
    return link


def context(settings) -> ToolContext:
    return ToolContext(settings=settings)


def test_paths_through_a_junction_are_rejected(settings, junction):
    with pytest.raises(ToolPermissionError):
        fs.resolve_workspace_path(context(settings), "jump/secret.txt")


def test_recursive_listing_does_not_descend_into_junctions(settings, junction):
    (Path(settings.workspace_dir) / "notes.txt").write_text("hello", encoding="utf-8")

    result = asyncio.run(fs._list_dir(context(settings), ".", recursive=True))

    assert result.ok is True
    names = {entry["name"] for entry in result.data["entries"]}
    assert names == {"notes.txt"}


def test_search_does_not_see_files_outside_the_workspace(settings, junction):
    result = asyncio.run(fs._search(context(settings), "*.txt", ".", 50))

    assert result.ok is True
    assert result.data["matches"] == []


def test_copying_a_directory_skips_junctions(settings, junction, outside):
    source = Path(settings.workspace_dir) / "data"
    source.mkdir()
    (source / "inside.txt").write_text("ok", encoding="utf-8")
    make_junction(source / "nested", outside)

    result = asyncio.run(fs._copy(context(settings), "data", "copy"))

    assert result.ok is True
    copied = Path(settings.workspace_dir) / "copy"
    assert (copied / "inside.txt").read_text(encoding="utf-8") == "ok"
    assert not (copied / "nested").exists()
    assert not (copied / "nested" / "secret.txt").exists()


def test_copying_or_moving_a_junction_itself_is_refused(settings, junction):
    copy_result = asyncio.run(fs._copy(context(settings), "jump", "copied"))
    move_result = asyncio.run(fs._move(context(settings), "jump", "moved"))

    assert copy_result.ok is False and "link or junction" in copy_result.error
    assert move_result.ok is False and "link or junction" in move_result.error


def test_deleting_a_junction_removes_only_the_link(settings, junction, outside):
    result = asyncio.run(fs._delete(context(settings), "jump"))

    assert result.ok is True
    assert not junction.exists()
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "TOP SECRET"


def test_parent_traversal_cannot_reach_or_delete_the_workspace_root(settings):
    workspace = Path(settings.workspace_dir)
    (workspace / "keep.txt").write_text("keep", encoding="utf-8")

    for raw in ("..", "a/../..", "sub/.."):
        with pytest.raises(ToolPermissionError):
            fs.resolve_workspace_entry(context(settings), raw)
    with pytest.raises(ToolPermissionError):
        asyncio.run(fs._delete(context(settings), ".."))
    with pytest.raises(ToolPermissionError):
        asyncio.run(fs._copy(context(settings), "..", "backup"))

    assert (workspace / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (workspace / "backup").exists()


def test_copy_into_a_destination_tree_with_a_junction_is_refused(settings, outside):
    workspace = Path(settings.workspace_dir)
    source = workspace / "src"
    (source / "evil").mkdir(parents=True)
    (source / "evil" / "payload.txt").write_text("payload", encoding="utf-8")
    destination = workspace / "dst"
    destination.mkdir()
    make_junction(destination / "evil", outside)

    result = asyncio.run(fs._copy(context(settings), "src", "dst", overwrite=True))

    assert result.ok is False
    assert "link or junction" in result.error
    assert not (outside / "payload.txt").exists()


def test_move_or_copy_onto_itself_is_refused(settings):
    workspace = Path(settings.workspace_dir)
    (workspace / "keep.txt").write_text("data", encoding="utf-8")

    move = asyncio.run(fs._move(context(settings), "keep.txt", "keep.txt", overwrite=True))
    copy = asyncio.run(fs._copy(context(settings), "keep.txt", "keep.txt", overwrite=True))

    assert move.ok is False and "same path" in move.error
    assert copy.ok is False and "same path" in copy.error
    assert (workspace / "keep.txt").read_text(encoding="utf-8") == "data"


def test_copying_a_directory_into_itself_is_refused(settings):
    workspace = Path(settings.workspace_dir)
    folder = workspace / "dir"
    folder.mkdir()
    (folder / "file.txt").write_text("data", encoding="utf-8")

    result = asyncio.run(fs._copy(context(settings), "dir", "dir/inner"))

    assert result.ok is False
    assert "into itself" in result.error


def _read_alt_stream(path: Path, stream: str) -> bytes | None:
    try:
        with open(f"{path}:{stream}", "rb") as handle:
            return handle.read()
    except OSError:
        return None


def test_alternate_data_stream_on_a_junction_is_refused(settings, junction, outside):
    workspace = Path(settings.workspace_dir)
    (workspace / "payload.txt").write_text("PAYLOAD", encoding="utf-8")

    for destination in ("jump:leak", "jump:leak2"):
        with pytest.raises(ToolPermissionError):
            asyncio.run(fs._copy(context(settings), "payload.txt", destination))
        with pytest.raises(ToolPermissionError):
            asyncio.run(fs._move(context(settings), "payload.txt", destination))

    assert _read_alt_stream(outside, "leak") is None
    assert _read_alt_stream(outside, "leak2") is None
    assert (workspace / "payload.txt").read_text(encoding="utf-8") == "PAYLOAD"


@pytest.mark.parametrize("destination", ["keep.txt ", "keep.txt."])
def test_move_or_copy_to_a_win32_alias_of_the_source_is_refused(settings, destination):
    workspace = Path(settings.workspace_dir)
    (workspace / "keep.txt").write_text("data", encoding="utf-8")

    with pytest.raises(ToolPermissionError):
        asyncio.run(fs._move(context(settings), "keep.txt", destination, overwrite=True))
    with pytest.raises(ToolPermissionError):
        asyncio.run(fs._copy(context(settings), "keep.txt", destination, overwrite=True))

    assert (workspace / "keep.txt").read_text(encoding="utf-8") == "data"


@pytest.mark.parametrize("path", ["NUL", "nul", "NUL ", "CON.txt", "sub/NUL", "COM1"])
def test_reserved_device_names_are_refused(settings, path):
    workspace = Path(settings.workspace_dir)
    (workspace / "payload.txt").write_text("payload", encoding="utf-8")

    with pytest.raises(ToolPermissionError):
        asyncio.run(fs._write_file(context(settings), path, "LOST"))
    with pytest.raises(ToolPermissionError):
        asyncio.run(fs._copy(context(settings), "payload.txt", path, overwrite=True))
    with pytest.raises(ToolPermissionError):
        asyncio.run(fs._mkdir(context(settings), path))
    with pytest.raises(ToolPermissionError):
        asyncio.run(fs._info(context(settings), path))

    assert not (workspace / "sub").exists()


def test_workspace_prefix_components_are_not_scanned_as_device_names(settings, tmp_path):
    unusual = tmp_path / "aux" / "workspace"
    configured = settings.model_copy(update={"workspace_dir": str(unusual)})
    ctx = ToolContext(settings=configured)

    resolved = fs.resolve_workspace_path(ctx, "notes.txt")

    assert resolved == unusual / "notes.txt"
    # A device name supplied *by the agent* is still rejected.
    with pytest.raises(ToolPermissionError):
        fs.resolve_workspace_path(ctx, "aux/notes.txt")


@pytest.mark.parametrize("name", [" NUL", "NUL .txt", "clock$.txt"])
def test_names_that_only_resemble_devices_are_allowed(settings, name):
    resolved = fs.resolve_workspace_path(context(settings), name)

    assert resolved.name == name


def test_deleting_a_junction_to_the_workspace_root_removes_only_the_link(settings):
    workspace = Path(settings.workspace_dir)
    link = workspace / "rootlink"
    make_junction(link, workspace)

    result = asyncio.run(fs._delete(context(settings), "rootlink"))

    assert result.ok is True, result.error
    assert not os.path.lexists(link)
    assert workspace.exists()


def test_deleting_a_dangling_junction_removes_only_the_link(settings, outside):
    workspace = Path(settings.workspace_dir)
    link = workspace / "dangling"
    make_junction(link, outside)
    shutil.rmtree(outside)

    result = asyncio.run(fs._delete(context(settings), "dangling"))

    assert result.ok is True, result.error
    assert not os.path.lexists(link)
