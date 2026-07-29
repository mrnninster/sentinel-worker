import os
import re
import sys
import pytz
import logging
from fuzzywuzzy import fuzz
from dateutil import parser
from datetime import datetime
from dotenv import load_dotenv
from urllib.parse import urlparse

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.youtube import Youtube
from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Newjerseygaming:
    """
    This scraper handles the request for the New Jersey gaming comission schedule.
    Here is what the request is expect to look like
    {
        "geodicts": [
            {
                "schedule_type": "unique_newjerseygaming",
                "url": "https://www.nj.gov/casinos/services/meetings/2025/approved/meeting_archive.html",
                "timezone": "America/New_York",
                "glitch_meetings": [],
                "debug": null,
                "channel_url": ""
            }
        ],
        "version": "test"
    }
    """

    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()
        self.channel_url = "https://www.youtube.com/@NJCCC/streams"

    def has_meaningful_time(self, time_str):
        """Check if string contains a meaningful time (not just default midnight)"""
        if not time_str or not time_str.strip():
            return False

        try:
            parsed_time = parser.parse(time_str, fuzzy=True, ignoretz=True)
            # Check if time is not the default midnight
            return parsed_time.time() != datetime.min.time()
        except (ValueError, TypeError, OverflowError):
            return False

    def unique_newjerseygaming(self, url, timezone):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        # Get live youtube meets
        live_youtube_meetings = []
        if self.channel_url:
            youtube = Youtube(url=self.channel_url, meeting_title="")
            if youtube.is_valid_youtube_streams_url():
                soup_str = self.scraper.scrape_html(url=self.channel_url)
                youtube_soup = self.scraper.convert_to_soup(soup_str)
                live_youtube_meetings = youtube.get_live_videos(youtube_soup)

        # Get calendar events
        calendar_event_section = soup.find("ul", class_="agenda striped")
        calendar_event_list = calendar_event_section.find_all("li")
        for calendar_event in calendar_event_list:

            # Base event details
            agenda_link = None
            meeting_link = None
            status = "Upcoming"
            time_element = None
            domain = urlparse(url).scheme + "://" + urlparse(url).netloc
            meeting_name = "New Jersey Casino Control Commission"

            # Process date element
            date_element = calendar_event.find("div", class_="datetime")
            month = date_element.find("div", class_="top").text.strip()
            day = date_element.find("p", class_="day").text.strip()
            year = date_element.find("p", class_="yr").text.strip()
            event_date = f"{month} {day}, {year}"

            # Process date element
            date_element = calendar_event.find("h3", class_="date")
            if date_element:
                has_meaningful_time = self.has_meaningful_time(
                    date_element.text.strip()
                )
                if has_meaningful_time:
                    date_element = date_element.text.strip()
                    date_element = parser.parse(date_element)
                    time_element = date_element.time()
                    time_element_str = time_element.strftime("%I:%M %p")

            if time_element:
                datetime_str = f"{event_date} {time_element_str}"
            else:
                datetime_str = f"{event_date} 10:30 AM"

            datetime_obj = parser.parse(datetime_str)
            meeting_date_time = datetime.strftime(
                datetime_obj, TimeFormatter.desired_format()
            )
            utc_time = TimeFormatter(meeting_date_time, timezone).get_utc_time(
                as_datetime=True
            )
            event_date = utc_time.isoformat().replace("+00:00", "Z")

            # Event element
            link_elements = calendar_event.find_all("a")
            for link_element in link_elements:
                if link_element.text.lower().strip() == "agenda":
                    agenda_link = link_element.get("href")
                    agenda_link = f"{domain}{agenda_link}"

            meeting = {
                "Meeting name": meeting_name,
                "Scheduled time": event_date,
                "Agenda link": agenda_link,
                "Meeting link": meeting_link,
                "Status": status,
            }

            self.meetings.append(meeting)

        # Get current time in UTC
        current_date = datetime.now(pytz.UTC).date()

        # Adding Youtube links
        if live_youtube_meetings:
            for youtube_meet in live_youtube_meetings[:]:
                for meeting in self.meetings:
                    meet_title = meeting["Meeting name"]
                    meet_date = parser.parse(meeting["Scheduled time"])
                    if (
                        meet_date.date() == current_date
                        and fuzz.token_set_ratio(
                            youtube_meet["video_title"], meet_title
                        )
                        > 85
                    ):
                        meeting["Status"] = "In Progress"
                        meeting["Meeting link"] = (
                            f"https://www.youtube.com/watch?v={youtube_meet['video_id']}"
                        )
                        live_youtube_meetings.remove(youtube_meet)
                        break

            # if there is only 1 live stream and 1 expected meet today
            in_progress_meetings = [
                meeting
                for meeting in self.meetings
                if meeting["Status"] == "In Progress"
            ]
            if not in_progress_meetings:
                today_meetings = [
                    meeting
                    for meeting in self.meetings
                    if parser.parse(meeting["Scheduled time"]).date() == current_date
                ]
                if len(today_meetings) == 1 and len(live_youtube_meetings) == 1:
                    video_id = live_youtube_meetings[0]["video_id"]
                    meeting_index = self.meetings.index(today_meetings[0])
                    self.meetings[meeting_index]["Status"] = "In Progress"
                    self.meetings[meeting_index][
                        "Meeting link"
                    ] = f"https://www.youtube.com/watch?v={video_id}"
        return self.meetings


if __name__ == "__main__":
    url = "https://www.nj.gov/casinos/services/meetings/2025/approved/meeting_archive.html"
    timezone = "America/New_York"
    schedule_type = "unique_newjerseygaming"
    run_test(url=url, timezone=timezone, schedule_type=schedule_type)
