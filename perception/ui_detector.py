"""UI element detection and desktop window enumeration.

Elements come from three sources, merged in order of trust:

1. the synthetic desktop's own widget tree (simulation mode - exact);
2. OpenCV contour/rectangle detection (real desktop, coarse grouping);
3. OCR text blocks (real desktop, exact label boxes).

Everything is normalised into :class:`UIElement` so the planner can resolve
"click the Downloads folder" to a real coordinate regardless of source.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from perception.ocr import OCRBlock, OCRResult

logger = logging.getLogger("visionpilot.perception.ui")

try:  # optional dependency
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    _CV2 = True
except Exception:  # pragma: no cover - optional
    cv2 = np = None  # type: ignore
    _CV2 = False


@dataclass
class UIElement:
    kind: str
    bbox: tuple[int, int, int, int]
    label: str = ""
    confidence: float = 0.0
    source: str = "unknown"
    role: str | None = None
    action: str | None = None

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bbox
        return (left + right) // 2, (top + bottom) // 2

    @property
    def width(self) -> int:
        return max(0, self.bbox[2] - self.bbox[0])

    @property
    def height(self) -> int:
        return max(0, self.bbox[3] - self.bbox[1])

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "bbox": list(self.bbox),
            "center": list(self.center),
            "confidence": round(self.confidence, 1),
            "source": self.source,
            "role": self.role,
            "action": self.action,
        }

    def describe(self) -> str:
        label = self.label or self.kind
        cx, cy = self.center
        return f"{self.kind} '{label}' at ({cx}, {cy})"


@dataclass
class WindowInfo:
    title: str
    bbox: tuple[int, int, int, int] | None = None
    pid: int | None = None
    visible: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "bbox": list(self.bbox) if self.bbox else None,
            "pid": self.pid,
            "visible": self.visible,
        }


# ---------------------------------------------------------------------------
# Window enumeration (Windows, via ctypes so no extra dependency is needed)
# ---------------------------------------------------------------------------
def list_windows(limit: int = 20) -> list[WindowInfo]:
    if os.name != "nt":
        return []
    try:  # pragma: no cover - Windows-only
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        windows: list[WindowInfo] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _callback(hwnd, _lparam):  # type: ignore[no-untyped-def]
            if len(windows) >= limit:
                return False
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if not title:
                return True
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            windows.append(
                WindowInfo(
                    title=title,
                    bbox=(rect.left, rect.top, rect.right, rect.bottom),
                    pid=int(pid.value),
                )
            )
            return True

        user32.EnumWindows(_callback, 0)
        return windows
    except Exception as exc:  # noqa: BLE001
        logger.debug("window enumeration failed: %s", exc)
        return []


def foreground_window() -> WindowInfo | None:
    if os.name != "nt":
        return None
    try:  # pragma: no cover - Windows-only
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return WindowInfo(
            title=buffer.value.strip(),
            bbox=(rect.left, rect.top, rect.right, rect.bottom),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("foreground window failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
class UIDetector:
    """Merge synthetic widgets, OpenCV boxes and OCR blocks into one element list."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    @property
    def cv_available(self) -> bool:
        return _CV2 and bool(self.settings.detect_ui_with_opencv)

    def detect(
        self,
        screenshot: Any,
        ocr: OCRResult,
        *,
        desktop: Any | None = None,
    ) -> list[UIElement]:
        elements: list[UIElement] = []
        if desktop is not None:
            for widget in desktop.widgets():
                left, top, right, bottom = widget["rect"]
                elements.append(
                    UIElement(
                        kind=widget.get("kind", "widget"),
                        bbox=(int(left), int(top), int(right), int(bottom)),
                        label=str(widget.get("label", "")),
                        confidence=100.0,
                        source="synthetic",
                        role=widget.get("kind"),
                    )
                )
            return elements

        if self.cv_available and getattr(screenshot, "image", None) is not None:
            elements.extend(self._detect_rectangles(screenshot.image))

        for block in ocr.blocks:
            if not block.bbox:
                continue
            elements.append(
                UIElement(
                    kind="text",
                    bbox=block.bbox,
                    label=block.text,
                    confidence=block.confidence,
                    source="ocr",
                    role="text",
                )
            )
        elements.extend(_heuristic_controls(elements))
        return _dedupe(elements)

    def _detect_rectangles(self, image: Any, max_elements: int = 60) -> list[UIElement]:
        try:  # pragma: no cover - visual heuristic, hard to unit test meaningfully
            array = np.array(image.convert("RGB"))
            gray = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
            edges = cv2.Canny(gray, 40, 120)
            contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            elements: list[UIElement] = []
            height, width = gray.shape[:2]
            for contour in contours:
                x, y, w, h = cv2.boundingRect(contour)
                if w < 24 or h < 12 or w * h > 0.6 * width * height:
                    continue
                if w > width * 0.95 and h > height * 0.9:
                    continue
                kind = "button" if h <= 48 and w <= 300 else "panel"
                elements.append(
                    UIElement(
                        kind=kind,
                        bbox=(x, y, x + w, y + h),
                        confidence=40.0,
                        source="opencv",
                        role=kind,
                    )
                )
                if len(elements) >= max_elements:
                    break
            return elements
        except Exception as exc:  # noqa: BLE001
            logger.debug("opencv detection failed: %s", exc)
            return []


def _heuristic_controls(elements: Iterable[UIElement]) -> list[UIElement]:
    """Promote OCR words that look like interactive labels to controls."""
    derived: list[UIElement] = []
    pattern = re.compile(
        r"^(ok|cancel|yes|no|close|open|save|delete|confirm|next|back|submit|apply|"
        r"确定|取消|保存|打开|关闭|是|否|下一步|上一步)$",
        re.IGNORECASE,
    )
    for element in elements:
        if element.source != "ocr":
            continue
        if pattern.match(element.label.strip()):
            derived.append(
                UIElement(
                    kind="button",
                    bbox=element.bbox,
                    label=element.label,
                    confidence=element.confidence,
                    source="ocr",
                    role="button",
                )
            )
    return derived


def _dedupe(elements: list[UIElement], tolerance: int = 6) -> list[UIElement]:
    kept: list[UIElement] = []
    for element in elements:
        duplicate = False
        for other in kept:
            if other.label != element.label or other.kind != element.kind:
                continue
            if all(abs(a - b) <= tolerance for a, b in zip(other.bbox, element.bbox)):
                duplicate = True
                break
        if not duplicate:
            kept.append(element)
    return kept


def find_element(
    elements: Iterable[UIElement],
    needle: str,
    *,
    kinds: Iterable[str] | None = None,
    prefer: str | None = None,
) -> UIElement | None:
    """Find the best element whose label contains ``needle``.

    Exact matches win over substring matches, and shorter labels win over longer
    ones (so "Save" beats "Save as draft").  This is the grounding primitive
    behind *"click the Save button"*.
    """
    if not needle:
        return None
    target = needle.strip().lower()
    allowed = set(kinds) if kinds else None
    candidates: list[tuple[tuple[int, int, int], UIElement]] = []
    for element in elements:
        if allowed and element.kind not in allowed:
            continue
        label = (element.label or "").strip().lower()
        if not label:
            continue
        if label == target:
            rank = 0
        elif target in label:
            rank = 1
        elif label in target:
            rank = 2
        else:
            continue
        prefer_rank = 0 if (prefer and element.kind == prefer) else 1
        candidates.append(((rank, prefer_rank, len(label)), element))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]
