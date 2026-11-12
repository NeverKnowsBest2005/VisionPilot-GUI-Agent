"""Memory layers: working, episodic, semantic and learned skills."""

from __future__ import annotations

from learning import SkillManager
from memory import Database, EpisodicMemory, SemanticMemory, WorkingMemory


def test_working_memory_tracks_plan_steps_and_observations(settings):
    del settings
    memory = WorkingMemory("task-1", "打开记事本并输入 hello", max_steps=8)
    assert memory.max_steps == 8

    memory.set_plan([{"description": "打开记事本"}, {"description": "输入文本"}])
    assert memory.remaining_steps() == 2
    memory.observe("Desktop 空桌面", signature="sig-1", active_window="VisionPilot Demo Desktop")
    assert memory.unchanged_count() == 0
    memory.observe("还是空桌面", signature="sig-1", active_window="VisionPilot Demo Desktop")
    assert memory.unchanged_count() == 1

    memory.record_step(
        tool="keyboard_type",
        arguments={"text": "hello"},
        ok=True,
        observation="typed 5 characters",
        planned_step="输入文本",
    )
    memory.advance()
    assert memory.current_step == 1
    assert memory.summary()["executed_steps"] == 1
    assert "keyboard_type" in memory.recent_steps_text()
    assert memory.plan_exhausted() is False


def test_episodic_memory_persists_and_recalls_similar_episode(settings):
    database = Database.from_settings(settings)
    try:
        episodic = EpisodicMemory(database)
        task_id = episodic.start_task("打开记事本，输入 hello world", execution_mode="simulate")
        episodic.add_step(
            task_id,
            index=1,
            phase="acting",
            tool="keyboard_type",
            arguments={"text": "hello world"},
            observation="typed 11 characters",
            ok=True,
        )
        episodic.finish_task(task_id, status="completed", steps_count=1, duration_ms=321)

        episode = episodic.save_episode(
            goal="打开记事本，输入 hello world",
            status="completed",
            success=True,
            task_id=task_id,
            steps_count=1,
            duration_ms=321,
            tokens=12,
            trajectory=[{"index": 1, "tool": "keyboard_type", "ok": True}],
            summary="success in 1 step",
            lessons=["先聚焦输入框再输入文本"],
        )
        assert episode is not None
        assert episodic.stats()["episodes"] == 1
        similar = episodic.similar_episodes("打开记事本并输入文字")
        assert similar and similar[0].goal == "打开记事本，输入 hello world"
        assert similar[0].lessons == ["先聚焦输入框再输入文本"]
        assert episodic.get_task(task_id)["steps"][0]["tool"] == "keyboard_type"
    finally:
        database.close()


def test_semantic_memory_upsert_recall_and_lessons(settings):
    database = Database.from_settings(settings)
    try:
        semantic = SemanticMemory(database)
        semantic.remember("preference", "language", "用户偏好中文界面", score=0.6)
        semantic.remember("preference", "language", "用户偏好中文界面（确认）", score=0.9)
        item = semantic.get("preference", "language")
        assert item is not None and "确认" in item.content
        assert item.score == 0.9

        semantic.remember_lesson("保存前先确认笔记窗口处于激活状态", goal="打开记事本并保存")
        assert semantic.lessons(limit=5)
        recalled = semantic.recall("记事本 保存", limit=3)
        assert recalled
        assert semantic.stats()["total"] >= 2
    finally:
        database.close()


def test_skill_manager_learns_updates_and_matches(settings):
    database = Database.from_settings(settings)
    try:
        skills = SkillManager(database)
        steps = [
            {"tool": "mouse_double_click", "arguments": {"element": "Notes"}},
            {"tool": "keyboard_type", "arguments": {"text": "hello"}},
            {"tool": "keyboard_press", "arguments": {"keys": ["ctrl", "s"]}},
        ]
        assert skills.learn("打开记事本，输入 hello 并保存", steps, success=False) is None
        learned = skills.learn("打开记事本，输入 hello 并保存", steps, success=True)
        assert learned is not None and learned.uses == 1

        again = skills.learn("打开记事本，输入 hello 并保存", steps, success=True)
        assert again is not None and again.id == learned.id and again.uses == 2

        matches = skills.match("打开记事本，输入 hello 并保存")
        assert matches and matches[0].id == learned.id
        prompt = SkillManager.render_prompt(matches)
        assert "SKILL:" in prompt and "mouse_double_click" in prompt
        assert skills.stats()["active"] == 1
    finally:
        database.close()
