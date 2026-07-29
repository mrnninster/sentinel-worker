import re
import json
import logging
import pytz
import os
import sys
import threading
from datetime import datetime, timedelta
from dateutil import parser
from bs4 import BeautifulSoup

# Add project path for imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from utils.scrape_html import HtmlScraper

try:
    from utils.youtube import Youtube as YoutubeUtils
except ImportError:
    YoutubeUtils = None

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
_SCHEDULED_FOR_RE = re.compile(r"Scheduled for\s+(.+)", re.IGNORECASE)
_STARTED_STREAMING_ON_RE = re.compile(
    r"Started streaming on\s+(.+?)(?:\s*[·|]|\s*$)",
    re.IGNORECASE,
)
_STARTED_STREAMING_AGO_RE = re.compile(
    r"Started streaming\s+(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago",
    re.IGNORECASE,
)
# 24/7 / always-on channel streams (not meeting broadcasts).
_DEFAULT_MAX_LIVE_AGE_HOURS = 24.0

# Shared across Youtube instances for one scrape job (cleared by service).
_YT_PAGE_CACHE: dict[str, object] = {}
_YT_PAGE_CACHE_LOCK = threading.Lock()
_YT_CLASSIFY_CACHE: dict[tuple[str, str, str], dict] = {}


def clear_youtube_page_cache() -> None:
    """Drop cached ytInitialData / classifications (call at start/end of a scrape)."""
    with _YT_PAGE_CACHE_LOCK:
        _YT_PAGE_CACHE.clear()
        _YT_CLASSIFY_CACHE.clear()


def _cache_key_for_url(url: str) -> str:
    return (url or "").strip().split("?", 1)[0].rstrip("/").lower()


