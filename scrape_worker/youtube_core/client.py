"""Playwright / HTML ytInitialData client with operation-scoped cache."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Optional

from utils.scrape_html import HtmlScraper

log = logging.getLogger(__name__)

_YT_INITIAL_DATA_RE = re.compile(
    r"var ytInitialData\s*=\s*(\{.*?\});\s*</script>",
    re.DOTALL,
)
_CONSENT_BUTTON_LABELS = (
    "Accept all",
    "Accept All",
    "I agree",
    "Agree",
    "Accept",
)

# Default shared cache (cleared per scrape/status by clear_cache).
_YT_PAGE_CACHE: dict[str, object] = {}
_YT_PAGE_CACHE_LOCK = threading.Lock()
_YT_CLASSIFY_CACHE: dict[tuple[str, str, str], dict] = {}


def clear_cache() -> None:
    """Drop cached ytInitialData / classifications."""
    with _YT_PAGE_CACHE_LOCK:
        _YT_PAGE_CACHE.clear()
        _YT_CLASSIFY_CACHE.clear()


def cache_key_for_url(url: str) -> str:
    return (url or "").strip().split("?", 1)[0].rstrip("/").lower()


def normalize_channel_base_url(channel_url: str) -> str:
    raw = (channel_url or "").strip()
    if not raw:
        return raw
    base = raw.split("?", 1)[0].rstrip("/")
    for suffix in ("/streams", "/videos", "/featured", "/live"):
        if base.lower().endswith(suffix):
            return base[: -len(suffix)]
    return base


def channel_tab_url(channel_url: str, tab: str) -> str:
    base = normalize_channel_base_url(channel_url)
    tab = (tab or "streams").strip().strip("/")
    return f"{base}/{tab}"


class YtInitialDataClient:
    """Fetch and cache YouTube channel page ytInitialData."""

    def __init__(self) -> None:
        self.scraper = None

    def _fetch_youtube_initial_data(self, url: str):
        """Load a YouTube channel page and return window.ytInitialData (cached per URL)."""
        key = cache_key_for_url(url)
        with _YT_PAGE_CACHE_LOCK:
            if key in _YT_PAGE_CACHE:
                log.info("ytInitialData cache hit url=%s", url)
                return _YT_PAGE_CACHE[key]

        data = self._fetch_youtube_initial_data_uncached(url)
        with _YT_PAGE_CACHE_LOCK:
            _YT_PAGE_CACHE[key] = data
        return data

    def _fetch_youtube_initial_data_many(self, urls: list[str]) -> dict[str, object]:
        """
        Fetch several channel pages in one Playwright browser (consent once).
        Skips URLs already in the page cache. Returns map of cache_key -> data.
        """
        needed: list[str] = []
        out: dict[str, object] = {}
        for url in urls:
            key = cache_key_for_url(url)
            with _YT_PAGE_CACHE_LOCK:
                if key in _YT_PAGE_CACHE:
                    out[key] = _YT_PAGE_CACHE[key]
                    log.info("ytInitialData cache hit url=%s", url)
                    continue
            needed.append(url)

        if not needed:
            return out

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            for url in needed:
                key = cache_key_for_url(url)
                html = self._fetch_with_html_scraper(url)
                data = self._extract_yt_initial_data(html)
                with _YT_PAGE_CACHE_LOCK:
                    _YT_PAGE_CACHE[key] = data
                out[key] = data
            return out

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            try:
                context = browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    timezone_id="America/New_York",
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                )
                page = context.new_page()
                for url in needed:
                    key = cache_key_for_url(url)
                    data = self._load_yt_initial_data_on_page(page, url)
                    with _YT_PAGE_CACHE_LOCK:
                        _YT_PAGE_CACHE[key] = data
                    out[key] = data
            finally:
                browser.close()
        return out

    def _fetch_youtube_initial_data_uncached(self, url: str):
        """Load a YouTube channel page and return window.ytInitialData."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.warning("Playwright not installed; falling back to HtmlScraper HTML parse")
            html = self._fetch_with_html_scraper(url)
            return self._extract_yt_initial_data(html)

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            try:
                context = browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    timezone_id="America/New_York",
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                )
                page = context.new_page()
                return self._load_yt_initial_data_on_page(page, url)
            finally:
                browser.close()

    def _load_yt_initial_data_on_page(self, page, url: str):
        log.info("Playwright navigating to %s", url)
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        self._dismiss_youtube_consent(page)
        try:
            page.wait_for_function(
                "() => window.ytInitialData && window.ytInitialData.contents",
                timeout=30_000,
            )
        except Exception:
            log.warning(
                "Timed out waiting for ytInitialData (url=%s final=%s)",
                url,
                page.url,
            )
            return self._extract_yt_initial_data(page.content())

        page.wait_for_timeout(1000)
        data = page.evaluate("() => window.ytInitialData")
        log.info("Loaded ytInitialData via Playwright (final_url=%s)", page.url)
        return data

    def _fetch_with_html_scraper(self, url: str) -> str:
        if self.scraper is None:
            self.scraper = HtmlScraper()
        html = self.scraper.scrape_html(url=url, render="true")
        if isinstance(html, dict) and "max_failure" in html:
            log.warning("HtmlScraper failed for %s: %s", url, html)
            return ""
        return html or ""

    @staticmethod
    def _dismiss_youtube_consent(page) -> None:
        try:
            current = page.url or ""
            has_banner = False
            try:
                has_banner = bool(page.locator("text=Before you continue").count())
            except Exception:
                has_banner = False
            if "consent.youtube" not in current and not has_banner:
                return

            log.info("YouTube consent page detected; attempting Accept all")
            for label in _CONSENT_BUTTON_LABELS:
                try:
                    btn = page.get_by_role("button", name=label)
                    if btn.count():
                        btn.first.click(timeout=4000)
                        log.info("Clicked consent button %r", label)
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=15_000)
                        except Exception:
                            pass
                        page.wait_for_timeout(1500)
                        return
                except Exception:
                    continue
            try:
                page.locator('button[aria-label*="Accept"]').first.click(timeout=3000)
                page.wait_for_timeout(1500)
            except Exception:
                log.warning("Could not click a YouTube consent button")
        except Exception:
            log.exception("Error while dismissing YouTube consent")

    @staticmethod
    def _extract_yt_initial_data(html: str):
        if not html:
            return None
        match = _YT_INITIAL_DATA_RE.search(html)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                log.exception("Failed to JSON-decode ytInitialData (regex path)")

        marker = "var ytInitialData = "
        start = html.find(marker)
        if start < 0:
            marker = "ytInitialData = "
            start = html.find(marker)
        if start < 0:
            return None
        json_str = html[start + len(marker) :]
        brace_count = 0
        end = None
        for i, char in enumerate(json_str):
            if char == "{":
                brace_count += 1
            elif char == "}":
                brace_count -= 1
                if brace_count == 0 and i > 0:
                    end = i + 1
                    break
        if end is None:
            return None
        try:
            return json.loads(json_str[:end])
        except json.JSONDecodeError:
            log.exception("Failed to JSON-decode ytInitialData (brace path)")
            return None

    @classmethod
    def _live_tab_items(cls, yt_initial_data: dict) -> list:
        return cls._tab_grid_items(
            yt_initial_data, preferred_titles=("live", "livestreams")
        )

    @classmethod
    def _videos_tab_items(cls, yt_initial_data: dict) -> list:
        return cls._tab_grid_items(yt_initial_data, preferred_titles=("videos",))

    @classmethod
    def _tab_grid_items(
        cls, yt_initial_data: dict, preferred_titles: tuple[str, ...]
    ) -> list:
        """Return richGrid contents for a named channel tab (Live / Videos / …)."""
        try:
            tabs = yt_initial_data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"]
        except (KeyError, TypeError):
            return []
        preferred = {t.lower() for t in preferred_titles}
        selected_items = []
        for element_i in tabs:
            renderer = element_i.get("tabRenderer") or element_i.get(
                "expandableTabRenderer"
            )
            if not renderer:
                continue
            title = (renderer.get("title") or "").lower().strip()
            content = renderer.get("content") or {}
            grid = content.get("richGridRenderer") or {}
            items = grid.get("contents") or []
            if title in preferred and items:
                return items
            if renderer.get("selected") and items and not selected_items:
                selected_items = items
        return selected_items

    @staticmethod
    def normalize_channel_base_url(channel_url: str) -> str:
        """Strip a tab suffix (/streams, /videos, …) from a channel URL."""
        url = (channel_url or "").strip().rstrip("/")
        url = re.sub(
            r"/(streams|videos|live|featured|community|playlists|channels|about)/?$",
            "",
            url,
            flags=re.IGNORECASE,
        )
        return url.rstrip("/")

    @classmethod
    def channel_tab_url(cls, channel_url: str, tab: str) -> str:
        """Build ``…/streams`` or ``…/videos`` from any channel / tab URL."""
        base = cls.normalize_channel_base_url(channel_url)
        tab = (tab or "streams").strip().strip("/")
        return f"{base}/{tab}"


def get_classify_cache() -> dict:
    return _YT_CLASSIFY_CACHE


def get_page_cache_lock() -> threading.Lock:
    return _YT_PAGE_CACHE_LOCK
