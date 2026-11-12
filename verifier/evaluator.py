"""Task evaluation - did we actually finish the job?

Step-level checks answer "did this click work"; task evaluation answers "is the
user's goal satisfied".  Criteria are extracted from the goal (quoted strings,
app names after "open", file names, typed text) and matched against the final
GUI state; a model judge is consulted only when the rules cannot decide.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from config.settings import Settings

if TYPE_CHECKING:  # pragma: no cover
    from perception.vision import GUIState

logger = logging.getLogger("visionpilot.verifier.evaluator")

_OPEN_VERBS = r"(?:open|launch|start|run|打开|启动|运行|进入)"
_TYPE_VERBS = r"(?:type|enter|write|fill|输入|键入|填写|写入)"
_FILE_RE = re.compile(r"[A-Za-z0-9_\-]+\.(?:txt|md|json|csv|log|py|html|png|jpg|pdf|docx|xlsx)")
_QUOTE_RE = re.compile(r"[\"'“”`]([^\"'“”`]{2,80})[\"'“”`]")

# App names appear in Chinese in goals but in English in window titles.
_APP_ALIASES = {
    "文件资源管理器": "file explorer",
    "资源管理器": "file explorer",
    "文件管理器": "file explorer",
    "记事本": "notes",
    "便签": "notes",
    "设置": "settings",
    "文档": "documents",
    "图片": "pictures",
    "桌面": "desktop",
}


def _canonical(text: str) -> str:
    lowered = (text or "").lower()
    for alias, canonical in _APP_ALIASES.items():
        lowered = lowered.replace(alias, canonical)
    return lowered



@dataclass
class Criterion:
    kind: str
    value: str
    matched: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "value": self.value,
            "matched": self.matched,
            "note": self.note,
        }


@dataclass
class EvaluationResult:
    success: bool
    confidence: float
    reason: str
    criteria: list[Criterion] = field(default_factory=list)
    checklist: list[dict[str, Any]] = field(default_factory=list)
    judged_by: str = "rules"
    failed_steps: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "criteria": [criterion.as_dict() for criterion in self.criteria],
            "checklist": self.checklist,
            "judged_by": self.judged_by,
            "failed_steps": self.failed_steps,
        }


class TaskEvaluator:
    """Decide whether the goal is satisfied by the final observation."""

    def __init__(self, settings: Settings, llm: Any | None = None) -> None:
        self.settings = settings
        self.llm = llm

    async def evaluate(
        self,
        goal: str,
        state: "GUIState | None",
        steps: list[Any] | None = None,
        plan: list[dict[str, Any]] | None = None,
    ) -> EvaluationResult:
        steps = steps or []
        plan = plan or []
        criteria = self.extract_criteria(goal)
        if state is not None:
            self._match_criteria(criteria, state)
            self._match_history(criteria, steps)

        failed_steps = 0
        failed_keys: set[Any] = set()
        for step in steps:
            ok = getattr(step, "ok", None)
            if ok is None and isinstance(step, dict):
                ok = step.get("ok", True)
            if ok is False:
                # A retried plan step is recorded once per attempt; count the
                # plan step once instead of reporting every failed attempt.
                key = getattr(step, "planned_step", None)
                if key is None and isinstance(step, dict):
                    key = step.get("planned_step")
                if not key:
                    key = getattr(step, "index", None)
                if key is None and isinstance(step, dict):
                    key = step.get("index")
                failed_keys.add(key if key is not None else id(step))
        failed_steps = len(failed_keys)

        checklist = self._build_checklist(plan, steps)
        unfinished = [item for item in checklist if item.get("status") not in {"done", "skipped", "success"}]

        mandatory = [criterion for criterion in criteria if criterion.kind in {"app", "text", "file"}]
        missing = [criterion for criterion in mandatory if not criterion.matched]

        if state is None:
            result = EvaluationResult(
                success=False,
                confidence=0.6,
                reason="no final observation was captured",
                criteria=criteria,
                checklist=checklist,
                failed_steps=failed_steps,
            )
        elif mandatory and not missing and failed_steps == 0:
            result = EvaluationResult(
                success=True,
                confidence=0.88,
                reason="all goal criteria are visible in the final screen",
                criteria=criteria,
                checklist=checklist,
                failed_steps=0,
            )
        elif mandatory and missing:
            result = EvaluationResult(
                success=False,
                confidence=0.7,
                reason="missing criteria: " + ", ".join(item.value for item in missing),
                criteria=criteria,
                checklist=checklist,
                failed_steps=failed_steps,
            )
        elif failed_steps == 0 and steps and not unfinished:
            result = EvaluationResult(
                success=True,
                confidence=0.6,
                reason="all planned steps completed without failures",
                criteria=criteria,
                checklist=checklist,
            )
        elif failed_steps > 0:
            result = EvaluationResult(
                success=False,
                confidence=0.65,
                reason=f"{failed_steps} step{'s' if failed_steps != 1 else ''} failed",
                criteria=criteria,
                checklist=checklist,
                failed_steps=failed_steps,
            )
        else:
            result = EvaluationResult(
                success=bool(steps),
                confidence=0.4,
                reason="goal is underspecified; relying on step results",
                criteria=criteria,
                checklist=checklist,
                failed_steps=failed_steps,
            )

        if (
            result.confidence < 0.6
            and self.settings.enable_llm_judge
            and self.llm is not None
            and state is not None
        ):
            judged = await self._llm_judge(goal, state, steps, result)
            if judged is not None:
                return judged
        return result

    # ------------------------------------------------------------- criteria
    def extract_criteria(self, goal: str) -> list[Criterion]:
        criteria: list[Criterion] = []
        seen: set[tuple[str, str]] = set()

        def add(kind: str, value: str) -> None:
            value = " ".join(value.strip().strip("\"'“”`").split())
            if kind == "app":
                # "open the Notes app and type ..." -> "Notes app"
                value = re.split(r"\s+(?:and|then)\s+", value, maxsplit=1, flags=re.I)[0]
                value = re.split(r"(?:输入|键入|填写|写入|点击|双击|保存)", value, maxsplit=1)[0]
            value = re.sub(r"\s+(?:app|application|window|folder|file|程序|窗口|文件夹)$", "", value, flags=re.I)
            value = re.sub(r"^(?:the|a|an|this|that|my|the current)\s+", "", value, flags=re.I)
            value = value.strip(" .。,，")
            if len(value) < 2 or value.lower() in {"the", "this", "that", "a", "an", "it"}:
                return
            key = (kind, value.lower())
            if key in seen:
                return
            seen.add(key)
            criteria.append(Criterion(kind=kind, value=value))

        for match in _QUOTE_RE.finditer(goal or ""):
            value = match.group(1).strip()
            if value:
                add("text", value)

        for match in re.finditer(_OPEN_VERBS + r"\s*([A-Za-z0-9_\- \u4e00-\u9fff]{2,40})", goal or "", re.I):
            add("app", match.group(1))

        for match in re.finditer(_TYPE_VERBS + r"\s*(.+)", goal or "", re.I):
            fragment = match.group(1)
            fragment = re.split(
                r"\s+(?:into|in|to|and then|then|并|然后)\s+|然后|接着|最后|"
                r"并(?=保存|打开|关闭|点击|输入|键入|填写|写入)",
                fragment,
                maxsplit=1,
                flags=re.I,
            )[0]
            add("text", fragment[:80])

        for match in _FILE_RE.finditer(goal or ""):
            add("file", match.group(0))
        return criteria

    def _match_criteria(self, criteria: list[Criterion], state: "GUIState") -> None:
        haystack = _canonical(
            "\n".join(
                [
                    state.active_window,
                    state.text,
                    " ".join(element.label for element in state.elements),
                    state.description,
                ]
            )
        )
        for criterion in criteria:
            needle = _canonical(criterion.value)
            matched = needle in haystack
            if not matched and criterion.kind == "app":
                # "Notes app" -> "notes"; also accept window title fragments.
                first = needle.split()[0] if needle.split() else needle
                matched = first in _canonical(state.active_window)
            criterion.matched = matched
            if not matched:
                criterion.note = "not found on the final screen"

    def _match_history(self, criteria: list[Criterion], steps: list[Any]) -> None:
        """Accept app/file criteria observed earlier.

        A goal like "enter Documents and open notes.txt" legitimately moves away
        from the Documents folder, so the final screen no longer shows it.  The
        folder was still visited; the action history is the evidence.
        """

        parts: list[str] = []
        for step in steps:
            if isinstance(step, dict):
                parts.append(str(step.get("planned_step") or ""))
                parts.append(str(step.get("tool") or ""))
                parts.append(str(step.get("observation") or ""))
            else:
                parts.append(str(getattr(step, "planned_step", "") or ""))
                parts.append(str(getattr(step, "tool", "") or ""))
                parts.append(str(getattr(step, "observation", "") or ""))
        history = _canonical("\n".join(parts))
        if not history:
            return
        for criterion in criteria:
            if criterion.matched or criterion.kind not in {"app", "file"}:
                continue
            needle = _canonical(criterion.value)
            if needle in history or (
                criterion.kind == "app"
                and needle.split()
                and needle.split()[0] in history
            ):
                criterion.matched = True
                criterion.note = "observed earlier in the task"

    def _build_checklist(
        self,
        plan: list[dict[str, Any]],
        steps: list[Any],
    ) -> list[dict[str, Any]]:
        checklist: list[dict[str, Any]] = []
        if plan:
            for index, item in enumerate(plan):
                status = item.get("status") or ("done" if index < len(steps) else "pending")
                checklist.append(
                    {
                        "label": item.get("description") or item.get("action") or f"step {index + 1}",
                        "status": status,
                    }
                )
            return checklist
        for index, step in enumerate(steps):
            ok = getattr(step, "ok", None)
            if ok is None and isinstance(step, dict):
                ok = step.get("ok", True)
            checklist.append(
                {
                    "label": str(getattr(step, "tool", None) or (step.get("tool") if isinstance(step, dict) else "") or f"step {index + 1}"),
                    "status": "done" if ok else "failed",
                }
            )
        return checklist

    # ---------------------------------------------------------------- judge
    async def _llm_judge(
        self,
        goal: str,
        state: "GUIState",
        steps: list[Any],
        base: EvaluationResult,
    ) -> EvaluationResult | None:
        history = []
        for step in steps[-8:]:
            tool = getattr(step, "tool", None) or (step.get("tool") if isinstance(step, dict) else "")
            observation = getattr(step, "observation", None) or (step.get("observation") if isinstance(step, dict) else "")
            history.append(f"- {tool}: {str(observation)[:120]}")
        prompt = (
            "Judge whether a desktop GUI agent completed the user's goal.\n"
            f"GOAL: {goal}\n"
            f"FINAL SCREEN:\n{state.to_prompt(1400)}\n"
            f"RECENT ACTIONS:\n" + "\n".join(history) + "\n"
            f"RULE-BASED RESULT: success={base.success} confidence={base.confidence} reason={base.reason}\n"
            'Answer ONLY JSON: {"success": true|false, "confidence": 0.0-1.0, "reason": "one sentence"}'
        )
        try:
            raw = await self.llm.complete(prompt, system="You are a strict GUI task evaluator.")  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM task evaluation failed: %s", exc)
            return None
        payload = raw if isinstance(raw, dict) else _extract_json(str(raw))
        if not isinstance(payload, dict) or "success" not in payload:
            return None
        try:
            confidence = max(0.0, min(1.0, float(payload.get("confidence", 0.7))))
        except (TypeError, ValueError):
            confidence = 0.7
        return EvaluationResult(
            success=bool(payload.get("success")),
            confidence=confidence,
            reason=str(payload.get("reason") or "model judgement"),
            criteria=base.criteria,
            checklist=base.checklist,
            judged_by="llm",
            failed_steps=base.failed_steps,
        )


def _extract_json(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
