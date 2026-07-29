import os
import sys
import re
import pytz
import logging
from fuzzywuzzy import fuzz
from dateutil import parser
from datetime import datetime
from dotenv import load_dotenv
from urllib.parse import urljoin

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.youtube import Youtube
from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test



class Valottery:
    """
    This is a scraper for the Virginia Lottery Board Meetings.
    
    Here is what the request is expect to look like
    - refresh_schedule :
    ```
    {
        "geodicts": [
            {
                "geoID": "1700005967507x454349218326133300",
                "schedule_type": "unique_valottery",
                "url": "https://valottery.com/aboutus/leadership#meetings",
                "timezone": "America/New_York",
                "glitch_meetings": [],
                "debug": false,
                "channel_url": ""
            }
        ],
        "version": "test"
    }
    ```
    - stream_request:
    ```
        {
            "last_status": "Upcoming",
            "schedule_type": "unique_valottery",
            "location": "Virginia",
            "session_ID": "1744290001855x719025355965829900",
            "demo_time_str": null,
            "version": "test",
            "stream_type": "streamlink",
            "detect_start_method": "calendar_detect",
            "detect_start_ocr_string": "",
            "detect_end_method": "calendar_detect",
            "detect_end_ocr_string": "",
            "meeting_title": "Virginia Lottery Board Meetings",
            "glitch_meetings": [],
            "has_recess": false,
            "is_restart": false,
            "schedule_url": "https://valottery.com/aboutus/leadership#meetings",
            "timezone": "America/New_York",
            "single_player_url": "",
            "test_stream_url": null,
            "channel_url": "",
            "meeting_id": "string",
            "passcode": "string",
            "dial_in_number": "string",
            "twilio_number": "string",
            "dail_in_wait_time": "8",
            "youtube_restart_ID": "string"
        }
    ```
    """
    
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()
        self.default_time = "9:30 am"
        self.meeting_name = "Virginia Lottery Board Meetings"
        self.channel_url = "https://www.youtube.com/@VirginiaLottery/streams"
        

    def parse_date_and_url(self, li_html: str, li_element=None):
        """
        Extract date, time, and URL from an <li> item.

        Args:
            li_html: The HTML string for an <li> element.
            li_element: Optional BeautifulSoup element for safer extraction.

        Returns:
            tuple: (date_or_None, time_or_None, url_or_None)
        """

        # -----------------------------
        # URL EXTRACTION
        # -----------------------------
        url_string = None

        # 1. Extract URL from <a> tag via BeautifulSoup
        if li_element:
            a_tag = li_element.find("a")
            if a_tag:
                url_string = a_tag.get("href")

        # 2. fallback: href="url"
        if not url_string:
            href_match = re.search(r'href="([^"]+)"', li_html)
            if href_match:
                url_string = href_match.group(1)

        # 3. fallback: plain text URLs
        if not url_string:
            url_match = re.search(r'(https?://[^\s<>"]+)', li_html)
            if url_match:
                url_string = url_match.group(1)
                
        # After extracting url_string
        if url_string and not url_string.startswith('http'):
            url_string = urljoin(url, url_string)

        # -----------------------------
        # TEXT EXTRACTION
        # -----------------------------
        if li_element:
            text = li_element.get_text(" ", strip=True)
        else:
            text = re.sub(r'<[^>]+>', ' ', li_html)
            text = " ".join(text.split())  # normalize whitespace

        # -----------------------------
        # DATE + OPTIONAL TIME
        # -----------------------------
        # Examples:
        #   "Tuesday, January 14, 2025"
        #   "Tuesday, January 14, 2025, at 1:30 pm"

        date_with_time_pattern = (
            r"((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
            r"[A-Za-z]+\s+\d{1,2},?\s+\d{4})"
            r"(?:,\s+at\s+(\d{1,2}:\d{2}\s+(?:am|pm)))?"
        )

        match = re.search(date_with_time_pattern, text, re.IGNORECASE)

        if not match:
            return None, None, url_string  # No date found

        date_text = match.group(1)
        time_string = match.group(2)  # may be None
        if not time_string:
            time_string = self.default_time

        return date_text, time_string, url_string


    def unique_valottery(self, url: str, timezone: str = "America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        current_datetime = datetime.now(tz=pytz.UTC)
        content = soup.find("div", class_="card-content")
        
        schedules = content.find_all("ul")
        # log.info(f"Schedules: {schedules}")
        
        # Get live youtube meets
        live_youtube_meetings = []
        if self.channel_url:
            youtube = Youtube(url=self.channel_url, meeting_title="")
            if youtube.is_valid_youtube_streams_url():
                soup_str = self.scraper.scrape_html(url=self.channel_url)
                youtube_soup = self.scraper.convert_to_soup(soup_str)
                live_youtube_meetings = youtube.get_live_videos(youtube_soup)
        
        # Loop through the schedules
        for schedule in schedules:
            items = schedule.find_all("li")
            for item in items:
                date_string, time_string, url_string = self.parse_date_and_url(str(item), item)
                if date_string is None or time_string is None:
                    continue
                # log.info(f"Date: {date_string}, Time: {time_string}, URL: {url_string}")
                
                date_time_string = f"{date_string} {time_string}"
                date_time = parser.parse(date_time_string, fuzzy=True)
                date_time = datetime.strftime(date_time, TimeFormatter.desired_format())
                meeting_datetime = TimeFormatter(date_time, timezone).get_utc_time(as_datetime=True)
                
                # If the meeting is in the future, continue
                if current_datetime > meeting_datetime:
                    continue
                
                # set the meeting time in UTC
                schedule_time = meeting_datetime.isoformat().replace("+00:00", "Z")
                
                # Create the meeting object
                meeting = {
                    "Meeting name": self.meeting_name,
                    "Scheduled time": schedule_time,
                    "Meeting link": url_string,
                    "Agenda link": None,
                    "Status": "Upcoming",
                }
                
                # Add the meeting to the list
                self.meetings.append(meeting)
        
        # Adding Youtube links
        current_date = current_datetime.date()
        
        # Find schdule meetings for the current date
        current_date_meetings_index = [
            index for index, meeting in enumerate(self.meetings) if parser.parse(meeting["Scheduled time"]).date() == current_date
        ]
        
        # If there is only one schedule meeting and one live video, update meeting in self.meetings
        if len(current_date_meetings_index) == 1 and len(live_youtube_meetings) == 1:
            self.meetings[current_date_meetings_index[0]]["Status"] = "In Progress"
            self.meetings[current_date_meetings_index[0]]["Meeting link"] = f"https://www.youtube.com/watch?v={live_youtube_meetings[0]['video_id']}"
            live_youtube_meetings.remove(live_youtube_meetings[0])
        
        elif len(current_date_meetings_index) > 1 or len(live_youtube_meetings) > 1:
            log.info(f"Multiple schedule meetings or live youtube meetings detected")
            log.info(f"No useable detection method available")
        return self.meetings

if __name__ == "__main__":

    url = f"https://valottery.com/aboutus/leadership#meetings"
    timezone = "America/New_York"
    schedule_type = "unique_valottery"
    run_test(url, timezone, schedule_type)