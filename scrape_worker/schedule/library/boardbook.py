import re
import os
import sys
import pytz
import logging

from dateutil import parser
from fuzzywuzzy import fuzz
from dotenv import load_dotenv
from urllib.parse import urlparse
from datetime import datetime

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.youtube import Youtube
from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test
from schedule.library.youtube import Youtube as YoutubeScraper

FUZZY_MATCH_THRESHOLD = 85


class Boardbook:
    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        self.channel_url = os.getenv("ARG_CHANNEL_URL")

    def boardbook_table(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        table = soup.find("table")
        now = datetime.now(tz=pytz.UTC)
        rows = table.find_all("tr", class_="row-for-board")

        # Current time in UTC
        current_datetime = datetime.now(tz=pytz.UTC)

        # Get live youtube meets
        live_youtube_meetings = []
        if self.channel_url:
            youtube = Youtube(url=self.channel_url, meeting_title="")
            if youtube.is_valid_youtube_streams_url():
                soup_str = self.scraper.scrape_html(url=self.channel_url)
                youtube_soup = self.scraper.convert_to_soup(soup_str)
                live_youtube_meetings = youtube.get_live_videos(youtube_soup)

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc
        for row in rows:
            columns = row.find_all("td")
            name_column = columns[0]
            div_elements = name_column.find_all("div")

            for div in div_elements:
                # Default status
                status = "Upcoming"

                if not div.has_attr("class"):
                    data = div.get_text(strip=True)

                    # Define a regex pattern to extract date, time, and meeting description
                    pattern = r"(?P<meeting_date>[a-zA-Z]+\s+\d{1,2},\s+\d{4}) at (?P<meeting_time>\d{1,2}:\d{2} [APMapm]{2}) - (?P<meeting_description>.+)"

                    # Match the pattern in the text
                    match = re.match(pattern, data)

                    # Check if a match is found
                    if match:
                        # Generate datetime from the match
                        meeting_date = match.group("meeting_date")

                        # Skip cancelled meetings
                        date_match = re.search(
                            r"Cancel(?:led|ed)(?=\b|[A-Z])",
                            meeting_date,
                            re.IGNORECASE,
                        )
                        if date_match:
                            continue

                        meeting_time = match.group("meeting_time")
                        raw_meeting_datetime = f"{meeting_date} {meeting_time}"

                        datetime_obj = parser.parse(raw_meeting_datetime, fuzzy=True)
                        meeting_date_time = datetime.strftime(
                            datetime_obj, TimeFormatter.desired_format()
                        )
                        utc_time = TimeFormatter(
                            meeting_date_time, timezone
                        ).get_utc_time(as_datetime=True)
                        event_date = utc_time.isoformat().replace("+00:00", "Z")

                        # Get the meeting name
                        meeting_name = match.group("meeting_description")

                        # Skip cancelled meetings
                        if re.search(
                            r"Cancel(?:led|ed)(?=\b|[A-Z])",
                            meeting_name,
                            re.IGNORECASE,
                        ):
                            continue

                        if utc_time > current_datetime:
                            agenda_link = None
                            try:
                                agenda_tag = columns[2]
                                path = agenda_tag.find("a").get("href")
                                agenda_page_url = domain + path
                                soup_new = self.scraper.fetch_with_bs(agenda_page_url)
                                soup_new = self.scraper.convert_to_soup(string=soup_new)
                                agenda_tag = soup_new.find(
                                    "div", id="AgendaItemViewOptions-QuickView"
                                )
                                link = (
                                    agenda_tag.find("a").get("href")
                                    if agenda_tag is not None
                                    else None
                                )
                                agenda_link = (domain + link) if agenda_tag else None
                            except Exception as e:
                                log.warning(
                                    f"Error fetching agenda link: {e}",
                                    exc_info=True,
                                )

                            meeting_link = None
                            self.meetings.append(
                                {
                                    "Meeting name": meeting_name,
                                    "Scheduled time": event_date,
                                    "Meeting link": meeting_link,
                                    "Agenda link": agenda_link,
                                    "Status": status,
                                }
                            )

        # Get upcoming youtube meets
        upcoming_youtube_meetings = []
        if self.channel_url:
            ytScraper = YoutubeScraper()
            upcoming_youtube_meetings = ytScraper.youtube_table(
                self.channel_url, timezone
            )
        log.info(f"Upcoming youtube meetings: {upcoming_youtube_meetings}")

        # Add upcoming youtube meets to the meetings
        if upcoming_youtube_meetings:
            self.meetings.extend(upcoming_youtube_meetings)

        # Adding Youtube live links
        if live_youtube_meetings:
            for youtube_meet in live_youtube_meetings[:]:
                for meeting in self.meetings:
                    meet_title = meeting["Meeting name"]
                    meet_date = parser.parse(meeting["Scheduled time"])
                    if (
                        meet_date.date() == current_datetime.date()
                        and fuzz.token_set_ratio(
                            youtube_meet["video_title"], meet_title
                        )
                        > FUZZY_MATCH_THRESHOLD
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
                    == current_datetime.date()
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
    # run_test(url="https://meetings.boardbook.org/Public/Organization/964", timezone="America/Chicago", schedule_type="boardbook_table")
    run_test(
        url="https://meeting.boeconnect.net/Public/Organization/570",
        timezone="America/Chicago",
        schedule_type="boardbook_table",
    )
