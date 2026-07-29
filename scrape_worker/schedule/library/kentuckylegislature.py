import re
import os
import sys
import pytz
import logging
from dateutil import parser
from fuzzywuzzy import fuzz
from datetime import datetime
from dotenv import load_dotenv


load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.library.youtube import Youtube
from schedule.schedule_scraper import run_test
from utils.youtube import Youtube as YoutubeUtils

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Kentuckylegislature:
    """
    Scraper for Kentucky Legislature committee meetings.

    Parses the Kentucky Legislative Calendar (https://apps.legislature.ky.gov/LegislativeCalendar)
    to extract committee meeting information. Also integrates YouTube streams from the
    Kentucky Legislative Research Commission channel for live and upcoming meetings.

    Kentucky-specific quirks:
    - Cancelled meetings are indicated by nested div elements within TimeAndLocation
    - Meeting times are in "HH:MM am/pm" format
    - Skips generic "House Convenes" and "Senate Convenes" entries
    """

    def __init__(self):
        """
        Initialize the Kentucky Legislature scraper.

        Sets up HTML scraper, YouTube channel URL, and legislative calendar URL.
        """
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        self.alt_calendar = "https://apps.legislature.ky.gov/LegislativeCalendar"
        self.channel_url = "https://www.youtube.com/@KYLRCCommitteeMeetings/streams"


    def unique_kentuckylegislature(self, url: str, timezone: str):
        """
        Scrape Kentucky Legislature committee meetings from legislative calendar and YouTube.

        Retrieves both upcoming committee meetings from the legislative calendar HTML page
        and integrates live/upcoming YouTube streams from the KYLRC channel.
        Skips cancelled meetings and duplicate entries.

        Args:
            url: Legislative calendar URL (https://apps.legislature.ky.gov/LegislativeCalendar)
            timezone: Timezone string for date parsing (e.g., "America/New_York")

        Returns:
            list: List of meeting dictionaries with keys:
                - "Meeting name": Committee/meeting title
                - "Scheduled time": ISO 8601 datetime string in UTC
                - "Meeting link": YouTube URL (for YouTube meetings) or None
                - "Agenda link": None (not available from calendar)
                - "Status": "Upcoming", "In progress", or other status
        """
        
        # Get upcoming youtube meets
        yt_scraper = Youtube()
        upcoming_yt_meetings, youtube_page_soup = yt_scraper.youtube_table(url=self.channel_url, return_soup=True)
        self.meetings.extend(upcoming_yt_meetings)
        
        # Get live youtube meets
        live_youtube_meetings = []
        if self.channel_url:
            youtubeutils = YoutubeUtils(url=self.channel_url, meeting_title="")
            if youtubeutils.is_valid_youtube_streams_url():
                live_youtube_meetings = youtubeutils.get_live_videos(youtube_page_soup)
                
            if live_youtube_meetings:
                for meeting in live_youtube_meetings:
                    self.meetings.append(
                                        {
                                            "Meeting name": meeting["video_title"],
                                            "Scheduled time": datetime.now(tz=pytz.UTC), # because meeting is already in progress
                                            "Meeting link": f"https://www.youtube.com/watch?v={meeting["video_id"]}",
                                            "Agenda link": None,
                                            "Status": "In progress",
                                        }
                                    )
                    
        # Get meetings from alternate calendar
        soup_string = self.scraper.scrape_html(url=self.alt_calendar)
        soup = self.scraper.convert_to_soup(soup_string)
        date_string = soup.find("div", class_="DateHeading").get_text(strip=True).lower()

        # Parse meetings from the calendar
        time_and_location_elements = soup.find_all("div", class_="TimeAndLocation")

        for time_location_elem in time_and_location_elements:
            # Check if this meeting is cancelled - TimeAndLocation only has children when cancelled
            if time_location_elem.find("div"):
                continue

            # Get the time and location text, then extract just the time
            time_location_text = time_location_elem.get_text(strip=True)
            if not time_location_text:
                log.warning("Empty time_location_text, skipping meeting")
                continue

            # Split by comma and take the first part (the time)
            time_only = time_location_text.split(',')[0].strip() if ',' in time_location_text else time_location_text

            try:
                date_time = f"{date_string} {time_only}"
                datetime_obj = parser.parse(date_time, fuzzy=True)
                meet_date_time_str = datetime.strftime(datetime_obj, TimeFormatter.desired_format())
                utc_time = TimeFormatter(meet_date_time_str, timezone).get_utc_time(as_datetime=True)
                meet_date_time = utc_time.isoformat().replace("+00:00", "Z")
            except Exception as e:
                log.warning(f"Error parsing date/time '{date_string} {time_only}': {e}")
                continue

            # Get the committee name/title from the next sibling element
            committee_elem = time_location_elem.find_next_sibling("div", class_=lambda x: x and ("CommitteeName" in x or "CancelledHeading" in x))

            if not committee_elem:
                log.warning(f"No committee element found for time: {time_only}")
                continue

            # Get the full title as-is or skip
            titles_to_skip = ["senate convenes", "house convenes"]
            title = committee_elem.get_text(strip=True)
            if title.lower() in titles_to_skip:
                continue
            
            # Check for duplicates
            is_duplicate = False
            for meet in self.meetings:
                if (meet_date_time == meet["Scheduled time"] and 
                    fuzz.token_set_ratio(meet["Meeting name"], title) > 85):
                    is_duplicate = True
                    break

            if not is_duplicate:
                meeting = {
                    "Meeting name": title,
                    "Scheduled time": meet_date_time,
                    "Meeting link": None,
                    "Agenda link": None,
                    "Status": "Upcoming",
                }
                self.meetings.append(meeting)
        return self.meetings


if __name__ == "__main__":
    url = "https://apps.legislature.ky.gov/LegislativeCalendar"
    timezone = "America/Chicago"
    schedule_type = "unique_kentuckylegislature"
    run_test(url=url, timezone=timezone, schedule_type=schedule_type)