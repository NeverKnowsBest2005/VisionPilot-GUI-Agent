"""Settings precedence: defaults < .env < environment < explicit overrides."""

from __future__ import annotations

from config import Settings, load_settings


def test_overrides_win_over_environment(tmp_path):
    settings = load_settings(
        env_file=None,
        overrides={"db_path": tmp_path / "custom.db", "max_steps": 7},
        environ={"VISIONPILOT_MAX_STEPS": "99", "VISIONPILOT_LLM_MODEL": "env-model"},
    )
    assert settings.db_path == tmp_path / "custom.db"
    assert settings.max_steps == 7
    assert settings.llm_model == "env-model"


def test_keyword_overrides_are_supported(tmp_path):
    settings = load_settings(env_file=None, environ={}, db_path=tmp_path / "k.db", execution_mode="live")
    assert settings.db_path == tmp_path / "k.db"
    assert settings.execution_mode == "live"
    assert settings.dry_run is False


def test_api_key_aliases_and_masking(monkeypatch):
    settings = Settings.load(
        env_file=None,
        overrides={"llm_api_key": None},
        environ={"DEEPSEEK_API_KEY": "sk-secret", "OPENAI_BASE_URL": "https://example.test/v1"},
    )
    assert settings.llm_api_key == "sk-secret"
    assert settings.llm_base_url == "https://example.test/v1"
    assert "sk-secret" not in str(settings.safe_dict())


def test_directories_are_created(tmp_path):
    settings = load_settings(
        env_file=None,
        environ={},
        log_dir=tmp_path / "logs",
        screenshot_dir=tmp_path / "shots",
        workspace_dir=tmp_path / "ws",
        db_path=tmp_path / "logs" / "db.sqlite",
    )
    assert settings.log_dir.is_dir()
    assert settings.screenshot_dir.is_dir()
    assert settings.workspace_dir.is_dir()
    assert settings.db_path.parent.is_dir()
