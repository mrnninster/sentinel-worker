import re
import os
import sys
import pytz
import json
import logging
from dotenv import load_dotenv
from dateutil.parser import parse
from urllib.parse import quote
from datetime import datetime, timedelta

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
from utils.scrape_html import HtmlScraper  # noqa: E402
from utils.format_time import TimeFormatter  # noqa: E402
from schedule.schedule_scraper import run_test  # noqa: E402

# Setup the log
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Calisenate:
    """
    This is a self-contained class that scrapes the California Senate schedule.

    Here is what the request is expect to look like
    {
        "geodicts": [
            {
                "geoID": "1736188947161x945637266683254700",
                "schedule_type": "unique_calisenate",
                "url": "https://www.senate.ca.gov/calendar?",
                "timezone": "America/Los_Angeles",
                "glitch_meetings": [],
                "debug": false,
                "channel_url": ""
            }
        ],
        "version": "test"
    }
    """

    def __init__(self):
        self.meetings = []
        self.agenda_url = None
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        self.base_url = "https://www.senate.ca.gov"
        self.param_keys = {
            "floor-session": {
                "date": "floorMeetingDate",
                "time": "floorMeetingTime",
            },
            "committee-hearings": {
                "date": "committeeHearingDate",
                "time": "committeeHearingTime",
                "note": "committeeHearingNote",
            },
        }

    def _has_adjourned_notice(
        self, daily_file_data: dict, date_value: str, timezone: str
    ) -> bool:
        try:
            event_date = parse(date_value).date()
        except (TypeError, ValueError):
            return False

        today_local = datetime.now(tz=pytz.timezone(timezone)).date()
        if event_date > today_local:
            return False

        for value in daily_file_data.values():
            if isinstance(value, dict) and isinstance(value.get("value"), str):
                if "adjourn" in value["value"].lower():
                    return True

        return False

    def get_in_progress_meetings(self, soup) -> list[str]:
        """Return live meeting URLs listed as in progress on the calendar page."""
        live_urls = []
        in_progress_tabs = soup.find_all("div", class_="status-in-progress")
        if in_progress_tabs:
            for in_progress_tab in in_progress_tabs:
                anchor = in_progress_tab.find_next("a", href=True)
                if not anchor:
                    log.warning(
                        "Could not find live meeting link for in-progress tab."
                    )
                    continue
                live_url = anchor.get("href")
                if not live_url:
                    log.warning("In-progress meeting link missing href attribute.")
                    continue
                stream_url = f"{self.base_url}{live_url}".lower()
                live_urls.append(stream_url)
        return live_urls

    def unique_calisenate(
        self, url, timezone="America/Los_Angeles"
    ) -> list[dict]:
        """Parse the California Senate calendar and return normalized meetings."""
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        live_urls = self.get_in_progress_meetings(soup)
        # log.info(f"Live URLs: {live_urls}")

        # Create a datetime object for today at midnight in the given timezone
        start_datetime = datetime.now(tz=pytz.UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        # Set the end datetime 60 days later
        end_datetime = start_datetime + timedelta(days=60)

        # Convert to ISO 8601 string
        iso_start_datetime = start_datetime.isoformat()
        iso_end_datetime = end_datetime.isoformat()

        # URL encode
        urlSafe_start_datetime = quote(iso_start_datetime)
        urlSafe_end_datetime = quote(iso_end_datetime)

        # Build API URL
        api_url = (
            f"{self.base_url}/api/dailyfile/fullcalendar-events?details=true"
            f"&start={urlSafe_start_datetime}&end={urlSafe_end_datetime}"
        )

        # Fetch and parse response
        payload = {
            "api_key": self.scraper.SCRAPERAPICOM_API_KEY,
            "url": api_url,
        }
        response = self.scraper.fetch_with_scraperapi(payload)
        json_response = json.loads(response)

        # Get the meetings from the json response
        for event in json_response:

            # Get event type
            event_type = event["eventType"]

            # If the event type is in the param keys, then we can parse the meeting
            if event_type in self.param_keys.keys():

                # Get the meeting time
                time_value = event["dailyFileEntityData"][
                    self.param_keys[event_type]["time"]
                ]
                if str(time_value).lower() == "upon call of the chair":
                    continue

                # Extract just the time portion (e.g., "9:30 a.m." or "9 a.m.")
                # before any description text
                time_match = re.match(
                    r"(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)",
                    str(time_value),
                    re.IGNORECASE,
                )
                if time_match:
                    time_value = time_match.group(1)
                else:
                    log.warning(f"Could not parse time from: {time_value}")
                    continue

                # Get the meeting name and clean HTML tags
                meeting_name = event["title"]
                # Replace <br> and <br /> tags with spaces
                meeting_name = re.sub(
                    r"<br\s*/?>", " ", meeting_name, flags=re.IGNORECASE
                )
                # Remove all other HTML tags
                meeting_name = re.sub(r"<[^>]+>", "", meeting_name)
                # Normalize whitespace (multiple spaces to single space)
                meeting_name = re.sub(r"\s+", " ", meeting_name)
                # Strip leading/trailing whitespace
                meeting_name = meeting_name.strip()

                # Get the meeting date
                date_value = event["dailyFileEntityData"][
                    self.param_keys[event_type]["date"]
                ]

                # Convert to UTC time
                time_string = f"{date_value} {time_value}"
                meeting_date_time = parse(time_string, fuzzy=True)
                meeting_date_time = datetime.strftime(
                    meeting_date_time, TimeFormatter.desired_format()
                )
                utc_time = TimeFormatter(meeting_date_time, timezone).get_utc_time(
                    as_datetime=True
                )
                meeting_date_time = utc_time.isoformat().replace("+00:00", "Z")

                # Get agenda
                event_id = event["dailyFileEntityData"]["entity_id"]
                agenda_url = (
                    f"{self.base_url}/api/dailyfile/render-agenda/{event_id}"
                    "?destination=/calendar"
                )

                # Get meeting link
                meeting_link = (
                    f"{self.base_url}/media-live-event/{event_id}?format=video".lower()
                )

                # Get the meeting status
                # Default to Upcoming, then check other conditions
                status = "Upcoming"
                # Check if meeting is in progress (highest priority)
                if meeting_link in live_urls:
                    status = "In progress"
                else:
                    # Check for cancelled status first
                    if event_type == "committee-hearings":
                        note_value = str(
                            event["dailyFileEntityData"][
                                self.param_keys[event_type]["note"]
                            ]["value"]
                        ).lower()
                        if re.search(r"Cancel(?:led|ed)", note_value, re.IGNORECASE):
                            status = "Cancelled"

                    # Mark ended only when an explicit adjourned notice exists.
                    if status != "Cancelled" and self._has_adjourned_notice(
                        event["dailyFileEntityData"], date_value, timezone
                    ):
                        status = "Ended"

                # # Debugging
                # log.info(f"Meeting name: {meeting_name} Status: {status}")
                # log.info(meeting_link)
                # log.info(live_urls[0])

                self.meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": meeting_date_time,
                        "Meeting link": meeting_link,
                        "Agenda link": agenda_url,
                        "Status": status,
                    }
                )

            # if the event type is not in the param keys, then we can't parse
            # the meeting
            else:
                log.warning(f"Event type {event['eventType']} not handled by parser")

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://www.senate.ca.gov/calendar?",
        schedule_type="unique_calisenate",
        timezone="America/Los_Angeles",
    )
