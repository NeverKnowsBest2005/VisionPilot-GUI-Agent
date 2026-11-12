"""Episodic memory - what happened, task by task.

Every run is persisted twice: incrementally as ``tasks``/``steps`` (so the web
console can follow a live run) and once at the end as a compact ``episode``
containing the full trajectory.  The episode is what the learning layer and the
recall mechanism work with, which keeps queries cheap.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from memory.database import Database, json_dumps, json_loads, now_iso
from memory.text import similarity, tokenize

logger = logging.getLogger("visionpilot.memory.episodic")


@dataclass
class EpisodeRecord:
    id: str
    goal: str
    status: str
    success: bool
    created_at: str
    task_id: str | None = None
    steps_count: int = 0
    duration_ms: int = 0
    tokens: int = 0
    trajectory: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    lessons: list[str] = field(default_factory=list)
    score: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "goal": self.goal,
            "status": self.status,
            "success": self.success,
            "steps_count": self.steps_count,
            "duration_ms": self.duration_ms,
            "tokens": self.tokens,
            "trajectory": self.trajectory,
            "summary": self.summary,
            "lessons": self.lessons,
            "created_at": self.created_at,
            "score": round(self.score, 4),
            "meta": self.meta,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "EpisodeRecord":
        return cls(
            id=row["id"],
            task_id=row.get("task_id"),
            goal=row["goal"],
            status=row.get("status", "unknown"),
            success=bool(row.get("success")),
            steps_count=int(row.get("steps_count") or 0),
            duration_ms=int(row.get("duration_ms") or 0),
            tokens=int(row.get("tokens") or 0),
            trajectory=json_loads(row.get("trajectory"), []) or [],
            summary=row.get("summary") or "",
            lessons=json_loads(row.get("lessons"), []) or [],
            created_at=row.get("created_at") or now_iso(),
        )


class EpisodicMemory:
    """Durable history of tasks, steps and episodes."""

    def __init__(self, database: Database) -> None:
        self.db = database

    # ----------------------------------------------------------------- tasks
    def start_task(
        self,
        goal: str,
        *,
        task_id: str | None = None,
        execution_mode: str = "simulate",
        meta: dict[str, Any] | None = None,
    ) -> str:
        task_id = task_id or uuid.uuid4().hex[:12]
        timestamp = now_iso()
        self.db.execute(
            """
            INSERT INTO tasks (id, goal, status, execution_mode, created_at, updated_at, meta)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, goal, "pending", execution_mode, timestamp, timestamp, json_dumps(meta or {})),
        )
        return task_id

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso()
        columns = ", ".join(f"{name} = ?" for name in fields)
        values = [
            json_dumps(value) if name == "meta" else value for name, value in fields.items()
        ]
        values.append(task_id)
        self.db.execute(f"UPDATE tasks SET {columns} WHERE id = ?", values)

    def finish_task(
        self,
        task_id: str,
        *,
        status: str,
        result: str = "",
        error: str | None = None,
        duration_ms: int = 0,
        tokens: int = 0,
        llm_calls: int = 0,
        steps_count: int = 0,
    ) -> None:
        self.update_task(
            task_id,
            status=status,
            result=result,
            error=error,
            duration_ms=int(duration_ms),
            tokens=int(tokens),
            llm_calls=int(llm_calls),
            steps_count=int(steps_count),
            finished_at=now_iso(),
        )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            return None
        row["meta"] = json_loads(row.get("meta"), {}) or {}
        row["steps"] = self.get_steps(task_id)
        return row

    def list_tasks(
        self, limit: int = 20, status: str | None = None
    ) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        if status:
            rows = self.db.query(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        for row in rows:
            row["meta"] = json_loads(row.get("meta"), {}) or {}
            row.pop("result", None)
        return rows

    # ----------------------------------------------------------------- steps
    def add_step(
        self,
        task_id: str,
        index: int,
        *,
        phase: str,
        tool: str,
        arguments: dict[str, Any] | None,
        observation: str,
        ok: bool,
        duration_ms: int = 0,
        verification: dict[str, Any] | None = None,
    ) -> int:
        cursor = self.db.execute(
            """
            INSERT INTO steps (task_id, idx, phase, tool, arguments, observation, ok,
                               duration_ms, verification, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                int(index),
                phase,
                tool,
                json_dumps(arguments or {}),
                observation[:4000],
                1 if ok else 0,
                int(duration_ms),
                json_dumps(verification or {}),
                now_iso(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def get_steps(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM steps WHERE task_id = ? ORDER BY idx ASC, id ASC", (task_id,)
        )
        for row in rows:
            row["arguments"] = json_loads(row.get("arguments"), {}) or {}
            row["verification"] = json_loads(row.get("verification"), {}) or {}
            row["ok"] = bool(row.get("ok"))
        return rows

    # -------------------------------------------------------------- episodes
    def save_episode(
        self,
        *,
        goal: str,
        status: str,
        success: bool,
        task_id: str | None = None,
        trajectory: list[dict[str, Any]] | None = None,
        summary: str = "",
        lessons: list[str] | None = None,
        steps_count: int = 0,
        duration_ms: int = 0,
        tokens: int = 0,
        meta: dict[str, Any] | None = None,
        episode_id: str | None = None,
    ) -> EpisodeRecord:
        episode_id = episode_id or uuid.uuid4().hex[:12]
        created_at = now_iso()
        self.db.execute(
            """
            INSERT INTO episodes (id, task_id, goal, status, success, steps_count, duration_ms,
                                  tokens, trajectory, summary, lessons, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                episode_id,
                task_id,
                goal,
                status,
                1 if success else 0,
                steps_count,
                duration_ms,
                tokens,
                json_dumps(trajectory or []),
                summary[:2000],
                json_dumps(lessons or []),
                created_at,
            ),
        )
        return EpisodeRecord(
            id=episode_id,
            task_id=task_id,
            goal=goal,
            status=status,
            success=success,
            steps_count=steps_count,
            duration_ms=duration_ms,
            tokens=tokens,
            trajectory=trajectory or [],
            summary=summary,
            lessons=lessons or [],
            created_at=created_at,
            meta=meta or {},
        )

    def get_episode(self, episode_id: str) -> EpisodeRecord | None:
        row = self.db.query_one("SELECT * FROM episodes WHERE id = ?", (episode_id,))
        return EpisodeRecord.from_row(row) if row else None

    def list_episodes(
        self, limit: int = 20, success: bool | None = None
    ) -> list[EpisodeRecord]:
        limit = max(1, min(200, int(limit)))
        if success is None:
            rows = self.db.query(
                "SELECT * FROM episodes ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        else:
            rows = self.db.query(
                "SELECT * FROM episodes WHERE success = ? ORDER BY created_at DESC LIMIT ?",
                (1 if success else 0, limit),
            )
        return [EpisodeRecord.from_row(row) for row in rows]

    def similar_episodes(
        self,
        goal: str,
        limit: int = 3,
        *,
        only_success: bool = True,
        pool: int = 200,
    ) -> list[EpisodeRecord]:
        """Most similar past episodes by token Jaccard, success-first."""
        if not goal:
            return []
        rows = self.db.query(
            "SELECT * FROM episodes ORDER BY created_at DESC LIMIT ?", (max(10, pool),)
        )
        target = tokenize(goal)
        matches: list[EpisodeRecord] = []
        for row in rows:
            episode = EpisodeRecord.from_row(row)
            if only_success and not episode.success:
                continue
            score = similarity(goal, episode.goal)
            if score <= 0:
                continue
            episode.score = score
            matches.append(episode)
        matches.sort(key=lambda item: (item.score, item.success), reverse=True)
        return matches[: max(1, limit)]

    def recent_failures(self, limit: int = 5) -> list[EpisodeRecord]:
        rows = self.db.query(
            "SELECT * FROM episodes WHERE success = 0 ORDER BY created_at DESC LIMIT ?",
            (max(1, limit),),
        )
        return [EpisodeRecord.from_row(row) for row in rows]

    # ----------------------------------------------------------------- stats
    def stats(self) -> dict[str, Any]:
        total = int(self.db.scalar("SELECT COUNT(*) FROM tasks") or 0)
        completed = int(self.db.scalar("SELECT COUNT(*) FROM tasks WHERE status = 'completed'") or 0)
        failed = int(self.db.scalar("SELECT COUNT(*) FROM tasks WHERE status IN ('failed','timeout','aborted')") or 0)
        running = int(self.db.scalar("SELECT COUNT(*) FROM tasks WHERE status IN ('planning','acting','verifying','replanning','pending')") or 0)
        episodes = int(self.db.scalar("SELECT COUNT(*) FROM episodes") or 0)
        wins = int(self.db.scalar("SELECT COUNT(*) FROM episodes WHERE success = 1") or 0)
        avg_steps = float(self.db.scalar("SELECT AVG(steps_count) FROM episodes") or 0.0)
        avg_duration = float(self.db.scalar("SELECT AVG(duration_ms) FROM episodes") or 0.0)
        tokens = int(self.db.scalar("SELECT COALESCE(SUM(tokens), 0) FROM episodes") or 0)
        tool_rows = self.db.query(
            "SELECT tool, COUNT(*) AS calls, SUM(ok) AS successes FROM steps "
            "WHERE tool IS NOT NULL AND tool != '' GROUP BY tool ORDER BY calls DESC"
        )
        return {
            "tasks_total": total,
            "tasks_completed": completed,
            "tasks_failed": failed,
            "tasks_running": running,
            "episodes": episodes,
            "success_rate": round(wins / episodes, 4) if episodes else 0.0,
            "avg_steps": round(avg_steps, 2),
            "avg_duration_ms": int(avg_duration),
            "tokens_total": tokens,
            "tool_usage": [
                {
                    "tool": row["tool"],
                    "calls": int(row["calls"] or 0),
                    "successes": int(row["successes"] or 0),
                }
                for row in tool_rows
            ],
        }

    def tool_reliability(self) -> dict[str, dict[str, float]]:
        rows = self.db.query(
            "SELECT tool, COUNT(*) AS calls, SUM(ok) AS successes FROM steps "
            "WHERE tool IS NOT NULL AND tool != '' GROUP BY tool"
        )
        reliability: dict[str, dict[str, float]] = {}
        for row in rows:
            calls = int(row["calls"] or 0)
            successes = int(row["successes"] or 0)
            reliability[row["tool"]] = {
                "calls": calls,
                "successes": successes,
                "rate": round(successes / calls, 3) if calls else 0.0,
            }
        return reliability

    def days_since(self, timestamp: str) -> float:
        try:
            when = datetime.fromisoformat(timestamp)
        except (TypeError, ValueError):
            return 999.0
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - when
        return delta.total_seconds() / 86400.0
