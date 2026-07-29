# scrape_html.py
import time
from enum import Enum
from random import uniform
from typing import Callable
from bs4 import BeautifulSoup
from pathlib import Path
import requests
from dotenv import load_dotenv
from urllib.parse import urlparse, parse_qs
from logging_config import get_dedicated_debug_logger, LOG_LEVEL

# Prefer project-root .env (cwd-independent under uvicorn/reload)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_PROJECT_ROOT / ".env", override=False)
load_dotenv(override=False)

log = get_dedicated_debug_logger(__name__)
log.setLevel(LOG_LEVEL)

# Browser-like headers for direct HTML fetches so sites return normal pages.
DEFAULT_HTML_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/115.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

_BLOCK_MARKERS = (
    "attention required",
    "just a moment",
    "cf-browser-verification",
    "sorry, you have been blocked",
    "enable javascript and cookies to continue",
    "checking your browser before accessing",
)


def _looks_blocked(body: str | None) -> bool:
    if not body:
        return True
    lower = body.lower()
    return any(marker in lower for marker in _BLOCK_MARKERS)


class HTMLTags(str, Enum):
    COLUMNS_TAG = "td"
    ROWS_TAG = "tr"
    LINK_TAG = "a"
    TABLE_TAG = "table"
    DIV_TAG = "div"
    H1_TAG = "h1"
    H2_TAG = "h2"
    H3_TAG = "h3"
    H4_TAG = "h4"
    H5_TAG = "h5"
    H6_TAG = "h6"
    PARAGRAPH_TAG = "p"
    LINK_ATTRIBUTE = "href"
    MARKED_COLUMNS_TAG = "ol"
    MARKED_ROWS_TAG = "li"
    LIST_ITEM_TAG = "li"
    LIST_TAG = "ul"
    SCRIPT_TAG = "script"
    SPAN_TAG = "span"


class HTMLAttributes(str, Enum):
    LINK_ATTRIBUTE = "href"
    CLASS_ATTRIBUTE = "class"
    ID_ATTRIBUTE = "id"
    ONCLICK_ATTRIBUTE = "onclick"


class ReturnType(str, Enum):
    TEXT = "text"  # default: identical to today (HTML text / soup string)
    BYTES = "bytes"  # raw bytes (e.g., PDFs)
    RESPONSE = "response"  # the full requests.Response object


