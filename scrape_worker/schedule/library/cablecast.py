# cablecast.py
import os
import re
import sys
import logging
from datetime import datetime
from typing import List, Dict, Optional
from urllib.parse import urlparse, urljoin

import pytz
import requests
from bs4 import BeautifulSoup
from dateutil import parser
from dotenv import load_dotenv

if __name__ == "__main__":
    load_dotenv()
    local_project_path = os.getenv("LOCAL_PROJECT_PATH")
    if local_project_path and local_project_path not in sys.path:
        sys.path.append(local_project_path)

from utils.utils_functions import get_api_json_call  # noqa: E402
from utils.scrape_html import HtmlScraper  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"
TITLE_MATCH_THRESHOLD = 0.7

STREAM_LINK_PATTERN = re.compile(r"/watch/stream/(\d+)")
SHOW_LINK_PATTERN = re.compile(r"/show/(\d+)")
NOW_TEXT_PATTERN = re.compile(r"\bNow\b", re.I)
NOW_STYLE_PATTERN = re.compile(r"background-color.*rgb\(25,\s*102,\s*57\)", re.I)
NOW_CLASS_PATTERN = re.compile(r"py-1.*w-24.*h-\[30px\]", re.I)

# Date patterns to extract and remove from meeting titles (order matters: try
# more specific first). Matched substrings are parsed with dateutil.parser.
_DATE_EXTRACTION_PATTERNS = [
    # Month name: "January 22, 2026", "Jan 22, 2026", "Jan 22 2026"
    (
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}"
    ),
    # Numeric: 1-22-2026, 1/22/2026, 01-22-2026, 12/15/25
    r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}",
    # ISO: 2026-01-22
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}",
]

# Patterns used for removal only (no parsing). Keep in sync with extraction.
_DATE_REMOVAL_ONLY_PATTERNS = _DATE_EXTRACTION_PATTERNS

MAX_UPCOMING_PARENT_TRAVERSAL = 5

# Title filter: keep only if title contains one of these (case-insensitive).
TITLE_KEYWORDS = [
    "board",
    "council",
    "commission",
    "committee",
    "conference",
    "house",
    "senate",
    "floor",
    "session",
    "joint",
    "budget",
    "hearing",
    "agenda",
]


def _extract_date_from_title(title: str):
    """Return date parsed from first date-like substring in title, else None."""
    if not title:
        return None
    for pat in _DATE_EXTRACTION_PATTERNS:
        m = re.search(pat, title, re.I)
        if not m:
            continue
        s = m.group(0).strip()
        try:
            dt = parser.parse(s, fuzzy=True)
            return dt.date()
        except (ValueError, TypeError, parser.ParserError):
            continue
    return None


def _title_has_date(title: str) -> bool:
    return _extract_date_from_title(title) is not None


def _title_matches_keywords(title: str) -> bool:
    if not title:
        return False
    t = title.lower()
    return any(kw in t for kw in TITLE_KEYWORDS)


