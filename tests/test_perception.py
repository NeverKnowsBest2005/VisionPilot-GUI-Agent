"""Perception details that are easy to get wrong: VLM scaling and the synthetic desktop."""

from __future__ import annotations

from datetime import datetime, timezone

from perception import GUIState, SimulatedDesktop
from perception.vision import VisionAnalyzer


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
