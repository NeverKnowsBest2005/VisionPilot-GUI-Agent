"""Screen capture plus the synthetic desktop used in simulation mode.

Two things live here:

``Screenshot`` / ``ScreenCapture``
    A backend-agnostic screen grabber.  It prefers ``mss`` (fast), falls back
    to Pillow's ``ImageGrab`` and PyAutoGUI, and finally to a synthetic
    renderer.  Each capture is saved under ``logs/screenshots`` and rotated.

``SimulatedDesktop``
    A small, deterministic desktop used when ``execution_mode == "simulate"``.
    Mouse and keyboard tools mutate it, it renders itself with Pillow, and it
    exposes its visible text so the whole Observe -> Verify loop can run on a
    machine without a GUI session (CI, unit tests, offline demos).
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config.settings import Settings

logger = logging.getLogger("visionpilot.perception.screenshot")

try:  # Pillow is a hard requirement of the project but stay import-safe.
    from PIL import Image, ImageDraw, ImageFont  # type: ignore
except Exception:  # pragma: no cover - only on a broken install
    Image = ImageDraw = ImageFont = None  # type: ignore


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Screenshot model
# ---------------------------------------------------------------------------
@dataclass
class Screenshot:
    image: Any | None
    width: int
    height: int
    timestamp: datetime
    sha: str
    backend: str
    path: Path | None = None
    text_hint: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def has_image(self) -> bool:
        return self.image is not None

    def png_bytes(self, max_width: int | None = None) -> bytes | None:
        if self.image is None:
            return None
        image = self.image
        if max_width and image.width > max_width:
            ratio = max_width / float(image.width)
            image = image.resize((max_width, max(1, int(image.height * ratio))))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()

    def to_base64(self, max_width: int | None = None) -> str | None:
        data = self.png_bytes(max_width=max_width)
        return base64.b64encode(data).decode("ascii") if data else None

    def summary(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "timestamp": self.timestamp.isoformat(),
            "sha": self.sha,
            "backend": self.backend,
            "path": str(self.path) if self.path else None,
            "has_image": self.has_image,
        }


# ---------------------------------------------------------------------------
# Synthetic desktop
# ---------------------------------------------------------------------------
class SimulatedDesktop:
    """Deterministic fake desktop shared by the simulated mouse/keyboard tools.

    It intentionally mimics a tiny Windows-like shell: a desktop with icons, a
    taskbar, a file browser and a text editor.  Actions mutate real state, so
    the verifier can observe genuine before/after differences.
    """

    SIZE = (1440, 900)
    TASKBAR_HEIGHT = 44
    TITLE_BAR_HEIGHT = 38

    def __init__(self, size: tuple[int, int] | None = None) -> None:
        self.size: tuple[int, int] = size or self.SIZE
        self.position: tuple[int, int] = (self.size[0] // 2, self.size[1] // 2)
        self.active_app = "Desktop"
        self.current_dir = "Documents"
        self.open_file: str | None = None
        self.files: dict[str, str] = {
            "notes.txt": "VisionPilot demo notes\n",
            "report.txt": "Quarterly report\nrevenue: 42\n",
        }
        self.buffers: dict[str, str] = {"Notes": "", "File Explorer": "", "Settings": ""}
        self.status = "Ready"
        self.scroll = 0
        self.clicks = 0
        self.action_log: list[str] = []
        self.last_input: str = ""
        self.apps = ["File Explorer", "Notes", "Settings"]

    # ------------------------------------------------------------- state text
    @property
    def active_window(self) -> str:
        if self.active_app == "Desktop":
            return "VisionPilot Demo Desktop"
        if self.active_app == "File Explorer":
            return f"File Explorer - {self.current_dir}"
        if self.active_app == "Notes":
            name = self.open_file or "Untitled"
            return f"Notes - {name}"
        return self.active_app

    def visible_lines(self) -> list[str]:
        """Text a human (or OCR) would see on screen."""
        lines: list[str] = [self.active_window]
        if self.active_app == "Desktop":
            lines.append("VisionPilot Demo Desktop")
            lines.extend(f"Icon: {app}" for app in self.apps)
            lines.append("Double-click an icon to open it")
            lines.extend(self.apps)
        elif self.active_app == "File Explorer":
            lines.append(f"Path: C:/Users/demo/{self.current_dir}")
            lines.extend(["< Up", "Documents", "Pictures", "notes.txt", "report.txt"])
            lines.append(f"Search: {self.buffers.get('File Explorer', '')}")
        elif self.active_app == "Notes":
            lines.append(f"File: {self.open_file or 'Untitled'}")
            lines.extend(self._note_lines())
        elif self.active_app == "Settings":
            lines.extend(["Appearance", "Privacy", "About", f"Search: {self.buffers.get('Settings', '')}"])
        lines.append(self.status)
        return lines

    def _note_lines(self) -> list[str]:
        content = self.buffers.get("Notes", "")
        if self.open_file and self.open_file in self.files:
            content = self.files[self.open_file] + content
        lines = content.splitlines() or ["(empty)"]
        return [f"Text: {line}" for line in lines]

    def text(self) -> str:
        return "\n".join(self.visible_lines())

    def snapshot(self) -> dict[str, Any]:
        return {
            "active_window": self.active_window,
            "active_app": self.active_app,
            "visible_text": self.text(),
            "status": self.status,
            "clicks": self.clicks,
        }

    # ---------------------------------------------------------------- layout
    def widgets(self) -> list[dict[str, Any]]:
        """Hit-testable widgets for the current screen."""
        width, height = self.size
        widgets: list[dict[str, Any]] = []

        if self.active_app == "Desktop":
            for index, app in enumerate(self.apps):
                top = 90 + index * 132
                widgets.append(
                    {
                        "kind": "icon",
                        "label": app,
                        "rect": (56, top, 152, top + 96),
                        "action": ("open", app),
                    }
                )
        else:
            left, top = 72, 58
            right, bottom = width - 72, height - self.TASKBAR_HEIGHT - 24
            widgets.append(
                {
                    "kind": "window",
                    "label": self.active_window,
                    "rect": (left, top, right, top + self.TITLE_BAR_HEIGHT),
                    "action": ("focus", self.active_app),
                }
            )
            if self.active_app == "File Explorer":
                row_y = top + 104
                for label, action in (
                    ("< Up", ("updir",)),
                    ("Documents", ("dir", "Documents")),
                    ("Pictures", ("dir", "Pictures")),
                    ("notes.txt", ("file", "notes.txt")),
                    ("report.txt", ("file", "report.txt")),
                ):
                    widgets.append(
                        {
                            "kind": "item",
                            "label": label,
                            "rect": (left + 30, row_y, left + 290, row_y + 26),
                            "action": action,
                        }
                    )
                    row_y += 30
            elif self.active_app == "Notes":
                widgets.append(
                    {
                        "kind": "editor",
                        "label": "Text area",
                        "rect": (left + 30, top + 90, right - 30, bottom - 20),
                        "action": ("focus", "Notes"),
                    }
                )

        # Taskbar buttons are always on top for hit-testing.
        button_width = 150
        for index, app in enumerate(self.apps):
            x = 16 + index * (button_width + 8)
            widgets.append(
                {
                    "kind": "taskbar",
                    "label": app,
                    "rect": (x, height - self.TASKBAR_HEIGHT + 6, x + button_width, height - 6),
                    "action": ("open", app),
                }
            )
        return widgets

    # --------------------------------------------------------------- actions
    def _log(self, message: str) -> None:
        self.action_log.append(message)
        del self.action_log[:-100]
        logger.debug("synthetic desktop: %s", message)

    def _open(self, app: str) -> str:
        self.active_app = app
        self.status = f"Opened {app}"
        self.scroll = 0
        self._log(f"open {app}")
        return f"opened {app} (window: {self.active_window})"

    def click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> str:
        self.position = (x, y)
        self.clicks += 1
        if button == "right":
            return f"right-clicked ({x}, {y}); context menu opened"
        for widget in reversed(self.widgets()):
            left, top, right, bottom = widget["rect"]
            if not (left <= x <= right and top <= y <= bottom):
                continue
            action = widget["action"]
            kind = action[0]
            if kind in {"open", "focus"}:
                return self._open(action[1])
            if kind == "dir":
                self.current_dir = action[1]
                self.status = f"Browsing {action[1]}"
                return f"navigated to {action[1]}"
            if kind == "updir":
                self.current_dir = "Documents"
                return "navigated up to Documents"
            if kind == "file":
                self.open_file = action[1]
                self.active_app = "Notes"
                self.status = f"Opened {action[1]}"
                return f"opened {action[1]} in Notes"
        return f"clicked ({x}, {y}) - no interactive element at that position"

    def move(self, x: int, y: int) -> None:
        self.position = (x, y)

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int) -> str:
        self.position = (to_x, to_y)
        return f"dragged from ({from_x}, {from_y}) to ({to_x}, {to_y})"

    def scroll(self, clicks: int, x: int | None = None, y: int | None = None) -> str:
        self.scroll = max(0, self.scroll - clicks)
        return f"scrolled {clicks} (offset {self.scroll})"

    def type_text(self, text: str) -> str:
        if self.active_app == "Desktop":
            self.status = "No active input field"
            return "typed into the desktop: no text field is focused"
        buffer = self.buffers.setdefault(self.active_app, "")
        self.buffers[self.active_app] = buffer + text
        self.last_input = text
        self.status = f"Typed {len(text)} characters"
        self._log(f"type {text!r}")
        return f"typed {len(text)} characters into {self.active_app}"

    def _note_content(self) -> str:
        base = self.files.get(self.open_file or "", "")
        return base + self.buffers.get("Notes", "")

    def _set_note_content(self, content: str) -> None:
        if self.open_file:
            self.files[self.open_file] = content
            self.buffers["Notes"] = ""
        else:
            self.buffers["Notes"] = content

    def press(self, keys: Iterable[str], repeats: int = 1) -> str:
        lowered = [str(key).lower() for key in keys]
        combo = set(lowered)
        description = f"pressed {'+'.join(lowered)}"
        if {"ctrl", "s"} <= combo:
            if self.active_app == "Notes" and self.open_file:
                self._set_note_content(self._note_content())
                self.status = f"Saved {self.open_file}"
            else:
                self.status = "Saved"
            return self.status
        if "esc" in combo:
            self.active_app = "Desktop"
            self.status = "Window closed"
            return self.status
        if self.active_app == "Desktop":
            self.status = "No active window"
            return description

        for _ in range(max(1, repeats)):
            if "enter" in combo:
                if self.active_app == "Notes":
                    self._set_note_content(self._note_content() + "\n")
                self.status = "Enter"
            elif "backspace" in combo:
                content = self._note_content()
                if self.active_app == "Notes":
                    self._set_note_content(content[:-1])
                self.status = "Backspace"
            elif "tab" in combo:
                if self.active_app == "Notes":
                    self._set_note_content(self._note_content() + "    ")
                self.status = "Tab"
            elif "space" in combo:
                if self.active_app == "Notes":
                    self._set_note_content(self._note_content() + " ")
            else:
                self.status = f"Key {'+'.join(lowered)}"
        return description

    # --------------------------------------------------------------- render
    def render(self) -> Any:
        """Render the desktop to a Pillow image (``None`` without Pillow)."""
        if Image is None:
            return None
        width, height = self.size
        image = Image.new("RGB", (width, height), (24, 26, 38))
        draw = ImageDraw.Draw(image)
        font = _load_font(17)
        title_font = _load_font(19, bold=True)
        small = _load_font(14)

        draw.rectangle((0, 0, width, height - self.TASKBAR_HEIGHT), fill=(30, 33, 48))
        draw.text((24, 14), "VisionPilot Demo Desktop  |  simulated", fill=(120, 130, 160), font=small)

        if self.active_app == "Desktop":
            for index, app in enumerate(self.apps):
                top = 90 + index * 132
                draw.rectangle((56, top, 152, top + 96), fill=(45, 49, 70), outline=(90, 100, 140))
                draw.rectangle((72, top + 14, 136, top + 66), fill=(58, 64, 92))
                draw.text((60, top + 102), app, fill=(225, 230, 245), font=font)
            draw.text((320, 120), "Double-click an icon to open it", fill=(150, 160, 190), font=font)
        else:
            left, top = 72, 58
            right, bottom = width - 72, height - self.TASKBAR_HEIGHT - 24
            draw.rectangle((left, top, right, bottom), fill=(38, 41, 58), outline=(110, 120, 160))
            draw.rectangle((left, top, right, top + self.TITLE_BAR_HEIGHT), fill=(52, 57, 80))
            draw.text((left + 14, top + 9), self.active_window, fill=(240, 244, 255), font=title_font)
            draw.text((right - 30, top + 9), "x", fill=(240, 120, 120), font=title_font)

            y = top + 60
            draw.text((left + 18, y), f"Path: C:/Users/demo/{self.current_dir}", fill=(140, 200, 255), font=small)
            y += 30
            if self.active_app == "File Explorer":
                for widget in self.widgets():
                    if widget["kind"] != "item":
                        continue
                    bx = widget["rect"][0]
                    by = widget["rect"][1]
                    label = widget["label"]
                    draw.rectangle((bx, by, bx + 250, by + 24), fill=(46, 51, 72))
                    draw.text((bx + 8, by + 3), label, fill=(225, 230, 245), font=small)
                draw.text((left + 18, bottom - 34), f"Search: {self.buffers.get('File Explorer', '')}", fill=(160, 200, 160), font=small)
            elif self.active_app == "Notes":
                draw.rectangle((left + 20, top + 92, right - 40, bottom - 46), fill=(30, 33, 46))
                ty = top + 100
                for line in self._note_lines():
                    draw.text((left + 30, ty), line, fill=(225, 230, 245), font=font)
                    ty += 26
                draw.text((left + 18, bottom - 34), f"Search: {self.buffers.get('Notes', '')}", fill=(160, 200, 160), font=small)
            elif self.active_app == "Settings":
                for index, item in enumerate(("Appearance", "Privacy", "About")):
                    draw.text((left + 30, top + 100 + index * 30), item, fill=(225, 230, 245), font=font)

        # Taskbar
        bar_top = height - self.TASKBAR_HEIGHT
        draw.rectangle((0, bar_top, width, height), fill=(18, 20, 30))
        for widget in self.widgets():
            if widget["kind"] != "taskbar":
                continue
            bx1, by1, bx2, by2 = widget["rect"]
            active = widget["label"] == self.active_app
            draw.rectangle((bx1, by1, bx2, by2), fill=(70, 90, 140) if active else (40, 44, 60))
            draw.text((bx1 + 10, by1 + 8), widget["label"], fill=(235, 238, 250), font=small)
        draw.text((width - 150, bar_top + 12), "VisionPilot", fill=(130, 140, 175), font=small)
        draw.text((24, height - self.TASKBAR_HEIGHT - 26), f"status: {self.status}", fill=(120, 220, 170), font=small)
        return image


def _load_font(size: int, bold: bool = False):
    if ImageFont is None:
        return None
    for name in (("arialbd.ttf" if bold else "arial.ttf"), "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:  # noqa: BLE001
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Screenshot backends
# ---------------------------------------------------------------------------
def _capture_mss() -> Any:
    import mss  # type: ignore
    from PIL import Image as PILImage

    with mss.mss() as sct:
        monitor = sct.monitors[0]
        shot = sct.grab(monitor)
        return PILImage.frombytes("RGB", shot.size, shot.rgb)


def _capture_pillow() -> Any:
    from PIL import ImageGrab

    return ImageGrab.grab(all_screens=True)


def _capture_pyautogui() -> Any:
    from executor.base import load_pyautogui

    gui = load_pyautogui()
    if gui is None:
        raise RuntimeError("pyautogui unavailable")
    return gui.screenshot()


class ScreenCapture:
    """Capture the screen with the best available backend."""

    def __init__(self, settings: Settings, desktop: SimulatedDesktop | None = None) -> None:
        self.settings = settings
        self.desktop = desktop
        if self.desktop is None and settings.uses_synthetic_desktop:
            self.desktop = SimulatedDesktop()
        self.backend = self._resolve_backend(settings.screenshot_backend)
        self.last: Screenshot | None = None
        self._counter = 0

    def _resolve_backend(self, requested: str) -> str:
        if requested == "synthetic":
            return "synthetic"
        if requested == "none":
            return "none"
        if requested == "mss":
            return "mss"
        if requested == "pillow":
            return "pillow"
        if requested == "pyautogui":
            return "pyautogui"
        # auto
        if self.desktop is not None:
            return "synthetic"
        for name, probe in (
            ("mss", lambda: __import__("mss")),
            ("pillow", lambda: __import__("PIL.ImageGrab")),
            ("pyautogui", lambda: __import__("pyautogui")),
        ):
            try:
                probe()
                return name
            except Exception:  # noqa: BLE001
                continue
        return "none"

    # ------------------------------------------------------------- capture
    def capture(self, *, save: bool = True) -> Screenshot:
        started = time.perf_counter()
        image = None
        backend = self.backend
        text_hint = None
        meta: dict[str, Any] = {}

        try:
            if backend == "synthetic" and self.desktop is not None:
                image = self.desktop.render()
                text_hint = self.desktop.text()
                meta["active_window"] = self.desktop.active_window
            elif backend == "mss":
                image = _capture_mss()
            elif backend == "pillow":
                image = _capture_pillow()
            elif backend == "pyautogui":
                image = _capture_pyautogui()
        except Exception as exc:  # noqa: BLE001 - degrade instead of crashing the loop
            logger.warning("screenshot backend %s failed: %s", backend, exc)
            meta["backend_error"] = str(exc)
            backend = "none"
            image = None

        width, height = (image.width, image.height) if image is not None else self._fallback_size()
        png = None
        if image is not None:
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            png = buffer.getvalue()
        digest_source = png or (text_hint or "").encode("utf-8") or str(time.time()).encode()
        sha = hashlib.sha1(digest_source).hexdigest()

        path = None
        if save and png:
            path = self._save_png(png)

        screenshot = Screenshot(
            image=image,
            width=width,
            height=height,
            timestamp=_now(),
            sha=sha,
            backend=backend,
            path=path,
            text_hint=text_hint,
            meta=meta,
        )
        screenshot.meta["capture_ms"] = int((time.perf_counter() - started) * 1000)
        self.last = screenshot
        if path is not None:
            self._rotate()
        return screenshot

    def capture_region(self, left: int, top: int, right: int, bottom: int) -> Screenshot:
        shot = self.capture(save=False)
        if shot.image is None:
            return shot
        box = (
            max(0, left),
            max(0, top),
            min(shot.width, right),
            min(shot.height, bottom),
        )
        cropped = shot.image.crop(box)
        buffer = io.BytesIO()
        cropped.save(buffer, format="PNG")
        png = buffer.getvalue()
        path = self._save_png(png)
        return Screenshot(
            image=cropped,
            width=cropped.width,
            height=cropped.height,
            timestamp=_now(),
            sha=hashlib.sha1(png).hexdigest(),
            backend=shot.backend,
            path=path,
            text_hint=shot.text_hint,
            meta={"cropped_from": str(shot.path)},
        )

    def _fallback_size(self) -> tuple[int, int]:
        if self.desktop is not None:
            return self.desktop.size
        from executor.base import get_screen_size

        return get_screen_size()

    def _save_png(self, png: bytes) -> Path:
        directory = Path(self.settings.screenshot_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self._counter += 1
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = directory / f"screen_{stamp}_{self._counter:04d}.png"
        try:
            path.write_bytes(png)
        except OSError as exc:  # pragma: no cover
            logger.warning("cannot save screenshot: %s", exc)
            return None  # type: ignore[return-value]
        return path

    def _rotate(self) -> None:
        keep = max(1, int(self.settings.keep_screenshots))
        try:
            files = sorted(
                Path(self.settings.screenshot_dir).glob("screen_*.png"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            for stale in files[keep:]:
                stale.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass


def latest_screenshot(settings: Settings) -> Path | None:
    directory = Path(settings.screenshot_dir)
    if not directory.exists():
        return None
    files = sorted(directory.glob("screen_*.png"), key=lambda item: item.stat().st_mtime, reverse=True)
    return files[0] if files else None
