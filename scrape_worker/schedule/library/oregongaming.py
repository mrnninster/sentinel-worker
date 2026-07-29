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


class Oregongaming:
    """
    This scraper handles the request for the oregon gaming comission schedule.
    Here is what the request is expect to look like
    {
        "geodicts": [
            {
                "schedule_type": "unique_oregongaming",
                "url": "https://www.oregonlottery.org/about/how-we-operate/commission-and-director-info/",
                "timezone": "America/Los_Angeles",
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
        self.channel_url = "https://www.youtube.com/@oregonlottery/streams"

    def unique_oregongaming(self, url, timezone="America/Los_Angeles"):
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
        calendar_events_section = soup.find_all(
            "div", class_="ol-typography orl-supporting-calibri-regular"
        )
        for calendar_event in calendar_events_section:
            meeting_date_section = calendar_event.find("ul")
            if meeting_date_section:
                meeting_dates = meeting_date_section.find_all("li")

                for meeting_date in meeting_dates:
                    meeting_a = meeting_date.find("a")
                    if not meeting_a:
                        continue
                    meeting_href = meeting_a.get("href")
                    if not meeting_href:
                        continue
                    meeting_page_link = f"{self.base_url}{meeting_href}"
                    log.info(f"Meeting page link => {meeting_page_link}")

                    # Get meeting soup
                    meeting_page_soup_str = self.scraper.scrape_html(
                        url=meeting_page_link
                    )
                    meeting_page_soup = self.scraper.convert_to_soup(
                        meeting_page_soup_str
                    )
                    meeting_datetime_tag = meeting_page_soup.find(
                        "h1", class_="ol-headline orl-headline-nexa-regular"
                    )
                    if not meeting_datetime_tag:
                        log.warning(f"No datetime tag found at {meeting_page_link}")
                        continue
                    datetime_str = meeting_datetime_tag.text.strip()
                    log.info(f"Datetime str => {datetime_str}")

                    # Get meeting name
                    event_name = "Oregon Lottery Commission Meeting"
                    status = "Upcoming"
                    agenda_link = None
                    meeting_link = None

                    datetime_obj = parser.parse(datetime_str, fuzzy=True)
                    meeting_date_time = datetime.strftime(
                        datetime_obj, TimeFormatter.desired_format()
                    )
                    utc_time = TimeFormatter(meeting_date_time, timezone).get_utc_time(
                        as_datetime=True
                    )
                    event_date = utc_time.isoformat().replace("+00:00", "Z")

                    meeting = {
                        "Meeting name": event_name,
                        "Scheduled time": event_date,
                        "Agenda link": agenda_link,
                        "Meeting link": meeting_link,
                        "Status": status,
                    }

                    self.meetings.append(meeting)
                break

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

        # log.info(f"Meetings => {self.meetings}")
        return self.meetings


if __name__ == "__main__":
    url = "https://www.oregonlottery.org/about/how-we-operate/commission-and-director-info/"
    timezone = "America/Los_Angeles"
    schedule_type = "unique_oregongaming"
    run_test(url=url, timezone=timezone, schedule_type=schedule_type)
