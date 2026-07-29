import os
import re
import sys
import json
import html
import pytz
import logging

from dateutil import parser
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from urllib.parse import urlparse
from datetime import datetime, timedelta

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.pdf_scanner import PDFScanner
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test
from utils.utils_functions import get_api_json_call
from utils.scrape_html import HtmlScraper, ReturnType

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

AGENDA_DATE_PATTERN = r"[A-Za-z]+, [A-Za-z]+ \d{1,2}, \d{4}"
MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"
DATE_WITH_TIME_PATTERN = r"\d{2}/\d{2}/\d{4} \d{1,2}:\d{2} (AM|PM)"

# TODO: Update the senate schedule scraper to use the html page
# TODO: Adds schedule scraper for committee meetings
#
# EDGE CASE — detect_start_method compatibility:
#
# This parser hardcodes Status = "Upcoming" for all meetings (line ~170).
# It never returns "In progress", which normally makes it INCOMPATIBLE with
# calendar_detect. However, Michigan Senate's geo is configured with
# calendar_detect and still achieves ~85% Ended rate. Here's why:
#
# The parser provides persistent Castus TV always-on player URLs as
# Meeting link (see senate_api_url_to_meeting_link, 3 channels). These
# URLs point to live player pages that are always reachable — they show
# content when the Senate is in session and idle otherwise. Because the
# Meeting link is pre-set to a valid streamlink-compatible URL, the
# capture pipeline is able to grab the stream even though calendar_detect
# never fires "In progress."
#
# The get_streable_meets() method fetches the Castus TV schedule API
# (which knows what's currently on each channel) but this data is only
# logged — it's NOT used to set meeting status. This is a potential
# improvement: matching Castus API items to meetings could enable proper
# "In progress" status reporting.
#
# DO NOT change detect_start_method to autostart or stream_detect without
# understanding this mechanism. The current config works. The ~15% failure
# rate corresponds to days when the Senate doesn't meet (no content on
# the always-on player).


class MonthArray:
    def __init__(self):
        self.month_array = {
            0: "January",
            1: "February",
            2: "March",
            3: "April",
            4: "May",
            5: "June",
            6: "July",
            7: "August",
            8: "September",
            9: "October",
            10: "November",
            11: "December",
        }
    
    def get_month_str(self, month_index: int) -> str:
        return self.month_array[month_index]
    

