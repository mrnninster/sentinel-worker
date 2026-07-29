# rhodeisland.py
import logging
import os
import re
from datetime import datetime
from urllib.parse import urlparse
from dateutil import parser

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test

from utils.utils_functions import remove_duplicates, get_api_json_call

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"
DEFAULT_PARAMS_FOR_API_CALL_CABLECAST_RHODEISLAND = {"site": 1}


class Rhodeisland:
    def __init__(self):
        self.timezone = None
        self.base_url = None
        self.url = None
        self.meetings = []
        self.self_contained_parser = True
        self.api_base_url = None

    def rhodeisland_cablecast_table(self, url: str, local_timezone: str) -> list:
        self.timezone = local_timezone
        self.url = url
        self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc

        self.api_base_url = f"{(p := urlparse(url)).scheme}://cloud.{p.netloc.split('.')[1]}.{p.netloc.split('.')[2]}/api/publicsitedata"

        params = {
            "currentDay": datetime.now().strftime("%Y-%m-%d"),
            "host": urlparse(url).netloc,
            **DEFAULT_PARAMS_FOR_API_CALL_CABLECAST_RHODEISLAND,
        }
        json_response = get_api_json_call(self.api_base_url + "/schedule", params)
        json_meetings = json_response.get("scheduleItems")

        count = 0
        total_meetings = len(json_meetings)

        for meeting in json_meetings:
            count += 1
            current_percent = round(count / total_meetings * 100)
            log.info(f"Processing meetings: {current_percent}% completed")

            # Check for fieldDisplay at meeting level
            meeting_field_display = meeting.get("fieldDisplay")
            # Also check in show.fieldDisplays (plural)
            show = meeting.get("show", {})
            show_field_displays = show.get("fieldDisplays", [])

            agenda_link = None
            meeting_link_text = None

            # Try meeting level fieldDisplay first
            # Check for at least 2 items: index 0 (meeting link text) and index 1 (agenda link)
            if meeting_field_display is not None and len(meeting_field_display) > 2:
                meeting_link_text = meeting_field_display[0].get("value")
                agenda_link = meeting_field_display[1].get("value")
            # Fallback to show.fieldDisplays
            elif show_field_displays:
                # Look for agenda link in fieldDisplays
                for field in show_field_displays:
                    if field.get("label") == "Agenda":
                        agenda_link = field.get("value")
                        break

            meeting_name = meeting.get("title")
            meeting_date = meeting.get("runDateTime")

            # Skip if no scheduled time
            if not meeting_date:
                continue

            # Filter for House/Senate floor sessions and legislative committees only
            meeting_name_lower = meeting_name.lower() if meeting_name else ""

            # House floor sessions (pattern: "House of Representatives: date")
            is_house_session = (
                "house of representatives" in meeting_name_lower and ":" in meeting_name
            )

            # Senate floor sessions (pattern: "Rhode Island Senate: date" or "Senate: date")
            is_senate_session = (
                "rhode island senate" in meeting_name_lower
                or meeting_name_lower.startswith("senate:")
            )

            # Legislative committees and commissions (must contain committee/commission AND legislative terms)
            is_committee = (
                "committee" in meeting_name_lower or "commission" in meeting_name_lower
            ) and (
                "joint" in meeting_name_lower
                or "house" in meeting_name_lower
                or "senate" in meeting_name_lower
                or "legislative" in meeting_name_lower
            )

            # Skip if not a House/Senate session or legislative committee
            if not (is_house_session or is_senate_session or is_committee):
                continue

            log.info("Found upcoming meeting")

            meeting_link = None
            status = "Upcoming"

            # Check isLive field from the API to determine if meeting is in progress
            is_live = show.get("isLive", False)

            # Determine status: In Progress if live, Cancelled if cancelled in text, else Upcoming
            if is_live:
                status = "In Progress"
                log.info("Found in progress meeting")
            elif re.search(r"cancel(?:led|ed)", meeting_name_lower):
                status = "Cancelled"
            else:
                status = "Upcoming"

            # Use vodUrl from show object as meeting link when available
            vod_url = show.get("vodUrl")
            if vod_url:
                meeting_link = vod_url

            self.meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
            )

        unique_data = remove_duplicates(self.meetings, "Scheduled time")
        return unique_data


if __name__ == "__main__":
    run_test(
        url="https://capitoltvri.cablecast.tv/schedule?site=1",
        schedule_type="rhodeisland_cablecast_table",
        timezone="America/New_York",
        get_full_archive_flag=False,
    )
