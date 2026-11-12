"""Perception package: screenshots, OCR, UI detection and vision."""

from perception.ocr import OCRBlock, OCREngine, OCRResult, tesseract_available
from perception.screenshot import ScreenCapture, Screenshot, SimulatedDesktop, latest_screenshot
from perception.ui_detector import (
    UIElement,
    UIDetector,
    WindowInfo,
    find_element,
    foreground_window,
    list_windows,
)
from perception.vision import GUIState, PerceptionPipeline, VisionAnalyzer

__all__ = [
    "GUIState",
    "OCRBlock",
    "OCREngine",
    "OCRResult",
    "PerceptionPipeline",
    "ScreenCapture",
    "Screenshot",
    "SimulatedDesktop",
    "UIElement",
    "UIDetector",
    "VisionAnalyzer",
    "WindowInfo",
    "find_element",
    "foreground_window",
    "latest_screenshot",
    "list_windows",
    "tesseract_available",
]
