import re
import os
import sys
import pytz
import logging
from dateutil import parser
from fuzzywuzzy import fuzz
from datetime import datetime
from dotenv import load_dotenv
from dateutil.parser import parse


logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.youtube import Youtube
from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test


class Lacounty:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()
        self.channel_url = "https://www.youtube.com/@LACountyBOS/streams"

    def unique_lacounty(self, url, timezone="America/Los_Angeles"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        # Get live youtube streams
        soup_str = self.scraper.scrape_html(url=self.channel_url)
        youtube_soup = self.scraper.convert_to_soup(soup_str)

        youtube = Youtube(url=self.channel_url, meeting_title="")
        live_youtube_meetings = youtube.get_live_videos(youtube_soup)

        local_timezone = pytz.timezone(timezone)
        current_time = datetime.now(local_timezone)
        cards = soup.find("div", class_="cards-column one_column")
        if cards:
            for item in cards:
                meeting_datetime_string = item.find(
                    class_="card-text icon calendar-date"
                ).text
                meeting_datetime = parse(meeting_datetime_string)

                if meeting_datetime.date() >= current_time.date():
                    meeting_title = item.find("h4", class_="card-title").get_text(
                        strip=True
                    )
                    meeting_time = (
                        item.find("p", class_="clock-time")
                        .get_text(strip=True)
                        .split("\n")[0]
                    )
                    agenda_div = item.find("div", class_="agendaLinksSpacing")
                    agenda_link = agenda_div.find(
                        "a", class_="card-link primary-card-link"
                    ).get("href")
                    meeting_date_time = parser.parse(
                        f"{meeting_time} {meeting_datetime_string}"
                    )

                    meeting_date_time = datetime.strftime(
                        meeting_date_time, TimeFormatter.desired_format()
                    )
                    utc_time = TimeFormatter(meeting_date_time, timezone).get_utc_time(
                        as_datetime=True
                    )
                    schedule_time = utc_time.isoformat().replace("+00:00", "Z")
                    status = "Upcoming"

                    meeting = {
                        "Meeting name": meeting_title,
                        "Scheduled time": schedule_time,
                        "Meeting link": None,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }
                    self.meetings.append(meeting)

        # Match meet by date and title
        if live_youtube_meetings:
            for youtube_meet in live_youtube_meetings[:]:
                for meeting in self.meetings:
                    meet_title = meeting["Meeting name"]
                    meet_date = parser.parse(meeting["Scheduled time"])
                    if (
                        meet_date.date() == current_time.date()
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
                    if parser.parse(meeting["Scheduled time"]).date()
                    == current_time.date()
                ]
                if len(today_meetings) == 1 and len(live_youtube_meetings) == 1:
                    video_id = live_youtube_meetings[0]["video_id"]
                    meeting_to_update_index = self.meetings.index(today_meetings[0])
                    meeting_to_update = self.meetings[meeting_to_update_index]
                    meeting_to_update["Status"] = "In Progress"
                    meeting_to_update["Meeting link"] = (
                        f"https://www.youtube.com/watch?v={video_id}"
                    )

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://bos.lacounty.gov/board-meeting-agendas/",
        schedule_type="unique_lacounty",
        timezone="America/Los_Angeles",
        get_full_archive_flag=False,
    )
