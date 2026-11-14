"""Shared pytest fixtures: every test gets an isolated simulated workspace."""

from __future__ import annotations

from pathlib import Path

import pytest

from config import PROJECT_ROOT, Settings, load_settings
from executor import build_default_registry


def _os_temp_is_writable() -> bool:
    """Whether ``python.exe`` may create files in the system temp directory."""
    import os

    for value in (os.environ.get("TEMP"), os.environ.get("TMP"), os.environ.get("TMPDIR")):
        if not value:
            continue
        try:
            probe = Path(value) / f".vp-pytest-probe-{os.getpid()}"
            probe.touch()
            probe.unlink()
            return True
        except OSError:
            continue
    return False


def _temp_dir_mode_is_listable() -> bool:
    """Whether a ``0o700`` directory can still be listed after creation.

    pytest creates its base and per-test temp dirs with ``mode=0o700``.  On
    Windows shells that run python at a low integrity level (sandboxed or
    restricted tokens) such a directory loses the inherited ACEs and cannot be
    read back, so every ``tmp_path`` fixture errors out with
    ``PermissionError [WinError 5]``.  0o750/0o770 directories are unaffected,
    which is what :func:`_relax_temp_dir_mode` relies on.
    """
    import os
    import tempfile
    import uuid

    probe = Path(tempfile.gettempdir()) / f".vp-mode-probe-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        os.mkdir(probe, 0o700)
    except OSError:
        return True  # cannot even probe; leave pytest alone
    try:
        list(probe.iterdir())
        return True
    except OSError:
        return False
    finally:
        try:
            probe.rmdir()
        except OSError:
            pass


def _relax_temp_dir_mode() -> None:
    """Create pytest temp dirs as 0o777 when 0o700 is unreadable on this host."""
    import os
    import pathlib

    os_mkdir = os.mkdir
    path_mkdir = pathlib.Path.mkdir

    def mkdir(path, mode: int = 0o777, *args, **kwargs):
        return os_mkdir(path, 0o777 if mode == 0o700 else mode, *args, **kwargs)

    def path_mkdir_impl(self, mode: int = 0o777, parents: bool = False, exist_ok: bool = False):
        return path_mkdir(
            self, 0o777 if mode == 0o700 else mode, parents=parents, exist_ok=exist_ok
        )

    os.mkdir = mkdir
    pathlib.Path.mkdir = path_mkdir_impl


def pytest_configure(config: pytest.Config) -> None:
    """Keep pytest's temp dirs out of the project root on locked-down hosts.

    When ``python.exe`` cannot write to ``%TEMP%`` (seen on Windows machines
    with restrictive AV/EDR policy) ``tempfile`` silently falls back to the
    current directory, so pytest's default ``basetemp`` and every ``tmp_path``
    land in the repository root.  Redirect them into the git-ignored
    ``.scratch`` folder instead of littering the working tree.
    """
    if not _temp_dir_mode_is_listable():
        _relax_temp_dir_mode()
    if getattr(config.option, "basetemp", None):
        return
    if _os_temp_is_writable():
        return
    config.option.basetemp = str(PROJECT_ROOT / ".scratch" / "pytest")


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    resolved = load_settings(
        env_file=None,
        overrides={
            "db_path": tmp_path / "test.db",
            "screenshot_dir": tmp_path / "screenshots",
            "workspace_dir": tmp_path / "workspace",
            "log_dir": tmp_path / "logs",
            "execution_mode": "simulate",
            "simulate_desktop": True,
            "llm_enabled": False,
            "enable_llm_judge": False,
            "step_settle_seconds": 0.0,
        },
        environ={},
    )
    resolved.ensure_directories()
    return resolved


@pytest.fixture()
def registry(settings: Settings):
    return build_default_registry(settings)
