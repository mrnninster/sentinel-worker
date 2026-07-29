# spokane.py
import os
import re
import logging
from datetime import datetime, timedelta, date
from urllib.parse import urljoin, urlparse

import pytz
from dateutil import parser

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test

from utils.scrape_html import HtmlScraper

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

DATE_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.000Z"
STREAM_WINDOW_HOURS = 8
STREAM_EARLY_MINUTES = 20


class Spokane:
    """
    Self contained scraper for City of Spokane City Council meetings.

    Config:
    {
        "schedule_type": "unique_spokane",
        "stream_type": "streamlink",
        "detect_start_method": "calendar_detect",
        "detect_end_method": "stream_detect",
    }
    """

    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True

    def unique_spokane(self, url: str, timezone: str = "America/Los_Angeles") -> list:
        base_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        # Use direct scraping (bypass ScraperAPI) - works fine for Spokane
        html = self.scraper.scrape_directly(url)
        if isinstance(html, dict) and html.get("max_failure"):
            log.warning("Spokane: scrape failed for %s", url)
            return []
        soup = self.scraper.convert_to_soup(html)

        content = self._find_meeting_content(soup)
        if not content:
            log.warning("Spokane: could not find meetings content block")
            return []

        local_tz = pytz.timezone(timezone)
        now_local = datetime.now(local_tz)

        current_date = None
        current_agenda_link = None
        agenda_used = False

        for child in content.find_all(recursive=False):
            if child.name == "h3" and "Section" in child.get("class", []):
                date_link = child.find("a", href=True)
                date_href = date_link.get("href") if date_link else None
                current_date = self._parse_date_from_href(
                    date_href
                ) or self._parse_date_from_text(
                    child.get_text(" ", strip=True), now_local
                )
                current_agenda_link = None
                agenda_used = False
                continue

            if (
                child.name == "ul"
                and "Tiles" in child.get("class", [])
                and current_date
            ):
                current_agenda_link = self._extract_agenda_link(child, base_url)
                continue

            if (
                child.name == "div"
                and "Section" in child.get("class", [])
                and current_date
            ):
                agenda_link = current_agenda_link if not agenda_used else None
                meeting = self._parse_meeting_section(
                    child,
                    current_date,
                    agenda_link,
                    base_url,
                    local_tz,
                    now_local,
                )
                if meeting:
                    if meeting.get("Agenda link"):
                        agenda_used = True
                    self.meetings.append(meeting)

        return self.meetings

    def _find_meeting_content(self, soup):
        heading = soup.find("h2", string=lambda s: s and "City Council Meetings" in s)
        if heading:
            return heading.find_parent("div", class_="Content")
        return None

    def _parse_date_from_href(self, href: str) -> date | None:
        if not href:
            return None
        match = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", href)
        if not match:
            return None
        year, month, day = map(int, match.groups())
        return date(year, month, day)

    def _parse_date_from_text(self, text: str, now_local: datetime) -> date | None:
        if not text:
            return None
        try:
            parsed = parser.parse(text, fuzzy=True, default=now_local)
        except (ValueError, parser.ParserError):
            return None
        return parsed.date()

    def _extract_agenda_link(self, ul, base_url: str) -> str | None:
        link = ul.find("a", href=True)
        if not link:
            return None
        return self._normalize_url(link.get("href"), base_url)

    def _parse_meeting_section(
        self,
        section,
        meeting_date: date,
        agenda_link: str | None,
        base_url: str,
        local_tz,
        now_local: datetime,
    ) -> dict | None:
        name_tag = section.find("h5")
        if not name_tag:
            return None
        meeting_name = name_tag.get_text(" ", strip=True)

        detail_link = None
        link_tag = name_tag.find("a", href=True)
        if link_tag:
            detail_link = self._normalize_url(link_tag.get("href"), base_url)

        time_text = self._extract_time_text(section)
        if not time_text:
            log.warning("Spokane: no time found for %s", meeting_name)
            return None

        meeting_dt_local = self._build_local_datetime(meeting_date, time_text, local_tz)
        if not meeting_dt_local:
            return None

        status = "Upcoming"
        if self._is_cancelled(meeting_name, section):
            status = "Cancelled"

        user_live_link = detail_link
        stream_link = None
        should_check_stream = now_local >= (
            meeting_dt_local - timedelta(minutes=STREAM_EARLY_MINUTES)
        )
        if detail_link and should_check_stream:
            stream_link = self._extract_stream_link(detail_link)
            if (
                stream_link
                and status != "Cancelled"
                and self._in_stream_window(meeting_dt_local, now_local)
            ):
                status = "In Progress"
        meeting_link = stream_link or user_live_link

        scheduled_time = meeting_dt_local.astimezone(pytz.UTC).strftime(DATE_UTC_FORMAT)

        return {
            "Meeting name": meeting_name,
            "Scheduled time": scheduled_time,
            "Meeting link": meeting_link,
            "Agenda link": agenda_link,
            "Status": status,
            "user_live_link": user_live_link,
        }

    def _extract_time_text(self, section) -> str | None:
        time_source = section.find("p")
        if not time_source:
            return None
        text = time_source.get_text(" ", strip=True)
        if not text:
            return None
        lowered = text.lower()
        if "noon" in lowered:
            return "12:00 PM"
        if "midnight" in lowered:
            return "12:00 AM"
        match = re.search(r"(\d{1,2}(?::\d{2})?\s*[ap]\.??m\.?)", text, re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    def _build_local_datetime(
        self, meeting_date: date, time_text: str, local_tz
    ) -> datetime | None:
        try:
            cleaned = re.sub(r"\.", "", time_text).strip().upper()
            parsed_time = parser.parse(cleaned).time()
        except (ValueError, parser.ParserError):
            return None
        naive_dt = datetime.combine(meeting_date, parsed_time)
        return local_tz.localize(naive_dt)

    def _extract_stream_link(self, detail_url: str) -> str | None:
        """
        Extract stream URL from the meeting detail page.

        Spokane uses two different video systems:
        - Live streams: JWPlayer via /citycable5/live/player.html iframe
        - Recordings: Vimeo player embeds

        Returns the full stream URL or None if no video found.
        """
        try:
            # Use direct scraping (bypass ScraperAPI) for faster response
            html = self.scraper.scrape_directly(detail_url)
        except Exception as exc:
            log.warning(
                "Spokane: failed to fetch detail page %s (%s)", detail_url, exc
            )
            return None
        if isinstance(html, dict) and html.get("max_failure"):
            return None
        soup = self.scraper.convert_to_soup(html)

        # Check for citycable5 live stream iframe (JWPlayer live channel)
        live_iframe = soup.find(
            "iframe", src=re.compile(r"citycable5/live", re.IGNORECASE)
        )
        if live_iframe:
            live_url = self._normalize_url(live_iframe.get("src"), detail_url)
            log.info("Spokane: found citycable5 live stream: %s", live_url)
            return live_url

        # Check for Vimeo recording iframe
        vimeo_iframe = soup.find("iframe", src=re.compile(r"vimeo\.com", re.IGNORECASE))
        if vimeo_iframe:
            return self._normalize_url(vimeo_iframe.get("src"), detail_url)

        return None

    def _normalize_url(self, href: str | None, base_url: str) -> str | None:
        if not href:
            return None
        if href.startswith("//"):
            return f"https:{href}"
        return urljoin(base_url, href)

    def _is_cancelled(self, meeting_name: str, section) -> bool:
        lowered_name = meeting_name.lower()
        if "cancel" in lowered_name:
            return True
        text = section.get_text(" ", strip=True).lower()
        return "cancel" in text

    def _in_stream_window(
        self, meeting_dt_local: datetime, now_local: datetime
    ) -> bool:
        start_window = meeting_dt_local - timedelta(minutes=STREAM_EARLY_MINUTES)
        end_window = meeting_dt_local + timedelta(hours=STREAM_WINDOW_HOURS)
        return start_window <= now_local <= end_window


if __name__ == "__main__":
    from pytz import timezone as pytz_timezone

    test_timezone = "America/Los_Angeles"
    test_tz = pytz_timezone(test_timezone)

    run_test(
        url="https://my.spokanecity.org/citycouncil/meetings/",
        schedule_type="unique_spokane",
        timezone=test_timezone,
        get_full_archive_flag=False,
        get_date_start=test_tz.localize(datetime(2026, 1, 10)),
    )
