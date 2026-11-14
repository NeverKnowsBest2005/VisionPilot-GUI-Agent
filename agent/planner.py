"""Planner: turn a goal into an ordered list of concrete tool calls.

Two planners share one interface:

* the LLM planner asks the model for a plan (used when a key is configured);
* the heuristic planner recognises the common demo intents so the project
  works without a key, and is always the safety net when the model fails.

Both produce :class:`~agent.state.Plan` objects, so the rest of the agent does
not care where a plan came from.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from agent.state import Plan, PlanStep
from config.settings import Settings

logger = logging.getLogger("visionpilot.agent.planner")

# Demo applications that the synthetic desktop knows about.
DEMO_APPS = ("File Explorer", "Notes", "Settings")

# Aliases across English and Chinese, lower-cased for matching.
ALIASES = {
    "文件资源管理器": "File Explorer",
    "资源管理器": "File Explorer",
    "文件管理器": "File Explorer",
    "file explorer": "File Explorer",
    "explorer": "File Explorer",
    "记事本": "Notes",
    "笔记": "Notes",
    "便签": "Notes",
    "notes": "Notes",
    "notepad": "Notes",
    "设置": "Settings",
    "settings": "Settings",
    "preferences": "Settings",
    "桌面": "Desktop",
    "desktop": "Desktop",
    "文档": "Documents",
    "documents": "Documents",
    "图片": "Pictures",
    "pictures": "Pictures",
    "照片": "Pictures",
}

_OPEN_VERB = re.compile(
    r"\b(open|launch|start|run|show)\b|打开|启动|运行|进入|切换到|展示", re.IGNORECASE
)
_CLICK_VERB = re.compile(
    r"\b(click|double[- ]?click|press|tap)\b|点击|双击|单击|点一下", re.IGNORECASE
)
_SAVE_VERB = re.compile(r"\bsave\b|\bctrl\s*\+\s*s\b|保存|存档|写盘", re.IGNORECASE)
_SCROLL_VERB = re.compile(r"\bscroll\b|滚动|下滑|翻页", re.IGNORECASE)
_TYPE_VERB = re.compile(
    r"\b(?:type|enter|write|input|fill|put)\s+(?:in\s+)?[\"“”']?(?P<text>[^\"“”'\n]+)|"
    r"(?:输入|键入|填写|写入|打字)[：:\s]*[\"“”']?(?P<text2>[^\"“”'\n]+)",
    re.IGNORECASE,
)
_QUOTED = re.compile(r"[\"“'‘`]([^\"“”'’`]{1,200})[\"”'’`]")
_FILE_RE = re.compile(r"[A-Za-z0-9_\-]+\.(?:txt|md|json|csv|log|py|html|png|jpg|pdf|docx|xlsx)")
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

_TRAILING_NOISE = re.compile(
    r"\s*(?:文件|文件夹|窗口|程序|应用|app|application|window|folder|file|please|谢谢|吧|。|，|,|\.)$",
    re.IGNORECASE,
)


class Planner:
    """Turns goals into plans, with an LLM path and a deterministic path."""

    def __init__(self, settings: Settings, reasoning: Any, registry: Any) -> None:
        self.settings = settings
        self.reasoning = reasoning
        self.registry = registry

    # ------------------------------------------------------------------ api
    async def create_plan(self, goal: str, state: Any, hints: str = "") -> Plan:
        payload: dict[str, Any] | None = None
        if self.settings.planner_mode != "heuristic" and getattr(self.reasoning, "available", False):
            payload = await self.reasoning.propose_plan(
                goal, state, self.registry.prompt_catalog(), hints=hints
            )
            plan = self._plan_from_payload(payload, goal, source="llm")
            if plan is not None and plan.steps:
                problems = self.validate_plan(plan)
                if problems:
                    logger.info("LLM plan discarded: %s", "; ".join(problems))
                else:
                    return plan

        plan = self.heuristic_plan(goal, state)
        if not plan.steps:
            logger.info("no heuristic plan for goal: %s", goal)
        return plan

    async def replan(
        self,
        goal: str,
        state: Any,
        plan: Plan,
        failure: str,
        history: str = "",
    ) -> Plan | None:
        if self.settings.planner_mode != "heuristic" and getattr(self.reasoning, "available", False):
            payload = await self.reasoning.revise_plan(
                goal, state, plan, failure, self.registry.prompt_catalog(), history=history
            )
            revised = self._plan_from_payload(payload, goal, source="llm")
            if revised is not None and revised.steps:
                problems = self.validate_plan(revised)
                if problems:
                    logger.info("revised LLM plan discarded: %s", "; ".join(problems))
                else:
                    return revised

        return self._heuristic_revision(goal, state, plan, failure)

    # ----------------------------------------------------------- validation
    def validate_plan(self, plan: Plan) -> list[str]:
        problems: list[str] = []
        if not plan.steps:
            problems.append("plan has no steps")
        if len(plan.steps) > self.settings.max_steps:
            problems.append(f"plan has {len(plan.steps)} steps (max {self.settings.max_steps})")
        for index, step in enumerate(plan.steps, start=1):
            tool = self.registry.get(step.tool) if step.tool else None
            if not step.tool:
                problems.append(f"step {index} has no tool")
                continue
            if tool is None:
                problems.append(f"step {index} uses unknown tool '{step.tool}'")
                continue
            if not tool.is_available():
                problems.append(f"step {index} tool '{step.tool}' is unavailable")
            try:
                tool.validate_arguments(dict(step.arguments or {}), allow_grounding=True)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"step {index} arguments invalid: {exc}")
        return problems

    # ----------------------------------------------------- payload → plan
    def _plan_from_payload(
        self, payload: dict[str, Any] | None, goal: str, source: str
    ) -> Plan | None:
        if not isinstance(payload, dict):
            return None
        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list):
            return None
        steps: list[PlanStep] = []
        dropped = 0
        for item in raw_steps[: self.settings.max_steps + 1]:
            if isinstance(item, PlanStep):
                steps.append(item)
                continue
            if not isinstance(item, dict):
                dropped += 1
                continue
            tool = str(item.get("tool") or "").strip()
            if not tool and item.get("action") in self.registry.names():
                tool = str(item["action"])
            if tool not in self.registry.names():
                dropped += 1
                continue
            arguments = item.get("arguments") or item.get("args") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            expect = item.get("expect") or item.get("expectation") or item.get("verify")
            description = str(item.get("description") or item.get("step") or "").strip()
            if not description:
                description = f"{tool} {arguments}"
            steps.append(
                PlanStep(
                    description=description,
                    tool=tool,
                    arguments=arguments,
                    expect=expect if isinstance(expect, dict) else None,
                )
            )
        if dropped:
            # A plan with silently removed steps is a different plan; fall back
            # to the heuristic planner instead of executing a truncated one.
            logger.info("LLM plan discarded: %d unusable step(s)", dropped)
            return None
        if not steps:
            return None
        return Plan(
            goal=goal,
            steps=steps,
            reasoning=str(payload.get("reasoning") or "").strip(),
            source=source,
        )

    # ---------------------------------------------------------- heuristics
    def heuristic_plan(self, goal: str, state: Any) -> Plan:
        text = (goal or "").strip()
        if not text:
            return Plan(goal=goal, steps=[], reasoning="empty goal", source="heuristic")

        steps: list[PlanStep] = []
        active_window = (getattr(state, "active_window", "") or "").lower()
        labels = self._visible_labels(state)

        app_target = self._match_app(text, labels)
        file_target = self._match_file(text, labels)
        folder_target = self._match_folder(text, labels)
        typed_text = self._extract_text(text)
        wants_open = bool(_OPEN_VERB.search(text))
        wants_click = bool(_CLICK_VERB.search(text))
        wants_save = bool(_SAVE_VERB.search(text))
        wants_scroll = bool(_SCROLL_VERB.search(text))

        # 1. Open the target application.
        if app_target and (wants_open or wants_click) and app_target.lower() not in active_window:
            steps.append(
                PlanStep(
                    description=f"双击图标打开 {app_target}",
                    tool="mouse_double_click",
                    arguments={"element": app_target},
                    expect={"window": app_target},
                )
            )

        # 2. Launch an installed application that is not part of the demo desktop.
        if wants_open and not app_target and not file_target and not folder_target and not typed_text:
            launch_target = self._extract_app_name(text)
            if launch_target and launch_target.casefold() not in active_window:
                steps.append(
                    PlanStep(
                        description=f"启动应用 {launch_target}",
                        tool="system_launch_app",
                        arguments={"name": launch_target, "wait": 2.0},
                        expect={"window": launch_target},
                    )
                )

        # 3. Open a file (through the file explorer when it is not on screen).
        if file_target:
            if not self._explorer_visible(state, active_window) and not self._explorer_opening(steps):
                steps.append(self._open_explorer_step())
            steps.append(
                PlanStep(
                    description=f"在文件管理器中打开 {file_target}",
                    tool="mouse_click",
                    arguments={"element": file_target},
                    expect={"text": file_target},
                )
            )

        # 4. Browse into a folder.
        elif folder_target and (wants_open or wants_click or folder_target.lower() in text.lower()):
            if not self._explorer_visible(state, active_window) and not self._explorer_opening(steps):
                steps.append(self._open_explorer_step())
            steps.append(
                PlanStep(
                    description=f"进入文件夹 {folder_target}",
                    tool="mouse_click",
                    arguments={"element": folder_target},
                    expect={"window": folder_target},
                )
            )

        # 5. Type text (an editor must be in front, otherwise open Notes first).
        if typed_text:
            if "notes" not in active_window and not any(
                "notes" in (step.arguments or {}).get("element", "").lower() for step in steps
            ):
                steps.append(
                    PlanStep(
                        description="打开记事本作为输入框",
                        tool="mouse_double_click",
                        arguments={"element": "Notes"},
                        expect={"window": "Notes"},
                    )
                )
            steps.append(
                PlanStep(
                    description=f"输入文本：{_short(typed_text)}",
                    tool="keyboard_type",
                    arguments={"text": typed_text},
                    expect={"text": typed_text},
                )
            )
            if wants_save:
                steps.append(
                    PlanStep(
                        description="保存（Ctrl+S）",
                        tool="keyboard_press",
                        arguments={"keys": ["ctrl", "s"]},
                        expect={"change": True},
                    )
                )

        # 6. Scroll the current view.
        if wants_scroll and not typed_text:
            steps.append(
                PlanStep(
                    description="向下滚动当前视图",
                    tool="mouse_scroll",
                    arguments={"clicks": -3},
                    expect={"change": True},
                )
            )

        # 7. Fallback: click the visible element the goal names.
        if not steps:
            element = self._match_element(text, state)
            if element is not None:
                steps.append(
                    PlanStep(
                        description=f"点击界面元素 {element}",
                        tool="mouse_click",
                        arguments={"element": element},
                        expect={"change": True},
                    )
                )

        # 7. Fallback: URL goals go to the browser tool.
        url_match = _URL_RE.search(text)
        if not steps and url_match:
            steps.append(
                PlanStep(
                    description=f"在浏览器中打开 {url_match.group(0)}",
                    tool="browser_open",
                    arguments={"url": url_match.group(0)},
                    expect={"change": True},
                )
            )

        reasoning = self._rationale(
            app_target, file_target, folder_target, typed_text, wants_save, wants_scroll
        )
        return Plan(goal=goal, steps=steps, reasoning=reasoning, source="heuristic")

    def _heuristic_revision(
        self, goal: str, state: Any, plan: Plan, failure: str
    ) -> Plan | None:
        """A conservative second attempt: retry once for UI timing, else give up."""
        if "not found" in failure.lower() or "could not find" in failure.lower():
            # The element may simply not be on screen: re-observe is handled by the
            # recovery manager, so a fresh plan from the new state is the useful move.
            revised = self.heuristic_plan(goal, state)
            if revised.steps:
                revised.reasoning = f"重新观察后重试：{failure[:160]}"
                revised.revision = plan.revision + 1
                return revised
        if "unavailable" in failure.lower() or "denied" in failure.lower():
            return None
        # Generic retry: keep the unfinished steps.
        remaining = [
            PlanStep(**step.as_dict()) for step in plan.steps if step.status in ("pending", "failed")
        ]
        if not remaining:
            return None
        return Plan(
            goal=goal,
            steps=remaining,
            reasoning=f"重试失败步骤：{failure[:160]}",
            source="heuristic",
            revision=plan.revision + 1,
        )

    # ------------------------------------------------------------- helpers
    def _visible_labels(self, state: Any) -> list[str]:
        if state is None:
            return []
        if hasattr(state, "element_labels"):
            return [label for label in state.element_labels() if label]
        return []

    def _match_app(self, text: str, labels: list[str]) -> str | None:
        lowered = text.lower()
        candidates = list(DEMO_APPS) + labels
        for alias, canonical in ALIASES.items():
            if alias in lowered and canonical in candidates:
                return canonical
        for app in DEMO_APPS:
            if app.lower() in lowered:
                return app
        # Any visible icon label mentioned verbatim in the goal.
        for label in labels:
            if label.lower() in lowered and label in DEMO_APPS:
                return label
        return None

    def _match_file(self, text: str, labels: list[str]) -> str | None:
        for match in _FILE_RE.finditer(text):
            name = match.group(0)
            for label in labels:
                if label.lower() == name.lower():
                    return label
            return name
        return None

    def _match_folder(self, text: str, labels: list[str]) -> str | None:
        lowered = text.lower()
        for alias, canonical in (("文档", "Documents"), ("documents", "Documents"),
                                 ("图片", "Pictures"), ("pictures", "Pictures")):
            if alias in lowered:
                return canonical
        for label in labels:
            if label.lower() in lowered and label in ("Documents", "Pictures", "Up"):
                return "Up" if label == "< Up" else label
        return None

    def _match_element(self, text: str, state: Any) -> str | None:
        labels = self._visible_labels(state)
        lowered = text.lower()
        best: tuple[int, str] | None = None
        for label in labels:
            needle = label.lower()
            if needle and needle in lowered:
                score = len(needle)
                if best is None or score > best[0]:
                    best = (score, label)
        if best is not None:
            return best[1]
        # Single salient word from the goal matching a label.
        words = [word for word in re.split(r"\W+", lowered) if len(word) > 2]
        for word in words:
            for label in labels:
                if word in label.lower():
                    return label
        return None

    def _extract_text(self, text: str) -> str | None:
        quoted = [part.strip() for part in _QUOTED.findall(text) if part.strip()]
        if quoted:
            # A quoted fragment that is a bare filename is a file, not text.
            for part in quoted:
                if not _FILE_RE.fullmatch(part):
                    return part
        match = _TYPE_VERB.search(text)
        if match:
            raw = match.group("text") or match.group("text2") or ""
            return _clean_text(raw)
        return None

    def _extract_app_name(self, text: str) -> str | None:
        """Pull a launchable application name out of an "open X" instruction."""
        match = _OPEN_VERB.search(text)
        if not match:
            return None
        tail = text[match.end():].strip(" \t:：,，。.、")
        tail = _clean_text(tail)
        tail = re.sub(r"^(?:the|a|an)\s+", "", tail, flags=re.IGNORECASE)
        tail = re.sub(r"\s*(?:app|application|程序|应用|软件)$", "", tail, flags=re.IGNORECASE)
        tail = tail.strip(" .,，。的")
        if not tail or len(tail) > 40:
            return None
        if _FILE_RE.fullmatch(tail) or _URL_RE.search(tail):
            return None
        return tail

    def _explorer_visible(self, state: Any, active_window: str) -> bool:
        labels = self._visible_labels(state)
        if "file explorer" in active_window:
            return True
        return any(label in ("Documents", "notes.txt", "< Up") for label in labels)

    @staticmethod
    def _explorer_opening(steps: list[PlanStep]) -> bool:
        return any(
            step.tool == "mouse_double_click"
            and str((step.arguments or {}).get("element", "")).lower() == "file explorer"
            for step in steps
        )

    def _open_explorer_step(self) -> PlanStep:
        return PlanStep(
            description="打开文件资源管理器",
            tool="mouse_double_click",
            arguments={"element": "File Explorer"},
            expect={"window": "File Explorer"},
        )

    @staticmethod
    def _rationale(
        app: str | None,
        file: str | None,
        folder: str | None,
        text: str | None,
        save: bool,
        scroll: bool,
    ) -> str:
        parts: list[str] = []
        if app:
            parts.append(f"目标应用 {app}")
        if file:
            parts.append(f"目标文件 {file}")
        if folder:
            parts.append(f"目标目录 {folder}")
        if text:
            parts.append(f"需要输入文本 {text[:40]!r}")
        if save:
            parts.append("需要保存")
        if scroll:
            parts.append("需要滚动")
        if not parts:
            return "未能从目标中识别出可执行步骤，只能尝试点击最相关的界面元素。"
        return "启发式规划：" + "，".join(parts) + "。"


def _short(text: str, limit: int = 40) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clean_text(raw: str) -> str:
    cleaned = raw.strip().strip("\"'“”‘’`")
    cleaned = re.split(
        r"\s+(?:into|in|to|and then|then|on)\s+|然后|接着|最后|"
        r"并(?=保存|打开|关闭|点击|输入|键入|填写|写入)|，(?:然后|并|再)",
        cleaned,
        maxsplit=1,
    )[0]
    cleaned = _TRAILING_NOISE.sub("", cleaned).strip()
    return cleaned[:200]
