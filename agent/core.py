"""The VisionPilot agent: Observe → Plan → Act → Verify → Recover → Learn."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from agent.planner import Planner
from agent.reasoning import LLMClient, ReasoningEngine
from agent.state import AgentState, Phase, Plan, PlanStep, TaskResult
from config.settings import Settings
from executor import GROUNDING_KEYS, ToolContext, ToolResult, build_default_registry, get_screen_size
from learning.optimizer import PerformanceOptimizer
from learning.skill_manager import SkillManager
from learning.trajectory import build_trajectory, extract_lessons, summarize
from memory.database import Database
from memory.episodic_memory import EpisodicMemory
from memory.semantic_memory import SemanticMemory
from memory.working_memory import WorkingMemory
from perception import GUIState, PerceptionPipeline
from perception.ui_detector import find_element
from verifier import Checker, RecoveryAction, RecoveryManager, TaskEvaluator

logger = logging.getLogger("visionpilot.agent")

EventCallback = Callable[[dict[str, Any]], Any]

# Tools whose effect is not reliably visible in a desktop screenshot. When the
# tool reports success but visual verification is inconclusive, the structured
# result is more trustworthy than "the screen looks identical".
_TRUST_ON_INCONCLUSIVE = {
    "fs_write",
    "fs_move",
    "fs_copy",
    "fs_delete",
    "fs_mkdir",
    "system_run_command",
    "system_info",
    "system_wait",
    "browser_get_text",
    "browser_screenshot",
    "browser_close",
}


class TaskAborted(Exception):
    """Raised internally when the user cancels a running task."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _verification_passed(
    step: Any,
    result: ToolResult,
    outcome: Any,
    verification_enabled: bool,
) -> bool:
    """Decide whether a step passed, independently of the tool's own claim.

    Non-visual tools are trusted when the checker cannot see their effect, but
    only when the step carried no explicit expectation.  An explicit
    ``expect`` is a contract the checker must confirm.
    """
    if not verification_enabled:
        return bool(result.ok)
    passed = bool(outcome is not None and outcome.success)
    if (
        not passed
        and result.ok
        and not step.expect
        and step.tool in _TRUST_ON_INCONCLUSIVE
        and (outcome is None or outcome.status in {"partial", "unknown"})
    ):
        # The tool reported success but a desktop screenshot cannot show the
        # effect (files, shell, browser text). Do not retry a mutating
        # operation just because the screen did not change.
        return True
    return passed


