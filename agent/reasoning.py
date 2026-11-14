"""Model-facing reasoning: the DeepSeek/OpenAI client and prompt builders.

The LLM is used for three things and nothing else: drafting plans, choosing a
concrete action when the plan is too abstract, and judging ambiguous outcomes.
Every call has a deterministic fallback, so the agent runs with or without a
key.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from config.settings import Settings

logger = logging.getLogger("visionpilot.agent.reasoning")

_SYSTEM = (
    "You are VisionPilot, an autonomous desktop GUI agent. You see the screen through "
    "OCR and UI detection, and you act only through the provided tools. Be concrete and "
    "conservative: prefer one small verifiable action at a time, never invent coordinates "
    "for elements you cannot see, and always state what the screen should show afterwards. "
    "Reply with JSON only when asked for JSON."
)


class LLMClient:
    """Thin async wrapper around the OpenAI-compatible chat API."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any | None = None
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.errors = 0

    @property
    def available(self) -> bool:
        return bool(self.settings.llm_ready)

    def _ensure_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI  # imported lazily; optional dependency

            self._client = AsyncOpenAI(
                api_key=self.settings.llm_api_key,
                base_url=self.settings.llm_base_url,
                timeout=float(self.settings.llm_timeout_sec),
                max_retries=0,
            )
        return self._client

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        if not self.available:
            raise RuntimeError("LLM is not configured (set VISIONPILOT_LLM_API_KEY)")
        if self.calls >= self.settings.llm_max_calls_per_task:
            raise RuntimeError(
                f"LLM call budget exhausted ({self.settings.llm_max_calls_per_task} calls per task)"
            )
        client = self._ensure_client()
        messages = [
            {"role": "system", "content": system or _SYSTEM},
            {"role": "user", "content": prompt},
        ]
        kwargs: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": self.settings.llm_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.settings.llm_max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(max(1, self.settings.llm_max_retries + 1)):
            try:
                response = await asyncio.wait_for(
                    client.chat.completions.create(**kwargs),
                    timeout=float(self.settings.llm_timeout_sec),
                )
                self.calls += 1
                usage = getattr(response, "usage", None)
                if usage is not None:
                    self.prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
                    self.completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
                choice = response.choices[0]
                return (choice.message.content or "").strip()
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self.errors += 1
                logger.warning("LLM call failed (attempt %d): %s", attempt + 1, exc)
                if attempt < self.settings.llm_max_retries:
                    await asyncio.sleep(0.6 * (attempt + 1))
        raise RuntimeError(f"LLM call failed: {last_error}")

    async def describe_image(self, image_base64: str, prompt: str) -> str:
        if not self.available:
            raise RuntimeError("LLM is not configured")
        client = self._ensure_client()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_base64}"},
                    },
                ],
            }
        ]
        self.calls += 1
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=self.settings.llm_vision_model,
                messages=messages,
                temperature=0.1,
                max_tokens=self.settings.llm_max_tokens,
            ),
            timeout=float(self.settings.llm_timeout_sec),
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
            self.completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
        return (response.choices[0].message.content or "").strip()

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def stats(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "errors": self.errors,
        }

    async def close(self) -> None:
        self._client = None


