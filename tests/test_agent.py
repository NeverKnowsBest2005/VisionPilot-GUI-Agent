"""End-to-end agent loop on the synthetic desktop (no LLM, no real input)."""

from __future__ import annotations

import asyncio

import pytest

from agent import VisionPilotAgent
from agent.state import Plan, PlanStep
from executor.base import ToolResult


def test_agent_completes_notes_workflow(settings):
    async def scenario():
        agent = VisionPilotAgent(settings=settings)
        try:
            result = await agent.run("打开记事本，输入 hello world，然后保存")
        finally:
            await agent.aclose()
        return result

    result = asyncio.run(scenario())
    assert result.success is True
    assert result.status == "completed"
    assert [step["tool"] for step in result.steps] == [
        "mouse_double_click",
        "keyboard_type",
        "keyboard_press",
    ]
    assert result.evaluation is not None and result.evaluation["success"] is True
    assert result.duration_ms >= 0
    assert result.episode_id


def test_agent_opens_settings_and_closes_cleanly(settings):
    async def scenario():
        agent = VisionPilotAgent(settings=settings)
        try:
            result = await agent.run("打开设置")
        finally:
            await agent.aclose()
        return result

    result = asyncio.run(scenario())
    assert result.success is True
    assert result.plan is not None and result.plan["steps"]
    assert "Settings" in (result.summary or "") or result.evaluation["success"] is True


def test_replanned_steps_replace_the_reported_plan(settings):
    """A replan must be visible in ``result.plan``, not only inside the loop."""
    replans = []

    async def scenario():
        agent = VisionPilotAgent(settings=settings)

        async def fake_replan(goal, state, plan, reason, history=None):
            replans.append(reason)
            return Plan(
                goal=goal,
                steps=[PlanStep(description="revised step", tool="system_info", arguments={})],
                source="heuristic",
            )

        real_execute = agent.registry.execute

        async def fake_execute(tool, arguments, context=None, *, timeout=30.0):
            if tool == "mouse_double_click":
                return ToolResult(tool=tool, ok=False, error="element not found: Notes")
            return await real_execute(tool, arguments, context, timeout=timeout)

        agent.planner.replan = fake_replan  # type: ignore[method-assign]
        agent.registry.execute = fake_execute  # type: ignore[method-assign]
        try:
            return await agent.run("打开记事本")
        finally:
            await agent.aclose()

    result = asyncio.run(scenario())

    assert replans, "the failing double click should have triggered a replan"
    assert result.plan is not None
    assert result.plan["revision"] == 1
    assert [step["description"] for step in result.plan["steps"]] == ["revised step"]
    # The failed attempts stay in the history, the revised step runs last.
    assert result.steps[-1]["tool"] == "system_info"


def test_agent_emits_events_for_the_dashboard(settings):
    recorded = []

    async def scenario():
        agent = VisionPilotAgent(
            settings=settings,
            event_callback=lambda event: recorded.append(event),
        )
        try:
            await agent.run("打开记事本")
        finally:
            await agent.aclose()

    asyncio.run(scenario())
    types = [event["type"] for event in recorded]
    assert "task_started" in types
    assert "observation" in types
    assert "plan_created" in types
    assert "task_finished" in types
    assert recorded[-1]["type"] == "task_finished"


def test_empty_goal_is_rejected(settings):
    agent = VisionPilotAgent(settings=settings)
    try:
        with pytest.raises(ValueError):
            asyncio.run(agent.run("   "))
    finally:
        asyncio.run(agent.aclose())