class VisionPilotAgent:
    """Orchestrates perception, planning, tools, verification and learning."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        registry: Any | None = None,
        perception: PerceptionPipeline | None = None,
        llm: LLMClient | None = None,
        database: Database | None = None,
        event_callback: EventCallback | None = None,
    ) -> None:
        from config.settings import load_settings

        self.settings = settings or load_settings()
        self.settings.ensure_directories()

        self.llm = llm if llm is not None else LLMClient(self.settings)
        judge_llm = self.llm if self.llm.available else None
        self.reasoning = ReasoningEngine(self.settings, self.llm)
        self._owns_registry = registry is None
        self.registry = registry if registry is not None else build_default_registry(self.settings)
        self.perception = perception or PerceptionPipeline(self.settings, llm=judge_llm)

        self.database = database or Database.from_settings(self.settings)
        self._owns_database = database is None
        self.episodic = EpisodicMemory(self.database)
        self.semantic = SemanticMemory(self.database)
        self.skills = SkillManager(self.database)
        self.optimizer = PerformanceOptimizer(
            self.settings, self.episodic, self.semantic, self.skills
        )

        self.planner = Planner(self.settings, self.reasoning, self.registry)
        self.checker = Checker(self.settings, judge_llm)
        self.evaluator = TaskEvaluator(self.settings, judge_llm)
        self.recovery = RecoveryManager(self.settings)

        self.task_id: str | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=800)
        self._event_callback = event_callback
        self._cancelled = False
        self._running = False

    # ------------------------------------------------------------ lifecycle
    @property
    def running(self) -> bool:
        return self._running

    def cancel(self) -> None:
        """Ask the running task to stop at the next safe point."""
        self._cancelled = True
        logger.info("cancellation requested for task %s", self.task_id)

    async def aclose(self) -> None:
        manager = getattr(self.registry, "browser_manager", None)
        # A shared registry (API TaskManager) owns the browser; closing it here
        # would break the next task.
        if self._owns_registry and manager is not None and getattr(manager, "started", False):
            try:
                await manager.close()
            except Exception:  # noqa: BLE001
                logger.debug("browser close failed", exc_info=True)
        if self.llm is not None:
            await self.llm.close()
        if self._owns_database:
            self.database.close()

    # ------------------------------------------------------------------ run
    async def run(
        self,
        goal: str,
        *,
        task_id: str | None = None,
        max_steps: int | None = None,
        confirmed: bool = False,
    ) -> TaskResult:
        """Run one goal to completion and return the full task result."""
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("goal must not be empty")
        if self._running:
            raise RuntimeError("the agent is already running a task")

        settings = self.settings
        self._running = True
        self._cancelled = False
        self._reset_llm_counters()

        task_id = self.episodic.start_task(
            goal,
            task_id=task_id,
            execution_mode=settings.execution_mode,
            meta={"synthetic_desktop": bool(self.perception.desktop)},
        )
        self.task_id = task_id
        working = WorkingMemory(task_id, goal, max_steps=max_steps or settings.max_steps)
        state = AgentState(task_id=task_id, goal=goal, phase=Phase.OBSERVING)
        deadline = time.monotonic() + float(settings.max_task_seconds)
        started = time.monotonic()

        await self._emit(
            "task_started",
            goal=goal,
            execution_mode=settings.execution_mode,
            llm=bool(self.llm.available),
        )

        status = "failed"
        error: str | None = None
        evaluation: Any = None
        plan = Plan(goal=goal, steps=[])
        try:
            observation = await self._observe(state)
            state.phase = Phase.PLANNING
            hints = self._memory_hints(goal)
            plan = await self.planner.create_plan(goal, observation, hints=hints)
            state.plan = plan
            await self._emit(
                "plan_created",
                plan=plan.to_prompt(),
                source=plan.source,
                reasoning=plan.reasoning,
                steps=[step.as_dict() for step in plan.steps],
                step_count=len(plan.steps),
            )

            if plan.steps:
                finished, status, error = await self._run_plan(
                    state, working, plan, deadline, max_steps=max_steps, confirmed=confirmed
                )
            else:
                finished = True

            state.phase = Phase.VERIFYING
            if state.current_state is not None:
                evaluation = await self.evaluator.evaluate(
                    goal,
                    state.current_state,
                    steps=working.steps,
                    plan=[step.as_dict() for step in plan.steps],
                )
                state.evaluation = evaluation.as_dict()
                await self._emit("evaluation", evaluation=evaluation.as_dict())

            success = bool(evaluation.success) if evaluation is not None else False
            if success:
                status = "completed"
                error = None
            elif status in ("completed", "running"):
                status = "failed"
                error = error or (
                    evaluation.reason if evaluation is not None else "task did not reach the goal"
                )
            elif evaluation is not None and not error:
                error = evaluation.reason
        except TaskAborted:
            status = "aborted"
            error = "task cancelled by user"
        except Exception as exc:  # noqa: BLE001
            logger.exception("task %s crashed", task_id)
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
        finally:
            success = status == "completed"
            state.phase = (
                Phase.COMPLETED
                if success
                else Phase.ABORTED
                if status == "aborted"
                else Phase.FAILED
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            tokens = self.llm.total_tokens if self.llm is not None else 0
            llm_calls = self.llm.calls if self.llm is not None else 0
            state.tokens, state.llm_calls = tokens, llm_calls
            summary = (
                evaluation.reason
                if evaluation is not None
                else (error or "task finished")
            )
            try:
                self.episodic.finish_task(
                    task_id,
                    status=status,
                    result=summary[:1000],
                    error=error,
                    duration_ms=duration_ms,
                    tokens=tokens,
                    llm_calls=llm_calls,
                    steps_count=len(working.steps),
                )
            except Exception:  # noqa: BLE001
                logger.exception("could not finish task record %s", task_id)

            episode_id: str | None = None
            try:
                episode_id = await self._learn(
                    state, working, plan, success=success, error=error, duration_ms=duration_ms
                )
            except Exception:  # noqa: BLE001
                logger.exception("learning phase failed")

        result = TaskResult(
            task_id=task_id,
            goal=goal,
            success=success,
            status=status,
            summary=summary,
            steps=[step.as_dict() for step in working.steps],
            duration_ms=duration_ms,
            tokens=tokens,
            llm_calls=llm_calls,
            evaluation=evaluation.as_dict() if evaluation is not None else None,
            plan=plan.as_dict() if plan is not None else None,
            error=error,
            episode_id=episode_id,
        )
        state.phase = Phase.COMPLETED if success else state.phase
        self._running = False
        await self._emit(
            "task_finished",
            status=status,
            success=success,
            summary=summary,
            duration_ms=duration_ms,
            steps=len(working.steps),
            tokens=tokens,
            result=result.as_dict(),
        )
        return result

    # ----------------------------------------------------------------- loop
    async def _run_plan(
        self,
        state: AgentState,
        working: WorkingMemory,
        plan: Plan,
        deadline: float,
        *,
        max_steps: int | None = None,
        confirmed: bool = False,
    ) -> tuple[bool, str, str | None]:
        settings = self.settings
        before = state.current_state
        context = ToolContext(
            settings=settings,
            desktop=self.perception.desktop,
            confirmed=confirmed,
            metadata={"task_id": state.task_id},
        )
        failures: dict[int, int] = {}
        replans_used = 0
        step_budget = max_steps or settings.max_steps

        for _ in range(step_budget):
            if self._cancelled:
                raise TaskAborted()
            if time.monotonic() > deadline:
                return False, "timeout", f"task exceeded {settings.max_task_seconds}s"

            nxt = plan.next_step()
            if nxt is None:
                return True, "running", None

            index, step = nxt
            state.step_index = index
            state.phase = Phase.ACTING
            step.status = "running"
            await self._emit(
                "step_started",
                index=index,
                step=step.as_dict(),
                tool=step.tool,
                description=step.description,
            )

            arguments, ground_error = self._ground(step.tool, step.arguments, before)
            if ground_error is not None:
                result = ToolResult(
                    tool=step.tool,
                    ok=False,
                    error=ground_error,
                    simulated=settings.dry_run,
                )
            else:
                result = await self.registry.execute(
                    step.tool,
                    arguments,
                    context,
                    timeout=settings.adaptive_step_timeout_sec,
                )

            step.duration_ms = result.duration_ms
            step.observation = result.to_observation()
            await self._settle()
            after = await self._observe(state)
            working.observe(
                self._observation_summary(after), after.signature(), after.active_window
            )

            outcome = None
            if settings.enable_verification:
                outcome = await self.checker.check(
                    before=before,
                    after=after,
                    action=step.as_dict(),
                    expectation=step.expect,
                    tool_result=result,
                )
            passed = _verification_passed(
                step, result, outcome, settings.enable_verification
            )

            verification = outcome.as_dict() if outcome is not None else {}
            working.record_step(
                step.tool,
                arguments,
                passed,
                step.observation,
                planned_step=step.description,
                duration_ms=result.duration_ms,
                verification=(outcome.status if outcome is not None else None),
            )
            self.episodic.add_step(
                state.task_id,
                len(working.steps),
                phase="act",
                tool=step.tool,
                arguments=arguments,
                observation=step.observation,
                ok=bool(result.ok),
                duration_ms=result.duration_ms,
                verification=verification,
            )
            await self._emit(
                "step_finished",
                index=index,
                step=step.as_dict(),
                tool=step.tool,
                ok=bool(result.ok),
                observation=(result.output or result.error or "")[:400],
                tool_ok=result.ok,
                verified=passed,
                output=result.output[:600],
                error=result.error,
                simulated=result.simulated,
                verification=verification,
            )

            if passed:
                plan.mark(index, "done", observation=step.observation)
                failures.pop(index, None)
                state.retries = 0
                before = after
                continue

            plan.mark(index, "failed", observation=step.observation)
            failures[index] = failures.get(index, 0) + 1
            attempt = failures[index] - 1
            decision = None
            if outcome is not None:
                decision = self.recovery.after_verification(
                    outcome,
                    attempt=attempt,
                    replans_used=replans_used,
                    unchanged_count=working.unchanged_count(),
                )
            if decision is None:
                decision = self.recovery.decide(
                    error=result.error or "action did not achieve the expected result",
                    tool_result=result,
                    outcome=outcome,
                    attempt=attempt,
                    replans_used=replans_used,
                    unchanged_count=working.unchanged_count(),
                )
            await self._emit(
                "recovery",
                action=decision.action.value,
                reason=decision.reason,
                index=index,
                attempt=attempt + 1,
            )

            if decision.action is RecoveryAction.RETRY:
                state.retries += 1
                if decision.delay_seconds > 0:
                    await asyncio.sleep(decision.delay_seconds)
                before = after
                continue

            if decision.action is RecoveryAction.RE_OBSERVE:
                state.recoveries += 1
                await asyncio.sleep(0.2)
                before = await self._observe(state)
                continue

            if decision.action is RecoveryAction.REPLAN:
                revised = await self.planner.replan(
                    state.goal,
                    after,
                    plan,
                    decision.reason,
                    history=working.recent_steps_text(),
                )
                if revised is None or not revised.steps:
                    return False, "failed", f"recovery could not find another way: {decision.reason}"
                revised.revision = plan.revision + 1
                plan = revised
                state.plan = plan
                replans_used += 1
                state.replans += 1
                working.replans += 1
                failures.clear()
                before = after
                await self._emit(
                    "plan_revised",
                    plan=plan.to_prompt(),
                    reasoning=plan.reasoning,
                    revision=plan.revision,
                    trigger=decision.reason,
                    reason=decision.reason,
                    step_count=len(plan.steps),
                )
                continue

            state.recoveries += 1
            return False, "failed", decision.reason

        return False, "max_steps", f"reached the {step_budget}-step limit without finishing"

    # -------------------------------------------------------------- helpers
    async def _observe(self, state: AgentState) -> GUIState:
        state.phase = Phase.OBSERVING
        observation = await self.perception.observe()
        state.current_state = observation
        await self._emit(
            "observation",
            active_window=observation.active_window,
            elements=len(observation.elements),
            text_chars=len(observation.text),
            screenshot=observation.screenshot_path,
            signature=observation.signature(),
            summary=self._observation_summary(observation),
        )
        return observation

    async def _settle(self) -> None:
        delay = float(self.settings.step_settle_seconds)
        if self.settings.dry_run:
            delay = min(delay, 0.05)
        if delay > 0:
            await asyncio.sleep(delay)

    def _ground(
        self, tool: str, arguments: dict[str, Any], gui_state: GUIState | None
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Resolve a symbolic element label into screen coordinates.

        The tool declaration decides which parameters receive the center
        (``x``/``y`` for most mouse tools, ``from_x``/``from_y`` for drag).
        """
        args = dict(arguments or {})
        label: str | None = None
        label_key: str | None = None
        for key in GROUNDING_KEYS:
            if key not in args:
                continue
            value = args.pop(key)
            if not isinstance(value, str) or not value.strip():
                return None, f"argument '{key}' must be a non-empty string"
            if label is None:
                label = value.strip()
                label_key = key
        if label is None:
            return args, None

        spec = self.registry.get(tool)
        if spec is None:
            return None, f"unknown tool '{tool}'"
        if not getattr(spec, "supports_grounding", False):
            return None, f"argument '{label_key}' is not supported by tool '{tool}'"

        elements = getattr(gui_state, "elements", []) or []
        element = find_element(elements, label)
        if element is None:
            return None, f"element '{label}' not found on screen"
        x, y = element.center

        # The screenshot and the input backend should use the same pixel
        # space (DPI awareness is enabled for the process). If they still
        # disagree, scale the grounded point into input coordinates.
        if not self.settings.dry_run and gui_state is not None:
            screen_w, screen_h = get_screen_size()
            source_w = int(getattr(gui_state, "width", 0) or 0)
            source_h = int(getattr(gui_state, "height", 0) or 0)
            if source_w and source_h and (source_w, source_h) != (screen_w, screen_h):
                x = x * screen_w / source_w
                y = y * screen_h / source_h

        for name in getattr(spec, "position_params", ("x", "y")):
            args[name] = int(round(x if name.endswith("x") else y))
        logger.debug(
            "grounded %r to %s for %s",
            label,
            {name: args[name] for name in getattr(spec, "position_params", ("x", "y"))},
            tool,
        )
        return args, None

    @staticmethod
    def _observation_summary(observation: GUIState) -> str:
        description = (observation.description or "").strip()
        if description:
            return f"{observation.active_window}: {description}"
        snippet = " ".join((observation.text or "").split())[:140]
        return f"{observation.active_window}: {snippet}"

    def _memory_hints(self, goal: str) -> str:
        if not self.settings.enable_learning:
            return ""
        try:
            return self.optimizer.prompt_hints(goal)
        except Exception:  # noqa: BLE001
            logger.debug("memory hints failed", exc_info=True)
            return ""

    def _reset_llm_counters(self) -> None:
        if self.llm is None:
            return
        self.llm.calls = 0
        self.llm.prompt_tokens = 0
        self.llm.completion_tokens = 0
        self.llm.errors = 0

    async def _learn(
        self,
        state: AgentState,
        working: WorkingMemory,
        plan: Plan,
        *,
        success: bool,
        error: str | None,
        duration_ms: int,
    ) -> str | None:
        if not self.settings.enable_learning or not working.steps:
            return None

        lessons = extract_lessons(state.goal, working.steps, success=success, error=error)
        if success:
            reflected = await self.reasoning.reflect(state.goal, working.steps, True)
            for lesson in reflected:
                if lesson not in lessons:
                    lessons.append(lesson)
            for lesson in lessons:
                try:
                    self.semantic.remember_lesson(lesson, goal=state.goal)
                except Exception:  # noqa: BLE001
                    logger.debug("could not store lesson", exc_info=True)
            try:
                self.skills.learn(state.goal, working.steps, success=True)
            except Exception:  # noqa: BLE001
                logger.debug("could not learn skill", exc_info=True)

        episode = self.episodic.save_episode(
            goal=state.goal,
            status=status_for(success),
            success=success,
            task_id=state.task_id,
            trajectory=build_trajectory(working.steps),
            summary=summarize(
                state.goal,
                working.steps,
                success=success,
                duration_ms=duration_ms,
                tokens=self.llm.total_tokens if self.llm is not None else 0,
            ),
            lessons=lessons,
            steps_count=len(working.steps),
            duration_ms=duration_ms,
            tokens=self.llm.total_tokens if self.llm is not None else 0,
            meta={"plan_source": plan.source, "plan_revision": plan.revision},
        )
        return episode.id

    async def _emit(self, event_type: str, **payload: Any) -> None:
        event = {
            "type": event_type,
            "task_id": self.task_id,
            "timestamp": _now(),
            **payload,
        }
        self.events.append(event)
        if self._event_callback is None:
            return
        try:
            outcome = self._event_callback(event)
            if inspect.isawaitable(outcome):
                await outcome  # type: ignore[func-returns-value]
        except Exception:  # noqa: BLE001
            logger.exception("event callback failed for %s", event_type)


def status_for(success: bool) -> str:
    return "completed" if success else "failed"
