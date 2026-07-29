import os
import sys
import json
import pytz
import logging
from datetime import datetime, timezone as dt_timezone

from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

SENATE_FLOOR_ISVP_URL = "https://www.senate.gov/isvp/?comm=stv&filename=stv"


class Ussenatefloor:
    """
    Scraper for US Senate floor sessions.

    Parses the Senate floor schedule JSON at
    senate.gov/legislative/schedule/floor_schedule.json to extract
    upcoming floor session information.
    """

    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True

    def unique_ussenatefloor(self, url: str, timezone: str):
        """
        Scrape US Senate floor session schedule from JSON endpoint.

        Args:
            url: JSON endpoint URL (https://www.senate.gov/legislative/schedule/floor_schedule.json)
            timezone: Timezone string (e.g., "America/New_York")

        Returns:
            list: Meeting dicts with standard keys.
        """
        raw_text = self.scraper.scrape_html(url=url)

        try:
            data = json.loads(raw_text)
        except (json.JSONDecodeError, TypeError) as e:
            log.warning(f"Failed to parse Senate floor schedule JSON: {e}")
            return self.meetings

        now_utc = datetime.now(tz=dt_timezone.utc)
        local_tz = pytz.timezone(timezone)

        # The JSON uses "floorProceedings" — an array of session objects
        # with individual date/time fields (conveneYear, conveneMonth, etc.)
        proceedings = []
        if isinstance(data, dict):
            proceedings = data.get("floorProceedings", [])

        if not proceedings:
            log.warning("No floor proceedings found in Senate floor schedule")
            return self.meetings

        for session in proceedings:
            convene_year = session.get("conveneYear", "")
            convene_month = session.get("conveneMonth", "")
            convene_day = session.get("conveneDay", "")
            convene_hour = session.get("conveneHour", "")
            convene_minutes = session.get("conveneMinutes", "")

            if not all([convene_year, convene_month, convene_day]):
                continue

            session_title = session.get("convenedSessionDescription", "Senate Floor Session")
            stream_url = session.get("convenedSessionStream", "") or SENATE_FLOOR_ISVP_URL

            # Build datetime from individual fields
            try:
                meeting_dt = datetime(
                    int(convene_year),
                    int(convene_month),
                    int(convene_day),
                    int(convene_hour) if convene_hour else 0,
                    int(convene_minutes) if convene_minutes else 0,
                )
                meeting_dt = local_tz.localize(meeting_dt)
                utc_dt = meeting_dt.astimezone(dt_timezone.utc)
                meet_date_time = utc_dt.isoformat().replace("+00:00", "Z")
            except (ValueError, TypeError) as e:
                log.warning(f"Error parsing floor schedule date/time: {e}")
                continue

            # Skip past sessions
            if utc_dt < now_utc:
                continue

            if not session_title:
                session_title = "Senate Floor Session"

            self.meetings.append({
                "Meeting name": session_title,
                "Scheduled time": meet_date_time,
                "Meeting link": stream_url,
                "Agenda link": None,
                "Status": "Upcoming",
            })

        return self.meetings


if __name__ == "__main__":
    url = "https://www.senate.gov/legislative/schedule/floor_schedule.json"
    tz = "America/New_York"
    schedule_type = "unique_ussenatefloor"
    run_test(url=url, timezone=tz, schedule_type=schedule_type)
