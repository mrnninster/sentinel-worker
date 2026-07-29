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

# Full month name date pattern
FULL_DATE_RE = re.compile(
    r"((?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},?\s+\d{4})",
    re.I,
)

# Short date pattern MM/DD/YY or MM/DD/YYYY
SHORT_DATE_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{2,4})")


class Wix:
    """
    Self-contained scraper for Wix-based government meeting pages.

    Wix sites use server-side rendering (SSR), so meeting content is often
    present in the static HTML within `wixui-rich-text` divs. This parser
    extracts dates and document links from the SSR content.

    Some Wix sites have minimal SSR content and need JavaScript rendering
    via ScraperAPI.
    """

    self_contained_parser = True

    def __init__(self):
        self.scraper = HtmlScraper()
        self.base_url: Optional[str] = None

    def wix_table(self, url: str, timezone: str) -> List[dict]:
        self.base_url = self._derive_base_url(url)
        soup = self._fetch(url)
        if not soup:
            return []

        # Strategy 1: Find date-labeled sections with document links
        meetings = self._parse_dated_sections(soup, url, timezone)

        # Strategy 2: Look for dated links to PDF files
        if not meetings:
            meetings = self._parse_dated_links(soup, url, timezone)

        # Strategy 3: Extract dates from rich text blocks
        if not meetings:
            meetings = self._parse_rich_text(soup, url, timezone)

        return meetings

    def _parse_dated_sections(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        """
        Parse sections where dates appear as standalone text elements
        followed by or near document links.
        """
        meetings = []
        seen_dates = set()
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        # Find all rich text containers
        for container in soup.find_all(
            "div", class_=re.compile(r"wixui-rich-text|rich-text", re.I)
        ):
            text = container.get_text(strip=True)
            if not text:
                continue

            # Check for full date pattern
            match = FULL_DATE_RE.search(text)
            if not match:
                continue

            date_str = match.group(1)
            scheduled_time = self._to_utc_iso(date_str, timezone)
            if not scheduled_time:
                continue

            utc_dt = self._parse_iso_to_utc(scheduled_time)
            if utc_dt and utc_dt < min_allowed:
                continue

            date_key = scheduled_time[:10]
            if date_key in seen_dates:
                continue
            seen_dates.add(date_key)

            # Try to get meeting name from context
            meeting_name = self._extract_name_from_context(
                container, date_str
            )

            # Look for agenda links nearby
            agenda_link = self._find_nearby_agenda(container)

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

    def _parse_dated_links(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        """Parse links whose text or URL contains dates."""
        meetings = []
        seen_dates = set()
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        for link in soup.find_all("a", href=True):
            link_text = link.get_text(strip=True)
            href = link["href"].strip()

            # Check for date in link text
            date_match = SHORT_DATE_RE.search(link_text)
            if not date_match:
                date_match = FULL_DATE_RE.search(link_text)
            if not date_match:
                continue

            date_str = date_match.group(1)
            scheduled_time = self._to_utc_iso(date_str, timezone)
            if not scheduled_time:
                continue

            utc_dt = self._parse_iso_to_utc(scheduled_time)
            if utc_dt and utc_dt < min_allowed:
                continue

            date_key = scheduled_time[:10]
            if date_key in seen_dates:
                continue
            seen_dates.add(date_key)

            # Determine if this is an agenda link
            abs_href = urljoin(self.base_url, href)
            is_doc = abs_href.lower().endswith(
                (".pdf", ".docx", ".doc")
            ) or "/_files/" in abs_href

            # Determine meeting type from context
            meeting_name = self._determine_meeting_type(link)

            status = self._determine_status(meeting_name, scheduled_time)

            meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": scheduled_time,
                    "Meeting link": url,
                    "Agenda link": abs_href if is_doc else None,
                    "Status": status,
                }
            )

        return meetings

    def _parse_rich_text(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        """Last resort: scan all text for date patterns."""
        meetings = []
        seen_dates = set()
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        text = soup.get_text(" ", strip=True)
        for match in FULL_DATE_RE.finditer(text):
            date_str = match.group(1)
            scheduled_time = self._to_utc_iso(date_str, timezone)
            if not scheduled_time:
                continue

            utc_dt = self._parse_iso_to_utc(scheduled_time)
            if utc_dt and utc_dt < min_allowed:
                continue

            date_key = scheduled_time[:10]
            if date_key in seen_dates:
                continue
            seen_dates.add(date_key)

            status = self._determine_status("Meeting", scheduled_time)

            meetings.append(
                {
                    "Meeting name": "City Council Meeting",
                    "Scheduled time": scheduled_time,
                    "Meeting link": url,
                    "Agenda link": None,
                    "Status": status,
                }
            )

        return meetings

    def _extract_name_from_context(
        self, container: Tag, date_str: str
    ) -> str:
        """Try to determine meeting name from the surrounding context."""
        # Meeting-related keywords to prefer
        meeting_kw = re.compile(
            r"council|board|commission|committee|meeting|session|hearing|agenda",
            re.I,
        )

        # Check for headings nearby that mention meeting-related terms
        parent = container.find_parent(["div", "section"])
        if parent:
            for h in parent.find_all(["h1", "h2", "h3", "h4", "h5"]):
                text = h.get_text(strip=True)
                if (
                    text
                    and date_str not in text
                    and len(text) > 3
                    and meeting_kw.search(text)
                ):
                    return text

        # Check page headings for meeting-related terms
        root = container.find_parent(["html", "[document]"])
        if root:
            for h in root.find_all(["h1", "h2", "h3"]):
                text = h.get_text(strip=True)
                if text and len(text) > 3 and meeting_kw.search(text):
                    return text

        return "City Council Meeting"

    def _determine_meeting_type(self, link: Tag) -> str:
        """Determine meeting type from link context."""
        # Walk up to find a heading
        for parent in link.parents:
            if parent.name in ["div", "section"]:
                for h in parent.find_all(
                    ["h1", "h2", "h3", "h4", "h5"], limit=3
                ):
                    text = h.get_text(strip=True)
                    if text and len(text) > 3:
                        # Clean up heading text
                        cleaned = re.sub(
                            r"\s*(?:Minutes|Agendas?|Packets?):\s*",
                            "",
                            text,
                            flags=re.I,
                        )
                        if cleaned:
                            return cleaned.strip(" -–—:,")
                break
        return "City Council Meeting"

    def _find_nearby_agenda(self, container: Tag) -> Optional[str]:
        """Find agenda/document links near a container."""
        # Check siblings and parent for PDF links
        parent = container.find_parent("div")
        search_area = parent if parent else container

        for link in search_area.find_all("a", href=True):
            href = link["href"].strip()
            if href.lower().endswith(".pdf") or "/_files/" in href:
                link_text = link.get_text(strip=True).lower()
                if "agenda" in link_text or "packet" in link_text:
                    return urljoin(self.base_url, href)

        # Fallback: any PDF link
        for link in search_area.find_all("a", href=True):
            href = link["href"].strip()
            if href.lower().endswith(".pdf") or "/_files/" in href:
                return urljoin(self.base_url, href)

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
            log.warning("Wix: error fetching %s: %s", url, e)
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
        url="https://www.breckenridgemn.net/citycouncil",
        timezone="America/Chicago",
        schedule_type="wix_table",
    )
