import os
import re
import sys
import logging
from datetime import datetime, timedelta
from typing import List, Optional
from urllib.parse import urljoin, urlparse

if __name__ == "__main__":
    sys.path.append(
        os.getenv("LOCAL_PROJECT_PATH")
        or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    )

import pytz
from bs4 import BeautifulSoup, Tag
from dateutil import parser as dateparser

from utils.scrape_html import HtmlScraper

log = logging.getLogger(__name__)

LOOKBACK_DAYS = 7


class Centralpoint:
    """
    Self-contained scraper for Oxcyon CentralPoint CMS meeting pages.

    CentralPoint pages list meetings as headings (h3/h4/strong) with
    "View / Download Agenda" links to PDF files. Dates are embedded in
    PDF filenames (e.g., "2-17-2026PRELIMINARYAGENDA.pdf").
    """

    self_contained_parser = True

    def __init__(self):
        self.scraper = HtmlScraper()
        self.base_url: Optional[str] = None

    def centralpoint_table(self, url: str, timezone: str) -> List[dict]:
        self.base_url = self._derive_base_url(url)
        soup = self._fetch(url)
        if not soup:
            return []

        meetings = []
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        # Find headings that describe meetings, paired with their agenda links
        for heading in soup.find_all(["h3", "h4", "strong", "b"]):
            meeting_name = heading.get_text(strip=True)
            if not meeting_name or len(meeting_name) < 3:
                continue

            # Skip generic headings
            if meeting_name.lower() in (
                "board of commissioners meeting agendas",
                "meeting agendas",
                "agendas",
                "minutes",
            ):
                continue

            # Find the next "View / Download Agenda" link after this heading
            agenda_link = None
            date_str = None
            next_link = heading.find_next("a", href=True)

            if next_link:
                href = next_link.get("href", "")
                # Only use links that point to uploaded documents
                if "/Uploads/" in href or href.lower().endswith(
                    (".pdf", ".docx", ".doc")
                ):
                    agenda_link = urljoin(self.base_url, href)
                    # Extract date from filename
                    date_str = self._extract_date_from_filename(href)

            if not date_str:
                # Try extracting date from the meeting name itself
                date_str = self._extract_date_from_text(meeting_name)

            if not date_str:
                continue

            scheduled_time = self._to_utc_iso(date_str, timezone)
            if not scheduled_time:
                continue

            utc_dt = self._parse_iso_to_utc(scheduled_time)
            if utc_dt and utc_dt < min_allowed:
                continue

            status = self._determine_status(meeting_name, scheduled_time)

            meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": scheduled_time,
                    "Meeting link": None,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
            )

        return meetings

    def _extract_date_from_filename(self, href: str) -> Optional[str]:
        """Extract a date from a PDF filename like '2-17-2026PRELIMINARY...'."""
        filename = href.split("/")[-1]
        # Pattern: M-D-YYYY or MM-DD-YYYY at start of filename
        match = re.search(r"(\d{1,2}-\d{1,2}-\d{2,4})", filename)
        if match:
            return match.group(1)
        # Pattern: M-DD-YY
        match = re.search(r"(\d{1,2}-\d{1,2}-\d{2})\b", filename)
        if match:
            return match.group(1)
        return None

    def _extract_date_from_text(self, text: str) -> Optional[str]:
        """Try to extract a date from meeting heading text."""
        # Look for patterns like "January 20, 2026" or "1/20/2026"
        match = re.search(
            r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
            r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
            r"Nov(?:ember)?|Dec(?:ember)?),?\s+\d{1,2},?\s+\d{2,4}",
            text,
            re.I,
        )
        if match:
            return match.group(0)
        match = re.search(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}", text)
        if match:
            return match.group(0)
        return None

    def _determine_status(self, title: str, scheduled_time: str) -> str:
        if re.search(r"cancel", title, re.I):
            return "Cancelled"
        utc_dt = self._parse_iso_to_utc(scheduled_time)
        if utc_dt and utc_dt < datetime.now(pytz.UTC):
            return "Past"
        return "Upcoming"

    def _fetch(self, url: str) -> Optional[BeautifulSoup]:
        try:
            html = self.scraper.scrape_html(url=url)
            if not html or (isinstance(html, dict) and "max_failure" in html):
                return None
            return self.scraper.convert_to_soup(string=html)
        except Exception as e:
            log.warning("Centralpoint: error fetching %s: %s", url, e)
            return None

    def _derive_base_url(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _to_utc_iso(self, time_str: str, timezone: str) -> Optional[str]:
        try:
            default_dt = datetime.now().replace(
                hour=12, minute=0, second=0, microsecond=0, tzinfo=None
            )
            dt = dateparser.parse(time_str, fuzzy=True, default=default_dt)
            if not dt:
                return None
            if dt.year < 2020 or dt.year > datetime.now().year + 2:
                return None
            if dt.tzinfo is None:
                local_tz = pytz.timezone(timezone)
                dt = local_tz.localize(dt)
            utc = dt.astimezone(pytz.UTC)
            return utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except (ValueError, TypeError):
            return None

    def _parse_iso_to_utc(self, iso_str: str) -> Optional[datetime]:
        try:
            dt = dateparser.parse(iso_str)
            if dt and dt.tzinfo is None:
                dt = pytz.UTC.localize(dt)
            return dt
        except (ValueError, TypeError):
            return None


if __name__ == "__main__":
    from schedule.schedule_scraper import run_test

    run_test(
        url="https://www.douglascountymn.gov/agendas",
        timezone="America/Chicago",
        schedule_type="centralpoint_table",
    )
