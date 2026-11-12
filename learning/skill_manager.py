"""Skill library: successful trajectories that become reusable procedures.

A skill is a named, replayable recipe plus a trigger description.  When a new
goal looks like a stored skill, the planner is shown the recipe so it can reuse
the approach instead of reasoning from scratch.  Skills are updated on every
successful reuse, so the library improves with use.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from memory.database import Database, json_dumps, json_loads, now_iso
from memory.text import similarity

logger = logging.getLogger("visionpilot.learning.skills")


@dataclass
class Skill:
    id: str
    name: str
    description: str
    trigger: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    success_count: int = 0
    fail_count: int = 0
    uses: int = 0
    version: int = 1
    active: bool = True
    created_at: str = ""
    updated_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0

    @property
    def success_rate(self) -> float:
        attempts = self.success_count + self.fail_count
        return self.success_count / attempts if attempts else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "trigger": self.trigger,
            "steps": self.steps,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "uses": self.uses,
            "version": self.version,
            "active": self.active,
            "success_rate": round(self.success_rate, 3),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "score": round(self.score, 3),
            "meta": self.meta,
        }

    def render(self, max_steps: int = 10) -> str:
        lines = [f"SKILL: {self.name} (success rate {self.success_rate:.0%}, used {self.uses}x)"]
        if self.description:
            lines.append(f"  when: {self.description}")
        for index, step in enumerate(self.steps[:max_steps]):
            args = json.dumps(step.get("arguments", {}), ensure_ascii=False, default=str)
            lines.append(f"  {index + 1}. {step.get('tool')}({args[:160]})")
        return "\n".join(lines)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Skill":
        return cls(
            id=row["id"],
            name=row["name"],
            description=row.get("description") or "",
            trigger=row.get("trigger") or "",
            steps=json_loads(row.get("steps"), []) or [],
            success_count=int(row.get("success_count") or 0),
            fail_count=int(row.get("fail_count") or 0),
            uses=int(row.get("uses") or 0),
            version=int(row.get("version") or 1),
            active=bool(row.get("active", 1)),
            created_at=row.get("created_at") or "",
            updated_at=row.get("updated_at") or "",
            meta=json_loads(row.get("meta"), {}) or {},
        )


class SkillManager:
    def __init__(self, database: Database) -> None:
        self.db = database

    # ---------------------------------------------------------------- learn
    def learn(
        self,
        goal: str,
        steps: Iterable[Any],
        *,
        success: bool,
        name: str | None = None,
    ) -> Skill | None:
        """Create or reinforce a skill from a finished trajectory."""
        from learning.trajectory import compact_recipe

        recipe = compact_recipe(steps)
        if not success or len(recipe) < 2 or len(recipe) > 20:
            return None
        trigger = _normalise_goal(goal)
        if not trigger:
            return None

        existing = self._find_similar(trigger)
        timestamp = now_iso()
        if existing is not None:
            self.db.execute(
                """
                UPDATE skills SET steps = ?, success_count = success_count + 1, uses = uses + 1,
                                  version = version + 1, updated_at = ?, active = 1
                WHERE id = ?
                """,
                (json_dumps(recipe), timestamp, existing.id),
            )
            logger.info("reinforced skill %s (%s)", existing.name, existing.id)
            updated = self.get(existing.id)
            if updated:
                updated.steps = recipe
                updated.version = existing.version + 1
            return updated

        skill_id = uuid.uuid4().hex[:12]
        display_name = (name or _skill_name(goal)).strip()[:80]
        self.db.execute(
            """
            INSERT INTO skills (id, name, description, trigger, steps, success_count, fail_count,
                                uses, version, active, created_at, updated_at, meta)
            VALUES (?, ?, ?, ?, ?, 1, 0, 1, 1, 1, ?, ?, ?)
            """,
            (
                skill_id,
                display_name,
                f"自动沉淀：{goal[:180]}",
                trigger,
                json_dumps(recipe),
                timestamp,
                timestamp,
                json_dumps({"source": "learned"}),
            ),
        )
        logger.info("learned new skill '%s' (%s) with %d steps", display_name, skill_id, len(recipe))
        return self.get(skill_id)

    def _find_similar(self, trigger: str) -> Skill | None:
        rows = self.db.query("SELECT * FROM skills WHERE active = 1 ORDER BY updated_at DESC LIMIT 100")
        best: tuple[float, Skill] | None = None
        for row in rows:
            skill = Skill.from_row(row)
            score = similarity(trigger, skill.trigger)
            if score >= 0.85 and (best is None or score > best[0]):
                best = (score, skill)
        return best[1] if best else None

    # ---------------------------------------------------------------- query
    def list(self, limit: int = 50, *, active_only: bool = True) -> list[Skill]:
        where = "WHERE active = 1" if active_only else ""
        rows = self.db.query(
            f"SELECT * FROM skills {where} ORDER BY success_count DESC, updated_at DESC LIMIT ?",
            (max(1, limit),),
        )
        return [Skill.from_row(row) for row in rows]

    def get(self, skill_id: str) -> Skill | None:
        row = self.db.query_one("SELECT * FROM skills WHERE id = ?", (skill_id,))
        return Skill.from_row(row) if row else None

    def match(self, goal: str, limit: int = 3, threshold: float = 0.28) -> list[Skill]:
        if not goal:
            return []
        matches: list[Skill] = []
        for skill in self.list(limit=200):
            lexical = similarity(goal, f"{skill.trigger} {skill.name}")
            score = 0.75 * lexical + 0.15 * skill.success_rate + 0.1 * min(1.0, skill.uses / 5)
            if lexical < 0.1 or score < threshold:
                continue
            skill.score = score
            matches.append(skill)
        matches.sort(key=lambda item: item.score, reverse=True)
        return matches[: max(1, limit)]

    def record_use(self, skill_id: str, success: bool) -> None:
        column = "success_count" if success else "fail_count"
        self.db.execute(
            f"UPDATE skills SET {column} = {column} + 1, uses = uses + 1, updated_at = ? WHERE id = ?",
            (now_iso(), skill_id),
        )

    def deactivate(self, skill_id: str) -> bool:
        cursor = self.db.execute(
            "UPDATE skills SET active = 0, updated_at = ? WHERE id = ?", (now_iso(), skill_id)
        )
        return cursor.rowcount > 0

    def stats(self) -> dict[str, Any]:
        total = int(self.db.scalar("SELECT COUNT(*) FROM skills") or 0)
        active = int(self.db.scalar("SELECT COUNT(*) FROM skills WHERE active = 1") or 0)
        uses = int(self.db.scalar("SELECT COALESCE(SUM(uses), 0) FROM skills") or 0)
        return {"total": total, "active": active, "uses": uses}

    @staticmethod
    def render_prompt(skills: Iterable[Skill], max_skills: int = 3) -> str:
        rendered = [skill.render() for skill in list(skills)[:max_skills]]
        return "\n\n".join(rendered)


def _normalise_goal(goal: str) -> str:
    text = " ".join((goal or "").lower().split())
    return text[:200]


def _skill_name(goal: str) -> str:
    text = " ".join((goal or "").split())
    if len(text) <= 60:
        return text or "unnamed skill"
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]
    return f"{text[:48]}... ({digest})"
