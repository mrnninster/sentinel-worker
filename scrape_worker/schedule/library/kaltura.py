# schedule/library/kaltura.py

import os
import re
import json
import requests
import logging
import pytz
from datetime import datetime
from typing import Optional, List, Dict
from urllib.parse import urlencode

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test
from schedule.schedule_scraper import clean_meeting_titles

# Setup the log
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Kaltura:
    def __init__(self):
        """
        Initialize the Kaltura class.

        Attributes:
            self_contained_parser (bool): Indicates if the parser is self-contained.
            meetings (List[Dict]): List to store meeting information.
            channel (str): Channel selector ("Live Streams", "TV 24/7", "combined").
        """
        self.self_contained_parser = True
        self.meetings: List[Dict] = []
        self.channel = os.getenv("ARG_CHANNEL_URL", "Live Streams")

    def fetch_schedule(self, date: str) -> Optional[Dict]:
        """
        Fetch the schedule data from the Kaltura API.

        Args:
            date (str): The date for which to fetch the schedule in YYYY-MM-DD format.

        Returns:
            dict: The JSON response from the API if successful, else None.
        """
        base_url = "https://thefloridachannel.org/api/tfc/v1/schedule"
        cb = f"{int(datetime.now().timestamp())}.{int(datetime.now().microsecond / 1000)}"
        params = {"cb": cb, "date": date}
        url = f"{base_url}?{urlencode(params)}"

        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://thefloridachannel.org/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.2 Safari/605.1.15",
            "schedule": "true",
        }

        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                log.info(f"Successfully fetched schedule for date {date}.")
                return response.json()
            else:
                log.warning(f"Failed to fetch schedule: HTTP {response.status_code}")
                return None
        except requests.RequestException as e:
            log.warning(f"HTTP request failed: {e}")
            return None

    def determine_status(
        self,
        live: bool,
        start_time_utc: str,
        end_time_utc: str,
        current_time_utc: datetime,
    ) -> str:
        """
        Determine the status of a meeting based on its live flag and end time.

        Args:
            live (bool): Whether the meeting is currently live.
            end_time_utc (str): The end time of the meeting in UTC ISO format.
            current_time_utc (datetime): The current UTC time.

        Returns:
            str: The status of the meeting ("In progress", "Ended", "Upcoming").
        """
        # Pre-filter end time to handle non-date strings, which imply live stream with no end time
        try:
            start_time = datetime.fromisoformat(start_time_utc.replace("Z", "+00:00"))
        except ValueError as e:
            log.warning(f"Error parsing start_time_utc '{start_time_utc}': {e}")
            return "Upcoming"

        if end_time_utc:
            try:
                end_time = datetime.fromisoformat(end_time_utc.replace("Z", "+00:00"))
            except ValueError:
                log.warning(f"Error parsing end_time_utc '{end_time_utc}': {e}")
                return "Upcoming"
        else:
            end_time = None

        if current_time_utc < start_time:
            return "Upcoming"
        elif start_time <= current_time_utc and (
            end_time is None or end_time > current_time_utc
        ):
            return "In progress"
        else:
            return "Ended"

    def kaltura_table_v1(
        self, url: str, timezone: str, agenda_url: Optional[str] = None
    ) -> List[Dict]:
        """
        Fetch and process the schedule using the Kaltura API.

        Args:
            url (str): The base URL (not used in this implementation).
            timezone (str): The timezone to localize meeting times.
            agenda_url (str, optional): The agenda URL (can be None).

        Returns:
            List[Dict]: A list of meetings with their details and statuses.
        """
        # Get current date in YYYY-MM-DD format based on the provided timezone
        tz = pytz.timezone(timezone)
        current_date = datetime.now(tz).strftime("%Y-%m-%d")

        # Fetch schedule data from the API
        schedule_data = self.fetch_schedule(current_date)
        if not schedule_data or not schedule_data.get("success"):
            log.warning("Failed to retrieve schedule data from the API.")
            return self.meetings

        # Determine which channels to process based on the channel selector
        channels_to_process = []
        if self.channel.lower() == "combined":
            channels_to_process = schedule_data.get("data", [])
            log.info("Processing all channels (combined).")
        else:
            # Filter channels based on the channel selector
            for channel in schedule_data.get("data", []):
                if channel.get("title", "").lower() == self.channel.lower():
                    channels_to_process.append(channel)
                    log.info(f"Processing channel: {channel.get('title')}")
                    break  # Assuming only one channel matches
            if not channels_to_process:
                log.warning(
                    f"No channel found matching '{self.channel}'. No meetings will be processed."
                )
                return self.meetings

        # Iterate over each channel's schedule
        for channel in channels_to_process:
            channel_title = channel.get("title", "")
            for event in channel.get("schedule", []):
                try:
                    # Extract necessary fields
                    meeting_title = event.get("title", "")
                    start_time_utc = event.get("start_time_utc")
                    end_time_utc = event.get("end_time_utc")
                    live = event.get("live", False)
                    stream_url = event.get("url", "")

                    if not start_time_utc:
                        log.warning(
                            f"Missing start_time_utc or for event '{meeting_title}'. Skipping."
                        )
                        continue

                    # Determine the status of the meeting
                    current_time_utc = datetime.now(pytz.utc)
                    status = self.determine_status(
                        live, start_time_utc, end_time_utc, current_time_utc
                    )

                    # clean stream url to find playlist if possible
                    if "format/url/protocol/https" in stream_url:
                        # Replace 'format/url' with 'format/applehttp'
                        # Keep 'protocol/https'
                        # Then add '/a.m3u8' if not already present
                        stream_url = stream_url.replace(
                            "format/url", "format/applehttp"
                        )
                        if not stream_url.endswith("/a.m3u8"):
                            if not stream_url.endswith("/"):
                                stream_url += "/"
                            stream_url += "a.m3u8"

                    # Append meeting information
                    self.meetings.append(
                        {
                            "Meeting name": meeting_title,
                            "Scheduled time": start_time_utc,
                            "Meeting link": stream_url,  # Using the stream URL from the API
                            "user_live_link": url,  # Using the base URL for browser access
                            "Agenda link": agenda_url,  # Can be None if not provided
                            "Status": status,
                        }
                    )

                except Exception as e:
                    log.warning(
                        f"Error processing event '{event}': {e}", exc_info=True
                    )
                    continue

        log.info(f"Total meetings fetched: {len(self.meetings)}")
        return self.meetings

    def get_kaltura_id_for_meeting(
        self, meeting_name: str, timezone: str = "America/New_York"
    ) -> Optional[str]:
        """
        NEW HELPER: Return the Kaltura 'id' of the first schedule event whose 'title' == meeting_name.
        We use the same TFC schedule endpoint for 'today' by default.
        """
        tz = pytz.timezone(timezone)
        current_date = datetime.now(tz).strftime("%Y-%m-%d")
        schedule_data = self.fetch_schedule(current_date)
        if not schedule_data or not schedule_data.get("success"):
            log.warning("Failed to retrieve schedule data from the TFC API.")
            return None

        # The schedule data: { "data": [ { "title": "...", "schedule": [ { "title": "MyMeeting", "id": "1_1tqjk60s", ... } ] } ] }
        for channel_obj in schedule_data.get("data", []):
            for event in channel_obj.get("schedule", []):
                this_meeting_name_clean_list = clean_meeting_titles(
                    [{"Meeting name": event.get("title")}]
                )
                this_meeting_clean = this_meeting_name_clean_list[0].get("Meeting name")
                # Compare EXACT match, or partial match if you prefer
                if this_meeting_clean == meeting_name:
                    found_id = event.get("id")  # e.g. "1_1tqjk60s"
                    if found_id:
                        log.info(
                            f"Found ID '{found_id}' for meeting '{meeting_name}'."
                        )
                        return found_id
        log.warning(f"No schedule item found with exact title='{meeting_name}'.")
        return None


if __name__ == "__main__":
    run_test(
        url="https://thefloridachannel.org/",
        schedule_type="kaltura_table_v1",
        timezone="America/New_York",
        get_full_archive_flag=False,
    )
