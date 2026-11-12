"""Central configuration for VisionPilot.

Configuration is loaded from environment variables (optionally via a ``.env``
file) so that no secret ever has to live inside the repository.  Every option
can be overridden programmatically, which is what the test-suite and the
command line entry point do.

Environment variable naming
---------------------------
Every field of :class:`Settings` maps to ``VISIONPILOT_<FIELD_NAME>`` in upper
case, e.g. ``llm_model`` -> ``VISIONPILOT_LLM_MODEL``.  A few well known
third-party variables are accepted as fallbacks (``DEEPSEEK_API_KEY``,
``OPENAI_API_KEY``, ``OPENAI_BASE_URL``) so that an existing shell setup keeps
working.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ExecutionMode = Literal["simulate", "live"]
PlannerMode = Literal["auto", "llm", "heuristic"]
ScreenshotBackend = Literal["auto", "mss", "pillow", "pyautogui", "synthetic", "none"]
OCRBackend = Literal["auto", "tesseract", "none"]

_TRUE = {"1", "true", "yes", "on", "y", "enabled"}
_FALSE = {"0", "false", "no", "off", "n", "disabled", ""}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"cannot interpret {value!r} as a boolean")


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


class Settings(BaseModel):
    """Immutable-ish runtime configuration for the whole application."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    # ------------------------------------------------------------------ meta
    app_name: str = "VisionPilot"
    version: str = "0.1.0"
    environment: str = "development"

    # --------------------------------------------------------------- runtime
    execution_mode: ExecutionMode = "simulate"
    """``simulate`` never touches the real mouse/keyboard; ``live`` does."""

    simulate_desktop: bool = True
    """When simulating, expose a synthetic desktop so the full loop is runnable
    on a machine without a GUI session (CI, tests, demos)."""

    # ------------------------------------------------------------------- llm
    llm_enabled: bool = True
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    llm_vision_model: str = ""
    llm_temperature: float = 0.2
    llm_max_tokens: int = 1600
    llm_timeout_sec: float = 60.0
    llm_max_retries: int = 2
    llm_max_calls_per_task: int = 40
    planner_mode: PlannerMode = "auto"
    vision_enabled: bool = True

    # ------------------------------------------------------------ perception
    screenshot_backend: ScreenshotBackend = "auto"
    screenshot_dir: Path = PROJECT_ROOT / "logs" / "screenshots"
    keep_screenshots: int = 80
    vision_max_width: int = 1024
    ocr_backend: OCRBackend = "auto"
    ocr_lang: str = "eng+chi_sim"
    ocr_min_confidence: float = 40.0
    detect_ui_with_opencv: bool = True

    # -------------------------------------------------------------- execution
    workspace_dir: Path = PROJECT_ROOT / "workspace"
    allow_shell: bool = False
    shell_timeout_sec: float = 15.0
    shell_allowlist: list[str] = Field(
        default_factory=lambda: [
            "echo",
            "dir",
            "type",
            "where",
            "whoami",
            "hostname",
            "date",
            "findstr",
        ]
    )
    confirm_dangerous: bool = True
    max_file_bytes: int = 2_000_000
    mouse_move_duration: float = 0.25
    browser_headless: bool = False

    # ---------------------------------------------------------------- agent
    max_steps: int = 20
    max_task_seconds: float = 300.0
    step_settle_seconds: float = 0.6
    max_retries_per_step: int = 2
    max_replans: int = 2
    max_consecutive_no_change: int = 3
    enable_verification: bool = True
    enable_learning: bool = True
    enable_llm_judge: bool = True
    adaptive_step_timeout_sec: float = 30.0

    # ----------------------------------------------------------------- memory
    db_path: Path = PROJECT_ROOT / "logs" / "visionpilot.db"
    episodic_recall_limit: int = 3
    semantic_recall_limit: int = 5
    skill_recall_limit: int = 3

    # ------------------------------------------------------------------- api
    api_host: str = "127.0.0.1"
    api_port: int = 8765
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    # ------------------------------------------------------------------ logs
    log_dir: Path = PROJECT_ROOT / "logs"
    log_level: str = "INFO"
    event_log_path: Path | None = None

    # ------------------------------------------------------------ validators
    @field_validator("execution_mode", "planner_mode", "screenshot_backend", "ocr_backend")
    @classmethod
    def _lowercase_choice(cls, value: Any) -> str:
        return str(value).strip().lower()

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, value: str) -> str:
        value = value.strip().upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        return value if value in allowed else "INFO"

    @field_validator("shell_allowlist", "cors_origins", mode="before")
    @classmethod
    def _coerce_list(cls, value: Any) -> list[str]:
        return _as_list(value)

    @field_validator(
        "llm_timeout_sec",
        "shell_timeout_sec",
        "step_settle_seconds",
        "max_task_seconds",
        "adaptive_step_timeout_sec",
        "mouse_move_duration",
    )
    @classmethod
    def _positive_float(cls, value: float) -> float:
        value = float(value)
        if value < 0:
            raise ValueError("must be non-negative")
        return value

    @field_validator(
        "max_steps",
        "max_retries_per_step",
        "max_replans",
        "max_consecutive_no_change",
        "keep_screenshots",
        "vision_max_width",
        "episodic_recall_limit",
        "semantic_recall_limit",
        "skill_recall_limit",
        "llm_max_calls_per_task",
        "max_file_bytes",
        "llm_max_retries",
    )
    @classmethod
    def _positive_int(cls, value: int) -> int:
        value = int(value)
        if value <= 0:
            raise ValueError("must be a positive integer")
        return value

    @field_validator("api_port")
    @classmethod
    def _port(cls, value: int) -> int:
        value = int(value)
        if not 1 <= value <= 65535:
            raise ValueError("port must be within 1..65535")
        return value

    def model_post_init(self, __context: Any) -> None:  # noqa: D105
        self.log_dir = Path(self.log_dir).expanduser().resolve()
        self.screenshot_dir = Path(self.screenshot_dir).expanduser().resolve()
        self.db_path = Path(self.db_path).expanduser().resolve()
        self.workspace_dir = Path(self.workspace_dir).expanduser().resolve()
        if self.event_log_path is not None:
            self.event_log_path = Path(self.event_log_path).expanduser().resolve()

    # -------------------------------------------------------------- helpers
    @property
    def dry_run(self) -> bool:
        """Alias that reads naturally in the tool implementations."""
        return self.execution_mode != "live"

    @property
    def uses_synthetic_desktop(self) -> bool:
        return self.dry_run and self.simulate_desktop

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_enabled and self.llm_api_key and self.llm_model)

    @property
    def vision_ready(self) -> bool:
        return bool(self.llm_ready and self.vision_enabled and self.llm_vision_model)

    @property
    def effective_event_log_path(self) -> Path:
        return self.event_log_path or (self.log_dir / "events.jsonl")

    def ensure_directories(self) -> None:
        for directory in (
            self.log_dir,
            self.screenshot_dir,
            self.workspace_dir,
            self.db_path.parent,
        ):
            Path(directory).mkdir(parents=True, exist_ok=True)

    def safe_dict(self) -> dict[str, Any]:
        """Configuration representation that is safe to send to a browser."""
        data = self.model_dump(mode="json")
        data["llm_api_key"] = "***" if self.llm_api_key else ""
        return data

    # ---------------------------------------------------------------- loader
    @classmethod
    def load(
        cls,
        env_file: str | Path | None = ".env",
        overrides: dict[str, Any] | None = None,
        environ: dict[str, str] | None = None,
    ) -> "Settings":
        """Build settings from ``.env``, the process environment and overrides.

        Precedence (highest last): defaults < ``.env`` < process environment
        < explicit ``overrides``.
        """
        if env_file:
            path = Path(env_file)
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            if path.exists():
                load_dotenv(path, override=False)

        env = dict(os.environ if environ is None else environ)
        values = _collect_env_values(cls, env)
        if overrides:
            values.update({key: value for key, value in overrides.items() if value is not None})
        settings = cls(**values)
        settings.ensure_directories()
        return settings


