import re
import os
import json
from dotenv import load_dotenv
from datetime import datetime, timedelta
from dateutil import parser, relativedelta


if __name__ == "__main__":
    import sys

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None


from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter


class Palmbeach:
    """
    This is a self contained scraper.
    It needs to generate the start and end time that
    is used in the api url, so the value of "url" from
    the refresh schedule request is not used here, rather
    it auto generates the url in the __init__ function

    Here is what the request is expect to look like
    {
        "geodicts": [
            {
                "schedule_type": "unique_palmbeach",
                "url": "https://pbc.gov/cal/event/GetEvents?start1=01%2F26%2F2025&end1=03%2F09%2F2025&s=", # Auto generated in this script
                "timezone": "America/New_York",
                "glitch_meetings": [],
                "debug": null,
                "channel_url": "https://www.youtube.com/@pbctvchannel20/live"
            }
        ],
        "version": "test"
    }
    """

    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True

        # Generate start and stop dates
        start_date = datetime.today()
        end_date = start_date + relativedelta.relativedelta(
            months=1
        )  # Exactly one month from today

        self.start_date = start_date.strftime("%m%%2F%d%%2F%Y")
        self.stop_date = end_date.strftime("%m%%2F%d%%2F%Y")

        self.PBC_url = f"https://pbc.gov/cal/event/GetEvents?start1={self.start_date}&end1={self.stop_date}&s="

    def unique_palmbeach(self, url, timezone="America/New_York"):

        # Get api data
        response = self.scraper.scrape_html(url=self.PBC_url)
        json_response = json.loads(response)

        # Generate meetings
        meetings = [
            item for item in json_response if item["channelLive"].lower() == "y"
        ]

        for meet in meetings:
            meeting_date_time = parser.parse(meet["start"], fuzzy=True)
            meeting_date_time = datetime.strftime(
                meeting_date_time, TimeFormatter.desired_format()
            )
            utc_time = TimeFormatter(meeting_date_time, timezone).get_utc_time(
                as_datetime=True
            )
            schedule_time = utc_time.isoformat().replace("+00:00", "Z")

            title = meet["title"].replace("\n", "")

            # Check for cancellation
            if re.search(
                r"cancel(?:led|ed)", str(meet["title"]).lower(), re.IGNORECASE
            ):
                status = "Cancelled"
            else:
                status = "Upcoming"

            # Create meeting object
            meeting = {
                "Meeting name": title,
                "Scheduled time": schedule_time,
                "Meeting link": None,
                "Agenda link": None,
                "Status": status,
            }

            # Add meeting to list
            self.meetings.append(meeting)

        # Return meetings
        return self.meetings


if __name__ == "__main__":
    from pytz import timezone as pytz_timezone
    from schedule.schedule_scraper import run_test

    # Generate start and stop dates
    start_date = datetime.today()
    end_date = start_date + relativedelta.relativedelta(
        months=1
    )  # Exactly one month from today

    start_date = start_date.strftime("%m%%2F%d%%2F%Y")
    stop_date = end_date.strftime("%m%%2F%d%%2F%Y")

    url = f"https://pbc.gov/cal/event/GetEvents?start1={start_date}&end1={end_date}&s="
    timezone = "America/New_York"
    schedule_type = "unique_palmbeach"

    # Make datetime.now() timezone aware
    tz = pytz_timezone(timezone)

    run_test(url, timezone, schedule_type)
