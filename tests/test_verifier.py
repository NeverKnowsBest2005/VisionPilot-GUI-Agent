"""Verification: expectation checks, task evaluation and recovery decisions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from executor import ToolResult
from perception import GUIState
from verifier import Checker, RecoveryAction, RecoveryManager, TaskEvaluator, expectations_for


def make_state(text: str = "", window: str = "VisionPilot Demo Desktop") -> GUIState:
    return GUIState(
        timestamp=datetime.now(timezone.utc),
        width=1440,
        height=900,
        active_window=window,
        text=text,
        elements=[],
        windows=[],
        description=f"active window: {window}",
        backend="synthetic",
    )


def test_expectations_are_extracted_from_action_and_plan_step():
    assert expectations_for({"expect": {"text": "hello"}}) == {"text": "hello"}
    step = {"verify": {"window": "Notes"}}
    assert expectations_for({}, step) == {"window": "Notes"}
    assert expectations_for({"arguments": {}}, None) is None


def test_checker_success_when_expected_text_appears(settings):
    checker = Checker(settings)
    before = make_state("")
    after = make_state("hello world", window="Notes - notes.txt")
    result = asyncio.run(
        checker.check(
            before=before,
            after=after,
            action={"tool": "keyboard_type", "arguments": {"text": "hello"}},
            expectation={"text": "hello"},
        )
    )
    assert result.success is True
    assert result.confidence >= 0.8


def test_checker_failure_when_expected_window_missing(settings):
    checker = Checker(settings)
    result = asyncio.run(
        checker.check(
            before=make_state(""),
            after=make_state("nothing", window="Desktop"),
            action={"tool": "mouse_double_click", "arguments": {"element": "Notes"}},
            expectation={"window": "Notes"},
        )
    )
    assert result.status == "partial"
    assert result.success is None
    assert "Notes" in " ".join(result.reasons)


def test_checker_reports_tool_errors(settings):
    checker = Checker(settings)
    failed = ToolResult(tool="mouse_click", ok=False, error="element not found")
    result = asyncio.run(
        checker.check(
            before=make_state(""),
            after=make_state(""),
            action={"tool": "mouse_click", "arguments": {}},
            tool_result=failed,
        )
    )
    assert result.success is False
    assert any("element not found" in reason for reason in result.reasons)


def test_checker_partial_without_visible_change(settings):
    checker = Checker(settings)
    result = asyncio.run(
        checker.check(
            before=make_state("same"),
            after=make_state("same"),
            action={"tool": "mouse_move", "arguments": {}},
        )
    )
    assert result.status == "partial"
    assert result.success is None


def test_evaluator_accepts_intermediate_folder_from_steps(settings):
    evaluator = TaskEvaluator(settings)
    state = make_state("hello world", window="Notes - notes.txt")
    steps = [
        {"tool": "mouse_double_click", "arguments": {"element": "File Explorer"}, "observation": "Opened File Explorer", "ok": True},
        {"tool": "mouse_click", "arguments": {"element": "Documents"}, "observation": "Browsing Documents", "ok": True},
        {"tool": "mouse_click", "arguments": {"element": "notes.txt"}, "observation": "Opened notes.txt", "ok": True},
    ]
    result = asyncio.run(
        evaluator.evaluate("打开文件资源管理器，进入 Documents 文件夹，打开 notes.txt", state, steps=steps)
    )
    assert result.success is True
    assert all(criterion.matched for criterion in result.criteria)


def test_evaluator_fails_when_text_never_typed(settings):
    evaluator = TaskEvaluator(settings)
    state = make_state("", window="Notes - Untitled")
    result = asyncio.run(evaluator.evaluate("打开记事本，输入 hello world", state, steps=[]))
    assert result.success is False


def test_recovery_retries_transient_and_replans_on_denial(settings):
    recovery = RecoveryManager(settings)

    transient = recovery.decide(error="operation timed out", attempt=0, replans_used=0)
    assert transient.action == RecoveryAction.RETRY
    assert transient.delay_seconds >= 0

    denied = recovery.decide(tool_result=ToolResult(tool="fs_delete", ok=False, denied=True))
    assert denied.action == RecoveryAction.REPLAN

    stuck = recovery.decide(error="nothing happened", unchanged_count=settings.max_consecutive_no_change)
    assert stuck.action == RecoveryAction.REPLAN