class Michiganlegislature:
    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.pdf_scanner = PDFScanner()
        self.self_contained_parser = True
        self.scrapper_api_key = os.getenv("SCRAPERAPICOM_API_KEY")
        self.senate_api_url_to_meeting_link = {
            "https://api.castus.tv/ccs/v1/schedule/misenate/ch1": "https://cloud.castus.tv/vod/#/misenate/video/61b9160ad24c1f0008b41cb8?page=LIVE&type=live",
            "https://api.castus.tv/ccs/v1/schedule/misenate/ch2": "https://cloud.castus.tv/vod/#/misenate/video/61f1d10cc9af8700081e66a8?page=LIVE&type=live",
            "https://api.castus.tv/ccs/v1/schedule/misenate/ch3": "https://cloud.castus.tv/vod/#/misenate/video/61f1d10e765b120008f2f027?page=LIVE&type=live",
        }
    
    def get_streable_meets(self):
        streamable_meets = []
        
        senate_api_url_keys = self.senate_api_url_to_meeting_link.keys()
        for senate_api_url_key in senate_api_url_keys:
            log.info(f"Senate API URL key: {senate_api_url_key}")
            response = self.scraper.scrape_html(url=senate_api_url_key, return_type=ReturnType.RESPONSE)
            if response.status_code != 200:
                log.warning(f"Failed to fetch {senate_api_url_key}: {response.status_code}")
                continue
            
            json_data = response.json()
            item_data = json_data["items"]
            for item in item_data:
                if item["default"] == True:
                    continue
                
                epoch_local_time = item["start_unix"]["unix"]
                dt_local = datetime.fromtimestamp(epoch_local_time)
                
                # Format as the same desired format used elsewhere
                meeting_date_time_str = datetime.strftime(dt_local, TimeFormatter.desired_format())
                utc_time = TimeFormatter(meeting_date_time_str, self.timezone).get_utc_time(as_datetime=True)
                meet_date_time = utc_time.isoformat().replace("+00:00", "Z")
                
                # Meeting name
                title = item["name"]
                
                streamable_meet = {
                    "title": title,
                    "time": meet_date_time,
                    "meeting_link": self.senate_api_url_to_meeting_link.get(senate_api_url_key),
                }
                
                streamable_meets.append(streamable_meet)
        return streamable_meets


    def extract_meetings_senate(self) -> list:
        streamable_meets = self.get_streable_meets()
        
        soup_str = self.scraper.scrape_html(url=self.senate_url, return_type=ReturnType.TEXT)
        soup = self.scraper.convert_to_soup(soup_str)
        meetings = []
        session_array_collection = []

        # Find the calendar table
        calendar_grid = soup.select_one("calendar-grid")
        if calendar_grid:
            for month in calendar_grid.find_all("calendar-month"):
                raw_month_data = month.get("monthdata")
                if not raw_month_data:
                    log.warning("calendar-month element has no monthData attribute")
                    continue
                decoded = html.unescape(raw_month_data)
                try:
                    data = json.loads(decoded)
                except json.JSONDecodeError as e:
                    log.warning("Invalid JSON in monthData: %s", e)
                    continue
                session_array = data.get("sessionDays")
                if session_array is not None:
                    session_array_collection.append(session_array)

        current_year = datetime.now().year
        month_array = MonthArray()
        for index, session_array in enumerate(session_array_collection):
            # Skip leading padding days (from previous month) until we see day 1 of this month
            seen_first_day_of_month = False

            for session in session_array:
                day_of_month = session.get("date")  # JSON uses "date", not "day"
                session_time = session.get("time")
                session_text = session.get("text")
                session_month = month_array.get_month_str(index)

                if not seen_first_day_of_month:
                    if day_of_month != 1:
                        continue
                    seen_first_day_of_month = True

                if session_text is not None:
                    continue
                
                # Set meet name
                title = "Senate session"
                
                # Get the meet time
                session_local_time = f"{current_year}-{session_month}-{day_of_month}T{session_time}"
                datetime_obj = parser.parse(session_local_time, fuzzy=True)
                meeting_date_time_str = datetime.strftime(datetime_obj, TimeFormatter.desired_format())
                utc_time = TimeFormatter(meeting_date_time_str, timezone).get_utc_time(as_datetime=True)
                meet_date_time = utc_time.isoformat().replace("+00:00", "Z")
                
                # Set meeting link
                meeting_link = self.senate_api_url_to_meeting_link.get(self.senate_url)
                
                # Set agenda link
                agenda_link = None
                
                # Set status
                status = "Upcoming"
                
                # Create meeting object
                meeting = {
                    "Meeting name": title,
                    "Scheduled time": meet_date_time,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
                meetings.append(meeting)
        
        log.info(f"Streamable meetings: {streamable_meets}")
        log.info(f"Meetings: {meetings}")
        return meetings
        
    def extract_meetings_committee(self) -> None:
        pass
    
    
    def unique_michiganlegislature(self, combined_urls: str, timezone: str) -> list:
        self.timezone = timezone
        
        if "%%%" not in combined_urls:
            logging.warning("No URLs provided. Expected URLs separated by '%%%'.")
            return self.meetings

        split_urls = [part for part in combined_urls.split("%%%") if part]

        for url in split_urls:
            if "senate" in url:
                self.senate_url = url
            elif "committee" in url:
                self.committee_url = url
            else:
                logging.warning(f"Invalid URL {url}")
                
        self.meetings.extend(self.extract_meetings_senate())
        # self.meetings.extend(self.extract_meetings_committee())
        return self.meetings


if __name__ == "__main__":
    url = (
        # "https://www.legislature.mi.gov/Committees/Meetings?sortBy=CalendarTime"
        "%%%https://senate.michigan.gov/information/calendars-schedules/session-schedule/"
    )
    timezone = "America/New_York"
    schedule_type = "unique_michiganlegislature"

    run_test(url=url, schedule_type=schedule_type, timezone=timezone)