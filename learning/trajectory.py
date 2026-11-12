"""Trajectory utilities: compress runs into reusable knowledge.

A trajectory is the ordered list of tool calls with their outcomes.  From it we
derive three artefacts: a compact replayable recipe (for skills), a human
readable summary (for episodes) and lessons (for semantic memory).
"""

from __future__ import annotations

import json
from typing import Any, Iterable

_MAX_ARG_CHARS = 300
_MAX_OBS_CHARS = 400


def _step_field(step: Any, name: str, default: Any = None) -> Any:
    if isinstance(step, dict):
        return step.get(name, default)
    return getattr(step, name, default)


def build_trajectory(steps: Iterable[Any], *, keep_failures: bool = True) -> list[dict[str, Any]]:
    """Convert step records into JSON-serialisable dicts."""
    trajectory: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        ok = _step_field(step, "ok", True)
        if not ok and not keep_failures:
            continue
        arguments = _step_field(step, "arguments", {}) or {}
        try:
            rendered = json.dumps(arguments, ensure_ascii=False, default=str)
        except TypeError:
            rendered = str(arguments)
        if len(rendered) > _MAX_ARG_CHARS:
            rendered = rendered[:_MAX_ARG_CHARS] + "..."
        trajectory.append(
            {
                "index": index + 1,
                "tool": _step_field(step, "tool", ""),
                "arguments": arguments,
                "arguments_preview": rendered,
                "observation": str(_step_field(step, "observation", ""))[:_MAX_OBS_CHARS],
                "ok": bool(ok),
                "duration_ms": int(_step_field(step, "duration_ms", 0) or 0),
                "planned_step": _step_field(step, "planned_step", ""),
            }
        )
    return trajectory


def compact_recipe(steps: Iterable[Any], *, max_steps: int = 12) -> list[dict[str, Any]]:
    """Tool calls only - the shape a skill stores for replay."""
    recipe: list[dict[str, Any]] = []
    for step in steps:
        if not _step_field(step, "ok", True):
            continue
        tool = _step_field(step, "tool", "")
        if not tool:
            continue
        arguments = _step_field(step, "arguments", {}) or {}
        recipe.append({"tool": tool, "arguments": arguments})
        if len(recipe) >= max_steps:
            break
    return recipe


def summarize(
    goal: str,
    steps: Iterable[Any],
    *,
    success: bool,
    duration_ms: int = 0,
    tokens: int = 0,
) -> str:
    steps = list(steps)
    tools: list[str] = []
    failures = 0
    for step in steps:
        tool = _step_field(step, "tool", "")
        if tool:
            tools.append(tool)
        if not _step_field(step, "ok", True):
            failures += 1
    head = "SUCCESS" if success else "FAILURE"
    unique: list[str] = []
    for tool in tools:
        if tool not in unique:
            unique.append(tool)
    parts = [
        f"{head}: {goal}",
        f"{len(steps)} steps, {failures} failure(s), {duration_ms} ms, {tokens} tokens",
    ]
    if unique:
        parts.append("tools: " + " -> ".join(unique[:10]))
    return "; ".join(parts)


def extract_lessons(
    goal: str,
    steps: Iterable[Any],
    *,
    success: bool,
    error: str | None = None,
) -> list[str]:
    """Rule-based lessons; cheap, deterministic and surprisingly useful."""
    steps = list(steps)
    lessons: list[str] = []
    failures = [
        (index + 1, step)
        for index, step in enumerate(steps)
        if not _step_field(step, "ok", True)
    ]
    if success:
        tools = [str(_step_field(step, "tool", "")) for step in steps if _step_field(step, "tool", "")]
        if tools:
            lessons.append(
                "Successful workflow for this kind of goal: " + " -> ".join(tools[:8])
            )
        if len(steps) <= 4:
            lessons.append("This goal can usually be completed in 4 steps or fewer.")
    else:
        if failures:
            index, step = failures[0]
            tool = _step_field(step, "tool", "unknown")
            observation = str(_step_field(step, "observation", ""))[:160]
            lessons.append(
                f"Step {index} using '{tool}' failed before the goal was reached ({observation})."
            )
        if error:
            lessons.append(f"Blocking error: {error[:200]}")
    retries: dict[str, int] = {}
    for step in steps:
        if not _step_field(step, "ok", True):
            tool = str(_step_field(step, "tool", ""))
            retries[tool] = retries.get(tool, 0) + 1
    for tool, count in retries.items():
        if count >= 2 and tool:
            lessons.append(
                f"'{tool}' failed {count} times in one task; observe the screen before retrying."
            )
    quoted = [part for part in _quoted(goal)]
    if quoted and success:
        lessons.append(
            "Quoted text in a goal is literal content: type exactly '" + quoted[0] + "'."
        )
    return lessons[:6]


def trajectory_text(steps: Iterable[Any], limit: int = 12) -> str:
    lines: list[str] = []
    for index, step in enumerate(steps):
        if index >= limit:
            break
        tool = _step_field(step, "tool", "")
        args = _step_field(step, "arguments", {}) or {}
        preview = json.dumps(args, ensure_ascii=False, default=str)[:120]
        status = "ok" if _step_field(step, "ok", True) else "FAILED"
        lines.append(f"{index + 1}. {tool}({preview}) -> {status}")
    return "\n".join(lines)


def _quoted(text: str) -> list[str]:
    import re

    return re.findall(r"[\"'“”`]([^\"'“”`]{2,80})[\"'“”`]", text or "")
