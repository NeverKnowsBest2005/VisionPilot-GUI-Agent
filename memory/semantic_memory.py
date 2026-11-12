"""Semantic memory - reusable knowledge distilled from experience.

Entries are small, typed facts ("preference", "lesson", "workflow", "fact").
Retrieval is keyword scoring over a bounded candidate set, which is plenty for
a personal desktop agent and keeps the project dependency-free.  The score is
also updated by use: facts that keep proving useful rise, stale ones pruned.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from memory.database import Database, json_dumps, json_loads, now_iso
from memory.text import keyword_score

logger = logging.getLogger("visionpilot.memory.semantic")

KINDS = ("preference", "lesson", "workflow", "fact", "hint")


@dataclass
class KnowledgeItem:
    id: str
    kind: str
    key: str
    content: str
    score: float = 0.5
    hits: int = 0
    created_at: str = ""
    updated_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    relevance: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "key": self.key,
            "content": self.content,
            "score": round(self.score, 3),
            "hits": self.hits,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "meta": self.meta,
            "relevance": round(self.relevance, 3),
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "KnowledgeItem":
        return cls(
            id=row["id"],
            kind=row["kind"],
            key=row["key"],
            content=row["content"],
            score=float(row.get("score") or 0.5),
            hits=int(row.get("hits") or 0),
            created_at=row.get("created_at") or "",
            updated_at=row.get("updated_at") or "",
            meta=json_loads(row.get("meta"), {}) or {},
        )

    def to_prompt_line(self) -> str:
        return f"[{self.kind}] {self.content}"


class SemanticMemory:
    """Long-lived, reusable knowledge."""

    def __init__(self, database: Database) -> None:
        self.db = database

    # ---------------------------------------------------------------- writes
    def remember(
        self,
        kind: str,
        key: str,
        content: str,
        *,
        score: float = 0.5,
        meta: dict[str, Any] | None = None,
    ) -> KnowledgeItem:
        kind = (kind or "fact").strip().lower()
        key = (key or "").strip()[:120]
        content = (content or "").strip()
        if not content:
            raise ValueError("knowledge content must not be empty")
        if not key:
            key = hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]
        timestamp = now_iso()
        existing = self.db.query_one(
            "SELECT * FROM knowledge WHERE kind = ? AND key = ?", (kind, key)
        )
        if existing:
            self.db.execute(
                """
                UPDATE knowledge SET content = ?, score = MAX(score, ?), updated_at = ?, meta = ?
                WHERE id = ?
                """,
                (content, float(score), timestamp, json_dumps(meta or existing.get("meta") or {}), existing["id"]),
            )
            row = self.db.query_one("SELECT * FROM knowledge WHERE id = ?", (existing["id"],))
        else:
            item_id = uuid.uuid4().hex[:12]
            self.db.execute(
                """
                INSERT INTO knowledge (id, kind, key, content, score, hits, created_at, updated_at, meta)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (item_id, kind, key, content, float(score), timestamp, timestamp, json_dumps(meta or {})),
            )
            row = self.db.query_one("SELECT * FROM knowledge WHERE id = ?", (item_id,))
        return KnowledgeItem.from_row(row)  # type: ignore[arg-type]

    def reinforce(self, item_id: str, delta: float = 0.03) -> None:
        self.db.execute(
            "UPDATE knowledge SET hits = hits + 1, score = MIN(1.0, score + ?), updated_at = ? WHERE id = ?",
            (max(-0.5, min(0.5, float(delta))), now_iso(), item_id),
        )

    def forget(self, item_id: str) -> bool:
        cursor = self.db.execute("DELETE FROM knowledge WHERE id = ?", (item_id,))
        return cursor.rowcount > 0

    # ---------------------------------------------------------------- reads
    def get(self, kind: str, key: str) -> KnowledgeItem | None:
        row = self.db.query_one(
            "SELECT * FROM knowledge WHERE kind = ? AND key = ?", (kind, key)
        )
        return KnowledgeItem.from_row(row) if row else None

    def by_kind(self, kind: str, limit: int = 50) -> list[KnowledgeItem]:
        rows = self.db.query(
            "SELECT * FROM knowledge WHERE kind = ? ORDER BY score DESC, hits DESC LIMIT ?",
            (kind, max(1, limit)),
        )
        return [KnowledgeItem.from_row(row) for row in rows]

    def all(self, kinds: Iterable[str] | None = None, limit: int = 100) -> list[KnowledgeItem]:
        kinds = list(kinds) if kinds else None
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            rows = self.db.query(
                f"SELECT * FROM knowledge WHERE kind IN ({placeholders}) "
                "ORDER BY score DESC, hits DESC LIMIT ?",
                (*kinds, max(1, limit)),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM knowledge ORDER BY score DESC, hits DESC LIMIT ?",
                (max(1, limit),),
            )
        return [KnowledgeItem.from_row(row) for row in rows]

    def recall(
        self,
        query: str,
        *,
        kinds: Iterable[str] | None = None,
        limit: int = 5,
        pool: int = 200,
    ) -> list[KnowledgeItem]:
        candidates = self.all(kinds=kinds, limit=pool)
        if not candidates:
            return []
        if not query.strip():
            for item in candidates:
                item.relevance = item.score
            return candidates[: max(1, limit)]
        scored: list[KnowledgeItem] = []
        for item in candidates:
            relevance = 0.7 * keyword_score(query, f"{item.key} {item.content}") + 0.3 * item.score
            if relevance <= 0.05:
                continue
            item.relevance = relevance
            scored.append(item)
        scored.sort(key=lambda item: (item.relevance, item.score), reverse=True)
        return scored[: max(1, limit)]

    def preferences(self, limit: int = 20) -> list[KnowledgeItem]:
        return self.by_kind("preference", limit=limit)

    def lessons(self, limit: int = 20) -> list[KnowledgeItem]:
        return self.by_kind("lesson", limit=limit)

    # ----------------------------------------------------------- convenience
    def remember_preference(self, key: str, value: str) -> KnowledgeItem:
        return self.remember("preference", key, value, score=0.8)

    def remember_lesson(self, lesson: str, *, goal: str = "", score: float = 0.55) -> KnowledgeItem:
        digest = hashlib.sha1(f"{goal}|{lesson}".encode("utf-8")).hexdigest()[:12]
        return self.remember(
            "lesson",
            f"lesson-{digest}",
            lesson,
            score=score,
            meta={"goal": goal},
        )

    def remember_workflow(self, name: str, steps: str, *, score: float = 0.6) -> KnowledgeItem:
        return self.remember("workflow", name, steps, score=score)

    # ---------------------------------------------------------------- admin
    def prune(self, keep: int = 400, min_score: float = 0.15) -> int:
        total = int(self.db.scalar("SELECT COUNT(*) FROM knowledge") or 0)
        removed = 0
        if total > keep:
            rows = self.db.query(
                "SELECT id FROM knowledge ORDER BY score ASC, updated_at ASC LIMIT ?",
                (total - keep,),
            )
            for row in rows:
                self.db.execute("DELETE FROM knowledge WHERE id = ?", (row["id"],))
                removed += 1
        removed += int(
            self.db.execute(
                "DELETE FROM knowledge WHERE score < ? AND hits = 0", (float(min_score),)
            ).rowcount
            or 0
        )
        return removed

    def stats(self) -> dict[str, Any]:
        rows = self.db.query(
            "SELECT kind, COUNT(*) AS count, AVG(score) AS avg_score FROM knowledge GROUP BY kind"
        )
        return {
            "total": int(self.db.scalar("SELECT COUNT(*) FROM knowledge") or 0),
            "by_kind": {
                row["kind"]: {
                    "count": int(row["count"] or 0),
                    "avg_score": round(float(row["avg_score"] or 0.0), 3),
                }
                for row in rows
            },
        }
