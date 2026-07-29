import os
import re
import sys
import json
import logging
from typing import Optional
from dateutil import parser
from datetime import datetime
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from urllib.parse import urlparse, urljoin

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test
from utils.scrape_html import HtmlScraper, ReturnType

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Carolinalegislature:

    def __init__(self):
        self.meetings = []
        self.api_data = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        self.video_data_api_url = "https://rest.scstatehouse.gov/redis/video/current"

    def get_api_data(self):
        api_response = self.scraper.scrape_html(url=self.video_data_api_url, return_type=ReturnType.RESPONSE)
        try:
            self.api_data = json.loads(api_response.text)["data"]
        except Exception as e:
            log.exception(f"An error occurred when getting API json response: {e}")
        
    def extract_time(self, text: str) -> Optional[str]:
        """
        Extract the time component in AM/PM format from a string.

        Args:
            text (str): The string to extract time from

        Returns:
            str: Time string in format "H:MM am/pm" or None if no time found
        """
        if not text or not isinstance(text, str):
            return None

        # Clean the text
        text = text.strip()

        # Pattern for 12-hour format (e.g., "10:00 am", "2:30 PM", "11:45am")
        pattern = r'\b(\d{1,2}):(\d{2})\s*(am|pm|AM|PM|a\.m\.|p\.m\.|A\.M\.|P\.M\.)\b'
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            hour, minute, period = match.groups()
            hour = int(hour)
            minute = int(minute)

            # Validate 12-hour format
            if 1 <= hour <= 12 and 0 <= minute <= 59:
                return f"{hour}:{minute:02d} {period.lower()}"

        return None
        
    def unique_carolinalegislature(self, url: str, timezone: str = "America/New_York") -> list[dict]:
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        # Get api data
        self.get_api_data()

        # Parse html
        domain = urlparse(url).scheme + "://" + urlparse(url).netloc
        content = soup.find("div", id="contentsection")
        if content is None:
            log.warning("No content section found")
            return self.meetings
        
        days = content.find_all("ul")
        
        # No meetings
        if len(days) == 0:
            return self.meetings
        
        # Meetings
        for day in days:
            target_span = day.select_one("span[style*='font-weight:bold'][style*='text-decoration:underline']")

            if target_span is not None:
                span_text = target_span.get_text(strip=True).lower()
                parsed_date_str = parser.parse(span_text).date().strftime("%Y-%m-%d")
                
                api_data_dict = {item["KEY"]: item for item in self.api_data}
                meets = day.find_all("li")
                for meet in meets:
                    # Get meeting ID
                    id_tag = meet.find_previous("a", attrs={"name": True})
                    if id_tag:
                        meeting_id = id_tag.get("name")
                        meet_data = api_data_dict.get(str(meeting_id))
                    else:
                        meeting_id = None
                        continue
                    
                    # Get video_url with KeyError handling
                    meeting_link = None
                    try:
                        meeting_link = meet_data["VIDEO_URL"] if meet_data else None
                    except (KeyError, IndexError, TypeError):
                        meeting_link = None
                    
                    # Get meeting status
                    status = "In progress" if meet_data and meet_data["IS_LIVE"] == True else "Upcoming"
                        
                    # Get meeting date and time
                    time = meet.find("span").get_text(strip=True).lower()
                    time = self.extract_time(time)
                    if time is None:
                        continue
                    
                    date_time = f"{parsed_date_str} {time}"
                    meeting_date_time = parser.parse(date_time)
                    meeting_date_time = datetime.strftime(meeting_date_time, TimeFormatter.desired_format())
                    utc_time = TimeFormatter(meeting_date_time, timezone).get_utc_time(as_datetime=True)
                    schedule_time = utc_time.isoformat().replace("+00:00", "Z")
                    
                    # Get title
                    li_text = meet.get_text()

                    # Skip cancelled meetings
                    if re.search(r"Cancel(?:led|ed)", li_text, re.IGNORECASE):
                        continue

                    # Prefer clean title from API data (no bill numbers or contact info)
                    api_title = (meet_data.get("VIDEO_TITLE") or "").strip() if meet_data else ""
                    if api_title:
                        title = api_title
                    else:
                        # Fallback: extract from HTML text
                        # Primary boundary: "Agenda Available"
                        agenda_pos = li_text.find('Agenda Available')
                        if agenda_pos != -1:
                            main_content = li_text[:agenda_pos].strip()
                        else:
                            # Secondary boundary: "REVISIONS:"
                            revisions_pos = li_text.find('REVISIONS:')
                            if revisions_pos != -1:
                                main_content = li_text[:revisions_pos].strip()
                            else:
                                # Ultimate fallback: use first line only (before any <br>)
                                lines = li_text.split('\n')
                                main_content = lines[0] if lines else li_text

                        # Extract title from main content
                        parts = main_content.split('--')
                        title = parts[-1].strip() if len(parts) >= 3 else 'Unknown Meeting'

                        # Strip contact info e.g. "(Contact Info: 803-212-6627)"
                        title = re.sub(r'\(Contact Info:[^)]*\)', '', title).strip()
                        # Strip bill number suffixes e.g. " on 199, 4756"
                        title = re.sub(r'\s+on\s+[\d,\s]+$', '', title).strip()
                        # Clean any remaining trailing commas
                        title = title.rstrip(',').strip()
                    
                    # Get Agenda
                    agenda_link = None
                    div_tags = meet.find_all("div")
                    for tag in div_tags:
                        if "agenda" in tag.get_text(strip=True).lower():
                            agenda_tag = tag.find("a")
                            if agenda_tag is not None:
                                agenda_href = agenda_tag.get("href")
                                if agenda_href:
                                    agenda_link = urljoin(domain, agenda_href)
                            break  # Also add break to stop after finding first match
                    
                    meeting = {
                        "Meeting name": title,
                        "Scheduled time": schedule_time,
                        "Meeting link": meeting_link,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }
                    self.meetings.append(meeting)
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://www.scstatehouse.gov/meetings.php?chamber=S&headerfooter=0",
        schedule_type="unique_carolinalegislature",
        get_full_archive_flag=True,
    )