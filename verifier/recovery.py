"""Recovery strategy: what to do when a step does not work.

The rules are deliberately simple and explainable: retry transient failures,
re-observe when the screen disagrees with our mental model, replan when the
approach itself is wrong, and never retry something that was denied or that
keeps producing no visible change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from config.settings import Settings

logger = logging.getLogger("visionpilot.verifier.recovery")


class RecoveryAction(str, Enum):
    RETRY = "retry"
    RE_OBSERVE = "re_observe"
    REPLAN = "replan"
    ABORT = "abort"


@dataclass
class RecoveryDecision:
    action: RecoveryAction
    reason: str
    delay_seconds: float = 0.0
    context: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "delay_seconds": self.delay_seconds,
            "context": self.context or {},
        }


_TRANSIENT_HINTS = (
    "timeout",
    "timed out",
    "temporarily",
    "busy",
    "not ready",
    "connection",
    "rate limit",
    "try again",
)


class RecoveryManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def decide(
        self,
        *,
        error: str | None = None,
        tool_result: Any | None = None,
        outcome: Any | None = None,
        attempt: int = 0,
        replans_used: int = 0,
        unchanged_count: int = 0,
    ) -> RecoveryDecision:
        """Pick the next move.  ``attempt`` is the number of failures so far."""
        message = (error or "").lower()
        denied = bool(getattr(tool_result, "denied", False))
        available = bool(getattr(tool_result, "available", True))
        status = getattr(outcome, "status", None)

        if denied:
            reason = f"permission denied: {error or 'tool refused the action'}"
            return RecoveryDecision(RecoveryAction.REPLAN, reason)

        if not available:
            return RecoveryDecision(
                RecoveryAction.REPLAN,
                f"backend unavailable: {getattr(tool_result, 'error', None) or error or 'missing dependency'}",
            )

        if "needs administrator" in message or "uac consent prompt" in message:
            # The prompt is on the secure desktop: retrying the same call can
            # never succeed, and it would only burn the step budget.
            return self._replan_or_abort(
                replans_used, f"needs a manual UAC approval: {error}"
            )

        if unchanged_count >= max(1, self.settings.max_consecutive_no_change):
            return RecoveryDecision(
                RecoveryAction.REPLAN,
                f"screen unchanged for {unchanged_count} attempts; the approach is not working",
            )

        if any(hint in message for hint in _TRANSIENT_HINTS):
            if attempt < max(1, self.settings.max_retries_per_step):
                return RecoveryDecision(
                    RecoveryAction.RETRY,
                    f"transient failure, retrying ({attempt + 1})",
                    delay_seconds=min(2.0, 0.4 * (attempt + 1)),
                )
            return self._replan_or_abort(replans_used, f"transient failure persisted: {error}")

        if "not found" in message or "could not find" in message or "no such" in message:
            if attempt == 0:
                return RecoveryDecision(
                    RecoveryAction.RE_OBSERVE,
                    "element not found; re-observing the screen to re-ground",
                )
            return self._replan_or_abort(replans_used, "element still not found after re-observation")

        if status == "failure":
            if attempt < max(1, self.settings.max_retries_per_step):
                return RecoveryDecision(
                    RecoveryAction.RETRY,
                    f"verification failed ({getattr(outcome, 'summary', lambda: '')() if callable(getattr(outcome, 'summary', None)) else 'no details'}); retrying",
                    delay_seconds=0.3,
                )
            return self._replan_or_abort(replans_used, "verification kept failing")

        if attempt < max(1, self.settings.max_retries_per_step):
            return RecoveryDecision(
                RecoveryAction.RETRY,
                f"unexpected failure: {error or 'unknown'}; retrying",
                delay_seconds=0.4,
            )
        return self._replan_or_abort(replans_used, f"unexpected failure persisted: {error or 'unknown'}")

    def _replan_or_abort(self, replans_used: int, reason: str) -> RecoveryDecision:
        if replans_used < max(0, self.settings.max_replans):
            return RecoveryDecision(RecoveryAction.REPLAN, reason)
        return RecoveryDecision(RecoveryAction.ABORT, f"giving up: {reason}")

    def after_verification(
        self,
        outcome: Any,
        *,
        attempt: int,
        replans_used: int,
        unchanged_count: int,
    ) -> RecoveryDecision | None:
        status = getattr(outcome, "status", None)
        if status == "success":
            return None
        if status == "partial":
            if unchanged_count >= 2:
                return self._replan_or_abort(replans_used, "partial result and the screen stopped changing")
            return RecoveryDecision(RecoveryAction.RETRY, "partial result; retrying the step once")
        if status == "failure":
            return self.decide(
                error=getattr(outcome, "summary", lambda: "verification failed")()
                if callable(getattr(outcome, "summary", None))
                else "verification failed",
                outcome=outcome,
                attempt=attempt,
                replans_used=replans_used,
                unchanged_count=unchanged_count,
            )
        return None
