"""Perception details that are easy to get wrong: VLM scaling and the synthetic desktop."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from perception import GUIState, SimulatedDesktop, WindowInfo
from perception.ocr import OCRResult
from perception.screenshot import Screenshot
from perception.vision import VisionAnalyzer


def make_screenshot() -> Screenshot:
    return Screenshot(
        image=None,
        width=1280,
        height=800,
        timestamp=datetime.now(timezone.utc),
        sha="0" * 64,
        backend="pillow",
        path=Path("screenshot.png"),
        meta={},
    )


def make_state(width: int = 1920, height: int = 1080) -> GUIState:
    return GUIState(
        timestamp=datetime.now(timezone.utc),
        width=width,
        height=height,
        active_window="",
        text="",
        elements=[],
        windows=[],
        description="",
        backend="mss",
    )


def test_vlm_coordinates_are_scaled_to_the_full_screenshot(settings):
    analyzer = VisionAnalyzer(settings)
    state = make_state()
    analyzer._merge_vlm_elements(
        state,
        [{"label": "OK", "kind": "button", "approx_x": 512, "approx_y": 300}],
        sent_width=1024,
        sent_height=576,
    )
    element = state.find("OK")
    assert element is not None
    center_x, center_y = element.center
    assert abs(center_x - 960) <= 1
    assert abs(center_y - 562) <= 1


def test_simulated_desktop_scroll_and_open_app():
    desktop = SimulatedDesktop()
    desktop.scroll(-3)
    assert desktop.scroll_offset == 3

    desktop.open_app("微信")
    assert desktop.active_app == "微信"
    assert "微信" in desktop.text()


def test_analyze_prefers_the_foreground_window(settings, monkeypatch):
    monkeypatch.setattr(
        "perception.vision.foreground_window",
        lambda: WindowInfo(title="抖音", bbox=(0, 0, 1280, 800), pid=292912),
    )
    analyzer = VisionAnalyzer(settings)
    state = asyncio.run(
        analyzer.analyze(
            make_screenshot(),
            OCRResult(text="", blocks=[]),
            [],
            [WindowInfo(title="VisionPilot", bbox=(0, 0, 1280, 800))],
        )
    )
    assert state.active_window == "抖音"


def test_analyze_keeps_the_simulated_window(settings, monkeypatch):
    monkeypatch.setattr(
        "perception.vision.foreground_window",
        lambda: WindowInfo(title="抖音", bbox=(0, 0, 1280, 800), pid=292912),
    )
    analyzer = VisionAnalyzer(settings)
    state = asyncio.run(
        analyzer.analyze(
            make_screenshot(),
            OCRResult(text="", blocks=[]),
            [],
            [WindowInfo(title="Notes", bbox=(0, 0, 1280, 800))],
            simulated=True,
        )
    )
    assert state.active_window == "Notes"
