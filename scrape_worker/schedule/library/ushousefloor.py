import os
import re
import sys
import pytz
import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from dateutil import parser as dateutil_parser

from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

HOUSE_FLOOR_YOUTUBE_URL = "https://www.youtube.com/@USHouseClerk/streams"

# Anchor phrase that precedes the actual scheduled time on live.house.gov.
# Examples:
#   "The next meeting is scheduled for approximately 6:30 P.M. today."
#   "The next meeting is scheduled for 10:00 a.m. on February 10, 2026."
_SCHEDULED_FOR_RE = re.compile(
    r"(?:next\s+meeting|house)\s+is\s+scheduled\s+(?:to\s+\w+\s+)?(?:for|at)\s+"
    r"(?:approximately\s+)?"
    r"(\d{1,2}:\d{2}\s*[ap]\.?m\.?)"  # group 1: time
    r"(?:\s+on\s+(.+?))??"             # group 2: optional date after "on"
    r"(?:\s+today)?"                   # optional "today"
    r"\s*[.\n]",                       # sentence end
    re.IGNORECASE,
)


class Ushousefloor:
    """
    Scraper for US House floor sessions.

    Parses live.house.gov to extract upcoming House floor activity schedule.
    Only picks up lines containing "is scheduled for" to avoid false positives
    from bill numbers, reference IDs, and other table noise.
    """

    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True

    def unique_ushousefloor(self, url: str, timezone: str):
        """
        Scrape US House floor session schedule from live.house.gov.

        Args:
            url: House floor schedule URL (https://live.house.gov/)
            timezone: Timezone string (e.g., "America/New_York")

        Returns:
            list: Meeting dicts with standard keys.
        """
        html_text = self.scraper.scrape_html(
            url=url,
            render="true",
            wait_for_selector="#activity-table",
        )
        soup = self.scraper.convert_to_soup(html_text)

        now_utc = datetime.now(tz=dt_timezone.utc)
        local_tz = pytz.timezone(timezone)

        # The page has a display-date element showing the legislative day,
        # e.g. "Monday, February 9, 2026". This may differ from the actual
        # calendar date after midnight — the page keeps showing the previous
        # day until it's updated. Use this as the reference for "today".
        page_date = None
        display_date_el = soup.find("span", class_="display-date")
        if display_date_el:
            try:
                page_date = dateutil_parser.parse(
                    display_date_el.get_text(strip=True)
                ).date()
            except (ValueError, OverflowError):
                pass
        if page_date is None:
            page_date = datetime.now(local_tz).date()

        body_text = soup.get_text(separator=" ", strip=True)

        for match in _SCHEDULED_FOR_RE.finditer(body_text):
            time_part = match.group(1).replace(".", "")  # "6:30 PM"
            date_part = match.group(2)                    # "February 10, 2026" or None

            # Determine whether "today" was used (no date_part, or explicit "today")
            full_match_text = match.group(0).lower()
            is_today = "today" in full_match_text or not date_part

            try:
                if is_today:
                    # Parse just the time, pin to the page's display-date
                    # (which may be yesterday if the page hasn't refreshed
                    # after midnight)
                    time_dt = dateutil_parser.parse(time_part)
                    meeting_dt = local_tz.localize(
                        datetime.combine(page_date, time_dt.time())
                    )
                else:
                    # Parse full "date time" string
                    meeting_dt = dateutil_parser.parse(
                        f"{date_part.strip()} {time_part}"
                    )
                    if meeting_dt.tzinfo is None:
                        meeting_dt = local_tz.localize(meeting_dt)

                utc_dt = meeting_dt.astimezone(dt_timezone.utc)

                # Sanity: skip past meetings and anything more than 14 days out
                if utc_dt < now_utc:
                    continue
                if utc_dt > now_utc + timedelta(days=14):
                    log.warning(
                        f"Skipping suspiciously far-future meeting: {utc_dt}"
                    )
                    continue

                meet_date_time = utc_dt.isoformat().replace("+00:00", "Z")
                self.meetings.append({
                    "Meeting name": "House Floor Session",
                    "Scheduled time": meet_date_time,
                    "Meeting link": HOUSE_FLOOR_YOUTUBE_URL,
                    "Agenda link": None,
                    "Status": "Upcoming",
                })
            except (ValueError, OverflowError) as e:
                log.warning(f"Failed to parse scheduled time: '{match.group(0)}': {e}")
                continue

        if not self.meetings:
            log.info("No 'next meeting is scheduled for' text found on live.house.gov")

        return self.meetings


if __name__ == "__main__":
    url = "https://live.house.gov/"
    tz = "America/New_York"
    schedule_type = "unique_ushousefloor"
    run_test(url=url, timezone=tz, schedule_type=schedule_type)
