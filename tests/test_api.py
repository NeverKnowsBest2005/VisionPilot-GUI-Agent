"""HTTP/WebSocket API surface, driven through FastAPI's TestClient."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from api.routes import create_app


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

        skills = client.get("/api/skills")
        assert skills.status_code == 200

        demo = client.post("/api/demo", json={"scenario": "settings"})
        assert demo.status_code == 200
        assert demo.json()["scenario"] == "settings"

        missing = client.post("/api/demo", json={"scenario": "nope"})
        assert missing.status_code == 400


def test_websocket_streams_task_events(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/events") as websocket:
            hello = websocket.receive_json()
            assert hello["type"] == "hello"
            assert hello["execution_mode"] == "simulate"
