import os
import re
import pytz
import logging
from fuzzywuzzy import fuzz
from datetime import datetime
from urllib.parse import urlparse

from dateutil import parser
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

if __name__ == "__main__":
    import sys

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from schedule.library.youtube import Youtube
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test
from utils.scrape_html import HtmlScraper, HTMLTags, HTMLAttributes

MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"
PATTERN_REMOVE_PARENTHESIS_MEETING_NAME = r"\(.*?\)"
TIME_PATTERN = r"^(\d{1,2}:\d{2}\s?(AM|PM))"


class Pennsylvaniahouse:
    """
    This is a self contained scraper for the Pennsylvania House.

    Here is what the request is expect to look like
    {
        "geodicts": [
            {
                "schedule_type": "unique_pennsylvaniahouse",
                "url": "https://www.palegis.us/house/committees/meeting-schedule",
                "timezone": "America/New_York",
                "glitch_meetings": [],
                "debug": null,
                "channel_url": "https://www.youtube.com/@PaHouseVideo/streams,https://www.youtube.com/@pahousegop/streams" # This is a comma separated list of youtube urls
            }
        ],
        "version": "test"
    }
    """

    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        self.scrapper_api_key = os.getenv("SCRAPERAPICOM_API_KEY")
        self.stream_sources = [
            {
                "name": "youtube",
                "url": "https://www.youtube.com/@PaHouseVideo/streams",
            },
            {
                "name": "youtube",
                "url": "https://www.youtube.com/@pahousegop/streams",
            },
        ]

    def unique_pennsylvaniahouse(self, url: str, timezone: str) -> list:
        self.timezone = timezone
        schedule_meetings = []

        # Get page soup
        soup_string = self.scraper.scrape_html(
            schedule_type="unique_pennsylvaniahouse", url=url
        )
        soup = self.scraper.convert_to_soup(soup_string)

        # Parse the page to get meetings
        day_collections = soup.find_all("div", class_="mt-3 mb-1 meetings")
        for day_collection in day_collections:
            meeting_data = day_collection["data-date"]
            meetings = day_collection.find_all(
                "div",
                class_="mb-1 meeting-featured-info-alt rounded p-3 text-start meeting",
            )
            for meeting in meetings:

                # Get meeting time
                time_div = meeting.find(
                    "i", class_="fa-duotone fa-clock me-1 fa-lg"
                ).next_sibling.text.strip()
                if "call of chair" in time_div.lower():
                    meeting_date_time = "call of chair"
                elif "off the floor" in time_div.lower():
                    continue
                else:
                    datetime_string = f"{meeting_data} {time_div}"
                    datetime = parser.parse(datetime_string, fuzzy=True)
                    desired_format = TimeFormatter.desired_format()
                    converted_datetime = datetime.strftime(desired_format)
                    meeting_date_time = TimeFormatter(converted_datetime, self.timezone)
                    meeting_date_time = meeting_date_time.get_utc_time()

                # Get other meeting name
                title_div = meeting.find("div", class_="col-12 h5 mb-1 text-start")
                meeting_name = title_div.text.strip()

                # Add meeting to schedule meetings
                schedule_meetings.append(
                    {"name": meeting_name, "time": meeting_date_time}
                )

        # Get streamable meetings
        for source in self.stream_sources:
            if source["name"] == "youtube":
                youtube = Youtube()
                source_meetings = youtube.youtube_table(source["url"], self.timezone)

                # Fuzzy match schedule meetings with source meetings
                # If a match is found,
                # - add the source meeting to the scraped meetings
                # - remove the source meeting from the source meetings
                # - remove the schedule meeting from the schedule meetings

                for meeting in schedule_meetings:
                    for source_meeting in source_meetings:
                        fuzz_ratio = fuzz.token_set_ratio(
                            meeting["name"].lower(),
                            source_meeting["Meeting name"].lower(),
                        )
                        if fuzz_ratio > 85:
                            if meeting["time"] == "call of chair":
                                source_meet_datetime = parser.parse(
                                    source_meeting["Scheduled time"],
                                    fuzzy=True,
                                )
                                if (
                                    source_meet_datetime.date()
                                    != datetime.now(tz=pytz.UTC).date()
                                ):
                                    continue
                            elif source_meeting["Scheduled time"] != meeting["time"]:
                                continue

                            self.meetings.append(source_meeting)
                            schedule_meetings.pop(schedule_meetings.index(meeting))
                            source_meetings.pop(source_meetings.index(source_meeting))
                            break
        return self.meetings


if __name__ == "__main__":
    url = "https://www.palegis.us/house/committees/meeting-schedule"
    timezone = "America/New_York"
    schedule_type = "unique_pennsylvaniahouse"

    run_test(url=url, schedule_type=schedule_type, timezone=timezone)
