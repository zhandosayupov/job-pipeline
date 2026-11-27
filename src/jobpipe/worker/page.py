"""The browser as the worker sees it. Appliers only talk to `Page`, so they are tested with a fake page
on HTML fixtures; `PlaywrightPage` is the real one (optional dependency group `worker`).

The browser runs with a dedicated persistent profile per platform (profiles/<platform>/), logged in by
the owner once. No stealth patches, no fingerprint changes, no proxies (ADR 0012).
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class Page(Protocol):
    def goto(self, url: str) -> None: ...
    def html(self) -> str: ...
    def url(self) -> str: ...
    def fill(self, key: str, value: str) -> None: ...
    def choose(self, key: str, option: str, kind: str) -> None: ...     # select / radio / checkbox
    def upload(self, key: str, path: str) -> None: ...
    def click(self, selector: str) -> None: ...
    def exists(self, selector: str) -> bool: ...
    def screenshot(self, path: str) -> None: ...
    def wait(self, seconds: float) -> None: ...
    def close(self) -> None: ...


class PlaywrightPage:
    """Chromium through Playwright, one persistent context per platform. Started per batch, closed after."""

    def __init__(self, profile_dir: Path, headless: bool = True, timeout_ms: int = 30000):
        from playwright.sync_api import sync_playwright  # optional dependency: uv sync --group worker

        self._pw = sync_playwright().start()
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._ctx = self._pw.chromium.launch_persistent_context(str(profile_dir), headless=headless)
        self._ctx.set_default_timeout(timeout_ms)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    def goto(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded")

    def html(self) -> str:
        return self._page.content()

    def url(self) -> str:
        return self._page.url

    def fill(self, key: str, value: str) -> None:
        self._page.locator(key).first.fill(value)

    def choose(self, key: str, option: str, kind: str) -> None:
        if kind == "select":
            self._page.locator(key).first.select_option(label=option)
        elif kind == "radio":
            self._page.get_by_label(option, exact=True).first.check()
        else:
            loc = self._page.locator(key).first
            (loc.check if option.lower() in ("yes", "true", "да", "on") else loc.uncheck)()

    def upload(self, key: str, path: str) -> None:
        self._page.locator(key).first.set_input_files(path)

    def click(self, selector: str) -> None:
        self._page.locator(selector).first.click()

    def exists(self, selector: str) -> bool:
        return self._page.locator(selector).count() > 0

    def screenshot(self, path: str) -> None:
        self._page.screenshot(path=path, full_page=True)

    def wait(self, seconds: float) -> None:
        self._page.wait_for_timeout(int(seconds * 1000))

    def close(self) -> None:
        self._ctx.close()
        self._pw.stop()
