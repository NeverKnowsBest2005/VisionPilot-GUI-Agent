"""End-to-end agent loop on the synthetic desktop (no LLM, no real input)."""

from __future__ import annotations

import asyncio

import pytest

from agent import VisionPilotAgent


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
