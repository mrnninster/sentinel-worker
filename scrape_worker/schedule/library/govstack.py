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


class Govstack:
    """
    Self-contained scraper for CivicLive/GovStack CMS meeting pages.

    CivicLive sites use React portlets that require JavaScript rendering
    to display meeting content. This parser uses ScraperAPI's render mode
    to get the rendered HTML.

    When ScraperAPI rendering is insufficient (e.g., React portlets need
    additional time or interaction), this parser falls back to parsing
    whatever static content is available, including PDF links and
    date-containing text.
    """

    self_contained_parser = True

    def __init__(self):
        self.scraper = HtmlScraper()
        self.base_url: Optional[str] = None

    def govstack_table(self, url: str, timezone: str) -> List[dict]:
        self.base_url = self._derive_base_url(url)

        # Try rendered fetch first (CivicLive needs JS)
        soup = self._fetch_rendered(url)
        if not soup:
            soup = self._fetch(url)
        if not soup:
            return []

        # Strategy 1: Table-based layout (rendered React portlet)
        meetings = self._parse_table(soup, url, timezone)

        # Strategy 2: Link-based extraction (PDF agenda links with dates)
        if not meetings:
            meetings = self._parse_agenda_links(soup, url, timezone)

        # Strategy 3: Generic date scan
        if not meetings:
            meetings = self._parse_date_scan(soup, url, timezone)

        return meetings

    def _parse_table(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        """Parse CivicLive table-based meeting layout (rendered React)."""
        meetings = []
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue

            for row in rows:
                cells = row.find_all(["td", "th"])
                if len(cells) < 2:
                    continue

                row_text = row.get_text(" ", strip=True)
                # Must contain a date
                date_match = re.search(
                    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
                    r"\w*\s+\d{1,2},?\s+\d{4}",
                    row_text,
                    re.I,
                )
                if not date_match:
                    date_match = re.search(
                        r"\d{1,2}/\d{1,2}/\d{2,4}", row_text
                    )
                if not date_match:
                    continue

                scheduled_time = self._to_utc_iso(row_text, timezone)
                if not scheduled_time:
                    continue

                utc_dt = self._parse_iso_to_utc(scheduled_time)
                if utc_dt and utc_dt < min_allowed:
                    continue

                meeting_name = self._extract_name_from_cells(cells)
                agenda_link = self._extract_link_from_row(row)

                status = self._determine_status(meeting_name, scheduled_time)

                meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": scheduled_time,
                        "Meeting link": url,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }
                )

        return meetings

    def _parse_agenda_links(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        """Parse PDF agenda links that contain dates."""
        meetings = []
        seen_dates = set()
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        date_re = re.compile(
            r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
            r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
            r"Nov(?:ember)?|Dec(?:ember)?)[.,]?\s+\d{1,2}[.,]?\s+\d{2,4}",
            re.I,
        )

        for link in soup.find_all("a", href=True):
            link_text = link.get_text(strip=True)
            href = link["href"].strip()

            if not link_text or not date_re.search(link_text):
                continue

            scheduled_time = self._to_utc_iso(link_text, timezone)
            if not scheduled_time:
                continue

            utc_dt = self._parse_iso_to_utc(scheduled_time)
            if utc_dt and utc_dt < min_allowed:
                continue

            date_key = scheduled_time[:10]
            if date_key in seen_dates:
                continue
            seen_dates.add(date_key)

            abs_href = urljoin(self.base_url, href)
            is_doc = abs_href.lower().endswith((".pdf", ".docx", ".doc"))

            meeting_name = self._extract_name_from_link(link_text)

            meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": scheduled_time,
                    "Meeting link": url,
                    "Agenda link": abs_href if is_doc else None,
                    "Status": self._determine_status(
                        meeting_name, scheduled_time
                    ),
                }
            )

        return meetings

    def _parse_date_scan(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        """Fallback: scan text for date patterns."""
        meetings = []
        seen_dates = set()
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        date_re = re.compile(
            r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
            r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
            r"Nov(?:ember)?|Dec(?:ember)?)[.,]?\s+\d{1,2}[.,]?\s+\d{2,4}",
            re.I,
        )

        for elem in soup.find_all(["li", "p", "div", "span"]):
            if elem.find(["li", "p"]):
                continue

            text = elem.get_text(" ", strip=True)
            if not text or len(text) < 8 or len(text) > 500:
                continue

            if not date_re.search(text):
                continue

            scheduled_time = self._to_utc_iso(text, timezone)
            if not scheduled_time:
                continue

            utc_dt = self._parse_iso_to_utc(scheduled_time)
            if utc_dt and utc_dt < min_allowed:
                continue

            date_key = scheduled_time[:10]
            if date_key in seen_dates:
                continue
            seen_dates.add(date_key)

            meeting_name = self._extract_name_from_text(text)
            agenda_link = self._extract_agenda_from_element(elem)

            meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": scheduled_time,
                    "Meeting link": url,
                    "Agenda link": agenda_link,
                    "Status": self._determine_status(
                        meeting_name, scheduled_time
                    ),
                }
            )

        return meetings

    def _extract_name_from_cells(self, cells) -> str:
        """Extract meeting name from table cells."""
        for cell in cells:
            text = cell.get_text(strip=True)
            # Skip cells that are just dates
            if re.match(
                r"^\d{1,2}/\d{1,2}/\d{2,4}$", text
            ) or re.match(
                r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)",
                text,
                re.I,
            ):
                continue
            if text and len(text) > 3:
                return text
        return "Council Meeting"

    def _extract_link_from_row(self, row: Tag) -> Optional[str]:
        """Extract the best document link from a table row."""
        for link in row.find_all("a", href=True):
            href = link["href"].strip()
            if href.lower().endswith((".pdf", ".docx", ".doc")):
                return urljoin(self.base_url, href)
        return None

    def _extract_name_from_link(self, text: str) -> str:
        """Extract meeting name from link text by removing dates."""
        cleaned = re.sub(
            r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
            r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
            r"Nov(?:ember)?|Dec(?:ember)?)[.,]?\s+\d{1,2}[.,]?\s+\d{2,4}",
            "",
            text,
            flags=re.I,
        )
        cleaned = re.sub(r"\d{1,2}/\d{1,2}/\d{2,4}", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—:,")
        return cleaned if cleaned else "Council Meeting"

    def _extract_name_from_text(self, text: str) -> str:
        """Extract meeting name from generic text."""
        return self._extract_name_from_link(text)

    def _extract_agenda_from_element(self, elem: Tag) -> Optional[str]:
        """Find agenda link in an element."""
        for link in elem.find_all("a", href=True):
            href = link["href"].strip()
            if href.lower().endswith((".pdf", ".docx", ".doc")):
                return urljoin(self.base_url, href)
        return None

    def _determine_status(self, title: str, scheduled_time: str) -> str:
        if re.search(r"cancel", title, re.I):
            return "Cancelled"
        utc_dt = self._parse_iso_to_utc(scheduled_time)
        if utc_dt and utc_dt < datetime.now(pytz.UTC):
            return "Past"
        return "Upcoming"

    def _fetch_rendered(self, url: str) -> Optional[BeautifulSoup]:
        """Fetch with JS rendering enabled."""
        try:
            html = self.scraper.scrape_html(
                url=url, render="true", wait_for_seconds=5
            )
            if not html or (isinstance(html, dict) and "max_failure" in html):
                return None
            return self.scraper.convert_to_soup(string=html)
        except Exception as e:
            log.warning("Govstack: error fetching rendered %s: %s", url, e)
            return None

    def _fetch(self, url: str) -> Optional[BeautifulSoup]:
        """Fetch without rendering (fallback)."""
        try:
            html = self.scraper.scrape_html(url=url)
            if not html or (isinstance(html, dict) and "max_failure" in html):
                return None
            return self.scraper.convert_to_soup(string=html)
        except Exception as e:
            log.warning("Govstack: error fetching %s: %s", url, e)
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
        url="https://www.corcoranmn.gov/our_government/council/agenda_packets",
        timezone="America/Chicago",
        schedule_type="govstack_table",
    )