def _remove_date_from_title_static(title: str) -> str:
    """Remove date patterns from meeting title. Module-level for reuse."""
    if not title:
        return title
    cleaned = title
    for pat in _DATE_REMOVAL_ONLY_PATTERNS:
        cleaned = re.sub(pat, "", cleaned, flags=re.I)
    cleaned = re.sub(r"^\s*[-:]\s*", "", cleaned)
    cleaned = re.sub(r"\s*[-:]\s*$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or title


def _build_detail_url(schedule_url: str, show_id) -> Optional[str]:
    """Build detail/show page URL from schedule URL and show ID."""
    if not schedule_url or show_id is None:
        return None
    try:
        parsed = urlparse(schedule_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        path = (parsed.path or "").rstrip("/")
        # e.g. /internetchannel/schedule -> /internetchannel/show/123
        path = re.sub(r"\bschedule\b", f"show/{show_id}", path, flags=re.I)
        if not path.startswith("/"):
            path = "/" + path
        return base + path
    except (ValueError, AttributeError, TypeError, re.error) as e:
        log.debug("Failed to build detail URL for %s: %s", schedule_url, e)
        return None


class Cablecast:
    """
    Self-contained scraper for Cablecast video streaming platforms.

    Multiple Streams/Channels:
        Some Cablecast installations support multiple simultaneous streams/sections
        (e.g., "LIVE WEB 1", "LIVE WEB 2"). When a meeting goes live, the liveStreamUrl
        field in the API response contains the stream URL for that specific
        channel/stream. The scraper prioritizes liveStreamUrl when available and
        the meeting is live.

    Note on Detection:
        Cablecast provides a "Now" indicator in the HTML schedule page (green
        button) that marks meetings currently in progress. The scraper detects
        this indicator and sets the meeting status to "In progress" accordingly.
        This enables calendar-based detection for both start and end times.

    Standard Config:
        schedule_type: cablecast_table
        schedule_url: https://example.cablecast.tv/schedule?site=1
        stream_type: streamlink (or appropriate stream type)
        detect_start_method: calendar_detect
        detect_end_method: calendar_detect
        timezone: America/New_York (or appropriate timezone)
    """

    def __init__(self):
        self.self_contained_parser = True
        self.meetings = []
        self.session = requests.Session()
        self.timezone = None
        self.base_url = None
        self.url = None
        self.scraper = HtmlScraper()
        self._schedule_soup_cache = None
        self._schedule_soup_url = None

    def cablecast_table(
        self, url: str, timezone: str = "America/New_York"
    ) -> List[Dict]:
        """
        Main entry point for Cablecast scraper.

        Args:
            url: Schedule page URL (e.g., https://example.cablecast.tv/schedule?site=1)
            timezone: Timezone string (e.g., "America/New_York")

        Returns:
            List of meeting dictionaries
        """
        if not url:
            log.warning("URL is required")
            return []

        self.timezone = timezone
        self.url = url
        self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc

        # Try API methods first, fall back to HTML scraping
        meetings = self._try_json_api(url)
        if not meetings:
            meetings = self._try_xml_api(url)
        if not meetings:
            meetings = self._scrape_html(url)

        # Filter out replays and clean meeting titles
        self.meetings = self._process_meetings(meetings)

        # Extract stream numbers from HTML for upcoming meetings
        # (API doesn't provide stream numbers, but HTML links do)
        self.meetings = self._add_stream_urls_from_html(self.meetings, url)

        # Check HTML for "Now" indicators to detect in-progress meetings
        self.meetings = self._check_now_indicators(self.meetings, url)

        # For live meetings, try to fetch updated stream URLs
        self.meetings = self._update_live_stream_urls(self.meetings)

        # Normalize status based on link type for today's meetings
        self.meetings = self._update_status_from_link(self.meetings)

        # Remove internal fields not meant for output
        self.meetings = self._strip_internal_fields(self.meetings)

        log.info("Found %s Cablecast meetings", len(self.meetings))
        return self.meetings

    def _try_json_api(self, url: str) -> List[Dict]:
        """Try to fetch data from JSON API endpoint."""
        api_base = None
        try:
            parsed_url = urlparse(url)
            host = parsed_url.netloc

            # Try cloud API pattern: cloud.{second}.{third} for subdomains
            parts = host.split(".")
            if len(parts) >= 3:
                # e.g., example.cablecast.tv -> cloud.cablecast.tv
                api_base = (
                    f"{parsed_url.scheme}://cloud.{parts[1]}.{parts[2]}"
                    f"/api/publicsitedata"
                )
            elif len(parts) == 2:
                # e.g., example.cablecast.tv -> cloud.cablecast.tv
                api_base = (
                    f"{parsed_url.scheme}://cloud.{parts[0]}."
                    f"cablecast.tv/api/publicsitedata"
                )
            else:
                # Try direct API path
                api_base = f"{parsed_url.scheme}://{host}/api/publicsitedata"

            # Extract site parameter from URL
            site = self._extract_site_param(url) or 1

            tz = pytz.timezone(self.timezone or "America/New_York")
            params = {
                "currentDay": datetime.now(tz).strftime("%Y-%m-%d"),
                "host": host,
                "site": site,
            }

            log.info("Attempting JSON API: %s/schedule", api_base)
            json_response = get_api_json_call(f"{api_base}/schedule", params)

            if not json_response or "scheduleItems" not in json_response:
                return []

            schedule_items = json_response.get("scheduleItems", [])
            meetings = []

            for item in schedule_items:
                meeting = self._parse_json_item(item)
                if meeting:
                    meetings.append(meeting)

            if meetings:
                log.info(
                    "Successfully fetched %s meetings from JSON API", len(meetings)
                )
                return meetings

        except Exception as e:
            if api_base:
                log.debug("JSON API not available for %s/schedule: %s", api_base, e)
            else:
                log.debug("JSON API not available: %s", e)

        return []

    def _try_xml_api(self, url: str) -> List[Dict]:
        """Try to fetch data from XML API endpoint."""
        xml_api_url = None
        try:
            parsed_url = urlparse(url)
            base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"

            # Try XML API pattern (like Eagle County)
            xml_api_url = f"{base_url}/cablecastapi/publicsitedata"
            site = self._extract_site_param(url) or 1
            xml_api_url += f"?site={site}"

            log.info("Attempting XML API: %s", xml_api_url)
            response = self.session.get(xml_api_url, timeout=30)
            response.raise_for_status()

            # Check if response is XML
            content_type = response.headers.get("Content-Type", "")
            if "xml" not in content_type.lower():
                return []

            soup = BeautifulSoup(response.text, "xml")
            schedule_items = soup.find_all("PublicSiteScheduleItem")

            if not schedule_items:
                return []

            meetings = []
            for item in schedule_items:
                meeting = self._parse_xml_item(item)
                if meeting:
                    meetings.append(meeting)

            if meetings:
                log.info(
                    "Successfully fetched %s meetings from XML API", len(meetings)
                )
                return meetings

        except Exception as e:
            if xml_api_url:
                log.debug("XML API not available for %s: %s", xml_api_url, e)
            else:
                log.debug("XML API not available: %s", e)

        return []

    def _scrape_html(self, url: str) -> List[Dict]:
        """Fallback to HTML scraping."""
        try:
            log.info("Scraping HTML from: %s", url)
            html = self.scraper.scrape_html(url=url)
            soup = self.scraper.convert_to_soup(html)
            self._schedule_soup_cache = soup
            self._schedule_soup_url = url

            meetings = []

            # Try to extract schedule date from heading
            schedule_date = None
            schedule_heading = soup.find(
                ["h1", "h2", "h3", "h4", "h5", "h6"],
                string=re.compile(r"Schedule", re.I),
            )
            if schedule_heading:
                heading_text = schedule_heading.get_text()
                date_match = re.search(r"(\w+\s+\d{1,2},?\s+\d{4})", heading_text)
                if date_match:
                    try:
                        schedule_date = parser.parse(
                            date_match.group(1), fuzzy=True
                        ).date()
                    except Exception:
                        pass

            # Use schedule date if found, otherwise use current date
            if schedule_date:
                current_date = schedule_date
            else:
                current_date = datetime.now(pytz.timezone(self.timezone)).date()

            # Look for schedule items in various formats
            # Pattern 1: List items (most common)
            schedule_items = soup.select("ul li, ol li")

            # Filter to items that look like schedule entries
            # (contain time pattern and a link)
            filtered_items = []
            for item in schedule_items:
                item_text = item.get_text()
                # Check if it has a time pattern and a link
                if re.search(
                    r"^(Now|\d{1,2}:\d{2}\s*(AM|PM))", item_text, re.I
                ) and item.find("a", href=True):
                    filtered_items.append(item)

            if not filtered_items:
                # Fallback: try all list items
                filtered_items = schedule_items

            for item in filtered_items:
                meeting = self._parse_html_item(item, current_date)
                if meeting:
                    meetings.append(meeting)

            if meetings:
                log.info("Successfully scraped %s meetings from HTML", len(meetings))
                return meetings

        except Exception as e:
            log.warning("HTML scraping failed: %s", e)

        return []

    def _parse_json_item(self, item: Dict) -> Optional[Dict]:
        """Parse a single item from JSON API response."""
        try:
            title = item.get("title", "").strip()
            if not title:
                return None

            # Extract date/time
            run_datetime = item.get("runDateTime")
            if not run_datetime:
                return None

            # Parse datetime - API returns UTC times (with Z suffix)
            try:
                dt = parser.parse(run_datetime, fuzzy=True)
                # If timezone info is missing but string ends with Z, it's UTC
                if dt.tzinfo is None and run_datetime.upper().endswith("Z"):
                    dt = pytz.UTC.localize(dt)
                # If still no timezone, treat as local time (backwards compatibility)
                if dt.tzinfo is None:
                    scheduled_time = self._format_utc_time(dt)
                else:
                    # Already has timezone - convert to UTC and format
                    utc_dt = dt.astimezone(pytz.UTC)
                    scheduled_time = (
                        utc_dt.strftime(MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT)[
                            :-3
                        ]
                        + "Z"
                    )
            except Exception as e:
                log.warning("Failed to parse datetime '%s': %s", run_datetime, e)
                return None

            # Check if live
            show = item.get("show", {})
            is_live = show.get("isLive", False)
            status = "In progress" if is_live else "Upcoming"

            # Get meeting link - for live meetings use liveStreamUrl, for upcoming
            # use VOD URL from API (HTML extraction will override if better link found)
            meeting_link = None
            if is_live:
                # When live, prefer liveStreamUrl if available
                meeting_link = show.get("liveStreamUrl")
                # Fallback to VOD URL if live stream URL not available
                if not meeting_link:
                    meeting_link = show.get("vodUrl")
                if not meeting_link:
                    meeting_link = item.get("vodUrl")
            else:
                # For upcoming meetings, prefer show URL over VOD m3u8 URL
                # Get show_id first, then construct show URL
                upcoming_show_id = (
                    show.get("showID")
                    or item.get("showID")
                    or show.get("showId")
                    or item.get("showId")
                )
                if upcoming_show_id:
                    # Construct show URL (preferred over VOD m3u8)
                    site = 1
                    if self.url:
                        site = self._extract_site_param(self.url) or 1
                    meeting_link = (
                        f"{self.base_url}/show/{upcoming_show_id}?site={site}"
                    )
                else:
                    # Fallback to VOD URL if no show_id
                    meeting_link = show.get("vodUrl")
                    if not meeting_link:
                        meeting_link = item.get("vodUrl")

            # Get agenda link from fieldDisplays
            agenda_link = None
            field_displays = item.get("fieldDisplay", [])
            if isinstance(field_displays, list):
                for field in field_displays:
                    if isinstance(field, dict):
                        label = field.get("label", "").lower()
                        if "agenda" in label:
                            agenda_link = field.get("value")
                            break

            # Check show fieldDisplays too
            if not agenda_link:
                show_field_displays = show.get("fieldDisplays", [])
                for field in show_field_displays:
                    if isinstance(field, dict):
                        label = field.get("label", "").lower()
                        if "agenda" in label:
                            agenda_link = field.get("value")
                            break

            show_id = (
                show.get("showID")
                or item.get("showID")
                or show.get("showId")
                or item.get("showId")
            )
            log.debug(
                "Extracted show_id: %s from show=%s, item keys=%s",
                show_id,
                show.get("showID"),
                list(item.keys())[:10],
            )
            user_live_link = None
            if is_live and show_id:
                user_live_link = f"{self.base_url}/show/{show_id}"
            # For upcoming, user_live_link set in _add_stream_urls_from_html.
            # detail_url used for secondary rerun check when no dates on main schedule.
            detail_url = None
            if show_id and self.url:
                detail_url = _build_detail_url(self.url, show_id)

            meeting_data = {
                "Meeting name": title,
                "Scheduled time": scheduled_time,
                "Meeting link": meeting_link,
                "Agenda link": agenda_link,
                "Status": status,
                "user_live_link": user_live_link,
                "show_id": show_id,
                "detail_url": detail_url,
            }
            log.debug(
                "Parsed meeting: title='%s', show_id=%s, status=%s",
                title,
                show_id,
                status,
            )

            return meeting_data

        except Exception as e:
            log.warning("Failed to parse JSON item: %s", e)
            return None

    def _parse_xml_item(self, item) -> Optional[Dict]:
        """Parse a single item from XML API response."""
        try:
            title_elem = item.find("Title")
            title = title_elem.text.strip() if title_elem else ""
            if not title:
                return None

            # Extract date/time
            run_datetime_elem = item.find("RunDateTime")
            if not run_datetime_elem:
                return None

            run_datetime = run_datetime_elem.text.strip()

            # Parse datetime - XML API also returns UTC times
            try:
                dt = parser.parse(run_datetime, fuzzy=True)
                # If timezone info is missing but string ends with Z, it's UTC
                if dt.tzinfo is None and run_datetime.upper().endswith("Z"):
                    dt = pytz.UTC.localize(dt)
                # If still no timezone, treat as local time (backwards compatibility)
                if dt.tzinfo is None:
                    scheduled_time = self._format_utc_time(dt)
                else:
                    # Already has timezone - convert to UTC and format
                    utc_dt = dt.astimezone(pytz.UTC)
                    scheduled_time = (
                        utc_dt.strftime(MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT)[
                            :-3
                        ]
                        + "Z"
                    )
            except Exception as e:
                log.warning("Failed to parse datetime '%s': %s", run_datetime, e)
                return None

            # Check if live (XML might have IsLive field)
            is_live_elem = item.find("IsLive")
            is_live = is_live_elem.text.lower() == "true" if is_live_elem else False
            status = "In progress" if is_live else "Upcoming"

            # Get meeting link - for live meetings use liveStreamUrl, for upcoming
            # use VOD URL from API (HTML extraction will override if better link found)
            meeting_link = None
            if is_live:
                # When live, try live stream URL first
                live_url_elem = item.find("LiveStreamUrl")
                if live_url_elem:
                    meeting_link = live_url_elem.text.strip()
                # Fallback to VOD URL
                if not meeting_link:
                    vod_url_elem = item.find("VodUrl")
                    meeting_link = vod_url_elem.text.strip() if vod_url_elem else None
            else:
                # For upcoming meetings, prefer show URL over VOD m3u8 URL
                show_id_elem = item.find("ShowID") or item.find("ShowId")
                if show_id_elem:
                    show_id = show_id_elem.text.strip()
                    # Construct show URL (preferred over VOD m3u8)
                    site = 1
                    if self.url:
                        parsed_url = urlparse(self.url)
                        base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
                        site = self._extract_site_param(self.url) or 1
                    else:
                        base_url = self.base_url or "https://example.cablecast.tv"
                    meeting_link = f"{base_url}/show/{show_id}?site={site}"
                else:
                    # Fallback to VOD URL if no show_id
                    vod_url_elem = item.find("VodUrl")
                    meeting_link = vod_url_elem.text.strip() if vod_url_elem else None

            # Get agenda link
            agenda_link = None
            field_displays = item.find_all("FieldDisplay")
            for field in field_displays:
                label_elem = field.find("Label")
                value_elem = field.find("Value")
                if label_elem and value_elem:
                    label = label_elem.text.lower()
                    if "agenda" in label:
                        agenda_link = value_elem.text.strip()
                        break

            meeting_data = {
                "Meeting name": title,
                "Scheduled time": scheduled_time,
                "Meeting link": meeting_link,
                "Agenda link": agenda_link,
                "Status": status,
            }

            return meeting_data

        except Exception as e:
            log.warning("Failed to parse XML item: %s", e)
            return None

    def _parse_html_item(self, item, current_date: datetime.date) -> Optional[Dict]:
        """Parse a single item from HTML."""
        try:
            item_text = item.get_text()
            # Look for time indicator (could be "Now", "3:05 AM", etc.)
            time_match = re.search(r"^(Now|\d{1,2}:\d{2}\s*(AM|PM))", item_text, re.I)
            if not time_match:
                return None

            time_text = time_match.group(1).strip()
            is_live = time_text.lower() == "now"

            # Find meeting link and title
            link_elem = item.find("a", href=True)
            if not link_elem:
                return None

            title = link_elem.get_text(strip=True)
            if not title:
                return None

            meeting_link = link_elem.get("href")
            if meeting_link and not meeting_link.startswith("http"):
                meeting_link = urljoin(self.base_url, meeting_link)

            # Try to extract date/time from the item or surrounding context
            # Look for date in the schedule context
            parent = item.find_parent()
            date_text = None
            if parent:
                date_elem = parent.find(
                    string=re.compile(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}")
                )
                if date_elem:
                    date_text = date_elem.strip()

            # If we have a time but no date, use current date
            if not date_text and not is_live:
                # Try to parse time and combine with current date
                time_match = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", time_text, re.I)
                if time_match:
                    hour = int(time_match.group(1))
                    minute = int(time_match.group(2))
                    am_pm = time_match.group(3).upper()
                    if am_pm == "PM" and hour != 12:
                        hour += 12
                    elif am_pm == "AM" and hour == 12:
                        hour = 0

                    dt = datetime.combine(
                        current_date,
                        datetime.min.time().replace(hour=hour, minute=minute),
                    )
                    scheduled_time = self._format_utc_time(dt)
                else:
                    return None
            elif is_live:
                # For live meetings, use current time
                now = datetime.now(pytz.timezone(self.timezone))
                scheduled_time = self._format_utc_time(now)
            else:
                return None

            status = "In progress" if is_live else "Upcoming"

            # Look for agenda link
            agenda_link = None
            agenda_elem = item.find("a", href=re.compile(r"agenda", re.I))
            if not agenda_elem:
                agenda_elem = item.find(string=re.compile(r"agenda", re.I))
                if agenda_elem:
                    parent_link = agenda_elem.find_parent("a", href=True)
                    if parent_link:
                        agenda_link = parent_link.get("href")
                        if agenda_link and not agenda_link.startswith("http"):
                            agenda_link = urljoin(self.base_url, agenda_link)

            return {
                "Meeting name": title,
                "Scheduled time": scheduled_time,
                "Meeting link": meeting_link,
                "Agenda link": agenda_link,
                "Status": status,
            }

        except Exception as e:
            log.debug("Failed to parse HTML item: %s", e)
            return None

    def _process_meetings(self, meetings: List[Dict]) -> List[Dict]:
        """
        Process meetings: filter by keywords, optionally filter replays by title
        date, clean titles (remove dates only when reruns exist).
        """
        processed = []
        current_date = datetime.now(pytz.timezone(self.timezone)).date()

        # 1. Filter by title keywords (board, council, commission, etc.)
        candidate = []
        for meeting in meetings:
            title = meeting.get("Meeting name", "")
            if not title:
                continue
            if not _title_matches_keywords(title):
                log.debug("Skipping (no keyword match): %s", title[:60])
                continue
            candidate.append(meeting)

        # 2. If no titles have dates, use detail-page rerun check (click when necessary)
        any_has_date = any(
            _title_has_date(m.get("Meeting name", "")) for m in candidate
        )
        if not any_has_date:
            for meeting in candidate:
                detail_url = meeting.get("detail_url")
                if not detail_url:
                    show_id = meeting.get("show_id")
                    if show_id and self.url:
                        detail_url = _build_detail_url(self.url, show_id)
                if not detail_url:
                    processed.append(meeting)
                    continue
                info = self._fetch_detail_rerun_info(detail_url)
                if info and self._is_rerun_from_detail(info, current_date):
                    log.debug(
                        "Skipping rerun (detail page): %s",
                        meeting.get("Meeting name", "")[:60],
                    )
                    continue
                processed.append(meeting)
            return processed

        # 3. Filter reruns by title date and strip dates from kept titles
        meetings_by_time = {}
        for m in candidate:
            st = m.get("Scheduled time", "")
            if st:
                time_key = st[:16] if len(st) >= 16 else st
                if time_key not in meetings_by_time:
                    meetings_by_time[time_key] = []
                meetings_by_time[time_key].append(m)

        for meeting in candidate:
            title = meeting.get("Meeting name", "")
            title_date = _extract_date_from_title(title)
            if title_date is not None and title_date < current_date:
                log.debug("Skipping replay: %s (date: %s)", title[:60], title_date)
                continue

            cleaned_title = _remove_date_from_title_static(title)
            meeting["Meeting name"] = cleaned_title

            st = meeting.get("Scheduled time", "")
            if st:
                time_key = st[:16] if len(st) >= 16 else st
                simultaneous = meetings_by_time.get(time_key, [])
                if len(simultaneous) > 1:
                    log.debug("Simultaneous meeting: %s", cleaned_title[:50])

            processed.append(meeting)

        return processed

    def _remove_date_from_title(self, title: str) -> str:
        """Remove date patterns from meeting title (delegates to static helper)."""
        return _remove_date_from_title_static(title)

    def _parse_scheduled_local(self, scheduled_time: Optional[str]):
        """Parse scheduled time string and return localized datetime."""
        if not scheduled_time or not self.timezone:
            return None
        local_tz = pytz.timezone(self.timezone)
        try:
            dt = parser.isoparse(scheduled_time)
        except (ValueError, TypeError):
            try:
                dt = parser.parse(scheduled_time)
            except (ValueError, TypeError, parser.ParserError):
                return None
        if dt.tzinfo is None:
            dt = local_tz.localize(dt)
        return dt.astimezone(local_tz)

    def _update_status_from_link(self, meetings: List[Dict]) -> List[Dict]:
        """Use link patterns to determine live vs ended.

        Preserves "In progress" status set by _check_now_indicators —
        the HTML "Now" indicator is authoritative and must not be
        overridden by the /show/ link heuristic.
        """
        current_time = datetime.now(pytz.timezone(self.timezone))
        for meeting in meetings:
            # Never downgrade a meeting already marked live by Now indicators
            if meeting.get("Status") == "In progress":
                continue

            link = (
                meeting.get("Meeting link") or meeting.get("user_live_link") or ""
            ).lower()

            if "/watch/stream/" in link:
                meeting["Status"] = "In progress"
            elif "/show/" in link:
                # Only mark as "Ended" if meeting is in the past
                # (upcoming meetings can also have /show/ links)
                scheduled_time = meeting.get("Scheduled time", "")
                if scheduled_time:
                    try:
                        dt = parser.isoparse(scheduled_time)
                        local_tz = pytz.timezone(self.timezone)
                        if dt.tzinfo:
                            local_dt = dt.astimezone(local_tz)
                        else:
                            local_dt = local_tz.localize(dt)
                        # Only mark as ended if scheduled time is in the past
                        if local_dt < current_time:
                            meeting["Status"] = "Ended"
                    except Exception:
                        # If we can't parse time, don't change status
                        pass

        return meetings

    def _strip_internal_fields(self, meetings: List[Dict]) -> List[Dict]:
        """Remove internal fields that should not be returned."""
        for meeting in meetings:
            meeting.pop("show_id", None)
            meeting.pop("detail_url", None)
        return meetings

    def _fetch_detail_rerun_info(self, detail_url: str) -> Optional[dict]:
        """
        Fetch show detail page and parse 'First Aired' and 'UPCOMING AIR TIMES'.
        Only used when no dates on main schedule. Returns None on fetch/parse error.
        """
        try:
            resp = self.session.get(detail_url, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            log.debug("Detail fetch failed %s: %s", detail_url, e)
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        first_aired = None
        upcoming_count = 0

        # "First Aired" (e.g. "1/7/2026")
        for node in soup.find_all(string=re.compile(r"First\s*Aired", re.I)):
            parent = node.parent if hasattr(node, "parent") else None
            if not hasattr(parent, "get_text"):
                continue
            block = parent.get_text()
            for m in re.finditer(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", block):
                try:
                    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    if y < 100:
                        y += 2000 if y < 50 else 1900
                    first_aired = datetime(y, mo, d).date()
                    break
                except (ValueError, TypeError):
                    pass
            if first_aired is not None:
                break

        # "UPCOMING AIR TIMES" – count list items in that section
        for node in soup.find_all(string=re.compile(r"UPCOMING\s*AIR\s*TIMES", re.I)):
            par = node.parent if hasattr(node, "parent") else None
            for _ in range(MAX_UPCOMING_PARENT_TRAVERSAL):
                if par is None or not hasattr(par, "find_all"):
                    break
                items = par.find_all(["li", "tr"], limit=50)
                items = [x for x in items if x.get_text(strip=True)]
                if items:
                    upcoming_count = len(items)
                    break
                par = getattr(par, "parent", None)
            if upcoming_count > 0:
                break

        return {"first_aired": first_aired, "upcoming_count": upcoming_count}

    def _is_rerun_from_detail(self, info: dict, today) -> bool:
        """True if detail-page info indicates a rerun (screen out)."""
        if info.get("first_aired") is not None and info["first_aired"] < today:
            return True
        if info.get("upcoming_count", 0) > 1:
            return True
        return False

    def _add_stream_urls_from_html(
        self, meetings: List[Dict], schedule_url: str
    ) -> List[Dict]:
        """
        Extract stream numbers from HTML schedule page and add stream URLs
        for upcoming meetings. The API doesn't provide stream numbers, but
        the HTML links contain them (e.g., /watch/stream/8?site=1).
        """
        try:
            # Only need to do this for upcoming meetings without meeting links
            # (live meetings already have links from API)
            upcoming_without_link = [
                m
                for m in meetings
                if m.get("Status") == "Upcoming" and not m.get("Meeting link")
            ]
            if not upcoming_without_link:
                return meetings

            log.debug(
                "Extracting stream URLs from HTML for %s upcoming meetings",
                len(upcoming_without_link),
            )

            soup = self._get_schedule_soup(schedule_url)
            if not soup:
                return meetings

            # Find all links to /watch/stream/{number}
            stream_links = soup.find_all("a", href=STREAM_LINK_PATTERN)
            show_links = soup.find_all("a", href=SHOW_LINK_PATTERN)

            # Create a mapping of show_id -> stream link (preferred method)
            show_id_to_stream = {}
            for link in stream_links:
                href = link.get("href", "")
                stream_match = STREAM_LINK_PATTERN.search(href)
                if stream_match:
                    stream_number = stream_match.group(1)
                    # Try to find show_id in the link or nearby
                    # Some stream links might have show_id in query params
                    # or nearby elements
                    show_id = None
                    # Check if there's a nearby show link with the same title
                    parent = link.find_parent(["li", "div", "article", "section"])
                    if parent:
                        nearby_show_link = parent.find("a", href=SHOW_LINK_PATTERN)
                        if nearby_show_link:
                            show_href = nearby_show_link.get("href", "")
                            show_match = SHOW_LINK_PATTERN.search(show_href)
                            if show_match:
                                show_id = show_match.group(1)
                    if show_id:
                        show_id_to_stream[show_id] = {
                            "stream_number": stream_number,
                            "full_href": href,
                        }

            # Create a mapping of show_id -> show link
            show_id_to_show_link = {}
            # Also create mapping by time + title for matching
            time_title_to_show_link = {}
            for link in show_links:
                href = link.get("href", "")
                show_match = SHOW_LINK_PATTERN.search(href)
                if show_match:
                    show_id = show_match.group(1)
                    show_id_to_show_link[show_id] = {
                        "full_href": href,
                    }
                    # Try to extract time and title from parent for matching
                    parent = link.find_parent(["li", "div", "article", "section"])
                    if parent:
                        parent_text = parent.get_text(strip=True)
                        # Try to extract time (e.g., "2:30 PM" or "4:00 PM")
                        time_match = re.search(
                            r"(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)", parent_text, re.I
                        )
                        if time_match:
                            time_str = time_match.group(0).lower()
                            # Get title (everything after time, cleaned)
                            title_part = parent_text[time_match.end() :].strip()
                            cleaned_title = self._remove_date_from_title(title_part)
                            cleaned_title = re.sub(
                                r"\d{1,2}:\d{2}\s*(AM|PM|am|pm)",
                                "",
                                cleaned_title,
                                flags=re.I,
                            ).strip()
                            if cleaned_title:
                                key = f"{time_str}_{cleaned_title.lower()}"
                                time_title_to_show_link[key] = {
                                    "full_href": href,
                                    "show_id": show_id,
                                }

            # Fallback: Create a mapping of meeting title -> stream/show link
            # (for when show_id not available)
            title_to_stream = {}
            for link in stream_links:
                href = link.get("href", "")
                stream_match = STREAM_LINK_PATTERN.search(href)
                if stream_match:
                    stream_number = stream_match.group(1)
                    link_text = link.get_text(strip=True)
                    # Remove date and time from title for matching
                    cleaned_title = self._remove_date_from_title(link_text)
                    # Remove time patterns like "11:00 pm" or "2:30 PM"
                    cleaned_title = re.sub(
                        r"\d{1,2}:\d{2}\s*(AM|PM|am|pm)", "", cleaned_title, flags=re.I
                    ).strip()
                    if cleaned_title:
                        title_to_stream[cleaned_title.lower()] = {
                            "stream_number": stream_number,
                            "full_href": href,
                        }

            title_to_show = {}
            for link in show_links:
                href = link.get("href", "")
                show_match = SHOW_LINK_PATTERN.search(href)
                if show_match:
                    link_text = link.get_text(strip=True)
                    # Remove date and time from title for matching
                    cleaned_title = self._remove_date_from_title(link_text)
                    # Remove time patterns
                    cleaned_title = re.sub(
                        r"\d{1,2}:\d{2}\s*(AM|PM|am|pm)", "", cleaned_title, flags=re.I
                    ).strip()
                    if cleaned_title:
                        title_to_show[cleaned_title.lower()] = {
                            "full_href": href,
                        }

            # Extract site parameter
            site = self._extract_site_param(schedule_url) or 1

            # Update meetings with stream URLs from HTML
            # (prefer stream URLs over VOD URLs for monitoring)
            for meeting in meetings:
                if meeting.get("Status") == "Upcoming":
                    show_id = meeting.get("show_id")
                    meeting_title = meeting.get("Meeting name", "").lower()

                    # First try to match by show_id to get stream URL
                    # (prefer stream over VOD)
                    if show_id and str(show_id) in show_id_to_stream:
                        stream_info = show_id_to_stream.get(str(show_id))
                        if stream_info:
                            stream_url = urljoin(
                                self.base_url, stream_info["full_href"]
                            )
                            site = self._extract_site_param(schedule_url) or 1
                            if "?site=" not in stream_url:
                                stream_url += f"?site={site}"
                            meeting["Meeting link"] = stream_url
                            meeting["user_live_link"] = stream_url
                            log.debug(
                                "Added stream URL for show_id %s: %s",
                                show_id,
                                stream_url,
                            )
                            continue

                    # Fallback: try to match by scheduled time + title
                    scheduled_time = meeting.get("Scheduled time", "")
                    if scheduled_time:
                        try:
                            # Parse scheduled time to get hour:minute
                            dt = parser.isoparse(scheduled_time)
                            local_tz = pytz.timezone(self.timezone)
                            local_dt = dt.astimezone(local_tz)
                            time_str = local_dt.strftime(
                                "%-I:%M %p"
                            ).lower()  # e.g., "2:30 pm"
                            key = f"{time_str}_{meeting_title}"
                            time_title_info = time_title_to_show_link.get(key)
                            if time_title_info:
                                show_url = urljoin(
                                    self.base_url, time_title_info["full_href"]
                                )
                                site = self._extract_site_param(schedule_url) or 1
                                if "?site=" not in show_url:
                                    show_url += f"?site={site}"
                                meeting["Meeting link"] = show_url
                                meeting["user_live_link"] = show_url
                                log.info(
                                    "Matched by time+title: %s -> %s", key, show_url
                                )
                                continue
                        except Exception as e:
                            log.debug("Failed to parse time for matching: %s", e)

                    # Fallback: try to match by title
                    stream_info = title_to_stream.get(meeting_title)
                    if stream_info:
                        # Construct full URL
                        stream_url = urljoin(self.base_url, stream_info["full_href"])
                        # Ensure site parameter is included
                        if "?site=" not in stream_url:
                            stream_url += f"?site={site}"
                        elif f"site={site}" not in stream_url:
                            stream_url = re.sub(
                                r"\?site=\d+", f"?site={site}", stream_url
                            )

                        meeting["Meeting link"] = stream_url
                        meeting["user_live_link"] = stream_url
                        log.debug(
                            "Added stream URL for '%s': %s",
                            meeting.get("Meeting name", "")[:50],
                            stream_url,
                        )
                        continue

                    show_info = title_to_show.get(meeting_title)
                    if show_info:
                        show_url = urljoin(self.base_url, show_info["full_href"])
                        if "?site=" not in show_url:
                            show_url += f"?site={site}"
                        elif f"site={site}" not in show_url:
                            show_url = re.sub(r"\?site=\d+", f"?site={site}", show_url)

                        meeting["Meeting link"] = show_url
                        meeting["user_live_link"] = show_url
                        log.debug(
                            "Added show URL for '%s': %s",
                            meeting.get("Meeting name", "")[:50],
                            show_url,
                        )

        except Exception as e:
            log.debug("Failed to extract stream URLs from HTML: %s", e)

        return meetings

    def _check_now_indicators(
        self, meetings: List[Dict], schedule_url: str
    ) -> List[Dict]:
        """
        Check HTML schedule page for "Now" indicators and update meeting status.

        The Cablecast schedule page displays a green "Now" button next to meetings
        that are currently in progress. This method scrapes the HTML to find these
        indicators and matches them to meetings by title.
        """
        try:
            log.debug("Checking HTML for 'Now' indicators")

            soup = self._get_schedule_soup(schedule_url)
            if not soup:
                return meetings

            # Find all "Now" indicators - they're spans with green background
            # Pattern: <span style="background-color:rgb(25, 102, 57)">Now</span>
            now_spans = soup.find_all(
                "span",
                string=NOW_TEXT_PATTERN,
                style=NOW_STYLE_PATTERN,
            )

            # Also check for "Now" text in elements with the specific classes
            if not now_spans:
                now_spans = soup.find_all(
                    "span",
                    string=NOW_TEXT_PATTERN,
                    class_=NOW_CLASS_PATTERN,
                )

            if not now_spans:
                # Fallback: just look for "Now" text in any span
                now_spans = soup.find_all("span", string=NOW_TEXT_PATTERN)

            if not now_spans:
                log.debug("No 'Now' indicators found in HTML")
                return meetings

            log.debug("Found %s 'Now' indicator(s) in HTML", len(now_spans))

            # For each "Now" indicator, find the associated meeting title
            now_meeting_titles = []
            for now_span in now_spans:
                # The meeting title is typically in a sibling or nearby element
                # Look for the parent container (usually an <li> or <div>)
                parent_item = now_span.find_parent(["li", "div", "article", "section"])

                if parent_item:
                    # Find the meeting link/title in the same container
                    link_elem = parent_item.find("a", href=True)
                    if link_elem:
                        title = link_elem.get_text(strip=True)
                        if title:
                            # Clean title (remove dates) to match our processed titles
                            cleaned_title = self._remove_date_from_title(title)
                            now_meeting_titles.append(cleaned_title)
                            log.debug(
                                "Found 'Now' meeting: '%s' (original: '%s')",
                                cleaned_title[:50],
                                title[:50],
                            )

            if not now_meeting_titles:
                log.debug("No meeting titles found associated with 'Now' indicators")
                return meetings

            # Update meeting status for matches
            updated_count = 0
            for meeting in meetings:
                meeting_title = meeting.get("Meeting name", "").strip()
                if not meeting_title:
                    continue

                # Check if this meeting title matches any "Now" meeting
                # Use fuzzy matching to handle slight variations
                for now_title in now_meeting_titles:
                    # Exact match
                    if meeting_title.lower() == now_title.lower():
                        if meeting.get("Status") != "In progress":
                            meeting["Status"] = "In progress"
                            updated_count += 1
                            log.info(
                                "Updated status to 'In progress' for: %s",
                                meeting_title[:50],
                            )
                        break

                    # Partial match (in case of title variations)
                    # Check if the core title matches (ignoring small differences)
                    meeting_words = set(meeting_title.lower().split())
                    now_words = set(now_title.lower().split())
                    # If most words match, consider it a match
                    if len(meeting_words) > 0 and len(now_words) > 0:
                        common_words = meeting_words.intersection(now_words)
                        # If at least 70% of words match, consider it a match
                        match_ratio = len(common_words) / max(
                            len(meeting_words), len(now_words)
                        )
                        if match_ratio >= TITLE_MATCH_THRESHOLD:
                            if meeting.get("Status") != "In progress":
                                meeting["Status"] = "In progress"
                                updated_count += 1
                                log.info(
                                    "Status 'In progress' for: %s (matched: %s)",
                                    meeting_title[:50],
                                    now_title[:50],
                                )
                            break

            if updated_count > 0:
                log.info(
                    "Updated %s meeting(s) to 'In progress' based on 'Now' indicators",
                    updated_count,
                )

        except Exception as e:
            log.debug("Failed to check 'Now' indicators: %s", e)

        return meetings

    def _update_live_stream_urls(self, meetings: List[Dict]) -> List[Dict]:
        """
        Update meeting links for live meetings by fetching current stream URLs.

        For meetings that are "In progress", this method can re-fetch the API
        to get the liveStreamUrl if it wasn't available initially. This is useful
        for capturing the correct stream URL for each channel/stream when multiple
        simultaneous meetings are occurring.
        """
        # Only update if we have live meetings and used JSON API
        live_meetings = [m for m in meetings if m.get("Status") == "In progress"]
        if not live_meetings or not hasattr(self, "url"):
            return meetings

        # Try to refresh live stream URLs for in-progress meetings
        try:
            parsed_url = urlparse(self.url)
            host = parsed_url.netloc
            parts = host.split(".")
            if len(parts) >= 3:
                api_base = (
                    f"{parsed_url.scheme}://cloud.{parts[1]}.{parts[2]}"
                    f"/api/publicsitedata"
                )
                site = self._extract_site_param(self.url) or 1

                tz = pytz.timezone(self.timezone or "America/New_York")
                params = {
                    "currentDay": datetime.now(tz).strftime("%Y-%m-%d"),
                    "host": host,
                    "site": site,
                }

                response = get_api_json_call(f"{api_base}/schedule", params)
                if response and "scheduleItems" in response:
                    # Create a lookup by title and time
                    api_items = {}
                    for item in response.get("scheduleItems", []):
                        item_title = item.get("title", "").strip()
                        item_time = item.get("runDateTime", "")
                        if item_title and item_time:
                            key = f"{item_title[:50]}_{item_time[:16]}"
                            api_items[key] = item

                    # Update meeting links for live meetings
                    for meeting in meetings:
                        if meeting.get("Status") == "In progress":
                            title = meeting.get("Meeting name", "")
                            scheduled_time = meeting.get("Scheduled time", "")
                            if title and scheduled_time:
                                key = f"{title[:50]}_{scheduled_time[:16]}"
                                api_item = api_items.get(key)
                                if api_item:
                                    show = api_item.get("show", {})
                                    live_url = show.get("liveStreamUrl")
                                    if live_url:
                                        meeting["Meeting link"] = live_url
                                        log.debug(
                                            "Updated live stream URL for: %s",
                                            title[:50],
                                        )

        except Exception as e:
            log.debug("Failed to update live stream URLs: %s", e)

        return meetings

    def _get_schedule_soup(self, schedule_url: str) -> Optional[BeautifulSoup]:
        """Fetch and cache schedule HTML soup to avoid repeated network calls."""
        if (
            self._schedule_soup_cache is not None
            and self._schedule_soup_url == schedule_url
        ):
            return self._schedule_soup_cache

        try:
            html = self.scraper.scrape_html(url=schedule_url)
            soup = self.scraper.convert_to_soup(html)
            self._schedule_soup_cache = soup
            self._schedule_soup_url = schedule_url
            return soup
        except Exception as e:
            log.debug("Failed to load schedule HTML: %s", e)
            return None

    def _format_utc_time(self, dt: datetime) -> str:
        """Convert a local datetime to UTC ISO string with millisecond precision."""
        local_tz = pytz.timezone(self.timezone)
        if dt.tzinfo is None:
            localized_dt = local_tz.localize(dt)
        else:
            localized_dt = dt.astimezone(local_tz)
        utc_dt = localized_dt.astimezone(pytz.UTC)
        return utc_dt.strftime(MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT)[:-3] + "Z"

    def _extract_site_param(self, url: str) -> Optional[int]:
        """Extract site parameter from URL."""
        if not url:
            return None
        try:
            parsed = urlparse(url)
            params = parsed.query.split("&")
            for param in params:
                if "=" in param:
                    key, value = param.split("=", 1)
                    if key.lower() == "site":
                        return int(value)
        except (ValueError, AttributeError, TypeError):
            pass
        return None


if __name__ == "__main__":
    from schedule.schedule_scraper import run_test

    run_test(
        url="https://capitoltvri.cablecast.tv/schedule?site=1",
        schedule_type="cablecast_table",
        timezone="America/New_York",
        get_full_archive_flag=False,
    )
