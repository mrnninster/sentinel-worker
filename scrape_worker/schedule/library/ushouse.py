import os
import re
import sys
import pytz
import logging
import time as time_module
from datetime import datetime, timedelta, timezone as dt_timezone
from dateutil import parser as dateutil_parser
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

DOCS_HOUSE_BASE = "https://docs.house.gov/Committee/Calendar"

# Number of days ahead to scrape for upcoming meetings
SCRAPE_DAYS_AHEAD = 14

# Module-level HTML cache to avoid redundant fetches when multiple child geos
# (each with a different committee_filter) are processed in the same batch.
_day_html_cache: dict[str, tuple[str, float]] = {}
_CACHE_TTL_SECONDS = 300  # 5 minutes


def _get_cached_html(day_id: str) -> str | None:
    if day_id in _day_html_cache:
        html, ts = _day_html_cache[day_id]
        if time_module.time() - ts < _CACHE_TTL_SECONDS:
            return html
        del _day_html_cache[day_id]
    return None


def _set_cached_html(day_id: str, html: str) -> None:
    _day_html_cache[day_id] = (html, time_module.time())


def _extract_root_committee(committee_text: str) -> str:
    """Extract the root committee name from a committee/subcommittee string.

    'Subcommittee X (Committee on Y)' → 'Committee on Y'
    'Committee on Y' → 'Committee on Y'
    """
    parent_match = re.search(r"\((.+?)\)\s*$", committee_text)
    return parent_match.group(1).strip() if parent_match else committee_text.strip()


class Ushouse:
    """
    Scraper for US House committee hearings.

    Fetches committee meeting data from docs.house.gov, the official
    U.S. House Committee Repository. Uses the ByDay endpoint which
    returns server-rendered HTML with no bot protection or API keys.
    """

    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True

    def _scrape_day(self, date: datetime, timezone: str, local_tz, committee_filter: str | None = None) -> list:
        """Scrape meetings for a single day from docs.house.gov ByDay page."""
        day_id = date.strftime("%m%d%Y")
        url = f"{DOCS_HOUSE_BASE}/ByDay.aspx?DayID={day_id}"
        date_str = date.strftime("%B %d, %Y")

        html_text = _get_cached_html(day_id)
        if html_text is None:
            try:
                html_text = self.scraper.scrape_html(url=url)
                _set_cached_html(day_id, html_text)
            except Exception as e:
                log.warning(f"Failed to fetch docs.house.gov for {day_id}: {e}")
                return []

        soup = self.scraper.convert_to_soup(html_text)
        table = soup.find("table", id="MainContent_GridViewMeetings")
        if not table:
            return []

        rows = table.find_all("tr")[1:]  # Skip header row
        day_meetings = []

        for row in rows:
            cells = row.find_all("td")
            # "No meetings found" rows have a single cell with colspan
            if not cells or row.find("td", colspan=True):
                continue

            if len(cells) < 2:
                continue

            # Title cell: <a href="ByEvent.aspx?EventID=NNNNN">Title</a>
            #             <span class="text-tiny">Committee Name</span>
            title_cell = cells[0]
            link_tag = title_cell.find("a")
            committee_span = title_cell.find("span", class_="text-tiny")

            if not link_tag:
                continue

            title = link_tag.get("title", "") or link_tag.get_text(strip=True)
            event_href = link_tag.get("href", "")
            event_url = f"{DOCS_HOUSE_BASE}/{event_href}" if event_href else None

            # Parse committee name from span
            committee_name = ""
            if committee_span:
                committee_text = committee_span.get_text(strip=True)

                # Apply committee filter if specified
                if committee_filter:
                    root = _extract_root_committee(committee_text)
                    if root.lower() != committee_filter.lower():
                        continue

                # Format: "Subcommittee Name (Parent Committee)" or just "Committee Name"
                parent_match = re.search(r"\((.+?)\)\s*$", committee_text)
                if parent_match:
                    parent_committee = parent_match.group(1)
                    sub_committee = committee_text[:parent_match.start()].strip()
                    committee_name = f"{parent_committee}: {sub_committee}"
                else:
                    committee_name = committee_text

            # Build meeting name
            if committee_name and title:
                meeting_name = f"{committee_name}: {title}"
            elif committee_name:
                meeting_name = committee_name
            else:
                meeting_name = title or "House Committee Meeting"

            # Time cell
            time_text = cells[1].get_text(strip=True) if len(cells) > 1 else ""

            # Parse datetime
            try:
                if time_text:
                    full_dt_str = f"{date_str} {time_text}"
                else:
                    full_dt_str = f"{date_str} 12:00 PM"

                meeting_dt = dateutil_parser.parse(full_dt_str)
                if meeting_dt.tzinfo is None:
                    meeting_dt = local_tz.localize(meeting_dt)

                utc_dt = meeting_dt.astimezone(dt_timezone.utc)
                meet_date_time = utc_dt.isoformat().replace("+00:00", "Z")
            except Exception as e:
                log.warning(f"Error parsing date/time '{date_str} {time_text}': {e}")
                continue

            day_meetings.append({
                "Meeting name": meeting_name,
                "Scheduled time": meet_date_time,
                "Meeting link": event_url,
                "Agenda link": event_url,
                "Status": "Upcoming",
            })

        return day_meetings

    def unique_ushouse(self, url: str, timezone: str):
        """
        Scrape US House committee hearings from docs.house.gov.

        Args:
            url: Schedule URL, optionally with ?committee_filter= param
                 to restrict results to a single committee.
            timezone: Timezone string (e.g., "America/New_York")

        Returns:
            list: Meeting dicts with standard keys.
        """
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        committee_filter = params.get("committee_filter", [None])[0]

        local_tz = pytz.timezone(timezone)
        today = datetime.now(tz=local_tz).replace(hour=0, minute=0, second=0, microsecond=0)

        for day_offset in range(SCRAPE_DAYS_AHEAD):
            target_date = today + timedelta(days=day_offset)
            day_meetings = self._scrape_day(target_date, timezone, local_tz, committee_filter)
            self.meetings.extend(day_meetings)

        return self.meetings


if __name__ == "__main__":
    url = "https://docs.house.gov/Committee/Calendar/ByDay.aspx"
    tz = "America/New_York"
    schedule_type = "unique_ushouse"
    run_test(url=url, timezone=tz, schedule_type=schedule_type)
