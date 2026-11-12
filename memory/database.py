"""SQLite persistence layer.

The database stores three kinds of memory (working memory stays in RAM):

``tasks`` / ``steps``
    the live record of what the agent is doing, step by step;
``episodes``
    completed task summaries with their full trajectory;
``knowledge`` / ``skills``
    semantic memory and reusable skills distilled from past episodes.

The wrapper is deliberately small: a single connection guarded by a lock,
``sqlite3.Row`` results converted to plain dictionaries, WAL mode for
concurrent readers (the API can list tasks while a run is writing).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

logger = logging.getLogger("visionpilot.memory.db")

SCHEMA_VERSION = 1

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        goal TEXT NOT NULL,
        status TEXT NOT NULL,
        execution_mode TEXT DEFAULT 'simulate',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT,
        steps_count INTEGER DEFAULT 0,
        duration_ms INTEGER,
        tokens INTEGER DEFAULT 0,
        llm_calls INTEGER DEFAULT 0,
        result TEXT,
        error TEXT,
        meta TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        idx INTEGER NOT NULL,
        phase TEXT,
        tool TEXT,
        arguments TEXT,
        observation TEXT,
        ok INTEGER,
        duration_ms INTEGER,
        verification TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS episodes (
        id TEXT PRIMARY KEY,
        task_id TEXT,
        goal TEXT NOT NULL,
        status TEXT NOT NULL,
        success INTEGER NOT NULL DEFAULT 0,
        steps_count INTEGER DEFAULT 0,
        duration_ms INTEGER DEFAULT 0,
        tokens INTEGER DEFAULT 0,
        trajectory TEXT,
        summary TEXT,
        lessons TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        key TEXT NOT NULL,
        content TEXT NOT NULL,
        score REAL DEFAULT 0.5,
        hits INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        meta TEXT,
        UNIQUE (kind, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skills (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        trigger TEXT DEFAULT '',
        steps TEXT,
        success_count INTEGER DEFAULT 0,
        fail_count INTEGER DEFAULT 0,
        uses INTEGER DEFAULT 0,
        version INTEGER DEFAULT 1,
        active INTEGER DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        meta TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_steps_task ON steps (task_id, idx)",
    "CREATE INDEX IF NOT EXISTS idx_episodes_created ON episodes (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_kind ON knowledge (kind)",
    "CREATE INDEX IF NOT EXISTS idx_skills_active ON skills (active)",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def json_loads(value: Any, default: Any = None) -> Any:
    if not value:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class Database:
    """Thread-safe SQLite helper."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=10.0
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=5000")
        self._initialise()

    @classmethod
    def from_settings(cls, settings: Any) -> "Database":
        return cls(settings.db_path)

    # ------------------------------------------------------------ lifecycle
    def _initialise(self) -> None:
        with self._lock:
            for statement in SCHEMA:
                self._connection.execute(statement)
            current = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if current < SCHEMA_VERSION:
                self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._connection.commit()
        logger.debug("database ready at %s", self.path)

    def close(self) -> None:
        with self._lock:
            try:
                self._connection.close()
            except sqlite3.Error:  # pragma: no cover
                pass

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -------------------------------------------------------------- queries
    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._connection.execute(sql, params)
            self._connection.commit()
            return cursor

    def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            self._connection.executemany(sql, rows)
            self._connection.commit()

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self._connection.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]

    def query_one(
        self, sql: str, params: Sequence[Any] | dict[str, Any] = ()
    ) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Any:
        row = self.query_one(sql, params)
        if not row:
            return None
        return next(iter(row.values()))

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._connection
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    # ---------------------------------------------------------------- admin
    def table_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table in ("tasks", "steps", "episodes", "knowledge", "skills"):
            try:
                counts[table] = int(self.scalar(f"SELECT COUNT(*) FROM {table}") or 0)
            except sqlite3.Error:  # pragma: no cover
                counts[table] = 0
        return counts

    def reset(self) -> None:
        with self._lock:
            for table in ("steps", "tasks", "episodes", "knowledge", "skills"):
                self._connection.execute(f"DELETE FROM {table}")
            self._connection.commit()
        logger.warning("database reset: %s", self.path)

    def vacuum(self) -> None:
        with self._lock:
            self._connection.execute("VACUUM")
