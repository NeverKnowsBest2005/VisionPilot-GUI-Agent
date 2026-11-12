"""Heuristic planner: it must produce grounded, executable steps offline."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from agent import Plan, PlanStep, Planner, ReasoningEngine
from perception import GUIState, UIElement

DESKTOP_ELEMENTS = [
    UIElement(kind="icon", bbox=(56, 90, 96, 96), label="File Explorer", confidence=1.0, source="synthetic"),
    UIElement(kind="icon", bbox=(56, 222, 96, 96), label="Notes", confidence=1.0, source="synthetic"),
    UIElement(kind="icon", bbox=(56, 354, 96, 96), label="Settings", confidence=1.0, source="synthetic"),
]


def make_state(
    window: str = "VisionPilot Demo Desktop",
    text: str = "",
    elements: list[UIElement] | None = None,
) -> GUIState:
    return GUIState(
        timestamp=datetime.now(timezone.utc),
        width=1440,
        height=900,
        active_window=window,
        text=text,
        elements=list(DESKTOP_ELEMENTS if elements is None else elements),
        windows=[],
        description="demo desktop",
        backend="synthetic",
    )


def build_planner(settings, registry):
    return Planner(settings, ReasoningEngine(settings), registry)


def test_open_app_plan_is_a_single_grounded_click(settings, registry):
    planner = build_planner(settings, registry)
    plan = asyncio.run(planner.create_plan("打开记事本", make_state()))
    assert plan.steps
    first = plan.steps[0]
    assert first.tool == "mouse_double_click"
    assert first.arguments["element"] == "Notes"
    assert first.expect == {"window": "Notes"}
    assert plan.source == "heuristic"


def test_type_and_save_plan_has_three_steps(settings, registry):
    planner = build_planner(settings, registry)
    plan = asyncio.run(planner.create_plan("打开记事本，输入 hello world，然后保存", make_state()))
    tools = [step.tool for step in plan.steps]
    assert tools == ["mouse_double_click", "keyboard_type", "keyboard_press"]
    assert plan.steps[1].arguments["text"] == "hello world"
    assert plan.steps[1].expect == {"text": "hello world"}
    assert plan.steps[2].arguments["keys"] == ["ctrl", "s"]


def test_english_goal_with_quotes(settings, registry):
    planner = build_planner(settings, registry)
    plan = asyncio.run(
        planner.create_plan('open the Notes app and type "hello from VisionPilot"', make_state())
    )
    typed = [step for step in plan.steps if step.tool == "keyboard_type"]
    assert typed and typed[0].arguments["text"] == "hello from VisionPilot"


def test_plan_respects_already_focused_app(settings, registry):
    planner = build_planner(settings, registry)
    plan = asyncio.run(
        planner.create_plan("打开记事本", make_state(window="Notes - Untitled"))
    )
    assert all(step.tool != "mouse_double_click" for step in plan.steps)


def test_unknown_app_uses_direct_launch(settings, registry):
    planner = build_planner(settings, registry)
    plan = asyncio.run(planner.create_plan("打开微信", make_state()))
    assert plan.steps
    first = plan.steps[0]
    assert first.tool == "system_launch_app"
    assert first.arguments["name"] == "微信"
    assert first.expect == {"window": "微信"}


def test_validate_plan_flags_unknown_tool(settings, registry):
    planner = build_planner(settings, registry)
    plan = Plan(goal="test", steps=[PlanStep(description="x", tool="does_not_exist")])
    warnings = planner.validate_plan(plan)
    assert any("does_not_exist" in warning for warning in warnings)


def test_replan_recovers_from_a_grounding_failure(settings, registry):
    planner = build_planner(settings, registry)
    plan = asyncio.run(planner.create_plan("打开记事本", make_state()))
    revised = asyncio.run(
        planner.replan("打开记事本", make_state(), plan, "element not found: Notes")
    )
    assert revised is not None and revised.steps
