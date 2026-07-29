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

# Month names for date extraction from link text like "February 2026 ..."
MONTH_RE = re.compile(
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)",
    re.I,
)


class Drupal:
    """
    Self-contained scraper for Drupal-based government meeting/agenda pages.

    Common patterns:
    1. Year headings (h2) followed by <ul> lists of agenda links.
       Link text: "February 2026 Grant City Council Meeting Agenda"
       Links to intermediate HTML pages containing actual PDF links.
    2. Direct PDF links organized under year/month headings.
    3. Views-based listing with date fields and document attachments.
    """

    self_contained_parser = True

    def __init__(self):
        self.scraper = HtmlScraper()
        self.base_url: Optional[str] = None

    def drupal_table(self, url: str, timezone: str) -> List[dict]:
        self.base_url = self._derive_base_url(url)
        soup = self._fetch(url)
        if not soup:
            return []

        # Strategy 1: Year-headed lists (most common Drupal pattern)
        meetings = self._parse_year_lists(soup, url, timezone)

        # Strategy 2: Views-based listing with node links
        if not meetings:
            meetings = self._parse_views_listing(soup, url, timezone)

        # Strategy 3: Generic dated links
        if not meetings:
            meetings = self._parse_dated_links(soup, url, timezone)

        return meetings

    def _parse_year_lists(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        """Parse year-headed <h2>/<h3> sections with <ul> lists of agenda links."""
        meetings = []
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)
        current_year = datetime.now().year

        for heading in soup.find_all(["h1", "h2", "h3"]):
            heading_text = heading.get_text(strip=True)

            # Check if this is a year heading (e.g., "2026")
            year_match = re.match(r"^(\d{4})$", heading_text.strip())
            if not year_match:
                continue

            year = int(year_match.group(1))
            # Only process current and previous year
            if year < current_year - 1:
                continue

            # Find the next <ul> after this heading
            ul = heading.find_next("ul")
            if not ul:
                continue

            for li in ul.find_all("li", recursive=False):
                link = li.find("a", href=True)
                if not link:
                    continue

                link_text = link.get_text(strip=True)
                href = link["href"].strip()

                # Extract month from link text like "February 2026 ... Agenda"
                month_match = MONTH_RE.search(link_text)
                if not month_match:
                    continue

                month_name = month_match.group(1)
                # Build a date string (use 15th of month since these are
                # monthly entries without specific days)
                date_str = f"{month_name} 15, {year}"
                scheduled_time = self._to_utc_iso(date_str, timezone)
                if not scheduled_time:
                    continue

                # Use a wider lookback for month-only entries since
                # the actual meeting could be any day in the month
                month_min = datetime.now(pytz.UTC) - timedelta(days=45)
                utc_dt = self._parse_iso_to_utc(scheduled_time)
                if utc_dt and utc_dt < month_min:
                    continue

                # Extract meeting name
                meeting_name = self._extract_name_from_link(link_text)

                # Build agenda link - follow to detail page if needed
                abs_href = urljoin(self.base_url, href)
                agenda_link = self._resolve_agenda_link(abs_href)

                status = self._determine_status(link_text, scheduled_time)

                meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": scheduled_time,
                        "Meeting link": abs_href,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }
                )

        return meetings

    def _parse_views_listing(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        """Parse Drupal Views-based listing with structured fields."""
        meetings = []
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        # Look for Views row containers
        for row in soup.find_all(
            "div", class_=re.compile(r"views-row|node-teaser", re.I)
        ):
            # Look for date field
            date_field = row.find(
                "span", class_=re.compile(r"date-display|field-date", re.I)
            )
            if not date_field:
                date_field = row.find("time")

            if not date_field:
                continue

            date_text = date_field.get_text(strip=True)
            scheduled_time = self._to_utc_iso(date_text, timezone)
            if not scheduled_time:
                continue

            utc_dt = self._parse_iso_to_utc(scheduled_time)
            if utc_dt and utc_dt < min_allowed:
                continue

            # Get title
            title_elem = row.find(["h2", "h3", "h4", "a"])
            meeting_name = (
                title_elem.get_text(strip=True) if title_elem else "Meeting"
            )

            # Get links
            agenda_link = self._extract_agenda_link(row)
            meeting_link = None
            heading_link = row.find("a", href=True)
            if heading_link:
                meeting_link = urljoin(
                    self.base_url, heading_link["href"].strip()
                )

            status = self._determine_status(meeting_name, scheduled_time)

            meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": scheduled_time,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
            )

        return meetings

    def _parse_dated_links(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        """Fallback: parse any links containing dates."""
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

            meeting_name = self._extract_name_from_link(link_text)
            abs_href = urljoin(self.base_url, link["href"].strip())
            agenda_link = (
                abs_href
                if abs_href.lower().endswith((".pdf", ".docx", ".doc"))
                else None
            )

            status = self._determine_status(link_text, scheduled_time)

            meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": scheduled_time,
                    "Meeting link": abs_href,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
            )

        return meetings

    def _resolve_agenda_link(self, detail_url: str) -> Optional[str]:
        """
        Follow a detail page link and find the actual PDF agenda link.
        Drupal sites often have intermediate pages containing file attachments.
        """
        if detail_url.lower().endswith((".pdf", ".docx", ".doc")):
            return detail_url

        try:
            soup = self._fetch(detail_url)
            if not soup:
                return None
            return self._extract_agenda_link(soup)
        except Exception:
            return None

    def _extract_agenda_link(self, container) -> Optional[str]:
        """Find the best agenda PDF link in a container."""
        for link in container.find_all("a", href=True):
            href = link["href"].strip()
            if href.lower().endswith(".pdf"):
                return urljoin(self.base_url, href)

        for link in container.find_all("a", href=True):
            href = link["href"].strip()
            if href.lower().endswith((".docx", ".doc")):
                return urljoin(self.base_url, href)

        return None

    def _extract_name_from_link(self, text: str) -> str:
        """Extract meeting name from link text by removing date patterns."""
        # Remove "Month Year" pattern (e.g., "February 2026")
        cleaned = re.sub(
            r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
            r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
            r"Nov(?:ember)?|Dec(?:ember)?)[.,]?\s+\d{1,2}[.,]?\s*\d{0,4}",
            "",
            text,
            flags=re.I,
        )
        # Remove standalone year
        cleaned = re.sub(r"\b\d{4}\b", "", cleaned)
        # Remove "Agenda" / "Minutes" suffix
        cleaned = re.sub(
            r"\s*[-–—]?\s*(?:Agenda|Minutes|Packet)s?\s*$",
            "",
            cleaned,
            flags=re.I,
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—:,")
        return cleaned if cleaned else "City Council Meeting"

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
            log.warning("Drupal: error fetching %s: %s", url, e)
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
        url="https://www.cityofgrant.us/CCMeetingAgendas",
        timezone="America/Chicago",
        schedule_type="drupal_table",
    )
