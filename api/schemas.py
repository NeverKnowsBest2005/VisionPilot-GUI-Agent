"""Pydantic models describing the public HTTP/WebSocket API of VisionPilot."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"
    app: str
    version: str
    execution_mode: str
    dry_run: bool
    synthetic_desktop: bool
    llm_ready: bool
    vision_ready: bool
    tools_total: int
    tools_available: int
    tasks_total: int = 0
    episodes: int = 0
    database: str
    uptime_seconds: float = 0.0


class TaskCreateRequest(BaseModel):
    goal: str = Field(min_length=2, max_length=2000, description="自然语言任务目标")
    max_steps: int | None = Field(default=None, ge=1, le=100, description="覆盖默认步数上限")
    confirmed: bool = Field(
        default=False,
        description="显式确认允许该任务执行危险操作（删除文件、运行命令等）",
    )


class TaskSummary(BaseModel):
    task_id: str
    goal: str
    status: str
    success: bool | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    steps: int = 0
    tokens: int = 0
    summary: str | None = None
    error: str | None = None


class TaskDetail(TaskSummary):
    step_records: list[dict[str, Any]] = Field(default_factory=list)
    evaluation: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    episode_id: str | None = None


class TaskCreatedResponse(BaseModel):
    task_id: str
    status: str
    goal: str
    created_at: str
    detail_url: str


class ToolInfo(BaseModel):
    name: str
    description: str
    danger: str
    category: str
    available: bool
    unavailable_reason: str | None = None
    parameters: list[dict[str, Any]] = Field(default_factory=list)


class ToolsResponse(BaseModel):
    count: int
    available: int
    tools: list[ToolInfo]


class SkillInfo(BaseModel):
    id: str
    name: str
    description: str = ""
    trigger: str = ""
    uses: int = 0
    success_count: int = 0
    fail_count: int = 0
    success_rate: float = 0.0
    version: int = 1
    active: bool = True
    steps: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


class SkillsResponse(BaseModel):
    count: int
    skills: list[SkillInfo]


class KnowledgeInfo(BaseModel):
    id: int
    kind: str
    key: str
    content: str
    score: float = 0.0
    hits: int = 0
    created_at: str | None = None
    updated_at: str | None = None


class KnowledgeResponse(BaseModel):
    count: int
    items: list[KnowledgeInfo]


class EpisodeInfo(BaseModel):
    id: int
    goal: str
    status: str
    success: bool
    steps_count: int
    duration_ms: int
    tokens: int
    summary: str = ""
    lessons: list[str] = Field(default_factory=list)
    created_at: str | None = None


class EpisodesResponse(BaseModel):
    count: int
    episodes: list[EpisodeInfo]


class StatsResponse(BaseModel):
    metrics: dict[str, Any]
    suggestions: list[dict[str, Any]]
    skills: dict[str, Any]
    tools: dict[str, Any]


class DemoRequest(BaseModel):
    scenario: str = Field(default="notes", description="notes | explorer | settings | english")


class DemoResponse(BaseModel):
    scenario: str
    goal: str
    task_id: str


class ErrorResponse(BaseModel):
    detail: str


class CancelResponse(BaseModel):
    task_id: str
    cancelled: bool
    status: str
