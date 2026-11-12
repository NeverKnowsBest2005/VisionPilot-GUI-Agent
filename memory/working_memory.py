"""Working memory - the agent's short term state for one task.

Working memory is intentionally in-process and ephemeral.  It holds the plan,
the step ledger, recent observations and free-form notes; the durable record
lives in SQLite via the episodic store.  Keeping it separate means the prompt
can always be built from a compact, current view instead of replaying the whole
history.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class StepRecord:
    """One executed action."""

    index: int
    tool: str
    arguments: dict[str, Any]
    ok: bool
    observation: str
    planned_step: str = ""
    duration_ms: int = 0
    verification: str | None = None
    timestamp: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "tool": self.tool,
            "arguments": self.arguments,
            "ok": self.ok,
            "observation": self.observation,
            "planned_step": self.planned_step,
            "duration_ms": self.duration_ms,
            "verification": self.verification,
            "timestamp": self.timestamp,
        }

    def compact(self) -> str:
        status = "ok" if self.ok else "fail"
        return f"#{self.index} {self.tool}({_short_args(self.arguments)}) -> {status}: {self.observation[:160]}"


@dataclass
class Observation:
    summary: str
    signature: str
    active_window: str = ""
    timestamp: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "signature": self.signature,
            "active_window": self.active_window,
            "timestamp": self.timestamp,
        }


def _short_args(arguments: dict[str, Any], limit: int = 120) -> str:
    parts = [f"{key}={value!r}" for key, value in arguments.items()]
    text = ", ".join(parts)
    return text if len(text) <= limit else text[: limit - 3] + "..."


class WorkingMemory:
    """Mutable state for the currently running task."""

    def __init__(
        self,
        task_id: str,
        goal: str,
        *,
        max_steps: int = 100,
        observation_window: int = 6,
    ) -> None:
        self.task_id = task_id
        self.goal = goal
        self.max_steps = max_steps
        self.created_at = _now()
        self.plan: list[dict[str, Any]] = []
        self.plan_revision = 0
        self.current_step = 0
        self.steps: list[StepRecord] = []
        self.observations: Deque[Observation] = deque(maxlen=max(2, observation_window))
        self.notes: list[str] = []
        self.tokens = 0
        self.llm_calls = 0
        self.attempts: dict[int, int] = {}
        self.replans = 0

    # ---------------------------------------------------------------- plan
    def set_plan(self, steps: list[dict[str, Any]], *, revision: int | None = None) -> None:
        self.plan = steps
        if revision is not None:
            self.plan_revision = revision
        self.current_step = 0
        self.attempts.clear()

    def current_plan_step(self) -> dict[str, Any] | None:
        if 0 <= self.current_step < len(self.plan):
            return self.plan[self.current_step]
        return None

    def advance(self) -> None:
        self.current_step += 1

    def plan_exhausted(self) -> bool:
        return self.current_step >= len(self.plan)

    def remaining_steps(self) -> int:
        return max(0, len(self.plan) - self.current_step)

    # --------------------------------------------------------------- steps
    def record_step(
        self,
        tool: str,
        arguments: dict[str, Any],
        ok: bool,
        observation: str,
        *,
        planned_step: str = "",
        duration_ms: int = 0,
        verification: str | None = None,
    ) -> StepRecord:
        record = StepRecord(
            index=len(self.steps) + 1,
            tool=tool,
            arguments=arguments,
            ok=ok,
            observation=observation,
            planned_step=planned_step,
            duration_ms=duration_ms,
            verification=verification,
        )
        self.steps.append(record)
        return record

    def note_attempt(self, step_index: int) -> int:
        self.attempts[step_index] = self.attempts.get(step_index, 0) + 1
        return self.attempts[step_index]

    def attempts_for(self, step_index: int) -> int:
        return self.attempts.get(step_index, 0)

    # -------------------------------------------------------- observations
    def observe(self, summary: str, signature: str, active_window: str = "") -> None:
        self.observations.append(
            Observation(summary=summary, signature=signature, active_window=active_window)
        )

    def last_signature(self) -> str | None:
        return self.observations[-1].signature if self.observations else None

    def unchanged_count(self) -> int:
        """How many consecutive observations carried the same signature."""
        if len(self.observations) < 2:
            return 0
        count = 0
        latest = self.observations[-1].signature
        for observation in reversed(list(self.observations)[:-1]):
            if observation.signature == latest:
                count += 1
            else:
                break
        return count

    def recent_observations(self, limit: int = 3) -> list[Observation]:
        return list(self.observations)[-limit:]

    # ---------------------------------------------------------------- notes
    def add_note(self, note: str) -> None:
        note = note.strip()
        if note:
            self.notes.append(note)
            del self.notes[:-20]

    def spend_tokens(self, tokens: int, calls: int = 1) -> None:
        if tokens > 0:
            self.tokens += int(tokens)
        self.llm_calls += int(calls)

    # --------------------------------------------------------------- render
    def recent_steps_text(self, limit: int = 8) -> str:
        recent = self.steps[-limit:]
        return "\n".join(step.compact() for step in recent) or "(no steps yet)"

    def summary(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "plan_steps": len(self.plan),
            "plan_revision": self.plan_revision,
            "current_step": self.current_step,
            "executed_steps": len(self.steps),
            "successful_steps": sum(1 for step in self.steps if step.ok),
            "tokens": self.tokens,
            "llm_calls": self.llm_calls,
            "replans": self.replans,
            "last_observation": self.observations[-1].as_dict() if self.observations else None,
            "unchanged_observations": self.unchanged_count(),
            "notes": self.notes[-5:],
            "created_at": self.created_at,
        }

    def to_prompt(self, max_steps: int = 10) -> str:
        lines = [
            f"GOAL: {self.goal}",
            f"PLAN (revision {self.plan_revision}, step {self.current_step + 1}/{max(1, len(self.plan))}):",
        ]
        for index, step in enumerate(self.plan[:max_steps]):
            marker = "->" if index == self.current_step else "  "
            lines.append(f"{marker} {index + 1}. {step.get('description', step.get('tool', ''))}")
        if len(self.plan) > max_steps:
            lines.append(f"   ... {len(self.plan) - max_steps} more steps")
        lines.append("RECENT ACTIONS:")
        lines.append(self.recent_steps_text())
        observations = self.recent_observations(2)
        if observations:
            lines.append("RECENT OBSERVATIONS:")
            for observation in observations:
                lines.append(f"- [{observation.active_window}] {observation.summary[:300]}")
        if self.notes:
            lines.append("NOTES:")
            lines.extend(f"- {note}" for note in self.notes[-5:])
        return "\n".join(lines)
