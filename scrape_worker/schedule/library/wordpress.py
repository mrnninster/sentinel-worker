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

# Date pattern for matching month-name dates in text
MONTH_NAME_DATE_RE = re.compile(
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)"
    r"[.,]?\s+\d{1,2}[.,]?\s+\d{4}",
    re.I,
)

# Numeric date patterns: M/D/YYYY, M-D-YYYY
NUMERIC_DATE_RE = re.compile(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}")

# Combined: matches any date-like text
ANY_DATE_RE = re.compile(
    r"(?:"
    + MONTH_NAME_DATE_RE.pattern
    + r"|"
    + NUMERIC_DATE_RE.pattern
    + r")",
    re.I,
)

# Pattern to extract dates embedded in URLs like /event/name-MM-DD-YYYY/
URL_DATE_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})/?$")

# YYYYMMDD dates in filenames like 20260217_City_Council_Meeting_Agenda.pdf
FILENAME_DATE_RE = re.compile(r"(20\d{2})(\d{2})(\d{2})")

# Words that indicate an agenda document
AGENDA_KEYWORDS = re.compile(r"agenda|packet", re.I)

# Words that indicate minutes (not agenda)
MINUTES_KEYWORDS = re.compile(r"minute|summary|recap", re.I)

# Words indicating cancellation
CANCEL_RE = re.compile(r"cancel", re.I)


class Wordpress:
    """
    Self-contained scraper for WordPress-based government meeting pages.

    WordPress sites use highly diverse themes (Avada, Elementor, CivicSprout,
    GeneratePress, etc.) so this parser employs multiple extraction strategies
    tried in priority order:

    1. **Event listings** -- Calendar/events plugins with structured date
       elements (month/day divs, headings with dates, event URLs with
       embedded dates).
    2. **Link-based extraction** -- Paragraphs or list items containing links
       whose text includes a date (e.g. "February 9, 2026 Agenda").
    3. **Article layout** -- <article> elements with headings and <time>
       elements.
    4. **Table layout** -- HTML tables with date/name columns.
    5. **Generic text scan** -- Any element (<li>, <p>, <div>) containing
       recognizable date text.
    6. **Hub/year page** -- If the page is an index with year links, follow
       the current-year link and re-parse.
    """

    self_contained_parser = True

    def __init__(self):
        self.scraper = HtmlScraper()
        self.base_url: Optional[str] = None

    def wordpress_table(self, url: str, timezone: str) -> List[dict]:
        """
        Parse meetings from a WordPress meeting page.

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

        # Strategy 1: WordPress event listings (calendar plugins)
        meetings = self._parse_event_listings(soup, url, timezone)

        # Strategy 2: Link-based extraction (dates in link text)
        if not meetings:
            meetings = self._parse_dated_links(soup, url, timezone)

        # Strategy 3: Article-based layout
        if not meetings:
            meetings = self._parse_article_layout(soup, url, timezone)

        # Strategy 4: Table-based layout
        if not meetings:
            meetings = self._parse_table_layout(soup, url, timezone)

        # Strategy 5: Generic text scan
        if not meetings:
            meetings = self._parse_text_scan(soup, url, timezone)

        # Strategy 6: Hub page with year links
        if not meetings:
            meetings = self._parse_hub_page(soup, url, timezone)

        return meetings

    # ------------------------------------------------------------------
    # Strategy 1: Event listings (calendar plugins)
    # ------------------------------------------------------------------

    def _parse_event_listings(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        """
        Parse WordPress event/calendar plugin listings.

        These typically have structured containers with:
        - Month/day display divs
        - Event title headings with links
        - Time text (e.g. "8:30 am")
        - URLs containing embedded dates like /event/name-MM-DD-YYYY/
        """
        meetings = []
        seen_links = set()
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        # Look for sections with "Upcoming Events" or similar headings
        event_containers = self._find_event_containers(soup)
        if not event_containers:
            return []

        for container in event_containers:
            # Each event typically has a heading with link and a date display
            headings = container.find_all(["h3", "h4", "h5"], recursive=True)
            for heading in headings:
                link_elem = heading.find("a", href=True)
                if not link_elem:
                    continue

                meeting_name = heading.get_text(strip=True)
                if not meeting_name:
                    continue

                href = link_elem["href"].strip()
                meeting_link = urljoin(self.base_url, href)

                # Deduplicate by link (same link from multiple containers)
                if meeting_link in seen_links:
                    continue
                seen_links.add(meeting_link)

                # Try to get date from URL pattern (e.g. /event/name-02-24-2026/)
                scheduled_time = self._extract_date_from_url(href, timezone)

                # Fallback: find date from sibling elements within the
                # heading's nearest wrapper (not the whole container)
                if not scheduled_time:
                    wrapper = heading.find_parent(
                        ["div", "li", "article"]
                    )
                    if wrapper and wrapper != container:
                        scheduled_time = self._extract_date_from_event_container(
                            wrapper, heading, timezone
                        )

                if not scheduled_time:
                    continue

                # Filter old meetings
                scheduled_utc = self._parse_iso_to_utc(scheduled_time)
                if scheduled_utc and scheduled_utc < min_allowed:
                    continue

                status = self._determine_status(meeting_name, scheduled_time)

                meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": scheduled_time,
                        "Meeting link": meeting_link,
                        "Agenda link": None,
                        "Status": status,
                    }
                )

        return meetings

    def _find_event_containers(self, soup: BeautifulSoup) -> List[Tag]:
        """
        Find containers that hold event listings.
        Look for sections/divs near "Upcoming Events" headings or with
        event-related class names.

        Walk up the DOM from the heading until we find an ancestor that
        actually contains event sub-headings (h3/h4/h5 with links).
        """
        containers = []

        # Look for headings containing "event" or "meeting"
        for heading in soup.find_all(["h2", "h3", "h4"]):
            text = heading.get_text(strip=True).lower()
            if any(
                kw in text
                for kw in ("upcoming event", "recent meeting", "upcoming meeting")
            ):
                # Walk up the DOM to find a parent containing event headings
                for ancestor in heading.parents:
                    if ancestor.name not in ("div", "section", "article"):
                        continue
                    # Check if this ancestor contains h3/h4/h5 with links
                    event_headings = ancestor.find_all(
                        ["h3", "h4", "h5"], recursive=True
                    )
                    linked_headings = [
                        h
                        for h in event_headings
                        if h.find("a", href=True)
                        and h.get_text(strip=True).lower() != text
                    ]
                    if linked_headings:
                        containers.append(ancestor)
                        break

        # Also look for divs with event-related classes
        for div in soup.find_all(
            "div",
            class_=re.compile(
                r"event-list|event_list|events-list|tribe-events|"
                r"upcoming-events|event-card|wp-block-event",
                re.I,
            ),
        ):
            # Only add if it actually contains links
            if div.find("a", href=True) and div not in containers:
                containers.append(div)

        return containers

    def _extract_date_from_url(
        self, href: str, timezone: str
    ) -> Optional[str]:
        """
        Extract a date from a URL like /event/commissioners-meeting-02-24-2026/
        """
        match = URL_DATE_RE.search(href)
        if not match:
            return None

        month, day, year = match.group(1), match.group(2), match.group(3)
        date_str = f"{month}/{day}/{year}"
        return self._to_utc_iso(date_str, timezone)

    def _extract_date_from_event_container(
        self, container: Tag, heading: Tag, timezone: str
    ) -> Optional[str]:
        """
        Extract date from structured event container elements.

        Looks for:
        - Sibling headings with dates (e.g. <h4>February 24</h4>)
        - Month/day display divs
        - Paragraph text with times
        """
        # Check sibling headings for date text
        parent = heading.find_parent(["div", "li", "article"])
        if not parent:
            return None

        # Look for date in any heading sibling
        for sibling_heading in parent.find_all(["h4", "h5", "h6"]):
            if sibling_heading == heading:
                continue
            date_text = sibling_heading.get_text(strip=True)
            if date_text:
                scheduled = self._to_utc_iso(date_text, timezone)
                if scheduled:
                    return scheduled

        # Look for month/day display divs
        month_div = parent.find(
            ["div", "span"],
            string=re.compile(
                r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)$", re.I
            ),
        )
        if month_div:
            day_div = month_div.find_next_sibling(["div", "span"])
            if day_div:
                month_text = month_div.get_text(strip=True)
                day_text = day_div.get_text(strip=True).strip('"')
                # Look for a time in a paragraph
                time_text = ""
                time_p = parent.find("p")
                if time_p:
                    time_text = time_p.get_text(strip=True)

                date_str = f"{month_text} {day_text} {time_text}"
                scheduled = self._to_utc_iso(date_str, timezone)
                if scheduled:
                    return scheduled

        # Try the full container text
        full_text = parent.get_text(" ", strip=True)
        if ANY_DATE_RE.search(full_text):
            return self._to_utc_iso(full_text, timezone)

        return None

    # ------------------------------------------------------------------
    # Strategy 2: Link-based extraction (dates in link text)
    # ------------------------------------------------------------------

    def _parse_dated_links(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        """
        Parse meetings from links whose text contains dates.

        Common pattern on WordPress sites: paragraphs or list items containing
        links like "February 9, 2026 Agenda" pointing to PDF files.
        Links are grouped under year headings (e.g. <h1>2026 Agendas</h1>).
        """
        # Find the main content area
        content = self._find_content_area(soup)

        meetings = []
        seen_dates = set()
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        for link in content.find_all("a", href=True):
            link_text = link.get_text(strip=True)
            if not link_text:
                continue

            href = link["href"].strip()

            # Check for date in link text or YYYYMMDD in filename/href
            has_text_date = ANY_DATE_RE.search(link_text)
            filename_date_match = FILENAME_DATE_RE.search(href) if not has_text_date else None

            if not has_text_date and not filename_date_match:
                continue

            # Skip minutes -- we want agenda links
            if MINUTES_KEYWORDS.search(link_text) and not AGENDA_KEYWORDS.search(
                link_text
            ):
                continue

            # Extract the date
            if has_text_date:
                scheduled_time = self._to_utc_iso(link_text, timezone)
            else:
                y, m, d = filename_date_match.group(1), filename_date_match.group(2), filename_date_match.group(3)
                scheduled_time = self._to_utc_iso(f"{m}/{d}/{y}", timezone)
            if not scheduled_time:
                continue

            # Filter old meetings
            scheduled_utc = self._parse_iso_to_utc(scheduled_time)
            if scheduled_utc and scheduled_utc < min_allowed:
                continue

            # Deduplicate by date (same date = same meeting)
            date_key = scheduled_time[:10]
            if date_key in seen_dates:
                # If we already have this date, update the existing meeting
                # with an agenda link if this one is better
                for m in meetings:
                    if m["Scheduled time"][:10] == date_key and not m["Agenda link"]:
                        abs_href = urljoin(self.base_url, href)
                        if self._is_document_link(abs_href):
                            m["Agenda link"] = abs_href
                continue
            seen_dates.add(date_key)

            # Determine meeting name from context
            if filename_date_match:
                meeting_name = self._extract_name_from_filename(link_text)
            else:
                meeting_name = self._extract_name_from_link_context(link, link_text)

            # Determine agenda link
            agenda_link = None
            abs_href = urljoin(self.base_url, href)
            if self._is_document_link(abs_href):
                agenda_link = abs_href

            status = self._determine_status(link_text, scheduled_time)

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

    def _extract_name_from_link_context(self, link: Tag, link_text: str) -> str:
        """
        Derive meeting name from the context around a dated link.

        Checks:
        1. The page's <h1> heading for a body/committee name
        2. Parent section headings
        3. The link text itself minus the date
        """
        # Try the page heading
        page = link.find_parent(["html", "[document]"])
        if page:
            h1 = page.find("h1")
            if h1:
                h1_text = h1.get_text(strip=True)
                # Filter out year headings like "2026 Agendas"
                if h1_text and not re.match(r"^\d{4}\s", h1_text):
                    # Use page heading as base name
                    return self._clean_meeting_name(h1_text)

        # Try parent section heading
        parent = link.find_parent(["div", "section"])
        if parent:
            for h in parent.find_all(["h2", "h3", "h4"], limit=3):
                h_text = h.get_text(strip=True)
                if h_text and not re.match(r"^\d{4}\s", h_text):
                    return self._clean_meeting_name(h_text)

        # Fallback: strip date from the link text itself
        name = self._remove_date_patterns(link_text)
        # Remove "Agenda" / "Packet" suffix
        name = re.sub(r"\s*(Agenda|Packet|Minutes)\s*$", "", name, flags=re.I).strip()
        return name if name else "City Council Meeting"

    # ------------------------------------------------------------------
    # Strategy 3: Article-based layout
    # ------------------------------------------------------------------

    def _parse_article_layout(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        """Parse WordPress article/post-based meeting layouts."""
        articles = soup.find_all("article")
        if not articles:
            return []

        meetings = []
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        for article in articles:
            meeting = self._parse_article(article, url, timezone)
            if not meeting:
                continue

            scheduled_utc = self._parse_iso_to_utc(meeting["Scheduled time"])
            if scheduled_utc and scheduled_utc < min_allowed:
                continue

            meetings.append(meeting)

        return meetings

    def _parse_article(
        self, article: Tag, url: str, timezone: str
    ) -> Optional[dict]:
        """Parse a single <article> element into a meeting dict."""
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
            # Prefer datetime attribute if available
            time_str = time_elem.get("datetime") or time_elem.get_text(strip=True)
        else:
            # Try to parse date from heading text
            if ANY_DATE_RE.search(meeting_name):
                time_str = meeting_name
                date_from_heading = True

        if not time_str:
            return None

        scheduled_time = self._to_utc_iso(time_str, timezone)
        if not scheduled_time:
            return None

        if date_from_heading:
            meeting_name = self._strip_date_from_name(meeting_name)

        # Extract detail link
        meeting_link = None
        heading_link = heading.find("a", href=True)
        if heading_link:
            meeting_link = urljoin(self.base_url, heading_link["href"].strip())

        agenda_link = self._extract_agenda_link(article)
        status = self._determine_status(meeting_name, scheduled_time)

        return {
            "Meeting name": meeting_name,
            "Scheduled time": scheduled_time,
            "Meeting link": meeting_link or url,
            "Agenda link": agenda_link,
            "Status": status,
        }

    # ------------------------------------------------------------------
    # Strategy 4: Table-based layout
    # ------------------------------------------------------------------

    def _parse_table_layout(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        """Parse table-based meeting layouts."""
        content = self._find_content_area(soup)
        tables = content.find_all("table")
        meetings = []
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        for table in tables:
            rows = table.find_all("tr")
            for row in rows:
                cells = row.find_all(["td", "th"])
                if len(cells) < 2:
                    continue

                row_text = row.get_text(" ", strip=True)
                if not ANY_DATE_RE.search(row_text):
                    continue

                meeting = self._parse_text_element(row_text, row, url, timezone)
                if meeting:
                    scheduled_utc = self._parse_iso_to_utc(
                        meeting["Scheduled time"]
                    )
                    if scheduled_utc and scheduled_utc < min_allowed:
                        continue
                    meetings.append(meeting)

        return meetings

    # ------------------------------------------------------------------
    # Strategy 5: Generic text scan
    # ------------------------------------------------------------------

    def _parse_text_scan(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        """
        Scan all list items, paragraphs, and divs for date text.
        Last-resort strategy for unusual layouts.
        """
        content = self._find_content_area(soup)
        meetings = []
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)
        seen_dates = set()

        for elem in content.find_all(["li", "p", "div"], recursive=True):
            # Skip deeply nested elements to avoid duplicates
            if elem.find(["li", "p"]):
                continue

            text = elem.get_text(" ", strip=True)
            if not text or len(text) < 8 or len(text) > 500:
                continue

            if not ANY_DATE_RE.search(text):
                continue

            meeting = self._parse_text_element(text, elem, url, timezone)
            if not meeting:
                continue

            date_key = meeting["Scheduled time"][:10]
            if date_key in seen_dates:
                continue
            seen_dates.add(date_key)

            scheduled_utc = self._parse_iso_to_utc(meeting["Scheduled time"])
            if scheduled_utc and scheduled_utc < min_allowed:
                continue

            meetings.append(meeting)

        return meetings

    # ------------------------------------------------------------------
    # Strategy 6: Hub page with year links
    # ------------------------------------------------------------------

    def _parse_hub_page(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        """
        Handle hub/index pages that link to year-specific sub-pages.
        Follow the current year link and re-parse.
        """
        current_year = str(datetime.now().year)
        year_link = None

        for link in soup.find_all("a", href=True):
            link_text = link.get_text(strip=True)
            href = link["href"].strip()

            # Match links whose text is just the year or contain the year
            if link_text == current_year or (
                current_year in link_text
                and len(link_text) < 20
                and current_year in href
            ):
                year_link = urljoin(self.base_url, href)
                break

        if not year_link or year_link == url:
            return []

        log.debug("wordpress: following year link %s", year_link)
        year_soup = self._fetch(year_link)
        if not year_soup:
            return []

        # Re-run strategies 1-5 on the year page
        meetings = self._parse_event_listings(year_soup, year_link, timezone)
        if not meetings:
            meetings = self._parse_dated_links(year_soup, year_link, timezone)
        if not meetings:
            meetings = self._parse_article_layout(year_soup, year_link, timezone)
        if not meetings:
            meetings = self._parse_table_layout(year_soup, year_link, timezone)
        if not meetings:
            meetings = self._parse_text_scan(year_soup, year_link, timezone)

        return meetings

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _find_content_area(self, soup: BeautifulSoup) -> Tag:
        """Find the main content area, falling back to whole document."""
        # Try <main> tag first
        main = soup.find("main")
        if main:
            return main

        # Try common WordPress content classes
        for selector in [
            {"id": re.compile(r"content|main", re.I)},
            {"class_": re.compile(r"entry-content|page-content|post-content", re.I)},
            {"class_": re.compile(r"content-area|site-content|main-content", re.I)},
            {"role": "main"},
        ]:
            found = soup.find("div", **selector)
            if found:
                return found

        return soup

    def _parse_text_element(
        self, text: str, element: Tag, url: str, timezone: str
    ) -> Optional[dict]:
        """
        Parse a text string that might contain a meeting date and name.
        """
        scheduled_time = self._to_utc_iso(text, timezone)
        if not scheduled_time:
            return None

        meeting_name = self._extract_meeting_name(text, element)
        agenda_link = self._extract_agenda_link(element)

        meeting_link = None
        first_link = element.find("a", href=True)
        if first_link:
            meeting_link = urljoin(self.base_url, first_link["href"].strip())

        status = self._determine_status(meeting_name, scheduled_time)

        return {
            "Meeting name": meeting_name,
            "Scheduled time": scheduled_time,
            "Meeting link": meeting_link or url,
            "Agenda link": agenda_link,
            "Status": status,
        }

    def _extract_meeting_name(self, text: str, element: Tag) -> str:
        """Extract a clean meeting name from text or element context."""
        # Check for a heading in or near the element
        heading = element.find(["h2", "h3", "h4", "h5", "strong", "b"])
        if heading:
            name = heading.get_text(strip=True)
            if name and len(name) > 3:
                cleaned = self._remove_date_patterns(name)
                if cleaned and len(cleaned) > 2:
                    return cleaned

        # Try page-level heading
        page_heading = self._get_page_heading(element)
        if page_heading:
            return page_heading

        return self._remove_date_patterns(text)

    def _get_page_heading(self, element: Tag) -> Optional[str]:
        """Get the page's main heading for use as meeting name."""
        root = element.find_parent(["html", "[document]"])
        if not root:
            return None

        h1 = root.find("h1")
        if h1:
            text = h1.get_text(strip=True)
            if text and not re.match(r"^\d{4}\s", text):
                return self._clean_meeting_name(text)
        return None

    def _clean_meeting_name(self, name: str) -> str:
        """Clean up a meeting name string."""
        # Remove common suffixes
        name = re.sub(
            r"\s*[-–—]\s*(?:Agenda|Minutes|Packet)s?\s*$", "", name, flags=re.I
        )
        name = re.sub(r"\s*(?:Agendas?\s*&\s*Minutes?)\s*$", "", name, flags=re.I)
        name = name.strip(" -–—:,")
        return name if name else "City Council Meeting"

    def _strip_date_from_name(self, text: str) -> str:
        """Strip date/time patterns from a meeting name string."""
        cleaned = self._remove_date_patterns(text)
        return cleaned if cleaned else "City Council Meeting"

    def _extract_name_from_filename(self, text: str) -> str:
        """Extract meeting name from a filename like 20260217_City_Council_Meeting_Agenda.pdf."""
        # Remove extension
        cleaned = re.sub(r"\.\w{2,4}$", "", text)
        # Remove YYYYMMDD prefix
        cleaned = FILENAME_DATE_RE.sub("", cleaned)
        # Replace underscores with spaces
        cleaned = cleaned.replace("_", " ")
        # Remove Agenda/Packet suffix
        cleaned = re.sub(
            r"\s*(?:Agenda\s*(?:and\s*)?Packet|Agenda|Packet)\s*$",
            "",
            cleaned,
            flags=re.I,
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—:,")
        return cleaned if cleaned else "City Council Meeting"

    def _remove_date_patterns(self, text: str) -> str:
        """Remove date/time patterns from a string, returning the remainder."""
        cleaned = re.sub(
            r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
            r",?\s*",
            "",
            text,
            flags=re.I,
        )
        # Handle "Month Day, Year" and variants
        cleaned = re.sub(
            r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
            r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
            r"Nov(?:ember)?|Dec(?:ember)?)[.,]?\s+\d{1,2}[.,]?\s+\d{2,4}",
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

        # First pass: look for links with "agenda" in text pointing to docs
        for link in container.find_all("a", href=True):
            href = link["href"].strip()
            link_text = link.get_text(strip=True).lower()
            if AGENDA_KEYWORDS.search(link_text) and self._is_document_link(href):
                return urljoin(self.base_url, href)

        # Second pass: any PDF link (prefer first one)
        for link in container.find_all("a", href=True):
            href = link["href"].strip()
            if href.lower().endswith(".pdf"):
                return urljoin(self.base_url, href)

        # Third pass: any document link
        for link in container.find_all("a", href=True):
            href = link["href"].strip()
            if any(href.lower().endswith(ext) for ext in doc_extensions):
                return urljoin(self.base_url, href)

        return None

    def _is_document_link(self, href: str) -> bool:
        """Check if a URL points to a document file."""
        lower = href.lower()
        return any(
            lower.endswith(ext) for ext in (".pdf", ".docx", ".doc", ".xlsx", ".xls")
        )

    def _determine_status(self, title: str, scheduled_time: str) -> str:
        """Determine meeting status based on title and time."""
        if CANCEL_RE.search(title):
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
                log.warning("wordpress: failed to fetch %s", url)
                return None
            return self.scraper.convert_to_soup(string=html)
        except Exception as e:
            log.warning("wordpress: error fetching %s: %s", url, e)
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

            # Reject dates that are clearly not meetings (too old or too far out)
            if dt.year < 2020 or dt.year > datetime.now().year + 2:
                return None

            if dt.tzinfo is None:
                local_tz = pytz.timezone(timezone)
                dt = local_tz.localize(dt)

            utc = dt.astimezone(pytz.UTC)
            return utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except (ValueError, TypeError, OverflowError) as e:
            log.debug(
                "wordpress: failed to parse time '%s': %s", time_str, e
            )
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
        url="https://www.caledoniamn.gov/city-government/city-council/agendas-and-minutes/",
        timezone="America/Chicago",
        schedule_type="wordpress_table",
    )
