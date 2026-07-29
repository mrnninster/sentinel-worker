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


class Sparq:
    """
    Scraper for Sparq Data meeting pages (meeting.sparqdata.com).

    Sparq meeting pages are server-rendered with meetings listed under
    <h4> headings containing the date and meeting type. Each meeting
    has agenda/minutes links with patterns:
      /Public/Agenda/{org_id}?meeting={meeting_id}
      /Public/Minutes/{org_id}?meeting={meeting_id}
    """

    self_contained_parser = True

    def __init__(self):
        self.scraper = HtmlScraper()
        self.base_url: Optional[str] = None

    def sparq_table(self, url: str, timezone: str) -> List[dict]:
        """
        Parse meetings from a Sparq Data meeting page.

        Args:
            url: Sparq meeting listing URL
                 (e.g., https://meeting.sparqdata.com/public/Organization/167)
            timezone: IANA timezone string.

        Returns:
            List of meeting dicts for the schedule refresh pipeline.
        """
        self.base_url = self._derive_base_url(url)
        soup = self._fetch(url)
        if not soup:
            return []

        meetings = []
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        # Sparq pages list meetings under <h4> headings
        headings = soup.find_all("h4")
        for heading in headings:
            meeting = self._parse_meeting_block(heading, timezone)
            if not meeting:
                continue

            scheduled_utc = self._parse_iso_to_utc(meeting["Scheduled time"])
            if scheduled_utc and scheduled_utc < min_allowed:
                continue

            meetings.append(meeting)

        # Fallback: try parsing from any structured container
        if not meetings:
            meetings = self._parse_list_fallback(soup, timezone)

        return meetings

    def _parse_meeting_block(self, heading: Tag, timezone: str) -> Optional[dict]:
        """
        Parse a meeting from an <h4> heading and its following siblings.

        Sparq format:
          <h4>February 10, 2026 - Regular Meeting</h4>
          <p><strong>Meeting Type:</strong> Regular</p>
          <p>Location text <a>map it</a></p>
          <ul>
            <li><a href="/Public/Agenda/167?meeting=731588">Agenda</a></li>
            <li><a href="/Public/Minutes/167?meeting=731588">Minutes</a></li>
          </ul>
        """
        text = heading.get_text(strip=True)
        if not text:
            return None

        # Parse date from heading text (e.g., "February 10, 2026 - Regular Meeting")
        scheduled_time = self._to_utc_iso(text, timezone)
        if not scheduled_time:
            return None

        # Extract meeting name — part after the date separator
        meeting_name = self._extract_name(text)

        # Collect sibling elements until the next heading
        siblings = []
        for sib in heading.next_siblings:
            if isinstance(sib, Tag):
                if sib.name and sib.name.startswith("h"):
                    break
                siblings.append(sib)

        # Extract agenda and meeting links from siblings
        agenda_link = None
        meeting_link = None
        for sib in siblings:
            for link in sib.find_all("a", href=True):
                href = link["href"].strip()
                link_text = link.get_text(strip=True).lower()

                if "agenda" in link_text or "/Agenda/" in href:
                    agenda_link = urljoin(self.base_url, href)
                elif "minutes" in link_text or "/Minutes/" in href:
                    if not meeting_link:
                        meeting_link = urljoin(self.base_url, href)

        status = self._determine_status(meeting_name, scheduled_time)

        return {
            "Meeting name": meeting_name,
            "Scheduled time": scheduled_time,
            "Meeting link": meeting_link or agenda_link,
            "Agenda link": agenda_link,
            "Status": status,
        }

    def _parse_list_fallback(
        self, soup: BeautifulSoup, timezone: str
    ) -> List[dict]:
        """Fallback parser for non-standard Sparq layouts."""
        meetings = []
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        # Look for any links to /Public/Agenda/ or /Public/Minutes/
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if "/Public/Agenda/" not in href and "/Public/Minutes/" not in href:
                continue

            # Walk up to find a text block with a date
            parent = link.find_parent(["li", "p", "div", "tr"])
            if not parent:
                continue

            text = parent.get_text(" ", strip=True)
            scheduled_time = self._to_utc_iso(text, timezone)
            if not scheduled_time:
                continue

            scheduled_utc = self._parse_iso_to_utc(scheduled_time)
            if scheduled_utc and scheduled_utc < min_allowed:
                continue

            meeting_name = self._extract_name(text)
            agenda_link = urljoin(self.base_url, href)
            status = self._determine_status(meeting_name, scheduled_time)

            meetings.append({
                "Meeting name": meeting_name,
                "Scheduled time": scheduled_time,
                "Meeting link": agenda_link,
                "Agenda link": agenda_link,
                "Status": status,
            })

        return meetings

    def _extract_name(self, text: str) -> str:
        """Extract meeting name from heading text like 'February 10, 2026 - Regular Meeting'."""
        # Try splitting on common separators
        for sep in [" - ", " – ", " — ", " | "]:
            if sep in text:
                parts = text.split(sep, 1)
                if len(parts) == 2 and len(parts[1].strip()) > 2:
                    return parts[1].strip()

        # Strip date from beginning
        cleaned = re.sub(
            r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
            r",?\s*",
            "",
            text,
            flags=re.I,
        )
        cleaned = re.sub(
            r"^(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
            r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
            r"Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{2,4}",
            "",
            cleaned,
            flags=re.I,
        )
        cleaned = re.sub(r"^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—:,")

        return cleaned if cleaned else "Board Meeting"

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
                log.warning("Sparq: failed to fetch %s", url)
                return None
            return self.scraper.convert_to_soup(string=html)
        except Exception as e:
            log.warning("Sparq: error fetching %s: %s", url, e)
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
            if not dt or dt.year < 2020 or dt.year > datetime.now().year + 2:
                return None
            if dt.tzinfo is None:
                local_tz = pytz.timezone(timezone)
                dt = local_tz.localize(dt)
            utc = dt.astimezone(pytz.UTC)
            return utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except (ValueError, TypeError) as e:
            log.debug("Sparq: failed to parse time '%s': %s", time_str, e)
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
        url="https://meeting.sparqdata.com/public/Organization/167",
        timezone="America/Chicago",
        schedule_type="sparq_table",
    )
