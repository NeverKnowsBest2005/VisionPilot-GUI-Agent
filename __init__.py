"""VisionPilot - a multimodal autonomous GUI agent.

The package layout follows the architecture described in ``README.md``:

``agent``       orchestration, planning, reasoning and state
``perception``  screenshots, OCR, UI detection and visual understanding
``executor``    tool interface plus mouse/keyboard/browser/file tools
``memory``      working, episodic and semantic memory on top of SQLite
``verifier``    independent result checking and failure recovery
``learning``    trajectory analysis, skill extraction and policy hints
``api``         FastAPI backend consumed by the web console
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
