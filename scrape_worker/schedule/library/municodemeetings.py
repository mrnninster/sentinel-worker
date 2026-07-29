import os
import re
import sys
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

if __name__ == "__main__":
    # Allow running as a script for local testing
    sys.path.append(
        os.getenv("LOCAL_PROJECT_PATH")
        or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    )

import pytz
from bs4 import BeautifulSoup
from dateutil import parser

from logging_config import LOG_LEVEL, get_dedicated_debug_logger
from utils.scrape_html import HtmlScraper


class Municodemeetings:
    """
    Self-contained scraper for Municode Meetings listings.

    Parses the tabular agenda/minutes view
    (e.g., https://madeirabeach-fl.municodemeetings.com/meetings),
    walks pagination, and optionally hydrates YouTube live streams
    for meetings that should be in progress.
    """

    self_contained_parser = True

    def __init__(self):
        self.scraper = HtmlScraper()
        self.log = get_dedicated_debug_logger(__name__)
        self.log.setLevel(LOG_LEVEL)
        self.base_url: Optional[str] = None
        self.lookback_days = int(
            os.getenv("MUNICODEMEETINGS_LOOKBACK_DAYS", "7")
        )

    def get_events(self, url: str, timezone: str) -> List[dict]:
        """Adapter entrypoint to match existing scraper interface."""
        return self.municodemeetings_table(url=url, timezone=timezone)

    def municodemeetings_table(self, url: str, timezone: str) -> List[dict]:
        """
        Parse all rows from the Municode Meetings table view.

        Args:
            url: Public meetings listing page (table view).
            timezone: IANA timezone string for the jurisdiction.

        Returns:
            List of meeting dictionaries keyed to the schedule refresh pipeline.
        """
        self.base_url = self._derive_base_url(url)
        meetings: List[dict] = []
        page_url = url
        seen_pages = set()
        attempted_alt_path = False

        min_allowed = datetime.now(pytz.UTC) - timedelta(
            days=self.lookback_days
        )

        while page_url and page_url not in seen_pages:
            seen_pages.add(page_url)
            self.log.debug("Municodemeetings: fetching page %s", page_url)
            soup = self._get_page_soup(page_url)
            if soup is None:
                break

            table = soup.find("table", class_=re.compile(r"views-table"))
            if not table or not table.tbody:
                self.log.warning(
                    "Municodemeetings: no meetings table found at %s", page_url
                )
                if not attempted_alt_path:
                    attempted_alt_path = True
                    if "meetings3" not in page_url:
                        alt_path = urljoin(self.base_url, "/meetings3")
                    else:
                        alt_path = urljoin(self.base_url, "/meetings")
                    self.log.debug(
                        "Municodemeetings: retrying with alt path %s", alt_path
                    )
                    page_url = alt_path
                    continue
                break

            rows = table.tbody.find_all("tr")
            self.log.debug(
                "Municodemeetings: parsing %d rows on %s", len(rows), page_url
            )
            stop_parsing = False
            for idx, row in enumerate(rows):
                self.log.debug(
                    "Municodemeetings: parsing row %d on %s", idx + 1, page_url
                )
                result = self._parse_row(row, timezone)
                if result:
                    meeting, start_dt = result
                    if start_dt < min_allowed:
                        self.log.debug(
                            "Municodemeetings: skipping meeting before "
                            "lookback window (%s < %s)",
                            start_dt,
                            min_allowed,
                        )
                        stop_parsing = True
                        break
                    meetings.append(meeting)

            if stop_parsing:
                self.log.debug(
                    "Municodemeetings: encountered meeting before lookback; "
                    "stopping parse"
                )
                break

            page_url = self._get_next_page_url(soup)
            if page_url:
                self.log.debug("Municodemeetings: found next page %s", page_url)
            else:
                self.log.debug("Municodemeetings: no further pages detected")

        return meetings

    def _derive_base_url(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _get_page_soup(self, url: str) -> Optional[BeautifulSoup]:
        try:
            html = self.scraper.scrape_html(url=url)
        except (ConnectionError, OSError) as exc:
            self.log.warning("Municodemeetings: failed to fetch %s (%s)", url, exc)
            return None
        return self.scraper.convert_to_soup(string=html)

    def _get_next_page_url(self, soup: BeautifulSoup) -> Optional[str]:
        pager_link = soup.select_one("ul.pager a[href]")
        if pager_link and pager_link["href"]:
            return urljoin(self.base_url, pager_link["href"])
        return None

    def _parse_row(
        self, row: BeautifulSoup, timezone: str
    ) -> Optional[Tuple[dict, datetime]]:
        """
        Parse a table row into a meeting dictionary and datetime.

        Returns:
            Tuple of (meeting_dict, start_dt) if successful, None otherwise.
        """
        date_cell = row.find("td", attrs={"data-th": "Date"})
        title_cell = row.find("td", attrs={"data-th": "Meeting"})
        if not date_cell or not title_cell:
            return None

        start_dt = self._parse_start_time(date_cell, timezone)
        if not start_dt:
            return None

        meeting_name = title_cell.get_text(strip=True)
        agenda_link = self._get_agenda_link(row)
        detail_url = self._extract_detail_url(row)
        status = self._determine_status(meeting_name, start_dt)

        live_stream = self._resolve_video_url(detail_url)
        if live_stream:
            if status not in {"Past", "Cancelled"}:
                status = "In Progress"
        elif status == "In Progress":
            status = "Upcoming"

        scheduled_time = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        meeting_link = live_stream or detail_url

        meeting = {
            "Meeting name": meeting_name,
            "Scheduled time": scheduled_time,
            "Meeting link": meeting_link,
            "Agenda link": agenda_link,
            "Status": status,
            "user_live_link": detail_url if live_stream else None,
        }
        return (meeting, start_dt)

    def _parse_start_time(
        self, cell: BeautifulSoup, timezone: str
    ) -> Optional[datetime]:
        span = cell.find("span", class_="date-display-single")
        date_text = None
        if span:
            date_text = span.get("content") or span.get_text(strip=True)
        else:
            date_text = cell.get_text(" ", strip=True)

        if not date_text:
            return None

        try:
            start_dt = parser.parse(date_text)
        except (ValueError, parser.ParserError) as exc:
            self.log.warning(
                "Municodemeetings: could not parse date '%s' (%s)", date_text, exc
            )
            return None

        if start_dt.tzinfo is None:
            local_tz = pytz.timezone(timezone)
            start_dt = local_tz.localize(start_dt)

        return start_dt.astimezone(pytz.UTC)

    def _determine_status(self, title: str, start_dt: datetime) -> str:
        lowered_title = title.lower()
        if "cancel" in lowered_title:
            return "Cancelled"

        now = datetime.now(pytz.UTC)
        if start_dt < now:
            return "Past"
        return "Upcoming"

    def _extract_detail_url(self, row: BeautifulSoup) -> Optional[str]:
        video_cell = row.find("td", attrs={"data-th": "Video"})
        view_cell = row.find("td", attrs={"data-th": "View"})
        for cell in (video_cell, view_cell):
            if not cell:
                continue
            link = cell.find("a", href=True)
            if link and link["href"]:
                return urljoin(self.base_url, link["href"])
        return None

    def _get_agenda_link(self, row: BeautifulSoup) -> Optional[str]:
        agenda_cell = row.find("td", attrs={"data-th": "Agenda"})
        if not agenda_cell:
            return None

        links = agenda_cell.find_all("a", href=True)
        if not links:
            return None

        # Prefer accessible HTML agendas when present
        for link in links:
            href = link["href"]
            if "adaHtmlDocument" in href:
                return urljoin(self.base_url, href)

        return urljoin(self.base_url, links[0]["href"])

    def _resolve_video_url(self, detail_url: Optional[str]) -> Optional[str]:
        if not detail_url:
            return None

        self.log.debug("Municodemeetings: fetching detail page %s", detail_url)
        soup = self._get_page_soup(detail_url)
        if soup is None:
            return None

        iframe = soup.select_one("iframe#mcc_agenda_video")
        if iframe and iframe.get("src"):
            normalized = self._normalize_youtube_url(iframe["src"])
            if normalized:
                self.log.debug(
                    "Municodemeetings: found iframe-based stream %s", normalized
                )
                return normalized

        anchor = soup.select_one(".field-name-field-video-link a[href]")
        if anchor:
            normalized = self._normalize_youtube_url(anchor["href"])
            if normalized:
                self.log.debug(
                    "Municodemeetings: found anchor-based stream %s", normalized
                )
                return normalized

        return None

    def _normalize_youtube_url(self, raw_url: str) -> Optional[str]:
        if not raw_url:
            return None

        if raw_url.startswith("//"):
            raw_url = f"https:{raw_url}"

        parsed = urlparse(raw_url)
        if "youtube.com" in parsed.netloc and "/embed/" in parsed.path:
            video_id = parsed.path.rstrip("/").split("/")[-1]
            return f"https://www.youtube.com/watch?v={video_id}"

        if "youtu.be" in parsed.netloc:
            video_id = parsed.path.lstrip("/")
            return f"https://www.youtube.com/watch?v={video_id}"

        if "youtube.com" in parsed.netloc and parsed.query:
            return raw_url

        return None


if __name__ == "__main__":
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test

    lookback_days = int(os.getenv("MUNICODEMEETINGS_LOOKBACK_DAYS", "7"))
    now_utc = datetime.now(tz=pytz.UTC)

    run_test(
        url="https://madeirabeach-fl.municodemeetings.com/meetings",
        timezone="America/New_York",
        schedule_type="municodemeetings_table",
        get_date_start=now_utc - timedelta(days=lookback_days),
        get_date_end=now_utc,
    )
