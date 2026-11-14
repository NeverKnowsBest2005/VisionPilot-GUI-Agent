"""Step verification: did the action actually do what we expected?

The checker combines cheap deterministic signals (tool success, screen diff,
text appearing/disappearing, window focus) with an optional model judgement
when the rules are inconclusive.  Keeping the deterministic layer first means
the agent still verifies correctly with no API key at all.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from config.settings import Settings

if TYPE_CHECKING:  # pragma: no cover
    from executor.base import ToolResult
    from perception.vision import GUIState

logger = logging.getLogger("visionpilot.verifier.checker")

# Tools whose result cannot be judged from the desktop screenshot.  Their
# structured ``ToolResult`` is authoritative: retrying them because "the screen
# looks identical" duplicates non-idempotent work (a second fs_write append, a
# second browser click, ...).
_NON_VISUAL_TOOLS = {
    "mouse_position",
    "fs_list",
    "fs_read",
    "fs_write",
    "fs_mkdir",
    "fs_move",
    "fs_copy",
    "fs_delete",
    "fs_search",
    "fs_info",
    "system_info",
    "system_wait",
    "system_run_command",
    "browser_open",
    "browser_click",
    "browser_fill",
    "browser_press",
    "browser_get_text",
    "browser_screenshot",
    "browser_close",
}


@dataclass
class CheckOutcome:
    status: str = "unknown"  # success | partial | failure | unknown
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    judged_by: str = "rules"

    @property
    def success(self) -> bool | None:
        if self.status == "success":
            return True
        if self.status == "failure":
            return False
        return None

    @property
    def ok(self) -> bool:
        return self.status in {"success", "partial"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "success": self.success,
            "confidence": round(self.confidence, 3),
            "reasons": self.reasons,
            "evidence": self.evidence,
            "judged_by": self.judged_by,
        }

    def summary(self) -> str:
        reason = "; ".join(self.reasons[:3]) or "no signals"
        return f"{self.status} ({self.confidence:.2f}) - {reason}"


def _as_expectation(expected: Any) -> dict[str, Any]:
    if expected is None:
        return {}
    if isinstance(expected, str):
        return {"expect": expected}
    if isinstance(expected, dict):
        return dict(expected)
    return {"expect": str(expected)}


def _quoted_parts(text: str) -> list[str]:
    parts: list[str] = []
    for opener, closer in (('"', '"'), ("'", "'"), ("“", "”"), ("`", "`")):
        if opener in text and closer in text:
            chunks = text.split(opener)[1:]
            for chunk in chunks:
                value = chunk.split(closer)[0].strip()
                if len(value) >= 2:
                    parts.append(value)
    return parts


class Checker:
    """Turn observations into a pass/fail judgement for one step."""

    def __init__(self, settings: Settings, llm: Any | None = None) -> None:
        self.settings = settings
        self.llm = llm

    async def check(
        self,
        *,
        before: "GUIState | None",
        after: "GUIState | None",
        action: dict[str, Any],
        expectation: Any = None,
        tool_result: "ToolResult | None" = None,
    ) -> CheckOutcome:
        outcome = self._rule_based(before, after, action, expectation, tool_result)
        if outcome.status in {"success", "failure"} and outcome.confidence >= 0.55:
            return outcome
        if (
            self.settings.enable_llm_judge
            and self.llm is not None
            and after is not None
            and outcome.status != "failure"
        ):
            judged = await self._llm_judge(before, after, action, expectation)
            if judged is not None:
                return judged
        return outcome

    # ------------------------------------------------------------- rule based
    def _rule_based(
        self,
        before: "GUIState | None",
        after: "GUIState | None",
        action: dict[str, Any],
        expectation: Any,
        tool_result: "ToolResult | None",
    ) -> CheckOutcome:
        expect = _as_expectation(expectation)
        reasons: list[str] = []
        evidence: dict[str, Any] = {"expectation": expect}

        tool_ok = True
        if tool_result is not None:
            evidence["tool_ok"] = tool_result.ok
            evidence["tool"] = tool_result.tool
            if not tool_result.ok:
                tool_ok = False
                reasons.append(f"tool '{tool_result.tool}' failed: {tool_result.error or 'unknown error'}")
                return CheckOutcome(
                    status="failure",
                    confidence=0.95,
                    reasons=reasons,
                    evidence=evidence,
                )

        if after is None:
            if expect:
                reasons.append("no observation after the action to verify against")
                return CheckOutcome(status="unknown", confidence=0.3, reasons=reasons, evidence=evidence)
            return CheckOutcome(
                status="success" if tool_ok else "failure",
                confidence=0.5 if tool_ok else 0.9,
                reasons=reasons + ["no visual verification available; trusting tool result"],
                evidence=evidence,
            )

        changed = before is not None and before.signature() != after.signature()
        new_text = _text_delta(before, after)
        lost_text = _text_delta(after, before)
        window_changed = bool(
            before is not None and before.active_window.strip() != after.active_window.strip()
        )
        evidence.update(
            {
                "screen_changed": changed,
                "window_changed": window_changed,
                "new_text": new_text[:5],
                "lost_text": lost_text[:5],
                "active_window": after.active_window,
            }
        )

        tool_name = str(action.get("tool") or "")
        if tool_ok and tool_name in _NON_VISUAL_TOOLS and not expect:
            # With no explicit expectation, a non-visual tool's own result is
            # the best evidence available.  An explicit expectation still has
            # to be checked below: silently skipping it would mark unverified
            # work as done.
            reasons.append(
                f"tool '{tool_name}' succeeded; its effect is not reliably visible on screen"
            )
            return CheckOutcome("success", 0.8, reasons, evidence)

        # Explicit expected text.
        text_want = expect.get("text") or expect.get("contains") or expect.get("expect_text")
        if isinstance(text_want, list):
            text_want = " ".join(str(part) for part in text_want)
        if text_want:
            needle = str(text_want).strip()
            if after.has_text(needle):
                reasons.append(f"expected text '{needle}' is visible")
                return CheckOutcome("success", 0.92, reasons, evidence)
            # quoted fragments may still match
            parts = _quoted_parts(needle) or [needle]
            matched = [part for part in parts if after.has_text(part)]
            if matched and len(matched) == len(parts):
                reasons.append(f"expected text fragments visible: {matched}")
                return CheckOutcome("success", 0.8, reasons, evidence)
            reasons.append(f"expected text '{needle}' not visible")
            if not changed:
                reasons.append("screen did not change either")
                return CheckOutcome("failure", 0.8, reasons, evidence)
            return CheckOutcome("partial", 0.45, reasons, evidence)

        # Explicit expected window.
        window_want = expect.get("window") or expect.get("window_title")
        if window_want:
            needle = str(window_want).strip()
            if needle.lower() in (after.active_window or "").lower():
                reasons.append(f"window '{needle}' is focused")
                return CheckOutcome("success", 0.9, reasons, evidence)
            reasons.append(f"window '{needle}' is not focused (got '{after.active_window}')")
            return CheckOutcome("failure" if not changed else "partial", 0.7, reasons, evidence)

        # Explicitly forbidden text.
        absent = expect.get("absent") or expect.get("not_text")
        if absent:
            needle = str(absent)
            if not after.has_text(needle):
                reasons.append(f"text '{needle}' is gone as expected")
                return CheckOutcome("success", 0.85, reasons, evidence)
            reasons.append(f"text '{needle}' is still visible")
            return CheckOutcome("failure", 0.85, reasons, evidence)

        # Generic "something should change".
        if expect.get("change") is True or expect.get("screen_changed") is True:
            if changed:
                reasons.append("screen changed as expected")
                return CheckOutcome("success", 0.75, reasons, evidence)
            reasons.append("screen did not change")
            return CheckOutcome("failure", 0.75, reasons, evidence)

        # Free-text expectation: look for meaningful keywords, quoted text first.
        raw = expect.get("expect") or expect.get("description") or expect.get("note")
        if raw:
            raw = str(raw)
            quoted = _quoted_parts(raw)
            if quoted:
                missing = [part for part in quoted if not after.has_text(part)]
                if not missing:
                    reasons.append(f"quoted text visible: {quoted}")
                    return CheckOutcome("success", 0.85, reasons, evidence)
                reasons.append(f"quoted text missing: {missing}")
            if changed:
                reasons.append("screen changed; free-text expectation assumed met")
                return CheckOutcome("success", 0.6, reasons, evidence)
            reasons.append("screen unchanged and expectation could not be matched")
            return CheckOutcome("unknown", 0.35, reasons, evidence)

        # No explicit expectation: rely on tool success plus a visible change.
        if not tool_ok:
            return CheckOutcome("failure", 0.9, reasons, evidence)
        if changed:
            reasons.append("action succeeded and the screen changed")
            return CheckOutcome("success", 0.7, reasons, evidence)
        reasons.append("action succeeded but the screen looks identical")
        return CheckOutcome("partial", 0.4, reasons, evidence)

    # ---------------------------------------------------------------- LLM
    async def _llm_judge(
        self,
        before: "GUIState | None",
        after: "GUIState",
        action: dict[str, Any],
        expectation: Any,
    ) -> CheckOutcome | None:
        prompt = (
            "You verify whether a desktop GUI agent's action achieved its expectation.\n"
            f"ACTION: {json.dumps(action, ensure_ascii=False)[:600]}\n"
            f"EXPECTATION: {json.dumps(expectation, ensure_ascii=False, default=str)[:400]}\n"
            f"BEFORE: {(before.to_prompt(900) if before else '(no previous state)')}\n"
            f"AFTER: {after.to_prompt(1100)}\n"
            'Answer ONLY JSON: {"status": "success|partial|failure|unknown", '
            '"confidence": 0.0-1.0, "reason": "one sentence"}'
        )
        try:
            raw = await self.llm.complete(prompt, system="You are a strict GUI test verifier.")  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM verification failed: %s", exc)
            return None
        payload = raw
        if isinstance(raw, str):
            payload = _extract_json(raw)
        if not isinstance(payload, dict):
            return None
        status = str(payload.get("status", "unknown")).lower()
        if status not in {"success", "partial", "failure", "unknown"}:
            status = "unknown"
        try:
            confidence = max(0.0, min(1.0, float(payload.get("confidence", 0.6))))
        except (TypeError, ValueError):
            confidence = 0.6
        return CheckOutcome(
            status=status,
            confidence=confidence,
            reasons=[str(payload.get("reason") or "model judgement")],
            evidence={"judged_by": "llm"},
            judged_by="llm",
        )


def _text_delta(first: "GUIState | None", second: "GUIState | None") -> list[str]:
    if first is None or second is None:
        return []
    old = {line.strip().lower() for line in (first.text or "").splitlines() if line.strip()}
    new = {line.strip().lower() for line in (second.text or "").splitlines() if line.strip()}
    return sorted(new - old)


def _extract_json(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def expectations_for(action: dict[str, Any], plan_step: dict[str, Any] | None = None) -> Any:
    """Collect the expectation from a plan step or the action itself."""
    if plan_step:
        for key in ("expect", "expectation", "expected", "verify"):
            if plan_step.get(key):
                return plan_step[key]
    for key in ("expect", "expectation", "expected"):
        if action.get(key):
            return action[key]
    return None
