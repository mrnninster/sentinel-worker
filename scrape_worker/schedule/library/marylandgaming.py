import os
import sys
import pytz
import json
import logging
from dateutil import parser
from fuzzywuzzy import fuzz
from dotenv import load_dotenv
from urllib.parse import urlparse
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.youtube import Youtube
from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test


class Marylandgaming:
    """
    This is a unique scraper for the Maryland Gaming Commission schedule.

    Here is what the scraper is expected to look like

    Request sample
    -------------
        - refresh_schedule :
            ```
                {
                    "geodicts": [
                        {
                            'geoID': '1759442611715x428818637128192300',
                            'schedule_type': 'unique_marylandgaming',
                            'url': 'https://www.mdgaming.com/commission/meeting-minutes-documents/',
                            'agenda_url': '',
                            'timezone': 'America/New_York',
                            'glitch_meetings': [],
                            'debug': False,
                            'channel_url': 'https://www.youtube.com/@mdlottery/streams'
                        }
                    ],
                    "version": "test"
                }
            ```

        - stream_request:
            ```
                {
                    "schedule_url": "https://www.mdgaming.com/commission/meeting-minutes-documents/",
                    "stream_type": "streamlink",
                    "meeting_title": "sample meeting title",
                    "location": "Maryland",
                    "session_ID": "1750786200914x167237754162907970",
                    "timezone": "America/New_York",
                    "schedule_type": "unique_marylandgaming",
                    "demo_time_str": null,
                    "single_player_url": "",
                    "version": "test",
                    "glitch_meetings": [],
                    "meeting_id": "",
                    "passcode": "",
                    "dial_in_number": "",
                    "twilio_number": "+18882942357",
                    "is_restart": true,
                    "last_status": "Upcoming",
                    "channel_url": "https://www.youtube.com/@mdlottery/streams",
                    "test_stream_url": null,
                    "has_recess": false,
                    "youtube_restart_ID": "",
                    "detect_start_method": "calendar_detect",
                    "detect_end_method": "calendar_detect",
                    "detect_end_ocr_string": ""
                }
            ```
    """

    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()
        self.channel_url = "https://www.youtube.com/@mdlottery/streams"

    def unique_marylandgaming(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        current_date = datetime.now(tz=pytz.UTC)
        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        # Get live youtube meets
        live_youtube_meetings = []
        if self.channel_url:
            youtube = Youtube(url=self.channel_url, meeting_title="")
            if youtube.is_valid_youtube_streams_url():
                soup_str = self.scraper.scrape_html(url=self.channel_url)
                youtube_soup = self.scraper.convert_to_soup(soup_str)
                live_youtube_meetings = youtube.get_live_videos(youtube_soup)

        meeting_section = soup.find("section", {"id": "content", "role": "main"})
        meeting_h4_items = meeting_section.find_all("h4")
        for item in meeting_h4_items:
            status = "Upcoming"
            meeting_link = None
            item_string = item.text.strip()
            item_date = parser.parse(item_string, fuzzy=True)

            if item_date.date() >= current_date.date():
                meeting_page_url = item.find("a")["href"]

                page_soup_str = self.scraper.scrape_html(url=meeting_page_url)
                page_soup = self.scraper.convert_to_soup(page_soup_str)

                meeting_title = page_soup.find("h1", class_="entry-title").text.strip()
                # log.info(f"Meeting title: {meeting_title}")

                content = page_soup.find("section", class_="entry-content")
                content_paragraphs = content.find_all("p")

                datetime_paragraph = content_paragraphs[0].text.strip()
                # log.info(f"Datetime string: {datetime_paragraph}")
                datetime_obj = parser.parse(datetime_paragraph, fuzzy=True)
                meeting_date_time = datetime.strftime(
                    datetime_obj, TimeFormatter.desired_format()
                )
                utc_time = TimeFormatter(meeting_date_time, timezone).get_utc_time(
                    as_datetime=True
                )
                event_date = utc_time.isoformat().replace("+00:00", "Z")

                if len(content_paragraphs) > 1:
                    agenda_paragraph = content_paragraphs[1].find("a")
                    agenda_link = agenda_paragraph.get("href")
                    if agenda_link.startswith("http"):
                        agenda_link = agenda_link
                    else:
                        agenda_link = f"{domain}{agenda_link}"
                else:
                    agenda_link = None
                    meeting_link = None

                    # check for potential meeting link
                    check = datetime_paragraph.lower().strip().split("in person")
                    log.info(f"Check: {check}")
                    if len(check) > 1:
                        meeting_link = content_paragraphs[0].find("a").get("href")
                        meeting_link_parts = meeting_link.split("?feature")
                        if len(meeting_link_parts) > 1:
                            meeting_link = meeting_link_parts[0]

                meeting = {
                    "Meeting name": meeting_title,
                    "Scheduled time": event_date,
                    "Agenda link": agenda_link,
                    "Meeting link": meeting_link,
                    "Status": status,
                }
                self.meetings.append(meeting)

        # Adding Youtube links
        if live_youtube_meetings:
            for youtube_meet in live_youtube_meetings[:]:
                for meeting in self.meetings:
                    meet_title = meeting["Meeting name"]
                    meet_date = parser.parse(meeting["Scheduled time"])
                    if (
                        meet_date.date() == current_date.date()
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
    run_test(
        url="https://www.mdgaming.com/commission/meeting-minutes-documents/",
        schedule_type="unique_marylandgaming",
    )
