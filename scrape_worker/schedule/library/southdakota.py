# southdakota.py
import logging
import os
from datetime import datetime
from urllib.parse import urlparse

import pytz
from dateutil import parser

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test

from utils.utils_functions import get_api_json_call

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"
DEFAULT_PARAMS_FOR_API_CALL_SOUTHDAKOTA = {"all": True}


class Southdakota:
    def __init__(self):
        self.timezone = None
        self.base_url = None
        self.url = None
        self.meetings = []
        self.self_contained_parser = True
        self.api_base_url = None

    def southdakota_table(self, url: str, local_timezone: str) -> list:
        self.timezone = local_timezone
        self.url = url
        self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc

        self.api_base_url = f"{self.base_url}/api"

        params = {**DEFAULT_PARAMS_FOR_API_CALL_SOUTHDAKOTA}

        json_response = get_api_json_call(
            self.api_base_url + "/Documents/Schedule", params
        )
        json_meetings = json_response.get("sessionMeetings")

        tz = pytz.timezone(local_timezone)
        now = datetime.now(tz)

        for meeting in json_meetings:
            meeting_name = (
                meeting.get("CommitteeFullName")
                or meeting.get("InterimYearCommitteeName")
                or meeting.get("Title")
            )
            if meeting_name is None:
                continue
            meeting_date = meeting.get("DocumentDate")
            meeting_link = meeting.get("RoomAudio")
            document_id = meeting.get("DocumentId")
            agenda_link = None
            if document_id:
                netloc = urlparse(self.api_base_url).netloc
                scheme = urlparse(self.api_base_url).scheme
                agenda_link = (
                    f"{scheme}://mylrc.{netloc}/api/Documents/{document_id}.pdf"
                )

            start_dt = parser.parse(meeting_date, fuzzy=True)
            if start_dt.tzinfo is None:
                start_dt = tz.localize(start_dt)
            else:
                start_dt = start_dt.astimezone(tz)

            end_raw = meeting.get("EndDate")
            if not end_raw:
                end_dt = None
            else:
                end_dt = parser.parse(end_raw, fuzzy=True)
                if end_dt.tzinfo is None:
                    end_dt = tz.localize(end_dt)
                else:
                    end_dt = end_dt.astimezone(tz)

            current_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if start_dt < current_date:
                continue

            status = "Upcoming"
            if end_dt is not None and start_dt < now < end_dt:
                status = "In Progress"

            # Format start_dt to UTC format for Scheduled time
            start_dt_utc = start_dt.astimezone(pytz.UTC)
            scheduled_time = (
                start_dt_utc.strftime(MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT)[:-3]
                + "Z"
            )

            self.meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": scheduled_time,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
            )

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://sdlegislature.gov/session/schedule",
        schedule_type="southdakota_table",
        timezone="America/New_York",
        get_full_archive_flag=False,
    )
