# playwright_utils.py
import asyncio
import os
import sys
from playwright.async_api import async_playwright
from urllib.parse import urljoin

from logging_config import get_dedicated_debug_logger, LOG_LEVEL

log = get_dedicated_debug_logger(__name__)
log.setLevel(LOG_LEVEL)

# Module-level cache to avoid repeated browser launch verification
_chromium_verified = False
# Track the process ID that set the cache (to handle fork situations)
_verified_pid = None


async def _safe_close_browser(browser, pw):
    """Safely close browser and playwright, handling exceptions."""
    try:
        if browser:
            await browser.close()
    except Exception:
        log.debug("browser cleanup failed", exc_info=True)
    try:
        if pw:
            await pw.stop()
            # Yield to event loop so Playwright's connection task can finish
            # and avoid "Task exception was never retrieved" at process exit.
            await asyncio.sleep(0.15)
    except Exception:
        log.debug("playwright stop failed", exc_info=True)


async def ensure_chromium_installed():
    """
    Idempotent: if Chromium is already in the cache, this is a quick no-op.
    First tries to verify browser is available by checking executable path.
    Only runs 'playwright install' if the browser appears to be missing.
    Uses module-level cache to avoid repeated verification overhead.

    Handles fork situations (e.g., Gunicorn workers) by tracking the PID
    that set the cache and invalidating it if the current PID differs.
    """
    global _chromium_verified, _verified_pid

    current_pid = os.getpid()

    # Invalidate cache if we're in a different process (fork happened)
    if _chromium_verified and _verified_pid != current_pid:
        _chromium_verified = False
        _verified_pid = None

    # Skip verification if already confirmed this session in this process
    if _chromium_verified:
        return

    # For local development, skip the pre-verification dance if browser
    # is likely already installed (checking PLAYWRIGHT_BROWSERS_PATH or
    # the default cache location). This avoids issues with Gunicorn forking.
    local_mode = os.getenv("ENV") == "LOCAL" or os.getenv("LOCAL_STREAMLINK_WORKAROUND")
    browsers_path = os.getenv(
        "PLAYWRIGHT_BROWSERS_PATH", os.path.expanduser("~/Library/Caches/ms-playwright")
    )
    chromium_exists = os.path.exists(browsers_path) and any(
        "chromium" in d
        for d in os.listdir(browsers_path)
        if os.path.isdir(os.path.join(browsers_path, d))
    )

    if local_mode and chromium_exists:
        # Trust that Chromium is installed, skip verification to avoid fork issues
        _chromium_verified = True
        _verified_pid = current_pid
        return

    # First, check if Chromium is already installed by verifying launch works
    pw = None
    browser = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        await browser.close()
        browser = None
        # Browser launched successfully, cache result and return
        _chromium_verified = True
        _verified_pid = current_pid
        return
    except Exception:
        # Browser launch failed, proceed with installation attempt
        pass
    finally:
        await _safe_close_browser(browser, pw)

    # If we get here, browser is not installed or had issues - try to install
    log.info("Installing Chromium")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "playwright",
        "install",
        "chromium",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out = await proc.communicate()
    if proc.returncode != 0:
        # Installation failed, but let's try launching anyway in case
        # the browser was already there (subprocess issues can cause false failures)
        pw = None
        browser = None
        try:
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(headless=True)
            await browser.close()
            browser = None
            # Browser works despite install failure - cache and return
            _chromium_verified = True
            _verified_pid = current_pid
            return
        except Exception as launch_error:
            # Browser genuinely not working
            raise RuntimeError(
                f"playwright install failed:\n{out[0].decode('utf-8', 'ignore')}\n"
                f"Browser launch also failed: {launch_error}"
            )
        finally:
            await _safe_close_browser(browser, pw)

    # Installation succeeded, cache result
    _chromium_verified = True
    _verified_pid = current_pid


# Shared launch args for Chromium-based scraping.
PLAYWRIGHT_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--disable-web-security",  # Bypass CORS for cross-origin iframe streams
]


def default_browser_headers():
    """
    Standard Chrome-like headers used across Playwright callers.
    """
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }


