"""OCR layer.

Tesseract is optional.  When it is installed every capture is recognised with
word-level bounding boxes; when it is not, the engine degrades gracefully and
the rest of the system (heuristics, synthetic desktop text, VLM) keeps working.
Bounding boxes matter: they are what lets the planner click a *label* instead
of a hard-coded coordinate.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from config.settings import Settings

logger = logging.getLogger("visionpilot.perception.ocr")

_TESSERACT_STATE: dict[str, Any] = {"probed": False, "available": False, "version": None}


def tesseract_available() -> bool:
    """Probe tesseract once (spawning the binary is relatively expensive)."""
    if not _TESSERACT_STATE["probed"]:
        _TESSERACT_STATE["probed"] = True
        try:  # pragma: no cover - depends on host install
            import pytesseract  # type: ignore

            version = pytesseract.get_tesseract_version()
            _TESSERACT_STATE["available"] = True
            _TESSERACT_STATE["version"] = str(version)
        except Exception as exc:  # noqa: BLE001
            logger.info("tesseract unavailable: %s", exc)
            _TESSERACT_STATE["available"] = False
    return bool(_TESSERACT_STATE["available"])


@dataclass
class OCRBlock:
    text: str
    bbox: tuple[int, int, int, int] | None = None
    confidence: float = 0.0

    @property
    def center(self) -> tuple[int, int] | None:
        if not self.bbox:
            return None
        left, top, right, bottom = self.bbox
        return (left + right) // 2, (top + bottom) // 2

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "bbox": list(self.bbox) if self.bbox else None,
            "center": list(self.center) if self.center else None,
            "confidence": round(self.confidence, 1),
        }


@dataclass
class OCRResult:
    text: str = ""
    blocks: list[OCRBlock] = field(default_factory=list)
    backend: str = "none"
    available: bool = False
    duration_ms: int = 0
    error: str | None = None

    def find(self, needle: str, *, case_sensitive: bool = False) -> list[OCRBlock]:
        if not needle:
            return []
        target = needle if case_sensitive else needle.lower()
        matches = []
        for block in self.blocks:
            haystack = block.text if case_sensitive else block.text.lower()
            if target in haystack:
                matches.append(block)
        return matches

    def has_text(self, needle: str) -> bool:
        if not needle:
            return True
        return needle.lower() in self.text.lower()

    def lines(self, limit: int = 80) -> list[str]:
        lines = [line.strip() for line in self.text.splitlines() if line.strip()]
        return lines[:limit]

    def compact(self, limit: int = 1200) -> str:
        text = " | ".join(self.lines(limit=40))
        return text[:limit]

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "blocks": [block.as_dict() for block in self.blocks],
            "backend": self.backend,
            "available": self.available,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


class OCREngine:
    """Recognise text from a screenshot, preferring word bounding boxes."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.backend = self._resolve_backend(settings.ocr_backend)

    def _resolve_backend(self, requested: str) -> str:
        if requested == "none":
            return "none"
        if requested == "tesseract":
            return "tesseract"
        return "tesseract" if tesseract_available() else "none"

    @property
    def available(self) -> bool:
        return self.backend == "tesseract" and tesseract_available()

    # ------------------------------------------------------------------ read
    def read(self, screenshot: Any) -> OCRResult:
        started = time.perf_counter()
        if screenshot is None or screenshot.image is None or not self.available:
            result = self._fallback(screenshot)
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            return result

        try:
            result = self._read_tesseract(screenshot.image)
        except Exception as exc:  # noqa: BLE001 - OCR must never break the loop
            logger.warning("OCR failed: %s", exc)
            result = self._fallback(screenshot)
            result.error = str(exc)
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result

    def _fallback(self, screenshot: Any) -> OCRResult:
        """Use the synthetic desktop's ground-truth text when OCR is absent."""
        if screenshot is not None and screenshot.text_hint:
            blocks = [
                OCRBlock(text=line.strip(), confidence=100.0)
                for line in screenshot.text_hint.splitlines()
                if line.strip()
            ]
            return OCRResult(
                text=screenshot.text_hint,
                blocks=blocks,
                backend="synthetic-text",
                available=True,
            )
        return OCRResult(backend="none", available=False)

    def _read_tesseract(self, image: Any) -> OCRResult:
        import pytesseract  # type: ignore
        from pytesseract import Output  # type: ignore

        lang = self._pick_language(pytesseract)
        data = pytesseract.image_to_data(
            image, lang=lang, output_type=Output.DICT, config="--psm 6"
        )
        blocks: list[OCRBlock] = []
        text_lines: list[str] = []
        current_line: list[str] = []
        current_key: tuple[int, int, int] | None = None

        count = len(data.get("text", []))
        for index in range(count):
            word = (data["text"][index] or "").strip()
            try:
                confidence = float(data["conf"][index])
            except (TypeError, ValueError):
                confidence = -1.0
            if not word or confidence < self.settings.ocr_min_confidence:
                if current_line:
                    text_lines.append(" ".join(current_line))
                    current_line = []
                    current_key = None
                continue

            key = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
            if current_key is not None and key != current_key and current_line:
                text_lines.append(" ".join(current_line))
                current_line = []
            current_key = key
            current_line.append(word)

            left = int(data["left"][index])
            top = int(data["top"][index])
            width = int(data["width"][index])
            height = int(data["height"][index])
            blocks.append(
                OCRBlock(
                    text=word,
                    bbox=(left, top, left + width, top + height),
                    confidence=confidence,
                )
            )
        if current_line:
            text_lines.append(" ".join(current_line))

        return OCRResult(
            text="\n".join(text_lines),
            blocks=blocks,
            backend="tesseract",
            available=True,
        )

    def _pick_language(self, pytesseract: Any) -> str:
        requested = self.settings.ocr_lang or "eng"
        try:
            installed = set(pytesseract.get_languages(config=""))
        except Exception:  # noqa: BLE001
            return requested
        wanted = [part for part in requested.split("+") if part in installed]
        return "+".join(wanted) if wanted else "eng"


def find_text_blocks(result: OCRResult, needle: str) -> list[OCRBlock]:
    """Case-insensitive fuzzy lookup helper used by grounding and verification."""
    return result.find(needle)
