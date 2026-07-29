import re
import os
import sys
import logging
from fuzzywuzzy import fuzz
from dateutil import parser
from datetime import datetime
from dotenv import load_dotenv
from urllib.parse import urlparse

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Ncgaming:
    """
    This is a unique scraper for the North Carolina Gaming Commission schedule.
    Here is what the requests are expected to look like
    
    Request samples
    -------------
        - refresh_schedule :
            
            {
                "geodicts": [
                    {
                        "geoID": "1759442611715x428818637128192300",
                        "schedule_type": "unique_ncgaming",
                        "url": "https://ncgaming.gov/meetings/",
                        "agenda_url": null,
                        "timezone": "America/New_York",
                        "glitch_meetings": [],
                        "debug": false,
                        "channel_url": ""
                    }
                ],
                "version": "test"
            }
            
            
        - stream_request:
            ```
                {
                    "schedule_url": "https://ncgaming.gov/meetings/",
                    "stream_type": "twilio_phone_code",                  <------ Depends on passcode presence (Alternate is twilio_phone_no_code)
                    "meeting_title": "sample meeting title",
                    "location": "North Carolina",
                    "session_ID": "1759442611715x428818637128192300",
                    "timezone": "America/New_York",
                    "schedule_type": "unique_ncgaming",                  <----- New schedule type
                    "demo_time_str": null,
                    "single_player_url": "",
                    "version": "test",
                    "glitch_meetings": [],
                    "meeting_id": "",                                    <----- Meeting ID(Required for twilio)
                    "passcode": "",                                      <----- Passcode(Optional for twilio)
                    "dial_in_number": "",                                <----- Dial in number(Required for twilio)
                    "twilio_number": "+18882942357",
                    "is_restart": true,
                    "last_status": "Upcoming",
                    "channel_url": "",
                    "test_stream_url": null,
                    "has_recess": false,
                    "youtube_restart_ID": "",
                    "detect_start_method": "autostart",                 <----- Detect start method
                    "detect_end_method": "stream_detect",               <----- Detect end method
                    "detect_end_ocr_string": ""
                }
            ```
    """
    
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_ncgaming(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        event_details = []
        domain = urlparse(url).scheme + "://" + urlparse(url).netloc
        
        event_cards = soup.find_all("div", class_="em-event em-item")
        for event in event_cards:
            title_elem = event.find("h3", class_="em-item-title")
            date_elem = event.find("div", class_="em-item-meta-line em-event-date")
            time_elem = event.find("div", class_="em-item-meta-line em-event-time")
            
            if not all([title_elem, date_elem, time_elem]):
                log.warning("Missing required elements in event card, skipping")
                continue
            
            title = title_elem.text.strip()
            date_str = date_elem.text.strip()
            time_str = time_elem.text.strip()
            
            # Handle different dash formats (with/without spaces, different dash characters)
            if " - " in time_str:
                start_time = time_str.split(" - ")[0].strip()
            elif " – " in time_str:  # en dash
                start_time = time_str.split(" – ")[0].strip()
            elif "—" in time_str:  # em dash
                start_time = time_str.split("—")[0].strip()
            elif "-" in time_str:
                start_time = time_str.split("-")[0].strip()
            else:
                start_time = time_str.strip()
            
            try:
                parsed_date = parser.parse(date_str, fuzzy=True)
                event_details.append({
                    "title": title,
                    "date": parsed_date,
                    "time": start_time
                })
            except (ValueError, TypeError) as e:
                log.warning(f"Error parsing date '{date_str}': {e}, skipping event")
                continue
        
        row_container = soup.find("tbody", class_="row-striping row-hover")
        if not row_container:
            log.warning("Table container not found, returning empty meetings list")
            return self.meetings
        
        rows = row_container.find_all("tr")
        
        for row in rows:
            status = "Upcoming"
            meet_date_time = None
            
            # Get the date (column-1: Date column)
            date_elem = row.find("td", class_="column-1")
            if not date_elem:
                log.warning("Missing date column in row, skipping")
                continue
            
            date_str = date_elem.text.strip()
            try:
                date_only = parser.parse(date_str, fuzzy=True)
            except (ValueError, TypeError) as e:
                log.warning(f"Error parsing date '{date_str}': {e}, skipping row")
                continue
            
            # Get the title (column-2: Meeting title column)
            title_elem = row.find("td", class_="column-2")
            if not title_elem:
                log.warning("Missing title column in row, skipping")
                continue
            
            meet_title = title_elem.text.strip()
            if not meet_title:
                log.warning("Empty meeting title, skipping")
                continue
            
            # Match with event details to get time
            matched_event_detail = None
            for i, event_detail in enumerate(event_details):
                if (
                    fuzz.token_set_ratio(event_detail["title"], meet_title) > 85
                    and event_detail["date"].date() == date_only.date()
                ):
                    try:
                        complete_time = f"{date_only.date().strftime('%Y-%m-%d')} {event_detail['time']}"
                        datetime_obj = parser.parse(complete_time, fuzzy=True)
                        meet_date_time_str = datetime.strftime(
                            datetime_obj, TimeFormatter.desired_format()
                        )
                        utc_time = TimeFormatter(meet_date_time_str, timezone).get_utc_time(
                            as_datetime=True
                        )
                        meet_date_time = utc_time.isoformat().replace("+00:00", "Z")
                        matched_event_detail = i
                        break
                    except (ValueError, TypeError) as e:
                        log.warning(f"Error parsing complete time: {e}")
                        continue
            
            # Remove matched event detail to prevent reuse
            if matched_event_detail is not None:
                event_details.pop(matched_event_detail)
            
            # Skip if we couldn't determine the meeting time
            if not meet_date_time:
                log.warning(f"Could not determine meeting time for '{meet_title}', skipping")
                continue
            
            # Get dial up info (column-4: Dial-in information column)
            dial_up_elem = row.find("td", class_="column-4")
            dial_up_info = dial_up_elem.text.strip() if dial_up_elem else ""
            
            # Parse the dial up info
            phone_number = None
            access_code = None
            passcode = None
            
            if dial_up_info:
                # Pattern for phone number (e.g., "(408) 418-9388")
                phone_pattern = r"\(?\d{3}\)?\s*\d{3}[-\s]?\d{4}"
                phone_match = re.search(phone_pattern, dial_up_info)
                if phone_match:
                    # Remove all non-digits
                    phone_number = re.sub(r"[^\d]", "", phone_match.group(0))
                    # Add country code if not present (US numbers)
                    if not phone_number.startswith("1"):
                        phone_number = "1" + phone_number
                    # Add + prefix
                    phone_number = f"+{phone_number}"
                
                # Pattern for access code (e.g., "Access Code: 2867 813 4236")
                access_code_pattern = r"Access Code:\s*([\d\s]+)"
                access_code_match = re.search(access_code_pattern, dial_up_info, re.IGNORECASE)
                if access_code_match:
                    # Remove all non-digits
                    access_code = re.sub(r"[^\d]", "", access_code_match.group(1))
                
                # Pattern for passcode (e.g., "Passcode: 12345" or "Password: 12345")
                passcode_pattern = r"(?:Password|Passcode):\s*(\w+)"
                passcode_match = re.search(passcode_pattern, dial_up_info, re.IGNORECASE)
                if passcode_match:
                    # Remove all non-digits
                    passcode = re.sub(r"[^\d]", "", passcode_match.group(1))
            
            # Get agenda link (column-5: Agenda link column)
            agenda_td = row.find("td", class_="column-5")
            agenda_link = None
            if agenda_td:
                agenda_elem = agenda_td.find("a")
                if agenda_elem:
                    agenda_href = agenda_elem.get("href")
                    if agenda_href:
                        # Make absolute URL if relative
                        if agenda_href.startswith("http"):
                            agenda_link = agenda_href
                        else:
                            agenda_link = f"{domain}{agenda_href}"

            meeting = {
                "Meeting name": meet_title,
                "Meeting link": None,  # Note: column-3 may contain meeting/video links if needed
                "Scheduled time": meet_date_time,
                "Phone number": phone_number,
                "Access ID": access_code,
                "Passcode": passcode,
                "Agenda link": agenda_link,
                "Status": status,
            }
            self.meetings.append(meeting)
        return self.meetings
    
    
if __name__ == "__main__":
    run_test(
        url="https://ncgaming.gov/meetings/",
        schedule_type="unique_ncgaming",
        timezone="America/New_York",
    )
