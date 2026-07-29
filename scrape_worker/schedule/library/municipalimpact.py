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


class Municipalimpact:
    """
    Self-contained scraper for Municipal Impact CMS meeting pages.

    Municipal Impact sites list agendas on a dedicated page (e.g. /agendas)
    with links whose text contains dates and meeting types:
      "February 11, 2026 Work Session" -> /documents/643/02-11-2026_Agenda.pdf

    Document files are hosted at /documents/{org_id}/ or on
    clients.municipalimpact.com/documents/.
    """

    self_contained_parser = True

    def __init__(self):
        self.scraper = HtmlScraper()
        self.base_url: Optional[str] = None

    def municipalimpact_table(self, url: str, timezone: str) -> List[dict]:
        self.base_url = self._derive_base_url(url)
        soup = self._fetch(url)
        if not soup:
            return []

        meetings = self._parse_dated_links(soup, url, timezone)
        return meetings

    def _parse_dated_links(
        self, soup: BeautifulSoup, url: str, timezone: str
    ) -> List[dict]:
        meetings = []
        seen_dates = set()
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        date_pattern = re.compile(
            r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
            r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
            r"Nov(?:ember)?|Dec(?:ember)?)"
            r"[.,]?\s+\d{1,2}[.,]?\s+\d{2,4}",
            re.I,
        )

        for link in soup.find_all("a", href=True):
            link_text = link.get_text(strip=True)
            if not link_text:
                continue

            if not date_pattern.search(link_text):
                continue

            href = link["href"].strip()

            # Skip minutes-only links
            if re.search(r"minute", link_text, re.I) and not re.search(
                r"agenda", link_text, re.I
            ):
                continue

            scheduled_time = self._to_utc_iso(link_text, timezone)
            if not scheduled_time:
                continue

            utc_dt = self._parse_iso_to_utc(scheduled_time)
            if utc_dt and utc_dt < min_allowed:
                continue

            # Deduplicate by date
            date_key = scheduled_time[:10]
            if date_key in seen_dates:
                # Update existing meeting's agenda link if needed
                for m in meetings:
                    if (
                        m["Scheduled time"][:10] == date_key
                        and not m["Agenda link"]
                    ):
                        abs_href = urljoin(self.base_url, href)
                        if abs_href.lower().endswith(
                            (".pdf", ".docx", ".doc")
                        ):
                            m["Agenda link"] = abs_href
                continue
            seen_dates.add(date_key)

            # Extract meeting name from the link text
            meeting_name = self._extract_name(link_text)

            # Build agenda link
            agenda_link = None
            abs_href = urljoin(self.base_url, href)
            if abs_href.lower().endswith((".pdf", ".docx", ".doc")):
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

    def _extract_name(self, text: str) -> str:
        """Extract meeting name by removing date patterns."""
        cleaned = re.sub(
            r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
            r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
            r"Nov(?:ember)?|Dec(?:ember)?)[.,]?\s+\d{1,2}[.,]?\s+\d{2,4}",
            "",
            text,
            flags=re.I,
        )
        cleaned = re.sub(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—:,")
        return cleaned if cleaned else "Council Meeting"

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
            log.warning(
                "Municipalimpact: error fetching %s: %s", url, e
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
        url="https://cityofgoodhue.gov/agendas",
        timezone="America/Chicago",
        schedule_type="municipalimpact_table",
    )