class Youtube:
    def __init__(self):
        self.meetings = []
        self.scraper = None
        self.self_contained_parser = True

    def _fetch_youtube_initial_data(self, url: str):
        """Load a YouTube channel page and return window.ytInitialData (cached per URL)."""
        key = _cache_key_for_url(url)
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
            key = _cache_key_for_url(url)
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
                key = _cache_key_for_url(url)
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
                    key = _cache_key_for_url(url)
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
    def _meetings_from_yt_data(cls, yt_initial_data: dict, timezone: str = "America/New_York"):
        meetings = []
        try:
            tabs = yt_initial_data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"]
            candidate_contents = []
            for element_i in tabs:
                renderer = element_i.get("tabRenderer") or element_i.get(
                    "expandableTabRenderer"
                )
                if not renderer:
                    continue
                title = (renderer.get("title") or "").lower().strip()
                selected = bool(renderer.get("selected"))
                content = renderer.get("content") or {}
                grid = content.get("richGridRenderer") or {}
                items = grid.get("contents") or []
                if title in ("live", "livestreams"):
                    candidate_contents = items
                    break
                if selected and not candidate_contents:
                    candidate_contents = items

            for item in candidate_contents:
                meeting = cls._meeting_from_rich_item(item, timezone)
                if meeting:
                    meetings.append(meeting)
        except Exception:
            log.debug("Live-tab parse failed; trying deep scan", exc_info=True)

        if meetings:
            return meetings

        for node in cls._walk_nodes(yt_initial_data):
            if isinstance(node, dict):
                if "videoRenderer" in node:
                    meeting = cls._meeting_from_video_renderer(
                        node["videoRenderer"], timezone
                    )
                    if meeting:
                        meetings.append(meeting)
                if "lockupViewModel" in node:
                    meeting = cls._meeting_from_lockup(
                        node["lockupViewModel"], timezone
                    )
                    if meeting:
                        meetings.append(meeting)
        return meetings

    @classmethod
    def _walk_nodes(cls, node):
        yield node
        if isinstance(node, dict):
            for value in node.values():
                yield from cls._walk_nodes(value)
        elif isinstance(node, list):
            for item in node:
                yield from cls._walk_nodes(item)

    @classmethod
    def _meeting_from_rich_item(cls, content: dict, timezone: str):
        if not isinstance(content, dict) or "continuationItemRenderer" in content:
            return None
        try:
            payload = content["richItemRenderer"]["content"]
        except (KeyError, TypeError):
            return None
        if "lockupViewModel" in payload:
            return cls._meeting_from_lockup(payload["lockupViewModel"], timezone)
        if "videoRenderer" in payload:
            return cls._meeting_from_video_renderer(payload["videoRenderer"], timezone)
        return None

    @classmethod
    def _meeting_from_lockup(cls, lockup: dict, timezone: str):
        """Parse modern YouTube Live-tab cards (lockupViewModel)."""
        if not isinstance(lockup, dict):
            return None

        badge = cls._lockup_badge_text(lockup)
        meta = lockup.get("metadata") or {}
        meta_vm = meta.get("lockupMetadataViewModel") or meta
        title = ((meta_vm.get("title") or {}).get("content") or "").strip()
        schedule_text = cls._lockup_schedule_text(meta_vm)

        if badge and badge.lower() not in ("upcoming",):
            if badge.lower() == "live" or not schedule_text:
                return None

        if not title or not schedule_text:
            return None

        scheduled_time = cls._parse_scheduled_for(schedule_text, timezone)
        if not scheduled_time:
            return None

        video_id = lockup.get("contentId")
        meeting_link = (
            f"https://www.youtube.com/watch?v={video_id}" if video_id else None
        )
        return {
            "Meeting name": title,
            "Scheduled time": scheduled_time,
            "Meeting link": meeting_link,
            "Agenda link": None,
            "Status": "Upcoming",
            "Stream type": "ts_youtube",
        }

    @staticmethod
    def _lockup_badge_text(lockup: dict) -> str:
        overlays = (
            ((lockup.get("contentImage") or {}).get("thumbnailViewModel") or {}).get(
                "overlays"
            )
            or []
        )
        for overlay in overlays:
            badges = (overlay.get("thumbnailBottomOverlayViewModel") or {}).get(
                "badges"
            ) or []
            for badge in badges:
                text = (badge.get("thumbnailBadgeViewModel") or {}).get("text")
                if text:
                    return str(text).strip()
        return ""

    @staticmethod
    def _lockup_schedule_text(meta_vm: dict) -> str:
        try:
            rows = (
                ((meta_vm.get("metadata") or {}).get("contentMetadataViewModel") or {})
                .get("metadataRows")
                or []
            )
            for row in rows:
                for part in row.get("metadataParts") or []:
                    content = ((part.get("text") or {}).get("content") or "").strip()
                    if content.lower().startswith("scheduled for"):
                        return content
        except Exception:
            return ""
        return ""

    @staticmethod
    def _parse_scheduled_for(text: str, timezone: str):
        cleaned = (text or "").replace("\u202f", " ").replace("\xa0", " ").strip()
        # also handle actual unicode narrow nbsp if present as char
        cleaned = cleaned.replace("\u202f", " ")
        cleaned = cleaned.replace(" ", " ").replace(" ", " ")
        match = _SCHEDULED_FOR_RE.search(cleaned)
        if not match:
            return None
        try:
            tz = pytz.timezone(timezone)
        except Exception:
            tz = pytz.timezone("America/New_York")
        try:
            local_dt = parser.parse(match.group(1), fuzzy=True)
            if local_dt.tzinfo is None:
                local_dt = tz.localize(local_dt)
            return local_dt.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            log.warning("Could not parse YouTube schedule text %r", text)
            return None

    @staticmethod
    def _meeting_from_video_renderer(video_data: dict, timezone: str = "America/New_York"):
        """Legacy path: videoRenderer + upcomingEventData unix startTime."""
        if not isinstance(video_data, dict):
            return None
        if "upcomingEventData" not in video_data:
            return None
        try:
            meeting_name = video_data["title"]["runs"][0]["text"]
            scheduled_time = int(video_data["upcomingEventData"]["startTime"])
        except (KeyError, TypeError, ValueError, IndexError):
            return None

        dt_object = datetime.fromtimestamp(scheduled_time, tz=pytz.utc)
        meeting_date_time = dt_object.isoformat().replace("+00:00", "Z")
        video_id = video_data.get("videoId")
        meeting_link = (
            f"https://www.youtube.com/watch?v={video_id}" if video_id else None
        )
        return {
            "Meeting name": meeting_name,
            "Scheduled time": meeting_date_time,
            "Meeting link": meeting_link,
            "Agenda link": None,
            "Status": "Upcoming",
            "Stream type": "ts_youtube",
        }

    def youtube_table(self, url, timezone="America/New_York", return_soup=False):
        """
        Upcoming meetings from the channel Live (/streams) tab.

        Fetches /streams once, classifies all cards, then filters to upcoming.
        """
        if self.scraper is None:
            self.scraper = HtmlScraper()

        classified = self.classify_channel_streams(url, timezone=timezone)
        meetings = [
            self.stream_item_to_meeting(item, timezone)
            for item in classified.get("upcoming") or []
        ]
        seen = set()
        unique = []
        for m in meetings:
            key = (m.get("Meeting link"), m.get("Meeting name"), m.get("Scheduled time"))
            if key in seen:
                continue
            seen.add(key)
            unique.append(m)
        meetings = unique

        log.info("youtube_table found %d upcoming meeting(s) on %s", len(meetings), url)
        self.meetings = meetings
        soup = None
        return (meetings, soup) if return_soup else meetings


    # ------------------------------------------------------------------
    # Live / concluded status (metadata only — no stream download)
    # Mirrors WallFly utils.youtube.Youtube.get_live_videos + DetectEnd.ts_youtube
    # ------------------------------------------------------------------

    @staticmethod
    def extract_video_id(value: str | None) -> str | None:
        """Extract an 11-char YouTube video id from a URL or bare id."""
        if not value:
            return None
        value = value.strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
            return value
        patterns = [
            r"[?&]v=([A-Za-z0-9_-]{11})",
            r"youtu\.be/([A-Za-z0-9_-]{11})",
            r"/live/([A-Za-z0-9_-]{11})",
            r"/shorts/([A-Za-z0-9_-]{11})",
            r"/embed/([A-Za-z0-9_-]{11})",
        ]
        for pat in patterns:
            m = re.search(pat, value)
            if m:
                return m.group(1)
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

    @staticmethod
    def _max_live_age_hours() -> float:
        raw = (os.getenv("YOUTUBE_MAX_LIVE_AGE_HOURS") or "").strip()
        if not raw:
            return _DEFAULT_MAX_LIVE_AGE_HOURS
        try:
            return max(0.0, float(raw))
        except ValueError:
            return _DEFAULT_MAX_LIVE_AGE_HOURS

    @classmethod
    def parse_started_streaming_text(cls, text: str):
        """
        Parse YouTube live start copy into a timezone-aware UTC datetime.

        Handles:
          - "Started streaming on Jan 13, 2025"
          - "Started streaming on Jul 22, 2026"
          - "Started streaming 3 days ago"
        """
        cleaned = (text or "").replace("\u202f", " ").replace("\xa0", " ").strip()
        if not cleaned:
            return None

        ago = _STARTED_STREAMING_AGO_RE.search(cleaned)
        if ago:
            amount = int(ago.group(1))
            unit = ago.group(2).lower()
            now = datetime.now(pytz.UTC)
            if unit.startswith("minute"):
                return now - timedelta(minutes=amount)
            if unit.startswith("hour"):
                return now - timedelta(hours=amount)
            if unit.startswith("day"):
                return now - timedelta(days=amount)
            if unit.startswith("week"):
                return now - timedelta(weeks=amount)
            if unit.startswith("month"):
                return now - timedelta(days=30 * amount)
            if unit.startswith("year"):
                return now - timedelta(days=365 * amount)
            return None

        on = _STARTED_STREAMING_ON_RE.search(cleaned)
        if on:
            chunk = on.group(1).strip()
        elif "started streaming on" in cleaned.lower():
            idx = cleaned.lower().index("started streaming on")
            chunk = cleaned[idx + len("started streaming on") :].strip()
            chunk = re.split(r"[·|\n\r]", chunk, maxsplit=1)[0].strip()
        else:
            return None

        try:
            local_dt = parser.parse(chunk, fuzzy=True)
            if local_dt.tzinfo is None:
                local_dt = pytz.UTC.localize(local_dt)
            return local_dt.astimezone(pytz.UTC)
        except Exception:
            log.debug("Could not parse started-streaming text %r", text)
            return None

    @classmethod
    def _extract_started_streaming_from_yt_data(cls, yt_data) -> datetime | None:
        """Walk ytInitialData for 'Started streaming on …' strings."""
        if not yt_data:
            return None
        for node in cls._walk_nodes(yt_data):
            if isinstance(node, str) and "started streaming" in node.lower():
                started = cls.parse_started_streaming_text(node)
                if started:
                    return started
            if isinstance(node, dict):
                simple = node.get("simpleText")
                if isinstance(simple, str) and "started streaming" in simple.lower():
                    started = cls.parse_started_streaming_text(simple)
                    if started:
                        return started
        return None

    def get_live_started_at(self, video_id: str):
        """Fetch watch page and return when the live stream started (UTC), if known."""
        if not video_id:
            return None
        url = f"https://www.youtube.com/watch?v={video_id}"
        yt_data = self._fetch_youtube_initial_data(url)
        started = self._extract_started_streaming_from_yt_data(yt_data)
        if started:
            return started
        # Some layouts only expose the phrase in raw HTML, not structured JSON.
        try:
            html = self._fetch_with_html_scraper(url)
            if html:
                return self.parse_started_streaming_text(html)
        except Exception:
            log.debug("HTML fallback for started-streaming failed video_id=%s", video_id)
        return None

    @classmethod
    def is_stale_live(cls, started_at: datetime, *, max_age_hours: float | None = None) -> bool:
        """True when a live stream has been running longer than max_age_hours."""
        if started_at is None:
            return False
        limit = cls._max_live_age_hours() if max_age_hours is None else max_age_hours
        if started_at.tzinfo is None:
            started_at = pytz.UTC.localize(started_at)
        age = datetime.now(pytz.UTC) - started_at.astimezone(pytz.UTC)
        return age.total_seconds() > limit * 3600

    def _filter_stale_live_items(self, live_items: list[dict]) -> tuple[list[dict], list[dict]]:
        """
        Drop always-on / multi-day lives from the live list.

        Returns (kept_live, skipped).
        """
        kept: list[dict] = []
        skipped: list[dict] = []
        max_hours = self._max_live_age_hours()
        for item in live_items:
            video_id = item.get("video_id")
            started = self.get_live_started_at(video_id) if video_id else None
            if started and self.is_stale_live(started, max_age_hours=max_hours):
                note = (
                    f"Skipped live stream older than {max_hours:g}h "
                    f"(Started streaming on {started.astimezone(pytz.UTC).strftime('%b %d, %Y')})"
                )
                log.info(
                    "Skipping stale live video_id=%s started=%s age_limit_h=%s",
                    video_id,
                    started.isoformat(),
                    max_hours,
                )
                skipped.append(
                    {
                        **item,
                        "status": "skipped",
                        "started_streaming_on": started.astimezone(pytz.UTC).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        "note": note,
                    }
                )
                continue
            if started:
                item = {
                    **item,
                    "started_streaming_on": started.astimezone(pytz.UTC).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                }
            kept.append(item)
        return kept, skipped

    @staticmethod
    def _legacy_is_live(video_data: dict) -> bool:
        """WallFly overlay-label check on videoRenderer."""
        try:
            overlays = video_data.get("thumbnailOverlays") or []
            if not overlays:
                return False
            overlay = overlays[0]
            status_text = (
                overlay.get("thumbnailOverlayTimeStatusRenderer") or {}
            ).get("text", {})
            label = (
                ((status_text.get("accessibility") or {}).get("accessibilityData") or {})
                .get("label", "")
                .lower()
            )
            return label == "live"
        except Exception:
            return False

    @classmethod
    def _classify_rich_item(cls, content: dict, timezone: str = "America/New_York") -> dict | None:
        """
        Classify one Live-tab card as live / upcoming / concluded.

        Returns dict with keys: status, video_id, video_title, meeting_link,
        scheduled_time (optional).
        """
        if not isinstance(content, dict) or "continuationItemRenderer" in content:
            return None
        try:
            payload = content["richItemRenderer"]["content"]
        except (KeyError, TypeError):
            return None

        # Modern UI
        if "lockupViewModel" in payload:
            lockup = payload["lockupViewModel"]
            badge = (cls._lockup_badge_text(lockup) or "").lower()
            meta = lockup.get("metadata") or {}
            meta_vm = meta.get("lockupMetadataViewModel") or meta
            title = ((meta_vm.get("title") or {}).get("content") or "").strip()
            video_id = lockup.get("contentId")
            if not video_id or not title:
                return None
            meeting_link = f"https://www.youtube.com/watch?v={video_id}"
            schedule_text = cls._lockup_schedule_text(meta_vm)
            if badge == "live":
                return {
                    "status": "live",
                    "video_id": video_id,
                    "video_title": title,
                    "meeting_link": meeting_link,
                }
            if badge == "upcoming" or schedule_text:
                scheduled = (
                    cls._parse_scheduled_for(schedule_text, timezone)
                    if schedule_text
                    else None
                )
                return {
                    "status": "upcoming",
                    "video_id": video_id,
                    "video_title": title,
                    "meeting_link": meeting_link,
                    "scheduled_time": scheduled,
                }
            # Past VODs / archives still listed on Live tab
            return {
                "status": "concluded",
                "video_id": video_id,
                "video_title": title,
                "meeting_link": meeting_link,
            }

        # Legacy UI
        if "videoRenderer" in payload:
            video_data = payload["videoRenderer"]
            try:
                title = video_data["title"]["runs"][0]["text"]
                video_id = video_data["videoId"]
            except (KeyError, TypeError, IndexError):
                return None
            meeting_link = f"https://www.youtube.com/watch?v={video_id}"
            if cls._legacy_is_live(video_data):
                return {
                    "status": "live",
                    "video_id": video_id,
                    "video_title": title,
                    "meeting_link": meeting_link,
                }
            if "upcomingEventData" in video_data:
                try:
                    start = int(video_data["upcomingEventData"]["startTime"])
                    scheduled = datetime.fromtimestamp(start, tz=pytz.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                except Exception:
                    scheduled = None
                return {
                    "status": "upcoming",
                    "video_id": video_id,
                    "video_title": title,
                    "meeting_link": meeting_link,
                    "scheduled_time": scheduled,
                }
            return {
                "status": "concluded",
                "video_id": video_id,
                "video_title": title,
                "meeting_link": meeting_link,
            }
        return None

    def classify_channel_streams(
        self, channel_url: str, timezone: str = "America/New_York"
    ) -> dict:
        """
        Scrape a channel /streams (Live) page and classify cards.

        Returns:
            {
              "live": [...],
              "upcoming": [...],
              "concluded": [...],
              "channel_url": str,
            }
        """
        streams_url = self.channel_tab_url(channel_url, "streams")
        cache_key = (_cache_key_for_url(streams_url), "streams", timezone)
        with _YT_PAGE_CACHE_LOCK:
            cached = _YT_CLASSIFY_CACHE.get(cache_key)
            if cached is not None:
                log.info("classify cache hit tab=streams url=%s", streams_url)
                return cached

        yt_data = self._fetch_youtube_initial_data(streams_url)
        result = {
            "live": [],
            "upcoming": [],
            "concluded": [],
            "skipped": [],
            "channel_url": streams_url,
        }
        if not yt_data:
            log.warning("No ytInitialData while classifying %s", streams_url)
            with _YT_PAGE_CACHE_LOCK:
                _YT_CLASSIFY_CACHE[cache_key] = result
            return result

        for item in self._live_tab_items(yt_data):
            classified = self._classify_rich_item(item, timezone)
            if not classified:
                continue
            result[classified["status"]].append(classified)

        kept_live, skipped = self._filter_stale_live_items(result["live"])
        result["live"] = kept_live
        result["skipped"] = skipped

        log.info(
            "Channel stream status live=%d upcoming=%d concluded=%d skipped=%d url=%s",
            len(result["live"]),
            len(result["upcoming"]),
            len(result["concluded"]),
            len(result["skipped"]),
            streams_url,
        )
        with _YT_PAGE_CACHE_LOCK:
            _YT_CLASSIFY_CACHE[cache_key] = result
        return result

    def classify_channel_videos(
        self, channel_url: str, timezone: str = "America/New_York"
    ) -> dict:
        """
        Scrape a channel /videos page. Cards are treated as concluded VODs
        (duration badges, not Live/Upcoming).
        """
        videos_url = self.channel_tab_url(channel_url, "videos")
        cache_key = (_cache_key_for_url(videos_url), "videos", timezone)
        with _YT_PAGE_CACHE_LOCK:
            cached = _YT_CLASSIFY_CACHE.get(cache_key)
            if cached is not None:
                log.info("classify cache hit tab=videos url=%s", videos_url)
                return cached

        yt_data = self._fetch_youtube_initial_data(videos_url)
        result = {
            "live": [],
            "upcoming": [],
            "concluded": [],
            "channel_url": videos_url,
        }
        if not yt_data:
            log.warning("No ytInitialData while classifying videos %s", videos_url)
            with _YT_PAGE_CACHE_LOCK:
                _YT_CLASSIFY_CACHE[cache_key] = result
            return result

        for item in self._videos_tab_items(yt_data):
            classified = self._classify_rich_item(item, timezone)
            if not classified:
                continue
            # Videos tab: force concluded unless explicitly live/upcoming
            if classified["status"] not in ("live", "upcoming"):
                classified = {**classified, "status": "concluded", "source_tab": "videos"}
            else:
                classified = {**classified, "source_tab": "videos"}
            result[classified["status"]].append(classified)

        log.info(
            "Channel videos status live=%d upcoming=%d concluded=%d url=%s",
            len(result["live"]),
            len(result["upcoming"]),
            len(result["concluded"]),
            videos_url,
        )
        with _YT_PAGE_CACHE_LOCK:
            _YT_CLASSIFY_CACHE[cache_key] = result
        return result

    def classify_channel_for_fallback(
        self, channel_url: str, timezone: str = "America/New_York"
    ) -> dict:
        """
        Merge Live (/streams) + Videos (/videos) tabs for schedule fallback.

        Each tab URL is fetched at most once (shared page cache; both tabs can
        load in one Playwright browser when neither is cached yet).
        """
        streams_url = self.channel_tab_url(channel_url, "streams")
        videos_url = self.channel_tab_url(channel_url, "videos")
        # Prefetch any missing tabs in a single browser session.
        self._fetch_youtube_initial_data_many([streams_url, videos_url])

        streams = self.classify_channel_streams(channel_url, timezone=timezone)
        videos = self.classify_channel_videos(channel_url, timezone=timezone)

        seen: set[str] = set()
        for bucket in ("live", "upcoming", "concluded"):
            for item in streams[bucket]:
                item.setdefault("source_tab", "streams")
                if item.get("video_id"):
                    seen.add(item["video_id"])

        merged = {
            "live": list(streams["live"]),
            "upcoming": list(streams["upcoming"]),
            "concluded": list(streams["concluded"]),
            "channel_url": streams.get("channel_url") or channel_url,
            "streams_url": streams.get("channel_url"),
            "videos_url": videos.get("channel_url"),
        }

        # Prefer streams live/upcoming if videos somehow has them
        for item in videos["live"] + videos["upcoming"]:
            vid = item.get("video_id")
            if not vid or vid in seen:
                continue
            merged[item["status"]].append(item)
            seen.add(vid)

        for item in videos["concluded"]:
            vid = item.get("video_id")
            if not vid or vid in seen:
                continue
            merged["concluded"].append(item)
            seen.add(vid)

        log.info(
            "Fallback channel merge live=%d upcoming=%d concluded=%d "
            "(streams=%s videos=%s)",
            len(merged["live"]),
            len(merged["upcoming"]),
            len(merged["concluded"]),
            merged.get("streams_url"),
            merged.get("videos_url"),
        )
        return merged

    # ------------------------------------------------------------------
    # Schedule fallback: merge /streams + /videos into primary meetings
    # ------------------------------------------------------------------

    _STATUS_LABELS = {
        "live": "Live",
        "upcoming": "Upcoming",
        "concluded": "Concluded",
    }

    @staticmethod
    def parse_date_from_title(title: str):
        """
        Extract a calendar date embedded in a VOD title.

        Handles titles like ``July 20, 2026 El Paso County Commissioners Court``.
        Returns a ``datetime.date`` or ``None``.
        """
        if not title or not str(title).strip():
            return None
        try:
            # Prefer an explicit Month Day, Year substring when present
            m = re.search(
                r"\b("
                r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
                r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
                r"Nov(?:ember)?|Dec(?:ember)?)"
                r"\s+\d{1,2},?\s+\d{4}"
                r"|"
                r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
                r"|"
                r"\d{4}-\d{2}-\d{2}"
                r")\b",
                title,
                re.IGNORECASE,
            )
            chunk = m.group(1) if m else title
            dt = parser.parse(chunk, fuzzy=True)
            return dt.date()
        except Exception:
            return None

    @classmethod
    def stream_item_local_date(cls, item: dict, timezone: str):
        """Best-effort local date for a classified stream card."""
        try:
            tz = pytz.timezone(timezone)
        except Exception:
            tz = pytz.UTC

        scheduled = item.get("scheduled_time")
        if scheduled:
            try:
                when = parser.parse(scheduled)
                if when.tzinfo is None:
                    when = tz.localize(when)
                return when.astimezone(tz).date()
            except Exception:
                pass

        return cls.parse_date_from_title(item.get("video_title") or "")

    @classmethod
    def stream_item_to_meeting(cls, item: dict, timezone: str) -> dict:
        """Convert a classified stream card into a Bubble-shaped meeting dict."""
        try:
            tz = pytz.timezone(timezone)
        except Exception:
            tz = pytz.UTC

        status_key = (item.get("status") or "concluded").lower()
        status = cls._STATUS_LABELS.get(status_key, "Concluded")
        title = (item.get("video_title") or "Meeting").strip()
        link = item.get("meeting_link") or (
            f"https://www.youtube.com/watch?v={item['video_id']}"
            if item.get("video_id")
            else None
        )

        scheduled = item.get("scheduled_time")
        if not scheduled:
            local_date = cls.stream_item_local_date(item, timezone)
            if status_key == "live":
                scheduled = datetime.now(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            elif local_date:
                # Unknown clock time — use local noon as a stable stub
                local_dt = tz.localize(
                    datetime(local_date.year, local_date.month, local_date.day, 12, 0)
                )
                scheduled = local_dt.astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                scheduled = datetime.now(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        return {
            "Meeting name": title,
            "Scheduled time": scheduled,
            "Meeting link": link,
            "Agenda link": None,
            "Status": status,
            "Stream type": "ts_youtube",
        }

    def apply_schedule_fallback(
        self,
        meetings: list[dict],
        *,
        channel_url: str,
        timezone: str = "America/New_York",
        on_primary_failure: str = "same_day_stub",
        match: str = "title_date",
        primary_empty: bool = False,
    ) -> dict:
        """
        Merge YouTube /streams + /videos cards into primary schedule meetings.

        Returns:
            {
              "meetings": [...],
              "youtube_used": bool,
              "notes": [str],
              "live_count": int,
              "upcoming_count": int,
              "concluded_count": int,
            }
        """
        notes: list[str] = []
        if on_primary_failure == "skip":
            return {
                "meetings": meetings,
                "youtube_used": False,
                "notes": ["youtube_fallback skipped (on_primary_failure=skip)"],
                "live_count": 0,
                "upcoming_count": 0,
                "concluded_count": 0,
            }

        classified = self.classify_channel_for_fallback(channel_url, timezone=timezone)
        all_items = (
            list(classified["live"])
            + list(classified["upcoming"])
            + list(classified["concluded"])
        )
        notes.append(
            f"youtube snapshot live={len(classified['live'])} "
            f"upcoming={len(classified['upcoming'])} "
            f"concluded={len(classified['concluded'])} "
            f"(streams+videos)"
        )
        if classified.get("streams_url"):
            notes.append(f"streams_url={classified['streams_url']}")
        if classified.get("videos_url"):
            notes.append(f"videos_url={classified['videos_url']}")

        try:
            tz = pytz.timezone(timezone)
        except Exception:
            tz = pytz.UTC
        today = datetime.now(tz).date()

        # Working copy
        result = [dict(m) for m in meetings]
        matched_video_ids: set[str] = set()

        def meeting_video_id(meeting: dict) -> str | None:
            for key in ("Meeting link", "meeting_link", "user_live_link", "user_archive_link"):
                vid = self.extract_video_id(meeting.get(key))
                if vid:
                    return vid
            return None

        def meeting_local_date(meeting: dict):
            raw = meeting.get("Scheduled time") or meeting.get("scheduled_time")
            if not raw:
                return None
            try:
                when = parser.parse(raw)
                if when.tzinfo is None:
                    when = tz.localize(when)
                return when.astimezone(tz).date()
            except Exception:
                return None

        # Overlay onto existing meetings
        for meeting in result:
            vid = meeting_video_id(meeting)
            hit = None
            if vid:
                for item in all_items:
                    if item.get("video_id") == vid:
                        hit = item
                        break
            if hit is None and match == "title_date":
                mdate = meeting_local_date(meeting)
                if mdate:
                    # Prefer live > upcoming > concluded for same day
                    for bucket in ("live", "upcoming", "concluded"):
                        for item in classified[bucket]:
                            if self.stream_item_local_date(item, timezone) == mdate:
                                hit = item
                                break
                        if hit:
                            break

            if not hit:
                continue

            matched_video_ids.add(hit["video_id"])
            label = self._STATUS_LABELS.get(hit["status"], "Concluded")
            meeting["Status"] = label
            meeting["Meeting link"] = hit.get("meeting_link") or meeting.get(
                "Meeting link"
            )
            meeting["Stream type"] = meeting.get("Stream type") or "ts_youtube"
            if hit.get("scheduled_time") and not meeting.get("Scheduled time"):
                meeting["Scheduled time"] = hit["scheduled_time"]
            notes.append(
                f"overlay video_id={hit['video_id']} → Status={label} "
                f"(tab={hit.get('source_tab', '?')})"
            )

        # Same-day stubs for live / upcoming / today's concluded VODs
        if on_primary_failure == "same_day_stub":
            stub_candidates = []
            stub_candidates.extend(classified["live"])
            stub_candidates.extend(classified["upcoming"])
            for item in classified["concluded"]:
                item_date = self.stream_item_local_date(item, timezone)
                if item_date == today:
                    stub_candidates.append(item)

            existing_dates = {
                d for d in (meeting_local_date(m) for m in result) if d is not None
            }
            existing_vids = {meeting_video_id(m) for m in result}
            existing_vids.discard(None)
            existing_vids |= matched_video_ids

            for item in stub_candidates:
                vid = item.get("video_id")
                if not vid or vid in existing_vids:
                    continue
                item_date = self.stream_item_local_date(item, timezone)
                # For live/upcoming without a date, treat as today
                if item_date is None and item.get("status") in ("live", "upcoming"):
                    item_date = today
                if item_date is not None and item_date in existing_dates:
                    # Already have a primary row for that day — overlay / attach
                    for meeting in result:
                        if meeting_local_date(meeting) == item_date:
                            meeting["Status"] = self._STATUS_LABELS.get(
                                item["status"], "Concluded"
                            )
                            meeting["Meeting link"] = item.get("meeting_link")
                            meeting["Stream type"] = (
                                meeting.get("Stream type") or "ts_youtube"
                            )
                            existing_vids.add(vid)
                            notes.append(
                                f"same-day attach video_id={vid} → "
                                f"{meeting.get('Meeting name')} "
                                f"(tab={item.get('source_tab', '?')})"
                            )
                            break
                    continue

                stub = self.stream_item_to_meeting(item, timezone)
                result.append(stub)
                existing_vids.add(vid)
                if item_date:
                    existing_dates.add(item_date)
                notes.append(
                    f"stub video_id={vid} status={stub['Status']} date={item_date} "
                    f"(tab={item.get('source_tab', '?')})"
                )

        elif on_primary_failure == "status_only" and primary_empty:
            notes.append(
                "status_only with empty primary — no meetings to overlay; "
                "no stubs created"
            )

        return {
            "meetings": result,
            "youtube_used": True,
            "notes": notes,
            "live_count": len(classified["live"]),
            "upcoming_count": len(classified["upcoming"]),
            "concluded_count": len(classified["concluded"]),
        }

    def get_live_videos(self, channel_url: str | None = None, soup=None) -> list[dict]:
        """
        WallFly-compatible live list (metadata only).

        Prefer ``channel_url`` (Playwright + modern UI). ``soup`` is accepted for
        callers that already fetched HTML (legacy path).
        """
        if channel_url:
            classified = self.classify_channel_streams(channel_url)
            return [
                {"video_id": v["video_id"], "video_title": v["video_title"]}
                for v in classified["live"]
            ]

        if soup is None:
            return []
        # Legacy soup → ytInitialData parse
        html = str(soup)
        yt_data = self._extract_yt_initial_data(html)
        if not yt_data:
            return []
        live = []
        for item in self._live_tab_items(yt_data):
            classified = self._classify_rich_item(item)
            if classified and classified["status"] == "live":
                live.append(
                    {
                        "video_id": classified["video_id"],
                        "video_title": classified["video_title"],
                    }
                )
        return live

    def check_stream_status(
        self,
        *,
        channel_url: str,
        video_id: str | None = None,
        video_url: str | None = None,
        timezone: str = "America/New_York",
    ) -> dict:
        """
        Check whether a YouTube stream is live, upcoming, or concluded.

        Same signal WallFly uses for DetectStart/DetectEnd.ts_youtube:
        presence of the video on the Live tab with a LIVE badge = live;
        absence of a previously-known live id = concluded.
        Does **not** download or probe HLS/media.
        """
        # Monitor polls must always re-fetch — a warm page/classify cache would
        # freeze status at the first snapshot and never reach "concluded".
        clear_youtube_page_cache()

        vid = video_id or self.extract_video_id(video_url)
        classified = self.classify_channel_streams(channel_url, timezone=timezone)

        live_ids = {v["video_id"]: v for v in classified["live"]}
        upcoming_ids = {v["video_id"]: v for v in classified["upcoming"]}
        concluded_ids = {v["video_id"]: v for v in classified["concluded"]}
        skipped_ids = {v["video_id"]: v for v in classified.get("skipped") or []}

        if not vid:
            return {
                "status": "channel_snapshot",
                "video_id": None,
                "video_title": None,
                "meeting_link": None,
                "live_videos": classified["live"],
                "upcoming_videos": classified["upcoming"],
                "concluded_on_page": classified["concluded"],
                "skipped_videos": classified.get("skipped") or [],
                "channel_url": channel_url,
            }

        if vid in skipped_ids:
            hit = skipped_ids[vid]
            return {
                "status": "skipped",
                "video_id": vid,
                "video_title": hit.get("video_title"),
                "meeting_link": hit.get("meeting_link"),
                "started_streaming_on": hit.get("started_streaming_on"),
                "note": hit.get("note")
                or "Live stream has been running longer than 24 hours; skipped",
                "live_videos": classified["live"],
                "upcoming_videos": classified["upcoming"],
                "concluded_on_page": classified["concluded"],
                "skipped_videos": classified.get("skipped") or [],
                "channel_url": channel_url,
            }

        if vid in live_ids:
            hit = live_ids[vid]
            return {
                "status": "live",
                "video_id": vid,
                "video_title": hit["video_title"],
                "meeting_link": hit["meeting_link"],
                "started_streaming_on": hit.get("started_streaming_on"),
                "live_videos": classified["live"],
                "upcoming_videos": classified["upcoming"],
                "concluded_on_page": classified["concluded"],
                "skipped_videos": classified.get("skipped") or [],
                "channel_url": channel_url,
            }
        if vid in upcoming_ids:
            hit = upcoming_ids[vid]
            return {
                "status": "upcoming",
                "video_id": vid,
                "video_title": hit["video_title"],
                "meeting_link": hit["meeting_link"],
                "scheduled_time": hit.get("scheduled_time"),
                "live_videos": classified["live"],
                "upcoming_videos": classified["upcoming"],
                "concluded_on_page": classified["concluded"],
                "skipped_videos": classified.get("skipped") or [],
                "channel_url": channel_url,
            }
        if vid in concluded_ids:
            hit = concluded_ids[vid]
            return {
                "status": "concluded",
                "video_id": vid,
                "video_title": hit["video_title"],
                "meeting_link": hit["meeting_link"],
                "live_videos": classified["live"],
                "upcoming_videos": classified["upcoming"],
                "concluded_on_page": classified["concluded"],
                "skipped_videos": classified.get("skipped") or [],
                "channel_url": channel_url,
            }

        # Not listed on the Live tab at all — treat as concluded/not currently live
        # (WallFly DetectEnd: video_id absent from live list ⇒ ended)
        return {
            "status": "concluded",
            "video_id": vid,
            "video_title": None,
            "meeting_link": f"https://www.youtube.com/watch?v={vid}",
            "live_videos": classified["live"],
            "upcoming_videos": classified["upcoming"],
            "concluded_on_page": classified["concluded"],
            "skipped_videos": classified.get("skipped") or [],
            "channel_url": channel_url,
            "note": (
                "Video id not found among Live-tab cards; treated as concluded "
                "(not currently live), matching WallFly DetectEnd.ts_youtube."
            ),
        }

    def youtube_table_la(self, url, timezone="America/New_York"):
        """
        Wrapper function to filter out SAP meetings for Los Angeles YouTube pages.

        """
        try:
            meetings = self.youtube_table(url, timezone)
            return [m for m in meetings if "SAP" not in m["Meeting name"]]
        except Exception:
            log.exception("Error in youtube_table_la()")
            return []

    def youtube_table_md(self, url, timezone="America/New_York"):
        """
        Maryland-specific YouTube scraper entry point.
        Self-contained parser that handles its own HTML scraping.
        Requires channel_url to be provided via ARG_CHANNEL_URL environment variable.

        Applies Maryland-specific filtering logic:
        - Only returns the first meeting of each day that hasn't been concluded
        - A meeting is considered "concluded" if all earlier meetings of that day
          have become live and ended (checked via YouTube live videos API)
        - For future days, always returns the first meeting of each day
        - Skips meetings when an earlier meeting of the same day is still live

        Maryland legislature schedules multiple placeholder meetings per day.
        This parser returns only the "next" meeting per day to avoid creating
        duplicate upcoming sessions. When a meeting ends, monitor_stream calls
        /refresh_schedules which re-runs this parser — the next placeholder
        then becomes visible and gets its own session.

        Bubble config:
            schedule_type: youtube_table_md
            stream_type: ts_youtube
            channel_url: required (e.g. https://www.youtube.com/@mga-session-senate445/streams)
            detect_start_method / detect_end_method: ts_youtube

        Monitoring extends until 12am local (instead of default 3h) to allow
        the refresh→monitor cycle to cover all meetings in a day.

        Args:
            url: Schedule URL (from geodict config, unused -
                channel_url comes from ARG_CHANNEL_URL env var)
            timezone: Timezone string (default: "America/New_York")

        Returns:
            list: List of meeting dictionaries with Maryland-specific filtering applied
        """
        # Initialize scraper if not already done
        if self.scraper is None:
            self.scraper = HtmlScraper()

        # Get channel_url from environment variable (required for youtube_table_md)
        channel_url = os.getenv("ARG_CHANNEL_URL")
        if not channel_url:
            raise ValueError(
                "ARG_CHANNEL_URL environment variable is required for youtube_table_md"
            )

        # Validate YouTube channel URL
        if YoutubeUtils is None:
            # Fallback to regular youtube_table if utils.youtube not available
            all_meetings = self.youtube_table(channel_url, timezone)
            # Add Stream type for YouTube meetings
            for meeting in all_meetings:
                meeting["Stream type"] = "ts_youtube"
            return all_meetings

        youtube_utils = YoutubeUtils(url=channel_url, meeting_title="")
        if not youtube_utils.is_valid_youtube_streams_url():
            raise ValueError(f"Invalid YouTube channel URL format: {channel_url}")

        # Get all upcoming meetings first (self-contained mode)
        youtube_table_result = self.youtube_table(
            channel_url, timezone, return_soup=True
        )
        if isinstance(youtube_table_result, tuple) and len(youtube_table_result) == 2:
            all_meetings, channel_soup = youtube_table_result
        else:
            all_meetings = youtube_table_result
            channel_soup = None

        if not all_meetings:
            return []

        # Parse timezone
        tz = pytz.timezone(timezone)
        current_time = datetime.now(pytz.utc)

        # Check currently live videos to see what's active
        live_videos = []
        try:
            if youtube_utils.is_valid_youtube_streams_url():
                live_soup = channel_soup
                if live_soup is None:
                    soup_str = self.scraper.scrape_html(url=channel_url)
                    live_soup = self.scraper.convert_to_soup(soup_str)
                    if live_soup is None:
                        live_soup = BeautifulSoup(soup_str, "html.parser")
                live_videos_data = youtube_utils.get_live_videos(live_soup)
                if live_videos_data:
                    live_videos = [v.get("video_id") for v in live_videos_data]
        except Exception:
            log.exception("Error checking live videos in youtube_table_md")
            # Continue with filtering even if live check fails

        # Group meetings by date (local date)
        meetings_by_date = {}
        for meeting in all_meetings:
            try:
                scheduled_time_str = meeting.get("Scheduled time", "")
                if not scheduled_time_str:
                    continue

                # Parse scheduled time
                if scheduled_time_str.endswith("Z"):
                    scheduled_time = datetime.fromisoformat(
                        scheduled_time_str.replace("Z", "+00:00")
                    )
                else:
                    scheduled_time = parser.parse(scheduled_time_str)

                # Convert to local timezone for date grouping
                local_scheduled = scheduled_time.astimezone(tz)
                date_key = local_scheduled.date()

                if date_key not in meetings_by_date:
                    meetings_by_date[date_key] = []
                meetings_by_date[date_key].append(
                    {
                        "meeting": meeting,
                        "scheduled_time": scheduled_time,
                        "local_scheduled": local_scheduled,
                    }
                )
            except Exception as e:
                log.warning(
                    "Error parsing meeting time in youtube_table_md: %s", e
                )
                continue

        # Sort meetings within each date by scheduled time
        for date_key in meetings_by_date:
            meetings_by_date[date_key].sort(key=lambda x: x["scheduled_time"])

        # Filter to only return "next" meeting per day
        filtered_meetings = []
        today_local = current_time.astimezone(tz).date()

        for date_key in sorted(meetings_by_date.keys()):
            date_meetings = meetings_by_date[date_key]

            # For future days, always return the first meeting
            if date_key > today_local:
                if date_meetings:
                    filtered_meetings.append(date_meetings[0]["meeting"])
                continue

            # For today or past days, find the first non-concluded meeting
            # A meeting is "concluded" if all earlier meetings of that day have
            # become live and ended (i.e., they're not in the live videos list)

            # Check if there's currently a live stream (if so, we skip later meetings)
            has_live_now = len(live_videos) > 0

            # Strategy: For each day, return only the "next" meeting:
            # 1. If there's a live stream now -> only return the first meeting
            #    if it hasn't started yet (scheduled time in future)
            #    Otherwise, don't return anything (wait for live meeting to finish)
            # 2. If no live stream now -> return the first meeting where:
            #    - It's the first meeting of the day, OR
            #    - All earlier meetings have concluded (their scheduled times passed)

            next_meeting = None

            if has_live_now:
                # There's a live stream - only return first meeting
                # if it hasn't started yet
                first_meeting_data = date_meetings[0]
                if first_meeting_data["scheduled_time"] > current_time:
                    # First meeting hasn't started yet, safe to return it
                    next_meeting = first_meeting_data["meeting"]
                # If first meeting's time has passed and it's live,
                # don't return anything (wait for it to finish)
            else:
                # No live stream - find first meeting where all
                # earlier ones have concluded
                for idx, meeting_data in enumerate(date_meetings):
                    meeting = meeting_data["meeting"]
                    scheduled_time = meeting_data["scheduled_time"]

                    if idx == 0:
                        # First meeting of the day - always include it
                        next_meeting = meeting
                        break

                    # For later meetings, check if all earlier meetings have concluded
                    # A meeting has "concluded" if its scheduled time has passed
                    # (and we know it's not live because has_live_now is False)
                    all_earlier_concluded = True
                    for earlier_idx in range(idx):
                        earlier_meeting_data = date_meetings[earlier_idx]
                        earlier_scheduled_time = earlier_meeting_data["scheduled_time"]

                        # If an earlier meeting's time hasn't passed yet,
                        # it hasn't concluded
                        if earlier_scheduled_time > current_time:
                            all_earlier_concluded = False
                            break

                    # If all earlier meetings have concluded
                    # (their times passed and they're not live),
                    # this is the next meeting to return
                    if all_earlier_concluded:
                        next_meeting = meeting
                        break

            if next_meeting:
                filtered_meetings.append(next_meeting)

        # Add Stream type for YouTube meetings
        for meeting in filtered_meetings:
            meeting["Stream type"] = "ts_youtube"

        return filtered_meetings


if __name__ == "__main__":
    import sys
    import os

    sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
    from utils.scrape_html import HtmlScraper

    # URL to test - you can change this to any YouTube URL you want to test with
    test_url = "https://www.youtube.com/@durhampublicschoolsboardof2290/streams"

    # Instantiate Youtube class and call youtube_table method (now self-contained)
    youtube_scraper = Youtube()
    meetings = youtube_scraper.youtube_table(test_url)

    # Print the meetings found
    if meetings:
        print("Meetings found:")
        for index, meeting in enumerate(meetings):
            print(f"{index + 1}) {meeting}")
    else:
        print("No meetings found.")