class ReasoningEngine:
    """Prompt orchestration around :class:`LLMClient`."""

    def __init__(self, settings: Settings, llm: LLMClient | None = None) -> None:
        self.settings = settings
        self.llm = llm

    @property
    def available(self) -> bool:
        return self.llm is not None and self.llm.available

    # ----------------------------------------------------------------- plan
    async def propose_plan(
        self,
        goal: str,
        state: Any,
        tool_catalog: str,
        hints: str = "",
    ) -> dict[str, Any] | None:
        if not self.available:
            return None
        prompt = (
            f"GOAL: {goal}\n\n"
            f"CURRENT SCREEN:\n{_state_prompt(state)}\n\n"
            f"AVAILABLE TOOLS:\n{tool_catalog}\n\n"
            f"{('HINTS FROM MEMORY:\n' + hints + chr(10) + chr(10)) if hints else ''}"
            "Write a short plan (2-8 steps). Each step is one tool call.\n"
            "Argument conventions:\n"
            "- To open an installed desktop application by name (e.g. 微信, WeChat, 设置), "
            "call system_launch_app with {\"name\": \"<app name>\"} instead of Start-menu keystrokes; "
            "set expect to {\"window\": \"<app name>\"}.\n"
            "- Use browser_open only when the user explicitly asks for a website/URL or browser task. "
            "Never substitute browser_open for launching a desktop app. If system_launch_app fails, "
            "do not launch the same app repeatedly; re-observe and use mouse/keyboard only when a "
            "relevant visible UI control is actually present, otherwise report the launch failure. "
            "If the result says the app needs administrator rights (needs_admin), do not retry it "
            "and do not fall back to a browser: stop and tell the user it needs a manual UAC "
            "approval.\n"
            "- To interact with a visible element, pass its label as argument \"element\" "
            "to a mouse tool (mouse_move/mouse_click/mouse_double_click/mouse_right_click/"
            "mouse_drag/mouse_scroll); do not guess coordinates unless you must. To type into "
            "a field, first click it, then call keyboard_type with \"text\".\n"
            "- keyboard_type takes \"text\"; keyboard_press takes \"keys\" (list, e.g. [\"ctrl\",\"s\"]).\n"
            "- For a window-opening step set expect to {\"window\": \"<title fragment>\"}.\n"
            "- For typing set expect to {\"text\": \"<the typed text>\"}.\n"
            "- To open a file/folder that is not visible yet, first open the containing app.\n"
            'Return JSON: {"reasoning": "...", "steps": [{"description": "...", '
            '"tool": "...", "arguments": {...}, "expect": {...}}]}'
        )
        try:
            raw = await self.llm.complete(prompt, json_mode=True)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM planning failed: %s", exc)
            return None
        return _parse_json(raw)

    # --------------------------------------------------------------- action
    async def choose_action(
        self,
        goal: str,
        state: Any,
        step: Any,
        history: str,
        tool_catalog: str,
        last_error: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.available:
            return None
        prompt = (
            f"GOAL: {goal}\n\n"
            f"CURRENT SCREEN:\n{_state_prompt(state)}\n\n"
            f"NEXT PLANNED STEP: {step.description}\n"
            f"INTENDED TOOL: {step.tool} arguments={json.dumps(step.arguments, ensure_ascii=False)[:400]}\n"
            f"RECENT ACTIONS:\n{history or '(none)'}\n"
            f"{('LAST ERROR: ' + last_error + chr(10)) if last_error else ''}\n"
            f"AVAILABLE TOOLS:\n{tool_catalog}\n\n"
            "Choose ONE concrete tool call that advances the goal. For mouse tools prefer "
            "\"element\" labels over guessed coordinates. Say what the screen should look like "
            "afterwards.\n"
            'Return JSON: {"reasoning": "...", "tool": "...", "arguments": {...}, "expect": {...}}'
        )
        try:
            raw = await self.llm.complete(prompt, json_mode=True)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM action selection failed: %s", exc)
            return None
        return _parse_json(raw)

    # --------------------------------------------------------------- revise
    async def revise_plan(
        self,
        goal: str,
        state: Any,
        plan: Any,
        failure: str,
        tool_catalog: str,
        history: str = "",
    ) -> dict[str, Any] | None:
        if not self.available:
            return None
        completed = [step.description for step in plan.steps if step.status == "done"]
        prompt = (
            f"GOAL: {goal}\n\n"
            f"CURRENT SCREEN:\n{_state_prompt(state)}\n\n"
            f"ORIGINAL PLAN:\n{plan.to_prompt()}\n\n"
            f"COMPLETED STEPS: {completed or '(none)'}\n"
            f"FAILURE: {failure}\n\n"
            f"RECENT ACTIONS:\n{history or '(none)'}\n\n"
            f"AVAILABLE TOOLS:\n{tool_catalog}\n\n"
            "Write a revised plan for the remaining work. Do not repeat completed steps. "
            "If the goal cannot be achieved with these tools, return an empty steps list and "
            "explain why in reasoning.\n"
            'Return JSON: {"reasoning": "...", "steps": [{"description": "...", "tool": "...", '
            '"arguments": {...}, "expect": {...}}]}'
        )
        try:
            raw = await self.llm.complete(prompt, json_mode=True)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM replanning failed: %s", exc)
            return None
        return _parse_json(raw)

    # -------------------------------------------------------------- reflect
    async def reflect(self, goal: str, steps: list[Any], success: bool) -> list[str]:
        if not self.available or not success:
            return []
        from learning.trajectory import trajectory_text

        prompt = (
            f"GOAL: {goal}\nOUTCOME: {'success' if success else 'failure'}\n"
            f"TRAJECTORY:\n{trajectory_text(steps, limit=16)}\n\n"
            "Write at most 2 short, reusable lessons for next time (English or Chinese, "
            "one sentence each, no numbering).\n"
            'Return JSON: {"lessons": ["...", "..."]}'
        )
        try:
            raw = await self.llm.complete(prompt, json_mode=True, max_tokens=300)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            logger.debug("LLM reflection failed: %s", exc)
            return []
        payload = _parse_json(raw) or {}
        lessons = payload.get("lessons") if isinstance(payload, dict) else None
        if not isinstance(lessons, list):
            return []
        return [str(item).strip() for item in lessons if str(item).strip()][:2]


def _state_prompt(state: Any, max_chars: int = 1600) -> str:
    if state is None:
        return "(no observation yet)"
    if hasattr(state, "to_prompt"):
        return state.to_prompt(max_chars=max_chars)
    return str(state)[:max_chars]


def _parse_json(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        logger.debug("could not parse JSON from model: %s", raw[:200])
        return None
    return payload if isinstance(payload, dict) else None
