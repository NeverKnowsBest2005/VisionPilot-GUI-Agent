"""Shared pytest fixtures: every test gets an isolated simulated workspace."""

from __future__ import annotations

from pathlib import Path

import pytest

from config import Settings, load_settings
from executor import build_default_registry


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
