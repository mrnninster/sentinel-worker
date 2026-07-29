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


class Pennsylvaniagaming:
    """
    This scraper handles the request for the Pennsylvania Gaming Control Board schedule.
    Here is what the request is expected to look like

    {
        "geodicts": [
            {
                "schedule_type": "unique_pennsylvaniagaming",
                "url": "https://gamingcontrolboard.pa.gov/about/board-meeting-calendar",
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
        self.channel_url = "https://www.youtube.com/@pagamingcontrolboard9753/streams"

    def unique_pennsylvaniagaming(self, url, timezone="America/New_York"):
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)
        # Get base url
        self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc

        # Get live youtube meets
        live_youtube_meetings = []
        if self.channel_url:
            youtube = Youtube(url=self.channel_url, meeting_title="")
            if youtube.is_valid_youtube_streams_url():
                soup_str = self.scraper.scrape_html(url=self.channel_url)
                youtube_soup = self.scraper.convert_to_soup(soup_str)
                live_youtube_meetings = youtube.get_live_videos(youtube_soup)

        # Get calendar events
        calendar_events_section = soup.find_all("div", class_="container")
        for calendar_event in calendar_events_section:
            event_container = calendar_event.find("div", class_="row-fluid")
            if event_container:
                month = event_container.find("h3").text.strip()
                dates = event_container.find_all("div", class_="single-event-container")
                for date in dates:
                    meeting_link = (
                        date.find("div", class_="button-content view-event-button")
                        .find("a")
                        .get("href")
                    )
                    meeting_link = self.base_url + meeting_link

                    # Get meeting soup
                    page_soup_str = self.scraper.scrape_html(url=meeting_link)
                    page_soup = self.scraper.convert_to_soup(page_soup_str)

                    # Get meeting datetime
                    meeting_date = page_soup.find(
                        "div", class_="meeting-node-date"
                    ).text.strip()
                    meeting_time = page_soup.find(
                        "div", class_="meeting-node-time"
                    ).text.strip()
                    meeting_datetime = f"{meeting_date} {meeting_time}"
                    datetime_obj = parser.parse(meeting_datetime, fuzzy=True)
                    meeting_date_time = datetime.strftime(
                        datetime_obj, TimeFormatter.desired_format()
                    )
                    utc_time = TimeFormatter(meeting_date_time, timezone).get_utc_time(
                        as_datetime=True
                    )
                    event_date = utc_time.isoformat().replace("+00:00", "Z")

                    # Get meeting name
                    meeting_name = page_soup.find(
                        "div", "meeting-node-type"
                    ).text.strip()
                    if not meeting_name:
                        meeting_name = "PGCB Public Board Meeting"

                    # Set agenda link
                    agenda_link = None
                    download_node = page_soup.find_all(
                        "div", class_="meeting-node-download-container"
                    )
                    for download in download_node:
                        title_options = [
                            "Download File Public Meeting Agenda",
                            "Download File Meeting Agenda",
                        ]
                        for title in title_options:
                            if title == download.text.strip():
                                agenda_href = download.find("a").get("href")
                                agenda_link = f"{self.base_url}{agenda_href}"
                                break

                    # Set meeting link
                    meeting_link = None

                    # Set meeting status
                    status = "Upcoming"

                    meeting = {
                        "Meeting name": meeting_name,
                        "Scheduled time": event_date,
                        "Meeting link": meeting_link,
                        "Agenda link": agenda_link,
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
    url = "https://gamingcontrolboard.pa.gov/about/board-meeting-calendar"
    timezone = "America/New_York"
    schedule_type = "unique_pennsylvaniagaming"
    run_test(url=url, timezone=timezone, schedule_type=schedule_type)
