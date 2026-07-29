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

# How far back (in days) to include past meetings
LOOKBACK_DAYS = 7


class Govoffice:
    """
    Self-contained scraper for GovOffice / Catalis CMS meeting pages.

    GovOffice sites use a consistent article-based layout where each meeting
    is an <article> element containing:
      - <h2> with meeting name (linked to detail page)
      - <time> element with human-readable datetime
      - Document links (agenda PDFs, minutes DOCX, etc.)

    Identifiable by: ?SEC= URL params, .asp pages, govoffice.com subdomains,
    or "Government Websites by Catalis" footer.
    """

    self_contained_parser = True

    def __init__(self):
        self.scraper = HtmlScraper()
        self.base_url: Optional[str] = None

    def govoffice_table(self, url: str, timezone: str) -> List[dict]:
        """
        Parse meetings from a GovOffice/Catalis meeting page.

        Args:
            url: Meeting listing page URL.
            timezone: IANA timezone string for the jurisdiction.

        Returns:
            List of meeting dicts for the schedule refresh pipeline.
        """
        self.base_url = self._derive_base_url(url)
        soup = self._fetch(url)
        if not soup:
            return []

        # Try article-based layout first (most common Catalis pattern)
        meetings = self._parse_article_layout(soup, url, timezone)

        # Fallback: try table-based layout
        if not meetings:
            meetings = self._parse_table_layout(soup, url, timezone)

        # Fallback: try list-based layout (older GovOffice sites)
        if not meetings:
            meetings = self._parse_list_layout(soup, url, timezone)

        # If the page is a hub (links to year-specific pages), follow links
        if not meetings:
            meetings = self._parse_hub_page(soup, url, timezone)

        return meetings

    def _parse_article_layout(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        """Parse the Catalis article-based meeting layout."""
        articles = soup.find_all("article")
        if not articles:
            return []

        meetings = []
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        for article in articles:
            meeting = self._parse_article(article, url, timezone)
            if not meeting:
                continue

            # Filter out very old meetings
            scheduled_utc = self._parse_iso_to_utc(meeting["Scheduled time"])
            if scheduled_utc and scheduled_utc < min_allowed:
                continue

            meetings.append(meeting)

        return meetings

    def _parse_article(
        self, article: Tag, url: str, timezone: str
    ) -> Optional[dict]:
        """Parse a single <article> element into a meeting dict."""
        # Extract meeting name from h2/h3 heading
        heading = article.find(["h2", "h3", "h4"])
        if not heading:
            return None

        meeting_name = heading.get_text(strip=True)
        if not meeting_name:
            return None

        # Extract datetime from <time> element
        time_elem = article.find("time")
        time_str = None
        date_from_heading = False
        if time_elem:
            time_str = time_elem.get_text(strip=True)
        else:
            # Fallback: try to reconstruct from date display divs
            time_str = self._extract_date_from_divs(article)

        if not time_str:
            # Fallback: try to parse date from the heading text itself
            # e.g. "February 23, 2026 Working Session"
            time_str = meeting_name
            date_from_heading = True

        scheduled_time = self._to_utc_iso(time_str, timezone)
        if not scheduled_time:
            return None

        # If the date was extracted from the heading, strip it from the name
        if date_from_heading:
            meeting_name = self._strip_date_from_name(meeting_name)

        # Extract detail link from heading
        meeting_link = None
        heading_link = heading.find("a", href=True)
        if heading_link:
            meeting_link = urljoin(self.base_url, heading_link["href"].strip())

        # Extract agenda/document links
        agenda_link = self._extract_agenda_link(article)

        # Determine status
        status = self._determine_status(meeting_name, scheduled_time)

        return {
            "Meeting name": meeting_name,
            "Scheduled time": scheduled_time,
            "Meeting link": meeting_link,
            "Agenda link": agenda_link,
            "Status": status,
        }

    def _extract_date_from_divs(self, container: Tag) -> Optional[str]:
        """
        Some GovOffice pages display dates as separate month/day/year divs
        without a <time> element. Try to reconstruct.
        """
        text_parts = []
        for div in container.find_all(["div", "span"], recursive=True):
            text = div.get_text(strip=True)
            if text and len(text) <= 10:
                text_parts.append(text)

        if len(text_parts) >= 3:
            combined = " ".join(text_parts[:4])
            try:
                result = dateparser.parse(combined, fuzzy=True)
                if result:
                    return combined
            except (ValueError, TypeError):
                pass
        return None

    def _parse_table_layout(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        """Parse table-based meeting layouts (older GovOffice sites)."""
        tables = soup.find_all("table")
        meetings = []
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        for table in tables:
            rows = table.find_all("tr")
            for row in rows:
                cells = row.find_all(["td", "th"])
                if len(cells) < 2:
                    continue

                # Try to find date and meeting name in cells
                row_text = row.get_text(" ", strip=True)
                meeting = self._parse_text_row(row_text, row, url, timezone)
                if meeting:
                    scheduled_utc = self._parse_iso_to_utc(
                        meeting["Scheduled time"]
                    )
                    if scheduled_utc and scheduled_utc < min_allowed:
                        continue
                    meetings.append(meeting)

        return meetings

    def _parse_list_layout(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        """Parse list-based meeting layouts."""
        # Look for content area with meeting listings
        content = soup.find(
            "div", class_=re.compile(r"content|main|body", re.I)
        ) or soup

        meetings = []
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        # Find links that look like meeting entries (contain dates)
        date_pattern = re.compile(
            r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
            r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
            r"Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}|"
            r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}",
            re.I,
        )

        for li in content.find_all(["li", "p", "div"]):
            text = li.get_text(" ", strip=True)
            if not date_pattern.search(text):
                continue

            meeting = self._parse_text_row(text, li, url, timezone)
            if meeting:
                scheduled_utc = self._parse_iso_to_utc(
                    meeting["Scheduled time"]
                )
                if scheduled_utc and scheduled_utc < min_allowed:
                    continue
                meetings.append(meeting)

        return meetings

    def _parse_hub_page(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        """
        Handle hub/index pages that link to year-specific meeting pages.
        Follow the current year link to get actual meetings.
        """
        current_year = str(datetime.now().year)
        year_link = None

        for link in soup.find_all("a", href=True):
            link_text = link.get_text(strip=True)
            if current_year in link_text:
                year_link = urljoin(self.base_url, link["href"].strip())
                break

        if not year_link:
            return []

        log.debug("Govoffice: following year link %s", year_link)
        year_soup = self._fetch(year_link)
        if not year_soup:
            return []

        # Parse the year-specific page
        meetings = self._parse_article_layout(year_soup, year_link, timezone)
        if not meetings:
            meetings = self._parse_table_layout(year_soup, year_link, timezone)
        if not meetings:
            meetings = self._parse_list_layout(year_soup, year_link, timezone)

        return meetings

    def _parse_text_row(
        self, text: str, element: Tag, url: str, timezone: str
    ) -> Optional[dict]:
        """
        Parse a text string that might contain a meeting date and name.
        Used as fallback for table/list layouts.
        """
        # Try to extract a date
        try:
            dt = dateparser.parse(text, fuzzy=True)
        except (ValueError, TypeError):
            return None

        if not dt or dt.year < 2020:
            return None

        scheduled_time = self._to_utc_iso(text, timezone)
        if not scheduled_time:
            return None

        # Extract meeting name — remove the date portion
        meeting_name = self._extract_meeting_name(text, element)

        # Extract links
        agenda_link = self._extract_agenda_link(element)
        meeting_link = None
        first_link = element.find("a", href=True)
        if first_link:
            meeting_link = urljoin(self.base_url, first_link["href"].strip())

        status = self._determine_status(meeting_name, scheduled_time)

        return {
            "Meeting name": meeting_name,
            "Scheduled time": scheduled_time,
            "Meeting link": meeting_link,
            "Agenda link": agenda_link,
            "Status": status,
        }

    def _strip_date_from_name(self, text: str) -> str:
        """Strip date/time patterns from a meeting name string."""
        cleaned = self._remove_date_patterns(text)
        return cleaned if cleaned else "City Council Meeting"

    def _extract_meeting_name(self, text: str, element: Tag) -> str:
        """Extract a clean meeting name from text or element context."""
        # First check if there's a heading nearby
        heading = element.find(["h2", "h3", "h4", "h5", "strong", "b"])
        if heading:
            name = heading.get_text(strip=True)
            if name and len(name) > 3:
                return name

        return self._remove_date_patterns(text)

    def _remove_date_patterns(self, text: str) -> str:
        """Remove date/time patterns from a string, returning the remainder."""
        cleaned = re.sub(
            r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
            r",?\s*",
            "",
            text,
            flags=re.I,
        )
        # Handle "Month, Day, Year" and "Month Day, Year" variants
        cleaned = re.sub(
            r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
            r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
            r"Nov(?:ember)?|Dec(?:ember)?),?\s+\d{1,2},?\s+\d{2,4}",
            "",
            cleaned,
            flags=re.I,
        )
        cleaned = re.sub(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}", "", cleaned)
        cleaned = re.sub(
            r"at\s+\d{1,2}:\d{2}\s*(?:AM|PM)?", "", cleaned, flags=re.I
        )
        cleaned = re.sub(r"\d{1,2}:\d{2}\s*(?:AM|PM)", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        cleaned = cleaned.strip(" -–—:,")

        return cleaned if cleaned else "City Council Meeting"

    def _extract_agenda_link(self, container: Tag) -> Optional[str]:
        """Extract the best agenda/document link from a container."""
        doc_extensions = (".pdf", ".docx", ".doc")

        for link in container.find_all("a", href=True):
            href = link["href"].strip()
            href_lower = href.lower()

            # Prefer PDF links
            if href_lower.endswith(".pdf"):
                return urljoin(self.base_url, href)

        # Fallback to any document link
        for link in container.find_all("a", href=True):
            href = link["href"].strip()
            if any(href.lower().endswith(ext) for ext in doc_extensions):
                return urljoin(self.base_url, href)

        return None

    def _determine_status(self, title: str, scheduled_time: str) -> str:
        """Determine meeting status based on title and time."""
        if re.search(r"cancel", title, re.I):
            return "Cancelled"

        utc_dt = self._parse_iso_to_utc(scheduled_time)
        if utc_dt:
            now = datetime.now(pytz.UTC)
            if utc_dt < now:
                return "Past"

        return "Upcoming"

    def _fetch(self, url: str) -> Optional[BeautifulSoup]:
        """Fetch a URL and return BeautifulSoup object."""
        try:
            html = self.scraper.scrape_html(url=url)
            if not html or (isinstance(html, dict) and "max_failure" in html):
                log.warning("Govoffice: failed to fetch %s", url)
                return None
            return self.scraper.convert_to_soup(string=html)
        except Exception as e:
            log.warning("Govoffice: error fetching %s: %s", url, e)
            return None

    def _derive_base_url(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _to_utc_iso(self, time_str: str, timezone: str) -> Optional[str]:
        """Parse a datetime string and convert to UTC ISO format."""
        try:
            # Use noon as default time to avoid current-time leaking into
            # date-only strings (Nebraska schedule drift pattern)
            default_dt = datetime.now().replace(
                hour=12, minute=0, second=0, microsecond=0, tzinfo=None
            )
            dt = dateparser.parse(
                time_str,
                fuzzy=True,
                default=default_dt,
            )
            if not dt:
                return None
            if dt.year < 2020 or dt.year > datetime.now().year + 2:
                return None

            if dt.tzinfo is None:
                local_tz = pytz.timezone(timezone)
                dt = local_tz.localize(dt)

            utc = dt.astimezone(pytz.UTC)
            return utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except (ValueError, TypeError) as e:
            log.debug("Govoffice: failed to parse time '%s': %s", time_str, e)
            return None

    def _parse_iso_to_utc(self, iso_str: str) -> Optional[datetime]:
        """Parse an ISO format string back to a UTC datetime."""
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
        url="https://tracymn.gov/meetings",
        timezone="America/Chicago",
        schedule_type="govoffice_table",
    )
