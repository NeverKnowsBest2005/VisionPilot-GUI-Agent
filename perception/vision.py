"""Visual understanding: turn a screenshot into a structured GUI state.

``PerceptionPipeline`` is the single entry point used by the agent loop.  It
captures the screen, runs OCR, detects UI elements, enumerates windows and -
when a vision-capable model is configured - asks the VLM for a higher level
description.  The result is a :class:`GUIState`: a compact, model-friendly
representation of *what is on screen right now*.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from config.settings import Settings
from perception.ocr import OCRBlock, OCREngine, OCRResult
from perception.screenshot import ScreenCapture, Screenshot, SimulatedDesktop
from perception.ui_detector import (
    UIElement,
    UIDetector,
    WindowInfo,
    find_element,
    foreground_window,
    list_windows,
)

logger = logging.getLogger("visionpilot.perception.vision")


@dataclass
class GUIState:
    """A snapshot of the desktop as the agent understands it."""

    timestamp: datetime
    width: int
    height: int
    active_window: str
    text: str
    elements: list[UIElement] = field(default_factory=list)
    windows: list[WindowInfo] = field(default_factory=list)
    description: str = ""
    screenshot_path: str | None = None
    screenshot_sha: str = ""
    backend: str = "none"
    ocr_backend: str = "none"
    ocr_available: bool = False
    vision_used: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------- lookups
    def find(
        self,
        needle: str,
        *,
        kinds: list[str] | None = None,
        prefer: str | None = None,
    ) -> UIElement | None:
        return find_element(self.elements, needle, kinds=kinds, prefer=prefer)

    def has_text(self, needle: str) -> bool:
        return bool(needle) and needle.lower() in (self.text or "").lower()

    def text_matches(self, needles: list[str]) -> list[str]:
        return [needle for needle in needles if self.has_text(needle)]

    def text_lines(self, limit: int = 40) -> list[str]:
        return [line.strip() for line in (self.text or "").splitlines() if line.strip()][:limit]

    def signature(self) -> str:
        """Stable fingerprint of the parts of the screen that matter."""
        payload = "|".join(
            [
                self.screenshot_sha or "",
                self.active_window or "",
                hashlib.sha1((self.text or "").encode("utf-8")).hexdigest(),
            ]
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def element_labels(self, limit: int = 24) -> list[str]:
        labels: list[str] = []
        for element in self.elements:
            label = (element.label or "").strip()
            if label and label not in labels:
                labels.append(label)
        return labels[:limit]

    # -------------------------------------------------------------- render
    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "width": self.width,
            "height": self.height,
            "active_window": self.active_window,
            "text": self.text,
            "elements": [element.as_dict() for element in self.elements],
            "windows": [window.as_dict() for window in self.windows],
            "description": self.description,
            "screenshot_path": self.screenshot_path,
            "screenshot_sha": self.screenshot_sha,
            "backend": self.backend,
            "ocr_backend": self.ocr_backend,
            "ocr_available": self.ocr_available,
            "vision_used": self.vision_used,
            "signature": self.signature(),
            "meta": self.meta,
        }

    def to_prompt(self, max_chars: int = 1600) -> str:
        lines = [
            f"ACTIVE WINDOW: {self.active_window or '(unknown)'}",
            f"SCREEN: {self.width}x{self.height}",
        ]
        if self.elements:
            shown = []
            for element in self.elements[:20]:
                label = element.label.strip()
                if not label:
                    continue
                shown.append(f"{element.kind}:{label}@{element.center}")
            if shown:
                lines.append("ELEMENTS: " + " | ".join(shown))
        visible = " | ".join(self.text_lines(limit=25))
        if visible:
            lines.append(f"VISIBLE TEXT: {visible}")
        if self.description:
            lines.append(f"SUMMARY: {self.description}")
        text = "\n".join(lines)
        return text[:max_chars]


class VisionAnalyzer:
    """Produce a :class:`GUIState`, optionally enriched by a VLM."""

    def __init__(self, settings: Settings, llm: Any | None = None) -> None:
        self.settings = settings
        self.llm = llm

    async def analyze(
        self,
        screenshot: Screenshot,
        ocr: OCRResult,
        elements: list[UIElement],
        windows: list[WindowInfo],
        *,
        simulated: bool = False,
    ) -> GUIState:
        active_window = ""
        if not simulated:
            # The foreground window is the truth; EnumWindows order only catches
            # up a moment later, which used to make the agent report the window it
            # had just activated as still "not focused".
            front = await asyncio.to_thread(foreground_window)
            if front is not None and front.title:
                active_window = front.title
        if not active_window and windows:
            active_window = windows[0].title
        if not active_window and screenshot.meta.get("active_window"):
            active_window = str(screenshot.meta["active_window"])

        state = GUIState(
            timestamp=screenshot.timestamp,
            width=screenshot.width,
            height=screenshot.height,
            active_window=active_window,
            text=ocr.text,
            elements=elements,
            windows=windows,
            screenshot_path=str(screenshot.path) if screenshot.path else None,
            screenshot_sha=screenshot.sha,
            backend=screenshot.backend,
            ocr_backend=ocr.backend,
            ocr_available=ocr.available,
            meta={
                "capture_ms": screenshot.meta.get("capture_ms"),
                "ocr_ms": ocr.duration_ms,
                "ocr_error": ocr.error,
            },
        )

        if self.llm is not None and self.settings.vision_ready and screenshot.has_image:
            try:
                enriched, sent_width, sent_height = await self._ask_vlm(screenshot, state)
            except Exception as exc:  # noqa: BLE001 - VLM is best effort
                logger.warning("VLM analysis failed: %s", exc)
                state.meta["vision_error"] = str(exc)
            else:
                if enriched:
                    state.description = enriched.get("description") or state.description
                    if enriched.get("active_window"):
                        state.active_window = str(enriched["active_window"])
                    self._merge_vlm_elements(
                        state,
                        enriched.get("elements") or [],
                        sent_width=sent_width,
                        sent_height=sent_height,
                    )
                    state.vision_used = True

        if not state.description:
            state.description = self._heuristic_description(state)
        return state

    # ------------------------------------------------------------------ VLM
    async def _ask_vlm(
        self, screenshot: Screenshot, state: GUIState
    ) -> tuple[dict[str, Any], int, int]:
        max_width = int(self.settings.vision_max_width or 0)
        sent_width, sent_height = screenshot.width, screenshot.height
        if max_width and sent_width > max_width:
            # Match ``Screenshot.png_bytes`` exactly so coordinates scale back
            # without a one-pixel drift.
            sent_height = max(1, int(sent_height * max_width / sent_width))
            sent_width = max_width
        image = screenshot.to_base64(max_width=max_width)
        prompt = (
            "You are the perception module of a desktop GUI agent. Look at the screenshot "
            "and describe the interface precisely.\n"
            f"The attached image is {sent_width}x{sent_height} pixels.\n"
            "Return ONLY JSON with this shape:\n"
            '{"active_window": str, "description": str, '
            '"elements": [{"label": str, "kind": "button|input|menu|icon|text|window|checkbox", '
            '"approx_x": int, "approx_y": int}]}\n'
            "Use the visible text as element labels. approx_x/approx_y must be pixel "
            "coordinates in the attached image. Keep the description under 80 words. "
            "If no window is visible, say so."
        )
        payload = await self.llm.describe_image(image, prompt)  # type: ignore[attr-defined]
        if isinstance(payload, str):
            payload = _extract_json(payload)
        return (payload if isinstance(payload, dict) else {}), sent_width, sent_height

    def _merge_vlm_elements(
        self,
        state: GUIState,
        items: list[Any],
        *,
        sent_width: int | None = None,
        sent_height: int | None = None,
    ) -> None:
        scale_x = state.width / sent_width if sent_width else 1.0
        scale_y = state.height / sent_height if sent_height else 1.0
        for item in items[:30]:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            if not label:
                continue
            existing = state.find(label)
            if existing is not None:
                if item.get("kind") and existing.kind in {"text", "button", "icon", "input"}:
                    existing.role = str(item["kind"])
                continue
            try:
                raw_x = int(item.get("approx_x"))
                raw_y = int(item.get("approx_y"))
            except (TypeError, ValueError):
                continue
            # The VLM sees a downscaled image; map its coordinates back to the
            # full screenshot so grounding clicks the right pixel.
            x = max(0, min(state.width - 1, int(round(raw_x * scale_x))))
            y = max(0, min(state.height - 1, int(round(raw_y * scale_y))))
            state.elements.append(
                UIElement(
                    kind=str(item.get("kind") or "element"),
                    bbox=(x - 20, y - 10, x + 20, y + 10),
                    label=label,
                    confidence=60.0,
                    source="vlm",
                    role=str(item.get("kind") or "element"),
                )
            )

    # ------------------------------------------------------------- heuristic
    def _heuristic_description(self, state: GUIState) -> str:
        parts = []
        if state.active_window:
            parts.append(f"Active window: {state.active_window}")
        labels = [element.label for element in state.elements if element.label][:6]
        if labels:
            parts.append("Visible controls/text: " + ", ".join(labels))
        elif state.text.strip():
            parts.append("Visible text: " + " | ".join(state.text_lines(limit=6)))
        changed = state.meta.get("backend_error")
        if changed:
            parts.append(f"capture note: {changed}")
        return ". ".join(parts) or "Desktop is visible but no text could be recognised"


class PerceptionPipeline:
    """Capture + OCR + detection + analysis, wired together."""

    def __init__(self, settings: Settings, llm: Any | None = None) -> None:
        self.settings = settings
        self.capture = ScreenCapture(settings)
        self.ocr = OCREngine(settings)
        self.detector = UIDetector(settings)
        self.analyzer = VisionAnalyzer(settings, llm=llm)
        self.last_state: GUIState | None = None
        self.last_ocr: OCRResult | None = None

    @property
    def desktop(self) -> SimulatedDesktop | None:
        return self.capture.desktop

    async def observe(self, *, save: bool = True) -> GUIState:
        screenshot = await asyncio.to_thread(self.capture.capture, save=save)
        ocr_result = await asyncio.to_thread(self.ocr.read, screenshot)

        windows: list[WindowInfo] = []
        if self.capture.desktop is not None:
            windows = [
                WindowInfo(
                    title=self.capture.desktop.active_window,
                    bbox=(0, 0, self.capture.desktop.size[0], self.capture.desktop.size[1]),
                )
            ]
        else:
            windows = await asyncio.to_thread(list_windows)
            if not windows:
                front = await asyncio.to_thread(foreground_window)
                if front is not None:
                    windows = [front]

        elements = await asyncio.to_thread(
            self.detector.detect, screenshot, ocr_result, desktop=self.capture.desktop
        )
        state = await self.analyzer.analyze(
            screenshot, ocr_result, elements, windows, simulated=self.capture.desktop is not None
        )
        self.last_state = state
        self.last_ocr = ocr_result
        logger.debug(
            "observed %s: %d elements, %d chars of text",
            state.active_window or "desktop",
            len(state.elements),
            len(state.text),
        )
        return state


def _extract_json(text: str) -> dict[str, Any] | None:
    """Tolerate fenced or chatty JSON from a vision model."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def ocr_blocks_preview(blocks: list[OCRBlock], limit: int = 10) -> list[str]:
    return [f"{block.text}@{block.center}" for block in blocks[:limit]]