def _collect_env_values(model: type[BaseModel], env: dict[str, str]) -> dict[str, Any]:
    """Map ``VISIONPILOT_*`` environment variables onto model fields."""
    known = set(model.model_fields)
    values: dict[str, Any] = {}
    for key, raw in env.items():
        if not key.startswith("VISIONPILOT_"):
            continue
        field = key[len("VISIONPILOT_") :].lower()
        if field not in known:
            continue
        values[field] = raw

    # Friendly fallbacks for common provider variables.
    alias_groups: Iterable[tuple[str, tuple[str, ...]]] = (
        ("llm_api_key", ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "VISIONPILOT_API_KEY")),
        ("llm_base_url", ("DEEPSEEK_BASE_URL", "OPENAI_BASE_URL")),
        ("llm_model", ("VISIONPILOT_MODEL",)),
    )
    for field, aliases in alias_groups:
        if values.get(field):
            continue
        for alias in aliases:
            if env.get(alias):
                values[field] = env[alias]
                break
    return values


def load_settings(
    env_file: str | Path | None = ".env",
    overrides: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
    **extra: Any,
) -> Settings:
    """Convenience wrapper used by ``main.py``, ``tests`` and the API package.

    ``overrides`` may be passed as a mapping or as direct keyword arguments.
    """

    merged: dict[str, Any] = dict(overrides or {})
    merged.update(extra)
    return Settings.load(env_file=env_file, overrides=merged, environ=environ)
