"""Logging helpers shared by the CLI, the API and the agent core."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any


def configure_logging(settings, *, stream: bool = True) -> logging.Logger:
    """Configure the root logger once and return the application logger.

    A rotating file handler keeps ``logs/visionpilot.log`` bounded, while a
    stream handler keeps the CLI useful.  Calling this function repeatedly is
    harmless: handlers are replaced, not stacked.
    """
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(settings.log_level).upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s",
        datefmt="%H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "visionpilot.log",
        maxBytes=4_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    if stream:
        stream_handler = logging.StreamHandler(stream=sys.stderr)
        stream_handler.setFormatter(formatter)
        stream_handler.setLevel(getattr(logging, str(settings.log_level).upper(), logging.INFO))
        root.addHandler(stream_handler)

    # Third-party noise stays out of the console.
    for noisy in ("httpx", "httpcore", "urllib3", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("visionpilot")


class JsonlEventWriter:
    """Append-only JSONL sink used as a durable trace of agent activity."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: dict[str, Any]) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except OSError:  # pragma: no cover - disk full / permission edge case
            logging.getLogger("visionpilot.events").warning("cannot append event log")