class HtmlScraper:
    """
    Fetches HTML or other web resources via direct HTTP, with Playwright
    fallback when the response is blocked/empty (e.g. Cloudflare).

    ScraperAPI is not used. Legacy ``fetch_with_scraperapi`` /
    ``post_with_scraperapi`` entry points still exist for parser compatibility
    but route through direct + Playwright instead.
    """

    DEFAULT_TIMEOUT = 60

    def __init__(self) -> None:
        # Kept for parsers that still read this attribute; always unused.
        self.SCRAPERAPICOM_API_KEY = None

    def retry_scrape_on_failure(
        self,
        scraper_fn: Callable,
        *args,
        max_attempts=5,
        backoff_factor=2,
        initial_delay=1,
        **kwargs,
    ):
        delay = initial_delay
        for attempt in range(max_attempts):
            try:
                response = scraper_fn(*args, **kwargs)
                return response

            except requests.RequestException as e:
                log.warning("Scrape request failed with error: %s", e)

                if attempt == max_attempts - 1:
                    log.warning("Scrape request final failure with error %s.", e)
                    return {"max_failure": True}

                jitter = uniform(0.0, delay)
                sleep_duration = delay + jitter
                log.info("Retrying in %.2f seconds...", sleep_duration)
                time.sleep(sleep_duration)
                delay *= backoff_factor

    def _finalize_from_response(
        self, response: requests.Response, return_type: ReturnType
    ):
        if return_type == ReturnType.RESPONSE:
            return response
        elif return_type == ReturnType.BYTES:
            return response.content
        else:
            return response.text

    def fetch_with_bs(
        self,
        url: str,
        *,
        return_type: ReturnType = ReturnType.TEXT,
        timeout: int = DEFAULT_TIMEOUT,
        headers: dict | None = None,
        verify: str | bool = True,
    ):
        effective_headers = {
            **DEFAULT_HTML_HEADERS,
            **(headers or {}),
        }
        r = requests.get(
            url, headers=effective_headers, timeout=timeout, verify=verify
        )
        r.raise_for_status()
        if return_type == ReturnType.TEXT:
            if _looks_blocked(r.text):
                raise requests.HTTPError(
                    f"Blocked or challenge page for {url} (status={r.status_code})",
                    response=r,
                )
            soup = BeautifulSoup(r.text, "html.parser")
            return str(soup)
        return self._finalize_from_response(r, return_type)

    def fetch_with_playwright(
        self,
        url: str,
        *,
        return_type: ReturnType = ReturnType.TEXT,
        timeout: int = DEFAULT_TIMEOUT,
        wait_for_selector: str | None = None,
        wait_for_seconds: float | None = None,
    ):
        """Fetch page HTML via headless Chromium (Cloudflare-friendly alternative)."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is required for blocked-page fallback but is not installed"
            ) from exc

        timeout_ms = max(5_000, int(timeout * 1000))
        log.info("Playwright fetch url=%s", url)
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
                    viewport={"width": 1280, "height": 720},
                    locale="en-US",
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                )
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if wait_for_selector:
                    page.wait_for_selector(wait_for_selector, timeout=timeout_ms)
                elif wait_for_seconds:
                    page.wait_for_timeout(int(float(wait_for_seconds) * 1000))
                else:
                    page.wait_for_timeout(1000)

                content = page.content()
                if return_type == ReturnType.BYTES:
                    return content.encode("utf-8", errors="replace")
                if return_type == ReturnType.RESPONSE:
                    return content
                return content
            finally:
                browser.close()

    def fetch_url(
        self,
        url: str,
        *,
        return_type: ReturnType = ReturnType.TEXT,
        timeout: int = DEFAULT_TIMEOUT,
        headers: dict | None = None,
        verify: str | bool = True,
        wait_for_selector: str | None = None,
        wait_for_seconds: float | None = None,
        prefer_playwright: bool = False,
    ):
        """
        Direct GET first; on block/failure, Playwright.
        If wait_for_* or prefer_playwright, go straight to Playwright.
        """
        if prefer_playwright or wait_for_selector or wait_for_seconds:
            return self.fetch_with_playwright(
                url,
                return_type=return_type,
                timeout=timeout,
                wait_for_selector=wait_for_selector,
                wait_for_seconds=wait_for_seconds,
            )

        direct = self.retry_scrape_on_failure(
            self.fetch_with_bs,
            url,
            max_attempts=3,
            backoff_factor=2,
            initial_delay=1,
            return_type=return_type,
            timeout=timeout,
            headers=headers,
            verify=verify,
        )
        if not (isinstance(direct, dict) and direct.get("max_failure")):
            if return_type != ReturnType.TEXT or not _looks_blocked(
                direct if isinstance(direct, str) else None
            ):
                return direct
            log.warning("Direct fetch looked blocked for %s; trying Playwright", url)
        else:
            log.warning("Direct fetch failed for %s; trying Playwright", url)

        try:
            return self.fetch_with_playwright(
                url,
                return_type=return_type,
                timeout=timeout,
                wait_for_selector=wait_for_selector,
                wait_for_seconds=wait_for_seconds,
            )
        except Exception as exc:
            log.warning("Playwright fetch failed for %s: %s", url, exc)
            return {"max_failure": True}

    def fetch_with_scraperapi(
        self,
        payload: dict,
        headers=None,
        *,
        return_type: ReturnType = ReturnType.TEXT,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        """
        Compatibility shim — ScraperAPI removed.
        Uses direct HTTP + Playwright for ``payload['url']``.
        """
        target = (payload or {}).get("url")
        if not target:
            raise ValueError("payload['url'] is required (ScraperAPI shim)")
        render = str((payload or {}).get("render", "false")).lower() == "true"
        result = self.fetch_url(
            target,
            return_type=return_type,
            timeout=timeout,
            headers=headers,
            prefer_playwright=render,
        )
        if isinstance(result, dict) and result.get("max_failure"):
            return ""
        return result

    def post_with_scraperapi(
        self,
        url: str,
        data: dict,
        *,
        return_type: ReturnType = ReturnType.TEXT,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        """Compatibility shim — direct POST instead of ScraperAPI."""
        r = requests.post(
            url,
            data=data,
            headers=DEFAULT_HTML_HEADERS,
            timeout=timeout,
        )
        r.raise_for_status()
        if return_type == ReturnType.TEXT:
            return r.text
        return self._finalize_from_response(r, return_type)

    def convert_to_soup(self, string: str):
        soup = BeautifulSoup(string, "html.parser")
        return soup

    def scrape_directly(self, url: str, verify: str | bool = True):
        try:
            result = self.fetch_url(url, verify=verify)
            if isinstance(result, dict) and result.get("max_failure"):
                raise ValueError(f"Failed to scrape URL '{url}'")
            return result
        except requests.RequestException as e:
            raise ValueError(f"Error directly scraping URL '{url}': {str(e)}") from e

    def scrape_html(
        self,
        schedule_type=None,
        url=None,
        render="false",
        wait_for_selector=None,
        wait_for_seconds=None,
        *,
        return_type: ReturnType = ReturnType.TEXT,
        timeout: int = DEFAULT_TIMEOUT,
        headers: dict | None = None,
        verify: str | bool = True,
    ):
        """Direct fetch + Playwright fallback (no ScraperAPI)."""
        scroll_pages = ["eventstribe_1_table", "eventstribe_2_table"]
        prefer_pw = str(render).lower() == "true"

        if schedule_type in scroll_pages:
            base_url = url
            responses = []
            page_number = 0 if schedule_type == "unique_nysenate" else 1
            while page_number <= 5:
                modified_url = base_url.replace("%num%", str(page_number))
                response = self.fetch_url(
                    modified_url,
                    return_type=return_type,
                    timeout=timeout,
                    headers=headers,
                    verify=verify,
                    prefer_playwright=prefer_pw,
                )
                if isinstance(response, dict) and response.get("max_failure"):
                    response = ""
                responses.append(response)
                page_number += 1
            return responses

        if schedule_type == "google_table":
            parsed_url = urlparse(url)
            query_params = parse_qs(parsed_url.query)
            response = None
            if "src" in query_params:
                response = query_params["src"][0]
                log.info("Extracted calendar id: %s", response)
            return response

        response = self.fetch_url(
            url,
            return_type=return_type,
            timeout=timeout,
            headers=headers,
            verify=verify,
            wait_for_selector=wait_for_selector,
            wait_for_seconds=wait_for_seconds,
            prefer_playwright=prefer_pw
            or bool(wait_for_selector)
            or bool(wait_for_seconds),
        )
        if isinstance(response, dict) and response.get("max_failure"):
            log.warning("All fetch strategies failed for %s", url)
            return "" if return_type == ReturnType.TEXT else response
        return response
