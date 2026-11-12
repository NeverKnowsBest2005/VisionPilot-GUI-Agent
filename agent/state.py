"""Agent state objects: phases, plans, steps and task results.

These are plain dataclasses on purpose - they are cheap to copy, easy to
serialise to JSON and do not pull in any framework.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Phase(str, Enum):
    IDLE = "idle"
    OBSERVING = "observing"
    PLANNING = "planning"
    ACTING = "acting"
    VERIFYING = "verifying"
    LEARNING = "learning"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"

    @property
    def terminal(self) -> bool:
        return self in {Phase.COMPLETED, Phase.FAILED, Phase.ABORTED}


@dataclass
class PlanStep:
    description: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    expect: Any = None
    status: str = "pending"  # pending | running | done | failed | skipped
    attempts: int = 0
    observation: str = ""
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "tool": self.tool,
            "arguments": self.arguments,
            "expect": self.expect,
            "status": self.status,
            "attempts": self.attempts,
            "observation": self.observation,
            "duration_ms": self.duration_ms,
        }

    def to_prompt(self, index: int | None = None) -> str:
        prefix = f"{index + 1}. " if index is not None else "- "
        expectation = f" expect={self.expect}" if self.expect else ""
        return f"{prefix}{self.description} [{self.tool}]{expectation} ({self.status})"


@dataclass
class Plan:
    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    reasoning: str = ""
    source: str = "heuristic"
    revision: int = 0

    def next_step(self) -> tuple[int, PlanStep] | None:
        for index, step in enumerate(self.steps):
            if step.status in {"pending", "failed"}:
                return index, step
        return None

    def mark(self, index: int, status: str, *, observation: str = "") -> None:
        if 0 <= index < len(self.steps):
            self.steps[index].status = status
            if observation:
                self.steps[index].observation = observation

    def pending_count(self) -> int:
        return sum(1 for step in self.steps if step.status in {"pending", "failed"})

    def completed(self) -> bool:
        return bool(self.steps) and all(step.status == "done" for step in self.steps)

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "reasoning": self.reasoning,
            "source": self.source,
            "revision": self.revision,
            "steps": [step.as_dict() for step in self.steps],
        }

    def to_prompt(self) -> str:
        lines = [f"GOAL: {self.goal}"]
        if self.reasoning:
            lines.append(f"PLAN RATIONALE: {self.reasoning}")
        lines.extend(step.to_prompt(index) for index, step in enumerate(self.steps))
        return "\n".join(lines)


@dataclass
class AgentState:
    task_id: str
    goal: str
    phase: Phase = Phase.IDLE
    plan: Plan | None = None
    current_state: Any = None
    step_index: int = 0
    error: str | None = None
    evaluation: Any = None
    replans: int = 0
    retries: int = 0
    recoveries: int = 0
    tokens: int = 0
    llm_calls: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def elapsed_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.started_at).total_seconds()

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "phase": self.phase.value,
            "step_index": self.step_index,
            "error": self.error,
            "replans": self.replans,
            "retries": self.retries,
            "recoveries": self.recoveries,
            "tokens": self.tokens,
            "llm_calls": self.llm_calls,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "plan": self.plan.as_dict() if self.plan else None,
            "evaluation": self.evaluation.as_dict() if hasattr(self.evaluation, "as_dict") else self.evaluation,
            "current_state": (
                self.current_state.as_dict()
                if hasattr(self.current_state, "as_dict")
                else self.current_state
            ),
        }


@dataclass
class TaskResult:
    task_id: str
    goal: str
    success: bool
    status: str
    summary: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0
    tokens: int = 0
    llm_calls: int = 0
    evaluation: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    error: str | None = None
    episode_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "success": self.success,
            "status": self.status,
            "summary": self.summary,
            "steps": self.steps,
            "duration_ms": self.duration_ms,
            "tokens": self.tokens,
            "llm_calls": self.llm_calls,
            "evaluation": self.evaluation,
            "plan": self.plan,
            "error": self.error,
            "episode_id": self.episode_id,
        }
