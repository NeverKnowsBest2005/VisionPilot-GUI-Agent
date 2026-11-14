"""Browser automation tools backed by Playwright.

Playwright is an optional dependency: when it is missing the tools report
themselves as unavailable and the planner simply never sees them.  A single
:class:`BrowserManager` keeps one page alive across tool calls so a task can
click, fill and read without relaunching Chromium every step.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from executor.base import (
    FunctionTool,
    Tool,
    ToolContext,
    ToolParameter,
    ToolResult,
)

logger = logging.getLogger("visionpilot.executor.browser")

_DEFAULT_TIMEOUT_MS = 15_000
_ALLOWED_URL_PREFIXES = ("http://", "https://")
_BLOCKED_URL_SCHEMES = ("file:", "javascript:", "data:", "vbscript:", "ftp:", "mailto:")


def _describe(exc: BaseException) -> str:
    """Readable one-line reason for a Playwright failure.

    Playwright raises ``Error`` subclasses whose ``str()`` is sometimes empty
    (notably when the driver process cannot be started), which used to surface
    as ``browser open failed: `` with no detail at all.
    """
    message = str(exc).strip()
    if message:
        return message
    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        message = str(cause).strip()
        if message:
            return f"{type(exc).__name__}: {message}"
    return type(exc).__name__


def normalise_url(url: str) -> str:
    """Validate/normalise a browser URL.

    Only ``http``, ``https`` and ``about:blank`` are allowed.  Notably
    ``file://`` is rejected: the sandbox protects filesystem tools, and the
    browser must not become a way to read local files.
    """
    text = str(url or "").strip()
    if not text:
        raise ValueError("url is required")
    lowered = text.lower()
    if lowered.startswith(_BLOCKED_URL_SCHEMES):
        raise ValueError(
            f"unsupported URL scheme in {text!r}; only http://, https:// and about:blank are allowed"
        )
    if lowered.startswith(_ALLOWED_URL_PREFIXES):
        return text
    if lowered == "about:blank" or lowered.startswith("about:blank?"):
        return text
    if "://" in text or lowered.startswith("about:"):
        raise ValueError(
            f"unsupported URL scheme in {text!r}; only http://, https:// and about:blank are allowed"
        )
    return "https://" + text.lstrip("/")


def ensure_writable_temp_dir() -> Path:
    """Return a temp directory usable by Playwright's Node driver.

    Playwright creates its artifacts directory with ``fs.mkdtemp`` inside the
    Node driver, which inherits ``TEMP``/``TMP`` from this process.  Some
    Windows hosts deny ``python.exe``/``node.exe`` write access to
    ``%LOCALAPPDATA%\\Temp`` even though the interactive shell can write there;
    the driver then fails with ``EPERM: operation not permitted, mkdtemp``.
    (Python's own ``tempfile`` silently falls back to the *current working
    directory* in that case, which is why stray ``tmp*`` folders can appear in
    the project root.)

    The first writable location among ``TEMP``/``TMP``/``TMPDIR`` is kept as
    is.  When none is writable, ``logs/tmp`` under the project is used and the
    environment is updated so the driver sees it too.
    """
    candidates = [
        value
        for value in (
            os.environ.get("TEMP"),
            os.environ.get("TMP"),
            os.environ.get("TMPDIR"),
        )
        if value
    ]
    seen: set[Path] = set()
    for value in candidates:
        directory = Path(value)
        if directory in seen:
            continue
        seen.add(directory)
        try:
            probe = directory / f".vp-temp-probe-{os.getpid()}"
            probe.touch()
            probe.unlink()
            return directory
        except OSError:
            continue

    fallback = Path(__file__).resolve().parent.parent / "logs" / "tmp"
    fallback.mkdir(parents=True, exist_ok=True)
    for variable in ("TEMP", "TMP", "TMPDIR"):
        os.environ[variable] = str(fallback)
    # ``gettempdir()`` caches its answer; reset the cache as well.
    tempfile.tempdir = str(fallback)
    logger.info("system temp directory is not writable; using %s", fallback)
    return fallback


def playwright_available() -> bool:
    try:  # pragma: no cover - depends on optional install
        import playwright.async_api  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


class BrowserManager:
    """Owns a Playwright browser/page pair for the lifetime of a task."""

    def __init__(self, *, headless: bool = True) -> None:
        self.headless = headless
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def started(self) -> bool:
        return self._page is not None

    async def page(self) -> Any:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # A previous loop (test or reload) owns the old objects; close what
            # we can and start fresh on this loop.
            try:
                await self.close()
            except Exception:  # noqa: BLE001 - objects may belong to a dead loop
                pass
            self._loop = loop
        if self._page is not None:
            return self._page

        # The Node driver spawns below and inherits TEMP; make sure it can
        # actually create its artifacts directory.
        ensure_writable_temp_dir()

        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(headless=self.headless)
            context = await self._browser.new_context(viewport={"width": 1280, "height": 800})
            self._page = await context.new_page()
            self._page.set_default_timeout(_DEFAULT_TIMEOUT_MS)
        except Exception:
            await self.close()
            raise
        logger.info("browser started (headless=%s)", self.headless)
        return self._page

    async def close(self) -> None:
        page, browser, playwright = self._page, self._browser, self._playwright
        self._page = self._browser = self._playwright = None
        for closer in (
            getattr(page, "close", None),
            getattr(browser, "close", None),
            getattr(playwright, "stop", None),
        ):
            if closer is None:
                continue
            try:
                await closer()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
async def _open(context: ToolContext, manager: BrowserManager, url: str) -> ToolResult:
    try:
        url = normalise_url(url)
    except ValueError as exc:
        return ToolResult(tool="browser_open", ok=False, error=str(exc))
    try:
        page = await manager.page()
        response = await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(250)
        title = await page.title()
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool="browser_open", ok=False, error=f"browser open failed: {_describe(exc)}")
    status = getattr(response, "status", None)
    return ToolResult(
        tool="browser_open",
        ok=True,
        output=f"opened {url} -> {title!r} (status {status})",
        data={"url": page.url, "title": title, "status": status},
    )


async def _click(context: ToolContext, manager: BrowserManager, selector: str) -> ToolResult:
    try:
        page = await manager.page()
        await page.click(selector, timeout=_DEFAULT_TIMEOUT_MS)
        await page.wait_for_timeout(250)
        title = await page.title()
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool="browser_click", ok=False, error=f"click failed: {_describe(exc)}")
    return ToolResult(
        tool="browser_click",
        ok=True,
        output=f"clicked {selector}; page is now {page.url}",
        data={"url": page.url, "title": title, "selector": selector},
    )


async def _fill(context: ToolContext, manager: BrowserManager, selector: str, text: str) -> ToolResult:
    try:
        page = await manager.page()
        await page.fill(selector, text, timeout=_DEFAULT_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool="browser_fill", ok=False, error=f"fill failed: {_describe(exc)}")
    return ToolResult(
        tool="browser_fill", ok=True, output=f"filled {selector}", data={"selector": selector, "text": text}
    )


async def _press(context: ToolContext, manager: BrowserManager, key: str) -> ToolResult:
    try:
        page = await manager.page()
        await page.keyboard.press(key)
        await page.wait_for_timeout(150)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool="browser_press", ok=False, error=f"key press failed: {_describe(exc)}")
    return ToolResult(tool="browser_press", ok=True, output=f"pressed {key}", data={"key": key})


async def _get_text(context: ToolContext, manager: BrowserManager, selector: str = "body", max_chars: int = 4000) -> ToolResult:
    try:
        page = await manager.page()
        text = await page.inner_text(selector, timeout=_DEFAULT_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool="browser_get_text", ok=False, error=f"read failed: {_describe(exc)}")
    text = " ".join(text.split())
    snippet = text[: max(100, int(max_chars))]
    return ToolResult(
        tool="browser_get_text",
        ok=True,
        output=snippet or "(empty)",
        data={"url": page.url, "selector": selector, "text": snippet, "length": len(text)},
    )


async def _screenshot(context: ToolContext, manager: BrowserManager, path: str = "browser.png") -> ToolResult:
    try:
        page = await manager.page()
        target = Path(path)
        if not target.is_absolute():
            target = Path(context.settings.workspace_dir) / target
        target = target.resolve()
        workspace = Path(context.settings.workspace_dir).resolve()
        try:
            target.relative_to(workspace)
        except ValueError:
            return ToolResult(tool="browser_screenshot", ok=False, error="path escapes the workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(target), full_page=False)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool="browser_screenshot", ok=False, error=f"screenshot failed: {_describe(exc)}")
    return ToolResult(
        tool="browser_screenshot",
        ok=True,
        output=f"saved browser screenshot to {target.name}",
        data={"path": str(target), "url": page.url},
    )


async def _close(context: ToolContext, manager: BrowserManager) -> ToolResult:
    await manager.close()
    return ToolResult(tool="browser_close", ok=True, output="browser closed")


def build_browser_tools(manager: BrowserManager | None = None) -> list[Tool]:
    """Factory returning browser tools bound to a shared manager."""
    manager = manager or BrowserManager()

    def available() -> bool:
        return playwright_available()

    tools: list[Tool] = [
        FunctionTool(
            name="browser_open",
            description="Open an http(s) URL in the automation browser (file:// is rejected).",
            category="browser",
            danger="caution",
            available=available,
            unavailable_reason="playwright is not installed (pip install playwright && playwright install chromium)",
            parameters=[
                ToolParameter(name="url", type="string", description="URL to open", required=True),
            ],
            handler=lambda ctx, url: _open(ctx, manager, url),
        ),
        FunctionTool(
            name="browser_click",
            description="Click an element identified by a CSS selector.",
            category="browser",
            danger="caution",
            available=available,
            unavailable_reason="playwright is not installed",
            parameters=[
                ToolParameter(name="selector", type="string", description="CSS selector", required=True),
            ],
            handler=lambda ctx, selector: _click(ctx, manager, selector),
        ),
        FunctionTool(
            name="browser_fill",
            description="Fill an input/textarea identified by a CSS selector.",
            category="browser",
            danger="caution",
            available=available,
            unavailable_reason="playwright is not installed",
            parameters=[
                ToolParameter(name="selector", type="string", description="CSS selector", required=True),
                ToolParameter(name="text", type="string", description="Text to enter", required=True),
            ],
            handler=lambda ctx, selector, text: _fill(ctx, manager, selector, text),
        ),
        FunctionTool(
            name="browser_press",
            description="Press a keyboard key inside the browser page.",
            category="browser",
            danger="caution",
            available=available,
            unavailable_reason="playwright is not installed",
            parameters=[
                ToolParameter(name="key", type="string", description="Key, e.g. Enter", required=True),
            ],
            handler=lambda ctx, key: _press(ctx, manager, key),
        ),
        FunctionTool(
            name="browser_get_text",
            description="Read the text content of a page element.",
            category="browser",
            available=available,
            unavailable_reason="playwright is not installed",
            parameters=[
                ToolParameter(name="selector", type="string", description="CSS selector", default="body"),
                ToolParameter(name="max_chars", type="integer", description="Character limit", default=4000),
            ],
            handler=lambda ctx, selector="body", max_chars=4000: _get_text(ctx, manager, selector, max_chars),
        ),
        FunctionTool(
            name="browser_screenshot",
            description="Save a screenshot of the current browser page into the workspace.",
            category="browser",
            available=available,
            unavailable_reason="playwright is not installed",
            parameters=[
                ToolParameter(name="path", type="string", description="Relative output path", default="browser.png"),
            ],
            handler=lambda ctx, path="browser.png": _screenshot(ctx, manager, path),
        ),
        FunctionTool(
            name="browser_close",
            description="Close the automation browser and release resources.",
            category="browser",
            available=available,
            unavailable_reason="playwright is not installed",
            parameters=[],
            handler=lambda ctx: _close(ctx, manager),
        ),
    ]
    # Expose the manager so the agent can shut it down deterministically.
    for tool in tools:
        setattr(tool, "manager", manager)
    return tools
