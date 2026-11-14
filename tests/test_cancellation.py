"""Cancelling a running task: the agent stops at its next checkpoint.

``POST /api/tasks/{id}/cancel`` used to only flip a flag that the loop checked
between steps, so a single 30 s tool call kept the task "running" long after the
user asked it to stop.  These tests pin the behaviour of the interruptible path.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent import VisionPilotAgent
from api.routes import TaskManager, create_app
from executor import ToolResult


class _BlockingAgent:
    """Stands in for VisionPilotAgent: runs until ``cancel`` is called."""

    instances: list["_BlockingAgent"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.stop = asyncio.Event()
        self.cancel_calls = 0
        type(self).instances.append(self)

    async def run(self, goal: str, **kwargs):
        await self.stop.wait()
        return SimpleNamespace(
            status="aborted",
            success=False,
            steps=[],
            error="task cancelled by user",
            duration_ms=1,
        )

    def cancel(self) -> None:
        self.cancel_calls += 1
        self.stop.set()

    async def aclose(self) -> None:
        pass


@pytest.fixture
def blocking_agent(monkeypatch):
    _BlockingAgent.instances = []
    monkeypatch.setattr("api.routes.VisionPilotAgent", _BlockingAgent)
    return _BlockingAgent


def test_cancel_interrupts_a_long_tool_call(settings, monkeypatch):
    """A 30 s tool call must not survive a cancel request."""

    async def slow_execute(self, name, arguments, context, **kwargs):
        await asyncio.sleep(30)
        return ToolResult(tool=name, ok=True)

    monkeypatch.setattr("executor.base.ToolRegistry.execute", slow_execute)
    events: list[dict] = []

    async def scenario():
        agent = VisionPilotAgent(
            settings=settings, event_callback=lambda event: events.append(event)
        )
        try:
            job = asyncio.create_task(agent.run("打开记事本，输入 hello，然后保存"))
            await asyncio.sleep(0.2)  # let planning finish and the tool start
            assert not job.done(), "the slow tool should still be running"
            started = time.monotonic()
            agent.cancel()
            result = await asyncio.wait_for(job, timeout=5)
            return result, time.monotonic() - started
        finally:
            await agent.aclose()

    result, elapsed = asyncio.run(scenario())

    assert result.status == "aborted"
    assert result.success is False
    assert result.error == "task cancelled by user"
    assert elapsed < 2.0, f"cancel took {elapsed:.2f}s"
    assert events[-1]["type"] == "task_finished"
    assert events[-1]["status"] == "aborted"


def test_manager_cancel_marks_the_task_cancelling(settings, blocking_agent):
    async def scenario():
        manager = TaskManager(settings)
        try:
            record = manager.submit("do something slow")
            job = manager._jobs[record.task_id]
            for _ in range(100):
                if record.status == "running":
                    break
                await asyncio.sleep(0.01)
            assert record.status == "running"

            assert await manager.cancel(record.task_id) is True
            # Immediate feedback for the UI, before the agent reaches its
            # checkpoint and reports the final status.
            assert record.status == "cancelling"
            assert blocking_agent.instances[-1].cancel_calls == 1

            await asyncio.wait_for(job, timeout=5)
            return record
        finally:
            await manager.shutdown()

    record = asyncio.run(scenario())

    assert record.status == "aborted"
    assert record.finished_at is not None
    assert record.result.error == "task cancelled by user"


def test_manager_cancel_is_idempotent_for_finished_tasks(settings, blocking_agent):
    async def scenario():
        manager = TaskManager(settings)
        try:
            record = manager.submit("finish quickly")
            for _ in range(100):
                if record.task_id in manager._agents:
                    break
                await asyncio.sleep(0.01)
            manager._agents[record.task_id].stop.set()  # let the fake agent return
            await asyncio.wait_for(manager._jobs[record.task_id], timeout=5)
            assert record.status == "aborted"

            assert await manager.cancel(record.task_id) is False
            with pytest.raises(KeyError):
                await manager.cancel("does-not-exist")
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_cancel_endpoint_reports_stopping_then_aborted(settings, blocking_agent):
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post("/api/tasks", json={"goal": "do something slow"})
        assert created.status_code == 201
        task_id = created.json()["task_id"]

        for _ in range(200):
            if client.get(f"/api/tasks/{task_id}").json()["status"] == "running":
                break
            time.sleep(0.02)

        response = client.post(f"/api/tasks/{task_id}/cancel")
        assert response.status_code == 200
        body = response.json()
        assert body["task_id"] == task_id
        assert body["cancelled"] is True
        assert body["status"] == "cancelling"

        final = None
        for _ in range(300):
            final = client.get(f"/api/tasks/{task_id}").json()
            if final["status"] in {"aborted", "cancelled", "completed", "failed"}:
                break
            time.sleep(0.02)

        assert final is not None and final["status"] == "aborted"
        assert final["error"] == "task cancelled by user"

        # A second click (or a click on a finished task) must not raise.
        again = client.post(f"/api/tasks/{task_id}/cancel")
        assert again.status_code == 200
        assert again.json()["cancelled"] is False

        missing = client.post("/api/tasks/not-a-task/cancel")
        assert missing.status_code == 404
