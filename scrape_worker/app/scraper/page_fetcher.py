"""
Playwright page fetcher.

Renders a URL in headless Chromium and returns raw HTML for HtmlCleaner.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal, Optional

from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

WaitUntil = Literal["load", "domcontentloaded", "networkidle"]

PLAYWRIGHT_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
]

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class FetchResult:
    url: str
    html: str
    final_url: str
    raw_bytes: int


async def fetch_page_html(
    url: str,
    *,
    wait: float = 2.0,
    wait_for_selector: Optional[str] = None,
    wait_until: WaitUntil = "domcontentloaded",
    navigation_timeout_ms: int = 30_000,
    headless: bool = True,
) -> FetchResult:
    """
    Navigate to ``url`` with Playwright and return the rendered HTML.

    Mirrors a typical headless scrape flow:
    goto → optional selector wait → settle sleep → page.content().
    """
    playwright = browser = context = None
    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=headless,
            args=PLAYWRIGHT_LAUNCH_ARGS,
        )
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            extra_http_headers=DEFAULT_HEADERS,
            java_script_enabled=True,
            locale="en-US",
        )
        page = await context.new_page()

        log.info("Navigating to %s (wait_until=%s)", url, wait_until)
        await page.goto(url, wait_until=wait_until, timeout=navigation_timeout_ms)

        if wait_for_selector:
            log.info("Waiting for selector %r", wait_for_selector)
            await page.wait_for_selector(wait_for_selector, timeout=15_000)

        if wait and wait > 0:
            log.info("Waiting %.1fs for JS to settle", wait)
            await asyncio.sleep(wait)

        html = await page.content()
        final_url = page.url
    finally:
        await _close(playwright, browser, context)

    raw_bytes = len(html.encode("utf-8"))
    log.info("Fetched %s bytes from %s", f"{raw_bytes:,}", final_url)
    return FetchResult(url=url, html=html, final_url=final_url, raw_bytes=raw_bytes)


async def _close(playwright, browser, context) -> None:
    try:
        if context:
            await context.close()
    except Exception:
        log.debug("context close failed", exc_info=True)
    try:
        if browser:
            await browser.close()
    except Exception:
        log.debug("browser close failed", exc_info=True)
    try:
        if playwright:
            await playwright.stop()
            await asyncio.sleep(0.1)
    except Exception:
        log.debug("playwright stop failed", exc_info=True)
