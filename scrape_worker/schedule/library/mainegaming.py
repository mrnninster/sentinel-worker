import os
import re
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


from utils.scrape_html import HtmlScraper


class Mainegaming:
    """
    This is the scraper for the maine gambling control board.
    This scraper return only 1 meeting since the page only
    indicates when the next meeting will be.

        Request sample
    -------------
        - refresh_schedule :
            ```
                {
                    "geodicts": [
                        {
                            "schedule_type": "unique_mainegaming",
                            "url": "https://www.maine.gov/dps/gcu/gambling-control-board/board-meetings-and-minutes",
                            "timezone": "America/New_York",
                            "glitch_meetings": [],
                            "debug": null,
                            "channel_url": ""
                        }
                    ],
                    "version": "test"
                }
            ```
        - stream_request:
            ```
                {
                    "schedule_url": "https://www.maine.gov/dps/gcu/gambling-control-board/board-meetings-and-minutes",
                    "stream_type": "twilio_phone_no_code",
                    "meeting_title": "sample meeting title",
                    "location": "Maine",
                    "session_ID": "1750786200914x167237754162907970",
                    "timezone": "America/New_York",
                    "schedule_type": "unique_mainegaming",
                    "demo_time_str": null,
                    "single_player_url": "",
                    "version": "test",
                    "glitch_meetings": [],
                    "meeting_id": "MEETING ID",
                    "passcode": "",
                    "dial_in_number": "DIAL IN NUMBER",
                    "twilio_number": "+18882942357",
                    "is_restart": false,
                    "last_status": "Upcoming",
                    "channel_url": "",
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

    def unique_mainegaming(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        # Initialize variables
        meeting_id = None
        agenda_link = None
        access_code = None
        meeting_link = None
        phone_number = None
        status = "Upcoming"
        meeting_name = "Maine Gambling Control Board Meeting"
        self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc

        # Get page content
        page_content = soup.find(
            "div", {"id": "maincontent2", "role": "main", "class": "article"}
        )

        # Get meeting date
        plain_text_content = page_content.find_all(
            "p", class_="plain text-align-center"
        )
        meeting_date = parser.parse(plain_text_content[0].text.strip(), fuzzy=True)
        meeting_date_time = datetime.strftime(
            meeting_date, TimeFormatter.desired_format()
        )
        utc_time = TimeFormatter(meeting_date_time, timezone).get_utc_time(
            as_datetime=True
        )
        event_date = utc_time.isoformat().replace("+00:00", "Z")

        # Get meeting id and phone number
        all_plain_contents = page_content.find_all("p", class_="plain")
        relevant_text = all_plain_contents[-1].text.strip()
        # log.info(f"Relevant text: {relevant_text}")

        # Extract phone number (digits only)
        phone_match = re.search(r"1[-\s]?\d{3}[-\s]?\d{3}[-\s]?\d{4}", relevant_text)
        phone_number = phone_match.group().replace(" ", "").replace("-", "")
        log.info(f"Phone match: {phone_number}")

        # Extract meeting ID (digits only)
        meeting_id_match = re.search(r"\d{9}", relevant_text)
        if meeting_id_match:
            meeting_id = meeting_id_match.group()

        # Get meeting agenda from the first table
        meeting_table = page_content.find("table", class_="tbstriped meetingtable")
        meeting_body = meeting_table.find("tbody")
        meeting_rows = meeting_body.find_all("tr")
        for row in meeting_rows:
            row_data = row.find_all("td")
            relevant_row = row_data[0]
            row_link = relevant_row.find("a")
            if row_link:
                row_link_date = parser.parse(row_link.text.strip(), fuzzy=True)
                if row_link_date.date() == utc_time.date():
                    agenda_link = f"{self.base_url}{row_link['href']}"

        # Set meeting
        meeting = {
            "Status": status,
            "Access ID": meeting_id,
            "Passcode": access_code,
            "Agenda link": agenda_link,
            "Meeting name": meeting_name,
            "Scheduled time": event_date,
            "Meeting link": meeting_link,
            "Phone number": phone_number,
            "Stream type": "twilio_phone_no_code",
        }

        self.meetings.append(meeting)
        return self.meetings


if __name__ == "__main__":
    schedule_type = "unique_mainegaming"
    timezone = "America/New_York"
    url = "https://www.maine.gov/dps/gcu/gambling-control-board/board-meetings-and-minutes"
    run_test(url=url, timezone=timezone, schedule_type=schedule_type)
