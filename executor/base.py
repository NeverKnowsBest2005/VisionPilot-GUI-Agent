"""Unified tool interface for VisionPilot.

Every capability the agent can invoke - mouse, keyboard, browser, filesystem,
system - is expressed as a :class:`Tool`.  The agent core never talks to
PyAutoGUI or Playwright directly; it only knows tool names and the JSON schema
the registry exposes to the language model.  That indirection is what makes the
execution layer swappable, recordable and testable.

Safety model
------------
Each tool declares a :attr:`Tool.danger` level:

``safe``
    Read-only or trivially reversible.
``caution``
    Mutates user data or moves the real cursor.
``dangerous``
    Destructive or runs commands; blocked unless the run explicitly allows it
    and (when ``confirm_dangerous`` is set) the caller passed a confirmation.

The registry additionally enforces a per-call timeout and converts every
exception into a structured :class:`ToolResult` so a single failing tool can
never tear down the agent loop.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import os
import platform
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal, Sequence

from pydantic import BaseModel, Field

logger = logging.getLogger("visionpilot.executor")

DangerLevel = Literal["safe", "caution", "dangerous"]
ParamType = Literal["string", "integer", "number", "boolean", "array", "object"]

_PY_AUTOGUI: Any = None
_PY_AUTOGUI_PROBED = False


# ---------------------------------------------------------------------------
# Optional backend access
# ---------------------------------------------------------------------------
def load_pyautogui() -> Any | None:
    """Load the live input backend.

    PyAutoGUI is preferred when installed.  Otherwise Windows falls back to the
    built-in ``SendInput`` backend, which also supports Unicode typing.
    """
    global _PY_AUTOGUI, _PY_AUTOGUI_PROBED
    if not _PY_AUTOGUI_PROBED:
        _PY_AUTOGUI_PROBED = True
        try:  # pragma: no cover - depends on the host desktop
            import pyautogui  # type: ignore

            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.05
            _PY_AUTOGUI = pyautogui
        except Exception as exc:  # noqa: BLE001 - pyautogui raises broadly on import
            logger.debug("pyautogui unavailable: %s", exc)
            _PY_AUTOGUI = None
        if _PY_AUTOGUI is None and os.name == "nt":
            try:  # pragma: no cover - requires a Windows desktop
                from executor.native_input import load_native_gui

                _PY_AUTOGUI = load_native_gui()
                if _PY_AUTOGUI is not None:
                    logger.info("pyautogui unavailable; using built-in Windows SendInput backend")
            except Exception as exc:  # noqa: BLE001
                logger.debug("native input backend unavailable: %s", exc)
    return _PY_AUTOGUI


def get_screen_size() -> tuple[int, int]:
    """Best-effort physical screen size, with a headless-safe default."""
    gui = load_pyautogui()
    if gui is not None:
        try:
            size = gui.size()
            return int(size[0]), int(size[1])
        except Exception:  # noqa: BLE001
            pass
    if os.name == "nt":
        try:  # pragma: no cover - Windows-only
            import ctypes

            user32 = ctypes.windll.user32
            width = int(user32.GetSystemMetrics(0))
            height = int(user32.GetSystemMetrics(1))
            if width and height:
                return width, height
        except Exception:  # noqa: BLE001
            pass
    return 1920, 1080


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class ToolParameter(BaseModel):
    """A single argument accepted by a tool (used to build JSON schema)."""

    name: str
    type: ParamType = "string"
    description: str = ""
    required: bool = False
    default: Any = None
    enum: list[Any] | None = None

    def schema_fragment(self) -> dict[str, Any]:
        fragment: dict[str, Any] = {"type": self.type}
        if self.description:
            fragment["description"] = self.description
        if self.enum:
            fragment["enum"] = self.enum
        if self.default is not None:
            fragment["default"] = self.default
        return fragment


@dataclass
class ToolResult:
    """Structured outcome of one tool invocation."""

    tool: str
    ok: bool
    output: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: int = 0
    simulated: bool = False
    denied: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "output": self.output,
            "data": self.data,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "simulated": self.simulated,
            "denied": self.denied,
        }

    def to_observation(self, max_chars: int = 800) -> str:
        """Compact string form fed back to the model."""
        if self.ok:
            text = self.output or "ok"
        else:
            text = f"ERROR: {self.error or 'unknown error'}"
            if self.output:
                text += f" | {self.output}"
        if len(text) > max_chars:
            text = text[: max_chars - 3] + "..."
        if self.simulated:
            text = f"[simulated] {text}"
        return text


class ToolArgumentError(ValueError):
    """Raised when a tool receives malformed arguments."""


class ToolPermissionError(PermissionError):
    """Raised when a tool call is blocked by the safety policy."""


class ToolContext(BaseModel):
    """Runtime context handed to every tool invocation."""

    model_config = {"arbitrary_types_allowed": True}

    settings: Any
    desktop: Any = None
    """Synthetic desktop used in simulation mode (see perception.screenshot)."""

    confirmed: bool = False
    """Set when the caller explicitly approved dangerous operations."""

    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def dry_run(self) -> bool:
        return bool(self.settings.dry_run)

    @property
    def workspace_dir(self):
        from pathlib import Path

        return Path(self.settings.workspace_dir)


# ---------------------------------------------------------------------------
# Tool base class
# ---------------------------------------------------------------------------
class Tool(abc.ABC):
    """Base class for every executable capability."""

    name: str = ""
    description: str = ""
    parameters: Sequence[ToolParameter] = ()
    danger: DangerLevel = "safe"
    category: str = "general"

    # ------------------------------------------------------------------ meta
    def is_available(self) -> bool:
        """Whether the underlying backend can run on this machine."""
        return True

    def unavailable_reason(self) -> str:
        return "tool backend is not available"

    def schema(self) -> dict[str, Any]:
        properties = {param.name: param.schema_fragment() for param in self.parameters}
        required = [param.name for param in self.parameters if param.required]
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "danger": self.danger,
            "available": self.is_available(),
            "unavailable_reason": None if self.is_available() else self.unavailable_reason(),
            "parameters": [param.model_dump() for param in self.parameters],
        }

    def compact_signature(self) -> str:
        """One-line description injected into text prompts."""
        args = ", ".join(
            f"{param.name}{'?' if not param.required else ''}:{param.type}"
            for param in self.parameters
        )
        return f"{self.name}({args}) - {self.description}"

    # ------------------------------------------------------------ validation
    def validate_arguments(self, arguments: dict[str, Any] | None) -> dict[str, Any]:
        arguments = dict(arguments or {})
        definitions = {param.name: param for param in self.parameters}

        unknown = set(arguments) - set(definitions)
        if unknown:
            raise ToolArgumentError(
                f"unknown argument(s) for {self.name}: {', '.join(sorted(unknown))}"
            )

        cleaned: dict[str, Any] = {}
        for param in self.parameters:
            if param.name in arguments and arguments[param.name] is not None:
                cleaned[param.name] = self._coerce(param, arguments[param.name])
            elif param.required:
                raise ToolArgumentError(f"missing required argument '{param.name}'")
            elif param.default is not None:
                cleaned[param.name] = param.default
        return cleaned

    def _coerce(self, param: ToolParameter, value: Any) -> Any:
        try:
            if param.type == "integer":
                if isinstance(value, bool):
                    raise ValueError
                coerced: Any = int(value)
            elif param.type == "number":
                if isinstance(value, bool):
                    raise ValueError
                coerced = float(value)
            elif param.type == "boolean":
                if isinstance(value, str):
                    coerced = value.strip().lower() in {"1", "true", "yes", "on"}
                elif isinstance(value, bool):
                    coerced = value
                else:
                    raise ValueError
            elif param.type == "array":
                if isinstance(value, str):
                    coerced = [part.strip() for part in value.split(",") if part.strip()]
                elif isinstance(value, (list, tuple, set)):
                    coerced = list(value)
                else:
                    raise ValueError
            elif param.type == "object":
                if not isinstance(value, dict):
                    raise ValueError
                coerced = value
            else:
                coerced = value if isinstance(value, str) else str(value)
        except (TypeError, ValueError) as exc:
            raise ToolArgumentError(
                f"argument '{param.name}' expects {param.type}, got {value!r}"
            ) from exc

        if param.enum and coerced not in param.enum:
            raise ToolArgumentError(
                f"argument '{param.name}' must be one of {param.enum}, got {coerced!r}"
            )
        return coerced

    # ------------------------------------------------------------- execution
    @abc.abstractmethod
    async def run(self, context: ToolContext, **kwargs: Any) -> ToolResult:
        """Execute the tool. Implementations must not raise for expected errors."""

    # -------------------------------------------------------------- helpers
    def _result(
        self,
        ok: bool,
        output: str = "",
        *,
        data: dict[str, Any] | None = None,
        error: str | None = None,
        simulated: bool = False,
        **extra: Any,
    ) -> ToolResult:
        payload = dict(data or {})
        payload.update(extra)
        return ToolResult(
            tool=self.name,
            ok=ok,
            output=output,
            data=payload,
            error=error,
            simulated=simulated,
        )


class FunctionTool(Tool):
    """Adapter that turns a plain coroutine into a :class:`Tool`."""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        parameters: Sequence[ToolParameter] = (),
        handler: Callable[..., Any],
        danger: DangerLevel = "safe",
        category: str = "general",
        available: Callable[[], bool] | None = None,
        unavailable_reason: str = "tool backend is not available",
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = tuple(parameters)
        self.danger = danger
        self.category = category
        self._handler = handler
        self._available = available or (lambda: True)
        self._unavailable_reason = unavailable_reason

    def is_available(self) -> bool:
        try:
            return bool(self._available())
        except Exception:  # noqa: BLE001
            return False

    def unavailable_reason(self) -> str:
        return self._unavailable_reason

    async def run(self, context: ToolContext, **kwargs: Any) -> ToolResult:
        result = self._handler(context, **kwargs)
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            result = await result
        if isinstance(result, ToolResult):
            if not result.tool:
                result.tool = self.name
            return result
        if isinstance(result, dict):
            return self._result(
                bool(result.get("ok", True)),
                str(result.get("output", "")),
                data=result.get("data") or {},
                error=result.get("error"),
                simulated=bool(result.get("simulated", context.dry_run)),
            )
        return self._result(True, str(result), simulated=context.dry_run)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class ToolRegistry:
    """Holds tools, validates calls and records each invocation."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        self.calls: list[ToolResult] = []
        self._call_limit = 500
        for tool in tools:
            self.register(tool)

    # ------------------------------------------------------------ collection
    def register(self, tool: Tool) -> Tool:
        if not tool.name:
            raise ValueError("tool must define a name")
        if tool.name in self._tools:
            raise ValueError(f"tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool
        return tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool]:
        return [self._tools[name] for name in self.names()]

    def available(self) -> list[Tool]:
        return [tool for tool in self.all() if tool.is_available()]

    def specs(self, only_available: bool = True) -> list[dict[str, Any]]:
        tools = self.available() if only_available else self.all()
        return [tool.schema() for tool in tools]

    def catalog(self) -> list[dict[str, Any]]:
        return [tool.summary() for tool in self.all()]

    def prompt_catalog(self, only_available: bool = True) -> str:
        tools = self.available() if only_available else self.all()
        return "\n".join(f"- {tool.compact_signature()}" for tool in tools)

    # ------------------------------------------------------------- execution
    async def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        context: ToolContext,
        *,
        timeout: float | None = None,
    ) -> ToolResult:
        started = time.perf_counter()
        tool = self.get(name)
        if tool is None:
            return self._record(
                ToolResult(
                    tool=name,
                    ok=False,
                    error=f"unknown tool '{name}'. Available tools: {', '.join(self.names())}",
                )
            )

        if not tool.is_available():
            return self._record(
                ToolResult(tool=name, ok=False, error=f"{name} unavailable: {tool.unavailable_reason()}")
            )

        if (
            tool.danger == "dangerous"
            and context.settings.confirm_dangerous
            and not context.confirmed
        ):
            return self._record(
                ToolResult(
                    tool=name,
                    ok=False,
                    denied=True,
                    error=(
                        f"'{name}' is a dangerous operation and requires explicit "
                        "confirmation (confirm_dangerous=true)"
                    ),
                )
            )

        try:
            validated = tool.validate_arguments(arguments)
        except ToolArgumentError as exc:
            return self._record(ToolResult(tool=name, ok=False, error=str(exc)))

        call_timeout = timeout or float(context.settings.adaptive_step_timeout_sec)
        logger.info("tool call %s args=%s", name, _short(validated))
        try:
            result = await asyncio.wait_for(tool.run(context, **validated), timeout=call_timeout)
        except asyncio.TimeoutError:
            result = ToolResult(tool=name, ok=False, error=f"tool timed out after {call_timeout:.1f}s")
        except ToolPermissionError as exc:
            result = ToolResult(tool=name, ok=False, denied=True, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - tools must never crash the loop
            logger.exception("tool %s raised", name)
            result = ToolResult(tool=name, ok=False, error=f"{type(exc).__name__}: {exc}")

        if not isinstance(result, ToolResult):
            result = ToolResult(tool=name, ok=False, error="tool returned an invalid result")
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return self._record(result)

    def _record(self, result: ToolResult) -> ToolResult:
        self.calls.append(result)
        if len(self.calls) > self._call_limit:
            del self.calls[: len(self.calls) - self._call_limit]
        return result

    # ---------------------------------------------------------------- stats
    def stats(self) -> dict[str, int]:
        by_cta: dict[str, int] = {}
        for call in self.calls:
            by_cta[call.tool] = by_cta.get(call.tool, 0) + 1
        return by_cta


def _short(payload: dict[str, Any], limit: int = 220) -> str:
    text = ", ".join(f"{key}={value!r}" for key, value in payload.items())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# Serialisation helpers shared with the model layer
# ---------------------------------------------------------------------------
def tool_specs_for_prompt(registry: ToolRegistry, only_available: bool = True) -> str:
    """Textual catalogue used inside prompts (keeps token use predictable)."""
    return registry.prompt_catalog(only_available=only_available)


def platform_summary() -> dict[str, str]:
    return {
        "os": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }
