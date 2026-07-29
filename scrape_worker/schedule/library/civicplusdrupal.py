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


class Civicplusdrupal:
    """
    Self-contained scraper for CivicPlus Drupal/Municode CMS meeting pages.

    These sites use a CivicPlus-branded Drupal CMS (powered by Municode) with
    a ``/meetings`` page displaying a table of upcoming meetings. The table has
    columns: Date, Meeting, Agendas, Agenda Packets, View.

    The pages require JavaScript rendering (Cloudflare protection) so we use
    ScraperAPI's render mode.

    Recent/past meetings are at ``/meetings/recent`` with additional columns
    for Minutes and Video/Audio. Agenda links point to
    ``meetings.municode.com`` HTML or blob storage PDFs.
    """

    self_contained_parser = True

    def __init__(self):
        self.scraper = HtmlScraper()
        self.base_url: Optional[str] = None

    def civicplusdrupal_table(self, url: str, timezone: str) -> List[dict]:
        self.base_url = self._derive_base_url(url)

        # Normalise to /meetings page
        meetings_url = self._normalise_meetings_url(url)

        # Fetch upcoming meetings
        soup = self._fetch_rendered(meetings_url)
        if not soup:
            return []

        meetings = self._parse_table(soup, meetings_url, timezone)

        # Also fetch recent meetings for lookback window
        recent_url = meetings_url.rstrip("/") + "/recent"
        recent_soup = self._fetch_rendered(recent_url)
        if recent_soup:
            recent = self._parse_table(recent_soup, meetings_url, timezone)
            # Deduplicate by (name, date)
            seen = {(m["Meeting name"], m["Scheduled time"][:10]) for m in meetings}
            for m in recent:
                key = (m["Meeting name"], m["Scheduled time"][:10])
                if key not in seen:
                    meetings.append(m)
                    seen.add(key)

        return meetings

    def _parse_table(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        """Parse the meetings table."""
        meetings = []
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        table = soup.find("table")
        if not table:
            return []

        rows = table.find_all("tr")
        if len(rows) < 2:
            return []

        # Detect column indices from header
        header_cells = rows[0].find_all(["th", "td"])
        headers = [c.get_text(strip=True).lower() for c in header_cells]
        date_col = self._find_col(headers, "date")
        meeting_col = self._find_col(headers, "meeting")
        agenda_col = self._find_col(headers, "agenda")
        packet_col = self._find_col(headers, "packet")

        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            # Extract date
            date_text = cells[date_col].get_text(" ", strip=True) if date_col < len(cells) else ""
            # Strip "Date" prefix if present (CivicPlus pattern)
            date_text = re.sub(r"^Date\s+", "", date_text, flags=re.I)
            # Handle pipe separator for time: "Feb 23, 2026 | 5:30pm"
            date_text = date_text.replace("|", "")

            scheduled_time = self._to_utc_iso(date_text, timezone)
            if not scheduled_time:
                continue

            utc_dt = self._parse_iso_to_utc(scheduled_time)
            if utc_dt and utc_dt < min_allowed:
                continue

            # Extract meeting name
            meeting_name = ""
            if meeting_col < len(cells):
                meeting_name = cells[meeting_col].get_text(" ", strip=True)
                # Strip "Meeting" prefix
                meeting_name = re.sub(r"^Meeting\s+", "", meeting_name, flags=re.I)
            if not meeting_name:
                meeting_name = "City Council Meeting"

            # Extract agenda link — prefer packet, then agenda
            agenda_link = None
            for col_idx in [packet_col, agenda_col]:
                if col_idx >= 0 and col_idx < len(cells):
                    agenda_link = self._extract_best_link(cells[col_idx])
                    if agenda_link:
                        break

            # Extract meeting detail link from View column
            meeting_link = url
            view_cell = cells[-1] if cells else None
            if view_cell:
                detail_link = view_cell.find("a", href=True)
                if detail_link:
                    meeting_link = urljoin(self.base_url, detail_link["href"].strip())

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

    def _find_col(self, headers: list, keyword: str) -> int:
        """Find the column index matching a keyword. Returns -1 if not found."""
        for i, h in enumerate(headers):
            if keyword in h:
                return i
        return -1

    def _extract_best_link(self, cell: Tag) -> Optional[str]:
        """Extract the best document link from a cell (prefer blob/PDF over HTML)."""
        links = cell.find_all("a", href=True)
        if not links:
            return None

        # Prefer blob storage links (direct download)
        for link in links:
            href = link["href"].strip()
            if "blob.core" in href or href.lower().endswith(".pdf"):
                return href

        # Fallback: Municode HTML viewer
        for link in links:
            href = link["href"].strip()
            if "municode.com" in href:
                return href

        return links[0]["href"].strip() if links else None

    def _normalise_meetings_url(self, url: str) -> str:
        """Ensure the URL points to the /meetings page."""
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        if not path.endswith("/meetings"):
            # Check if the path contains meetings
            if "/meetings" not in path:
                # Append /meetings
                return f"{self.base_url}/meetings"
        return url

    def _determine_status(self, title: str, scheduled_time: str) -> str:
        if re.search(r"cancel", title, re.I):
            return "Cancelled"
        utc_dt = self._parse_iso_to_utc(scheduled_time)
        if utc_dt and utc_dt < datetime.now(pytz.UTC):
            return "Past"
        return "Upcoming"

    def _fetch_rendered(self, url: str) -> Optional[BeautifulSoup]:
        """Fetch with JS rendering (needed for Cloudflare protection)."""
        try:
            html = self.scraper.scrape_html(
                url=url, render="true", wait_for_seconds=3
            )
            if not html or (isinstance(html, dict) and "max_failure" in html):
                return None
            return self.scraper.convert_to_soup(string=html)
        except Exception as e:
            log.warning(
                "Civicplusdrupal: error fetching %s: %s", url, e
            )
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
        url="https://www.mapleplainmn.gov/meetings",
        timezone="America/Chicago",
        schedule_type="civicplusdrupal_table",
    )
