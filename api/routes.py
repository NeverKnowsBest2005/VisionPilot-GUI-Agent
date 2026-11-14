"""FastAPI application for VisionPilot.

The API is intentionally thin: every task runs in the background through
:class:`VisionPilotAgent`, progress is streamed over WebSocket and durable
history lives in SQLite.  The same app factory is used by ``main.py serve`` and
by the tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from agent import VisionPilotAgent
from api.schemas import (
    CancelResponse,
    DemoRequest,
    DemoResponse,
    EpisodeInfo,
    EpisodesResponse,
    ErrorResponse,
    HealthResponse,
    KnowledgeInfo,
    KnowledgeResponse,
    SkillInfo,
    SkillsResponse,
    StatsResponse,
    TaskCreateRequest,
    TaskCreatedResponse,
    TaskDetail,
    TaskSummary,
    ToolInfo,
    ToolsResponse,
)
from config import PROJECT_ROOT, Settings, load_settings
from config.logging import JsonlEventWriter, configure_logging
from executor import build_default_registry
from learning import PerformanceOptimizer
from learning.skill_manager import SkillManager
from memory import Database, EpisodicMemory, SemanticMemory

logger = logging.getLogger("visionpilot.api")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"

DEMO_SCENARIOS: dict[str, str] = {
    "notes": "打开记事本，输入 VisionPilot 已经跑通端到端闭环，然后保存",
    "explorer": "打开文件资源管理器，进入 Documents 文件夹，打开 notes.txt",
    "settings": "打开设置",
    "english": 'open the Notes app and type "hello from VisionPilot"',
}

# Final statuses a task can report; "cancelling" is the transient state between
# a cancel request and the agent's next checkpoint.
TASK_STATUSES = {"queued", "running", "completed", "failed", "aborted", "cancelled"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TaskRecord:
    """In-memory view of a task; the durable copy lives in SQLite."""

    def __init__(
        self,
        task_id: str,
        goal: str,
        max_steps: int | None = None,
        confirmed: bool = False,
    ) -> None:
        self.task_id = task_id
        self.goal = goal
        self.max_steps = max_steps
        self.confirmed = confirmed
        self.status = "queued"
        self.created_at = utcnow()
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.result: Any | None = None
        self.error: str | None = None
        self.events: list[dict[str, Any]] = []
        self.agent: VisionPilotAgent | None = None

    # ------------------------------------------------------------------ views
    def summary(self) -> TaskSummary:
        result = self.result
        steps = len(getattr(result, "steps", []) or [])
        return TaskSummary(
            task_id=self.task_id,
            goal=self.goal,
            status=self.status,
            success=getattr(result, "success", None),
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            duration_ms=getattr(result, "duration_ms", None),
            steps=steps,
            tokens=getattr(result, "tokens", 0) or 0,
            summary=getattr(result, "summary", None),
            error=self.error or getattr(result, "error", None),
        )

    def detail(self) -> TaskDetail:
        base = self.summary()
        result = self.result
        return TaskDetail(
            **base.model_dump(),
            step_records=list(getattr(result, "steps", []) or []),
            evaluation=getattr(result, "evaluation", None),
            plan=getattr(result, "plan", None),
            events=list(self.events[-200:]),
            episode_id=getattr(result, "episode_id", None),
        )


class TaskManager:
    """Own the agent lifecycle, the event stream and the shared database."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self.settings.ensure_directories()
        self.started_at = time.monotonic()
        self.database = Database.from_settings(self.settings)
        self.episodic = EpisodicMemory(self.database)
        self.semantic = SemanticMemory(self.database)
        self.skills = SkillManager(self.database)
        self.optimizer = PerformanceOptimizer(
            self.settings, self.episodic, self.semantic, self.skills
        )
        self.registry = build_default_registry(self.settings)
        self.tasks: dict[str, TaskRecord] = {}
        self._jobs: dict[str, asyncio.Task] = {}
        self._agents: dict[str, VisionPilotAgent] = {}
        self._subscribers: set[WebSocket] = set()
        self._writer = JsonlEventWriter(self.settings.effective_event_log_path)
        self._limit = asyncio.Semaphore(1 if not self.settings.dry_run else 4)
        self._closed = False

    # ------------------------------------------------------------- lifecycle
    def submit(
        self,
        goal: str,
        max_steps: int | None = None,
        confirmed: bool = False,
    ) -> TaskRecord:
        self._prune()
        task_id = uuid.uuid4().hex[:12]
        record = TaskRecord(task_id, goal, max_steps=max_steps, confirmed=confirmed)
        self.tasks[task_id] = record
        job = asyncio.create_task(self._execute(record), name=f"visionpilot-{task_id}")
        self._jobs[task_id] = job
        return record

    def _prune(self, keep: int = 60) -> None:
        if len(self.tasks) <= keep:
            return
        finished = [
            task for task in self.tasks.values()
            if task.status in {"completed", "failed", "aborted", "cancelled"}
        ]
        for task in sorted(finished, key=lambda item: item.created_at)[: len(self.tasks) - keep]:
            self.tasks.pop(task.task_id, None)

    async def _execute(self, record: TaskRecord) -> None:
        try:
            async with self._limit:
                if record.status == "cancelled":
                    return
                record.status = "running"
                record.started_at = utcnow()
                await self._publish_status(record)
                agent = VisionPilotAgent(
                    self.settings,
                    registry=self.registry,
                    database=self.database,
                    event_callback=lambda event: self._on_event(record, event),
                )
                record.agent = agent
                self._agents[record.task_id] = agent
                try:
                    result = await agent.run(
                        record.goal,
                        task_id=record.task_id,
                        max_steps=record.max_steps,
                        confirmed=record.confirmed,
                    )
                finally:
                    await agent.aclose()
                    self._agents.pop(record.task_id, None)
                record.result = result
                record.status = result.status if result.status in TASK_STATUSES else (
                    "completed" if result.success else "failed"
                )
                record.error = result.error
        except asyncio.CancelledError:
            record.status = "cancelled"
            record.finished_at = record.finished_at or utcnow()
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced through the API
            logger.exception("task %s crashed", record.task_id)
            record.status = "failed"
            record.error = f"{type(exc).__name__}: {exc}"
        finally:
            record.finished_at = record.finished_at or utcnow()
            self._jobs.pop(record.task_id, None)
            await self._publish_status(record)

    async def _on_event(self, record: TaskRecord, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        event.setdefault("task_id", record.task_id)
        event.setdefault("timestamp", utcnow())
        record.events.append(event)
        if len(record.events) > 600:
            del record.events[: len(record.events) - 600]
        self._writer.write(event)
        await self._broadcast(event)

    async def _publish_status(self, record: TaskRecord) -> None:
        await self._broadcast(
            {
                "type": "task_status",
                "task_id": record.task_id,
                "status": record.status,
                "success": getattr(record.result, "success", None),
                "timestamp": utcnow(),
            }
        )

    async def cancel(self, task_id: str) -> bool:
        record = self.tasks.get(task_id)
        if record is None:
            raise KeyError(task_id)
        if record.status in {"completed", "failed", "aborted", "cancelled"}:
            return False
        agent = self._agents.get(task_id)
        if agent is not None:
            agent.cancel()
            if record.status in {"queued", "running"}:
                # Show "stopping" immediately: the agent stops at its next
                # checkpoint (usually within milliseconds) and then reports the
                # final "aborted" status.
                record.status = "cancelling"
                await self._publish_status(record)
            return True
        job = self._jobs.get(task_id)
        if job is not None and not job.done():
            job.cancel()
            record.status = "cancelled"
            record.finished_at = utcnow()
            await self._publish_status(record)
            return True
        return False

    # ------------------------------------------------------------ websockets
    async def register(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._subscribers.add(websocket)

    def unregister(self, websocket: WebSocket) -> None:
        self._subscribers.discard(websocket)

    async def _broadcast(self, event: dict[str, Any]) -> None:
        if not self._subscribers:
            return
        payload = json.dumps(event, ensure_ascii=False, default=str)
        dead: list[WebSocket] = []
        for websocket in list(self._subscribers):
            try:
                await websocket.send_text(payload)
            except Exception:  # noqa: BLE001 - a broken socket must not break a task
                dead.append(websocket)
        for websocket in dead:
            self.unregister(websocket)

    # -------------------------------------------------------------- shutdown
    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        for agent in list(self._agents.values()):
            agent.cancel()
        jobs = [job for job in self._jobs.values() if not job.done()]
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        for agent in list(self._agents.values()):
            try:
                await agent.aclose()
            except Exception:  # noqa: BLE001
                logger.debug("error closing agent", exc_info=True)
        self._agents.clear()
        manager = getattr(self.registry, "browser_manager", None)
        if manager is not None and getattr(manager, "started", False):
            try:
                await manager.close()
            except Exception:  # noqa: BLE001
                logger.debug("error closing browser", exc_info=True)
        for websocket in list(self._subscribers):
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass
        self._subscribers.clear()
        try:
            self.database.close()
        except Exception:  # noqa: BLE001
            logger.debug("error closing database", exc_info=True)


# --------------------------------------------------------------------- routes
router = APIRouter(prefix="/api")


def get_manager(request: Request) -> TaskManager:
    manager = getattr(request.app.state, "manager", None)
    if manager is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=503, detail="task manager is not ready")
    return manager


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    manager = get_manager(request)
    counts = manager.database.table_counts()
    available = manager.registry.available()
    return HealthResponse(
        app=manager.settings.app_name,
        version=manager.settings.version,
        execution_mode=manager.settings.execution_mode,
        dry_run=manager.settings.dry_run,
        synthetic_desktop=manager.settings.uses_synthetic_desktop,
        llm_ready=manager.settings.llm_ready,
        vision_ready=manager.settings.vision_ready,
        tools_total=len(manager.registry.all()),
        tools_available=len(available),
        tasks_total=counts.get("tasks", 0),
        episodes=counts.get("episodes", 0),
        database=str(manager.settings.db_path),
        uptime_seconds=round(time.monotonic() - manager.started_at, 1),
    )


@router.get("/config")
async def config(request: Request) -> dict[str, Any]:
    return get_manager(request).settings.safe_dict()


@router.get("/tasks", response_model=list[TaskSummary])
async def list_tasks(request: Request, limit: int = 30) -> list[TaskSummary]:
    manager = get_manager(request)
    records = sorted(manager.tasks.values(), key=lambda item: item.created_at, reverse=True)
    return [record.summary() for record in records[: max(1, min(limit, 200))]]


@router.post("/tasks", response_model=TaskCreatedResponse, status_code=201)
async def create_task(payload: TaskCreateRequest, request: Request) -> TaskCreatedResponse:
    manager = get_manager(request)
    record = manager.submit(
        payload.goal.strip(),
        max_steps=payload.max_steps,
        confirmed=payload.confirmed,
    )
    return TaskCreatedResponse(
        task_id=record.task_id,
        status=record.status,
        goal=record.goal,
        created_at=record.created_at,
        detail_url=f"/api/tasks/{record.task_id}",
    )


@router.get("/tasks/{task_id}", response_model=TaskDetail)
async def get_task(task_id: str, request: Request) -> TaskDetail:
    manager = get_manager(request)
    record = manager.tasks.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    return record.detail()


@router.post("/tasks/{task_id}/cancel", response_model=CancelResponse)
async def cancel_task(task_id: str, request: Request) -> CancelResponse:
    manager = get_manager(request)
    try:
        cancelled = await manager.cancel(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found") from None
    record = manager.tasks[task_id]
    return CancelResponse(task_id=task_id, cancelled=cancelled, status=record.status)


@router.get("/stats", response_model=StatsResponse)
async def stats(request: Request) -> StatsResponse:
    manager = get_manager(request)
    try:
        metrics = manager.optimizer.metrics()
        suggestions = manager.optimizer.suggestions()
    except Exception as exc:  # noqa: BLE001 - stats must never take the API down
        logger.warning("could not compute metrics: %s", exc)
        metrics, suggestions = {}, []
    return StatsResponse(
        metrics=metrics,
        suggestions=suggestions,
        skills=manager.skills.stats(),
        tools=manager.registry.stats(),
    )


@router.get("/tools", response_model=ToolsResponse)
async def tools(request: Request) -> ToolsResponse:
    manager = get_manager(request)
    items: list[ToolInfo] = []
    for tool in manager.registry.all():
        items.append(
            ToolInfo(
                name=tool.name,
                description=tool.description,
                danger=tool.danger,
                category=tool.category,
                available=tool.is_available(),
                unavailable_reason=None if tool.is_available() else tool.unavailable_reason(),
                parameters=[parameter.model_dump() for parameter in tool.parameters],
            )
        )
    return ToolsResponse(
        count=len(items),
        available=sum(1 for item in items if item.available),
        tools=items,
    )


@router.get("/skills", response_model=SkillsResponse)
async def list_skills(request: Request, limit: int = 50, active_only: bool = False) -> SkillsResponse:
    manager = get_manager(request)
    skills = [SkillInfo(**skill.as_dict()) for skill in manager.skills.list(limit=limit, active_only=active_only)]
    return SkillsResponse(count=len(skills), skills=skills)


@router.get("/memory/episodes", response_model=EpisodesResponse)
async def list_episodes(request: Request, limit: int = 20, only_success: bool = False) -> EpisodesResponse:
    manager = get_manager(request)
    episodes = manager.episodic.list_episodes(limit=limit, success=True if only_success else None)
    items = [
        EpisodeInfo(
            id=episode.id,
            goal=episode.goal,
            status=episode.status,
            success=episode.success,
            steps_count=episode.steps_count,
            duration_ms=episode.duration_ms,
            tokens=episode.tokens,
            summary=episode.summary,
            lessons=episode.lessons,
            created_at=episode.created_at,
        )
        for episode in episodes
    ]
    return EpisodesResponse(count=len(items), episodes=items)


@router.get("/memory/lessons", response_model=KnowledgeResponse)
async def list_lessons(request: Request, limit: int = 20) -> KnowledgeResponse:
    manager = get_manager(request)
    items = [
        KnowledgeInfo(**item.as_dict())
        for item in manager.semantic.lessons(limit=limit)
    ]
    return KnowledgeResponse(count=len(items), items=items)


@router.post("/demo", response_model=DemoResponse)
async def run_demo(payload: DemoRequest, request: Request) -> DemoResponse:
    goal = DEMO_SCENARIOS.get(payload.scenario)
    if goal is None:
        raise HTTPException(
            status_code=400,
            detail=f"unknown scenario '{payload.scenario}'; expected one of {sorted(DEMO_SCENARIOS)}",
        )
    manager = get_manager(request)
    record = manager.submit(goal)
    return DemoResponse(scenario=payload.scenario, goal=goal, task_id=record.task_id)


# ---------------------------------------------------------- websocket origins
_LOOPBACK_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}


def _origin_key(value: str) -> tuple[str, str, int] | None:
    """Normalize an origin so default ports and case never cause a mismatch."""
    try:
        parsed = urlsplit(str(value).strip())
        scheme = parsed.scheme.casefold()
        host = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return None
    if not scheme or not host:
        return None
    if port is None:
        port = {"http": 80, "https": 443}.get(scheme)
    if port is None:
        return None
    return scheme, host, port


def _ws_origin_allowed(
    origin: str,
    host_header: str,
    scheme: str,
    allowed: set[tuple[str, str, int]],
    allow_any: bool,
) -> bool:
    if allow_any:
        return True
    key = _origin_key(origin)
    if key is None:
        return False
    if key in allowed:
        return True
    # The bundled console connects to its own host.  `fastapi dev` serves it
    # on port 8000 (or any --port), which is not necessarily in cors_origins.
    # Allow exactly that same origin when the host is loopback; otherwise a
    # page on any other site could reach the local event stream.
    expected = _origin_key(f"{'https' if scheme == 'wss' else 'http'}://{host_header}")
    if expected is None or expected[1] not in _LOOPBACK_HOSTNAMES:
        return False
    return key == expected


# ------------------------------------------------------------------ factory
def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application.

    Passing ``settings`` keeps tests hermetic (temporary DB, screenshot dir and
    workspace); production uses ``.env`` through :func:`load_settings`.
    """

    resolved = settings or load_settings()
    resolved.ensure_directories()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager = TaskManager(resolved)
        app.state.manager = manager
        logger.info(
            "VisionPilot API ready (mode=%s, tools=%d, db=%s)",
            resolved.execution_mode,
            len(manager.registry.all()),
            resolved.db_path,
        )
        try:
            yield
        finally:
            await manager.shutdown()

    app = FastAPI(
        title="VisionPilot GUI Agent",
        description="Observe → Understand → Plan → Act → Verify → Learn desktop agent",
        version=resolved.version,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        # An empty list means no cross-origin access.  Never fall back to "*":
        # this API is unauthenticated and accepts the confirmed flag.
        allow_origins=resolved.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    if FRONTEND_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
        # Vite writes absolute asset URLs (/assets/index-<hash>.js), so serving
        # the dist folder only under /static leaves the console a blank page.
        assets_dir = FRONTEND_DIR / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        target = FRONTEND_DIR / "index.html"
        if not target.exists():  # pragma: no cover - packaging edge case
            raise HTTPException(status_code=404, detail="frontend is missing")
        return FileResponse(target)

    # CORSMiddleware ignores non-HTTP scopes, so the WebSocket endpoint needs
    # its own Origin check: otherwise any web page can open ws://127.0.0.1 and
    # receive every task event (goals, observations, tool output).
    allowed_ws_origins = {
        key for key in (_origin_key(origin) for origin in resolved.cors_origins) if key
    }
    allow_any_ws_origin = "*" in {origin.strip() for origin in resolved.cors_origins}
    if allow_any_ws_origin:
        logger.warning(
            "VISIONPILOT_CORS_ORIGINS contains '*': WebSocket origin checks are disabled"
        )

    @app.websocket("/ws/events")
    async def ws_events(websocket: WebSocket, task_id: str | None = None) -> None:
        origin = websocket.headers.get("origin")
        if origin is not None and not _ws_origin_allowed(
            origin,
            websocket.headers.get("host", ""),
            websocket.url.scheme,
            allowed_ws_origins,
            allow_any_ws_origin,
        ):
            logger.warning("rejected WebSocket connection from origin %s", origin)
            await websocket.close(code=1008)
            return
        manager: TaskManager = websocket.app.state.manager
        await manager.register(websocket)
        try:
            if task_id and task_id in manager.tasks:
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "snapshot",
                            "task_id": task_id,
                            "events": manager.tasks[task_id].events[-100:],
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                )
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "hello",
                        "execution_mode": resolved.execution_mode,
                        "dry_run": resolved.dry_run,
                        "timestamp": utcnow(),
                    },
                    ensure_ascii=False,
                )
            )
            while True:
                message = await websocket.receive_text()
                if message.strip().lower() in {"ping", "keepalive"}:
                    await websocket.send_text('{"type":"pong"}')
        except WebSocketDisconnect:
            pass
        finally:
            manager.unregister(websocket)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        logger.exception("unhandled API error on %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": f"{type(exc).__name__}: {exc}"},
        )

    return app


app = create_app()
