"""HTTP/WebSocket API surface, driven through FastAPI's TestClient."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.routes import TaskManager, create_app


def test_health_and_index(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        payload = health.json()
        assert payload["execution_mode"] == "simulate"
        assert payload["dry_run"] is True
        assert payload["tools_total"] >= 29
        assert payload["tools_available"] >= 20

        index = client.get("/")
        assert index.status_code == 200
        assert "VisionPilot" in index.text


def test_create_task_and_poll_to_completion(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post("/api/tasks", json={"goal": "打开记事本，输入 api test，然后保存"})
        assert created.status_code in (200, 201)
        task_id = created.json()["task_id"]

        detail = None
        for _ in range(100):
            response = client.get(f"/api/tasks/{task_id}")
            assert response.status_code == 200
            detail = response.json()
            if detail["status"] in {"completed", "failed", "aborted", "cancelled"}:
                break
            time.sleep(0.05)

        assert detail is not None and detail["status"] == "completed"
        assert detail["success"] is True
        assert [step["tool"] for step in detail["step_records"]] == [
            "mouse_double_click",
            "keyboard_type",
            "keyboard_press",
        ]
        assert any(event["type"] == "task_finished" for event in detail["events"])

        listing = client.get("/api/tasks").json()
        assert any(item["task_id"] == task_id for item in listing)

        stats = client.get("/api/stats")
        assert stats.status_code == 200
        assert stats.json()["metrics"]["episodes"] >= 1


def test_tools_skills_and_demo_endpoints(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        tools = client.get("/api/tools").json()
        names = {tool["name"] for tool in tools["tools"]}
        assert {"mouse_click", "keyboard_type", "fs_read"} <= names
        assert tools["count"] == len(names)

        command = next(tool for tool in tools["tools"] if tool["name"] == "system_run_command")
        assert command["available"] is False
        assert command["unavailable_reason"]

        skills = client.get("/api/skills")
        assert skills.status_code == 200

        demo = client.post("/api/demo", json={"scenario": "settings"})
        assert demo.status_code == 200
        assert demo.json()["scenario"] == "settings"

        missing = client.post("/api/demo", json={"scenario": "nope"})
        assert missing.status_code == 400


def test_cors_does_not_trust_arbitrary_origins(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        }
        blocked = client.options("/api/tasks", headers={"Origin": "https://evil.example", **headers})
        assert "access-control-allow-origin" not in blocked.headers

        allowed = client.options(
            "/api/tasks",
            headers={"Origin": "http://localhost:5173", **headers},
        )
        assert allowed.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_websocket_streams_task_events(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/events") as websocket:
            hello = websocket.receive_json()
            assert hello["type"] == "hello"
            assert hello["execution_mode"] == "simulate"


def test_websocket_rejects_foreign_origins(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/ws/events", headers={"origin": "https://evil.example"}
            ) as websocket:
                websocket.receive_json()

        with client.websocket_connect(
            "/ws/events", headers={"origin": "http://localhost:5173"}
        ) as websocket:
            assert websocket.receive_json()["type"] == "hello"


def test_websocket_allows_the_loopback_serving_origin(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        with client.websocket_connect(
            "ws://127.0.0.1:8000/ws/events",
            headers={"origin": "http://127.0.0.1:8000"},
        ) as websocket:
            assert websocket.receive_json()["type"] == "hello"

        # A non-loopback origin is still rejected even when it is the only
        # origin the test client could claim to be same-site with.
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "ws://127.0.0.1:8000/ws/events",
                headers={"origin": "https://evil.example"},
            ) as websocket:
                websocket.receive_json()


def test_websocket_honours_default_port_equivalence(settings):
    configured = settings.model_copy(
        update={
            "cors_origins": [
                "http://localhost:80",
                "https://app.example.com:443",
            ]
        }
    )
    app = create_app(configured)
    with TestClient(app) as client:
        for origin in ("http://localhost", "https://app.example.com"):
            with client.websocket_connect(
                "/ws/events", headers={"origin": origin}
            ) as websocket:
                assert websocket.receive_json()["type"] == "hello"


def test_wildcard_cors_configuration_allows_websocket_origins(settings):
    wildcard = settings.model_copy(update={"cors_origins": ["*"]})
    app = create_app(wildcard)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws/events", headers={"origin": "https://any.example"}
        ) as websocket:
            assert websocket.receive_json()["type"] == "hello"


class _FakeAgent:
    seen: dict[str, object] = {}

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def run(self, goal: str, **kwargs):
        type(self).seen = {"goal": goal, **kwargs}
        return SimpleNamespace(status="completed", success=True, steps=[], error=None)

    async def aclose(self) -> None:
        pass


def test_confirmed_flag_is_passed_to_the_agent(settings, monkeypatch):
    monkeypatch.setattr("api.routes.VisionPilotAgent", _FakeAgent)

    async def scenario():
        manager = TaskManager(settings)
        try:
            record = manager.submit("delete a file", confirmed=True)
            await manager._jobs[record.task_id]
            return record
        finally:
            await manager.shutdown()

    record = asyncio.run(scenario())

    assert _FakeAgent.seen["confirmed"] is True
    assert _FakeAgent.seen["goal"] == "delete a file"
    assert record.status == "completed"


def test_confirmation_defaults_to_false(settings, monkeypatch):
    monkeypatch.setattr("api.routes.VisionPilotAgent", _FakeAgent)

    async def scenario():
        manager = TaskManager(settings)
        try:
            record = manager.submit("open Notes")
            await manager._jobs[record.task_id]
            return record
        finally:
            await manager.shutdown()

    asyncio.run(scenario())

    assert _FakeAgent.seen["confirmed"] is False