async def create_browser_context(headless: bool = True, extra_headers=None):
    """
    Create a Playwright browser + context with shared settings.
    Returns (playwright, browser, context).
    """
    await ensure_chromium_installed()

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        headless=headless,
        args=PLAYWRIGHT_LAUNCH_ARGS,
    )

    headers = default_browser_headers()
    if extra_headers:
        headers = {**headers, **extra_headers}

    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        extra_http_headers=headers,
        java_script_enabled=True,
        locale="en-US",
    )

    return playwright, browser, context


async def close_playwright_resources(
    playwright, browser=None, context=None, log=None
):
    """
    Close Playwright resources safely without swallowing cancellation.
    """
    try:
        if context:
            try:
                for page in list(context.pages):
                    try:
                        await page.close()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        if log:
                            log.exception("Error closing Playwright page")
            except Exception:
                if log:
                    log.exception("Error enumerating Playwright pages")

            try:
                await context.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                if log:
                    log.exception("Error closing Playwright context")

        if browser:
            try:
                await browser.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                if log:
                    log.exception("Error closing Playwright browser")
    finally:
        if playwright:
            try:
                await playwright.stop()
            except asyncio.CancelledError:
                raise
            except Exception:
                if log:
                    log.exception("Error stopping Playwright")


class BrowserManager:
    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None
        self.m3u8_urls = set()  # Use a set to store unique .m3u8 URLs

    @staticmethod
    def playwright_calendars():

        return [
            "unique_orpharm",
            "boarddocs_table",
            "legistarclick_table",
            "unique_hsd",
            "unique_flpharm",
            "unique_nyesdpharm",
            "nycrules_table",
            "unique_arizonalegislature",
            "unique_oregonlegislature",
        ]

    @staticmethod
    def get_browser_headers():
        """
        Returns a dictionary of HTTP headers that mimic a real Chrome browser.
        """
        return default_browser_headers()

    async def launch_browser(self):
        # Ensure Chromium is installed before launching
        await ensure_chromium_installed()

        if not hasattr(self, "_playwright"):
            self._playwright = await async_playwright().start()

        # Launch browser with additional args to avoid detection
        self.browser = await self._playwright.chromium.launch(
            headless=True,
            args=PLAYWRIGHT_LAUNCH_ARGS,
        )

        # Create a new context with realistic browser settings
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            extra_http_headers=self.get_browser_headers(),
            java_script_enabled=True,
            locale="en-US",
        )

    async def close_browser(self):
        if self.context:
            await self.context.close()
            self.context = None
        if self.browser:
            await self.browser.close()
            self.browser = None
            self.page = None
        if hasattr(self, "_playwright"):
            await self._playwright.stop()

    async def setup_m3u8_listener(self):
        self.page.on(
            "request",
            lambda req: asyncio.create_task(self.handle_request(req)),
        )
        log.info("m3u8 listener started")

    async def handle_request(self, request):
        url = request.url
        if ".m3u8" in url:
            log.debug(f"m3u8 url requested: {url}")
        else:
            log.debug(f"non-m3u8 url requested: {url}")

    async def get_iframes_from_url(self, url, delay=1):

        if not self.browser:
            await self.launch_browser()

        if not self.page:
            self.page = await self.context.new_page()

        await self.page.goto(url)
        await asyncio.sleep(delay)  # Wait for the page to render

        # Extract iframe details
        iframes = await self.page.query_selector_all("iframe")
        iframe_urls = []
        for iframe in iframes:
            src = await iframe.get_attribute("src")
            iframe_urls.append(src)

        return iframe_urls

    async def retrieve_meetings(
        self,
        url,
        timezone,
        parser_method,
        selector=".event-item.js-load-event",
    ):

        meetings = []
        retry_count = 1
        max_retries = 4
        _ = 1  # exponential backoff factor
        load_timeout = 5000  # page load timeout in ms

        while True:
            log.debug("In retrieve_meetings: browser")
            log.debug(self.browser)
            log.debug("In retrieve_meetings: page")
            log.debug(self.page)
            try:
                if not self.browser:
                    await self.launch_browser()
                if self.page is None:
                    log.info(f"Playwright: no page detected, navigating to url {url}")
                    self.page = await self.context.new_page()
                    await self.page.goto(url)
                elif retry_count < 3:
                    log.debug("Playwright: reloading page")
                    await self.page.reload(timeout=load_timeout)
                elif retry_count == 3:
                    log.debug("Playwright: closing and reopening tab")
                    await self.page.close()
                    self.page = await self.context.new_page()
                    await self.page.goto(url)
                else:
                    try:
                        log.debug("Playwright: clearing cache and cookies")
                        await self.context.clear_cookies()
                        log.debug("Playwright: closing and reopening tab")
                        await self.page.close()
                        self.page = await self.context.new_page()
                        log.debug("Playwright: reloading page")
                        # Wait for at least one event item to be loaded in the
                        # schedule
                        await self.page.close()
                        self.page = await self.context.new_page()
                        await self.page.goto(url)
                    except Exception as clear_error:
                        log.warning(f"Error clearing cache/cookies: {clear_error}")

                if parser_method == "kaltura_table_v1":
                    await self.page.wait_for_selector(selector, timeout=load_timeout)
                    log.debug(f"Playwright: Reloaded successfully on attempt {retry_count}")

                await asyncio.sleep(1)  # Ensure all events are loaded
                log.debug(f"calling parser_method after {retry_count} attempt(s)")
                meetings = await parser_method(self.page, url, timezone)
                return meetings, False  # No exception occurred

            except Exception as e:
                log.warning(
                    f"Playwright error during load/scrape: {e}. attempts: {retry_count}"
                )

                # Check if browser is still active
                if not self.browser or not self.browser.is_connected():
                    if not self.browser:
                        log.warning("self.browser = None")
                    raise Exception(
                        "Playwright: Browser appears to have crashed or disconnected"
                    )
                elif not self.page or self.page.is_closed():
                    raise Exception(
                        "Playwright: Page appears to have crashed (page None or closed)"
                    )
                else:
                    retry_count += 1
                    if retry_count > max_retries:
                        raise Exception("Playwright: Exceeded max reloads, aborting")
                    log.warning("Playwright: Browser appears to be active, retrying ...")
                    # no sleep needed since timeout provides built-in buffer

    async def search_for_m3u8_url(self, url):
        if not self.browser:
            await self.launch_browser()
        self.page = await self.context.new_page()
        await self.setup_m3u8_listener()
        await self.page.goto(url, wait_until="domcontentloaded")
        log.info(f"searching {url} for m3u8:")
        await self.search_DOM_for_m3u8_urls(self.page)
        await self.search_iframes_recursively(self.page)
        if not self.m3u8_urls:
            log.debug("No .m3u8 URL found.")

    async def search_DOM_for_m3u8_urls(self, context):
        content = await context.content()
        url = self.find_playlist_in_content(content, context.url)
        if url:
            self.m3u8_urls.add(url)
            return True
        return False

    async def search_iframes_recursively(self, context):
        iframes = await context.query_selector_all("iframe")
        for iframe in iframes:
            frame = await iframe.content_frame()
            if frame:
                log.debug(f"searching frame {frame} for m3u8 url:")
                await self.search_DOM_for_m3u8_urls(frame)
                await self.search_iframes_recursively(frame)

    def find_playlist_in_content(self, content, base_url):
        start_index = content.find(".m3u8")
        if start_index == -1:
            log.debug("no .m3u8 url found")
            return None
        start_bound = max(
            content.rfind('"', 0, start_index),
            content.rfind("'", 0, start_index),
            content.rfind("\n", 0, start_index),
        )
        end_bound = min(
            content.find('"', start_index),
            content.find("'", start_index),
            content.find("\n", start_index),
        )
        if start_bound != -1 and end_bound != -1:
            url = content[start_bound + 1 : end_bound].strip()
            if url.startswith("//"):
                protocol = "https:" if base_url.startswith("https") else "http:"
                url = protocol + url
            elif not url.startswith(("http://", "https://")):
                url = urljoin(base_url, url)
            log.info(f"returning full playlist url: {url}")
            return url
        log.debug("no valid .m3u8 url found")
        return None


# Testing of the BrowserManager
async def main():
    url = "https://miamifl.granicus.com/livestreams/3/player?autoplay=1"
    # Example URL, replace with your target

    browser_manager = BrowserManager()
    await browser_manager.launch_browser()
    await browser_manager.search_for_m3u8_url(url)
    if browser_manager.m3u8_urls:
        log.info("Found .m3u8 URLs:")
        for url in browser_manager.m3u8_urls:
            log.debug(url)
    else:
        log.debug("No .m3u8 URL found.")
    await browser_manager.close_browser()


if __name__ == "__main__":
    asyncio.run(main())
