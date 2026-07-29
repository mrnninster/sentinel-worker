# illinois.py
import os
import re
import sys
import json
import logging
from urllib.parse import quote
from dateutil import parser
from datetime import datetime
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from bs4 import NavigableString

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test


class Illinois:

    def __init__(self):
        self.meetings = []
        self.base_url = "https://ilga.gov"
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        self.timezone = None
        self.url = None
        self.response_cache = {}
        self.hearing_status_api = f"{self.base_url}/api/Hearings/GetHearingStatusForAV?hearingid="
        self.hearing_details_api = f"{self.base_url}/api/audiovideo/GetStatusByKeyword?strkeyword="
        self.senate_session_api = f"{self.base_url}/api/audiovideo/GetStatusByEntity?strEntity=Senate"
    
    def session_schedules(self, soup: BeautifulSoup, timezone: str):
        """
        This method gets all the session schedules for the illinois
        legislature
        """
        meetings = []
        
        # Find the h4 with "Session Schedule", then get the wrapping div.card
        header = soup.find("h4", class_="card-header-light", string=lambda t: t and "session schedule" in t.lower())
        if header:
            card_div = header.find_parent("div", class_="card")
            
            if card_div:
                calendar_section = card_div.find("div", class_="col-md", id="calendarSection")
                if calendar_section:
                    
                    # get session date
                    date_h5 = calendar_section.find("h5")
                    session_date = None
                    if date_h5:
                        session_date = date_h5.get_text(strip=True)
                        
                    # get session time
                    p_time = calendar_section.find("p")
                    meeting_time = None
                    if p_time:
                        meeting_time = p_time.get_text(strip=True) 
                        
                    # check if either is None, then skip
                    if session_date is None or meeting_time is None:
                        return meetings
                        
                    # combine date and time, then parse to datetime
                    dtstr = f"{session_date} {meeting_time}"
                    try:
                        local_meeting_date = parser.parse(dtstr, fuzzy=True)
                        desired_event_time = datetime.strftime(local_meeting_date, TimeFormatter.desired_format())
                        utc_time = TimeFormatter(desired_event_time, timezone).get_utc_time(as_datetime=True)
                        meeting_datetime_iso = utc_time.isoformat().replace("+00:00", "Z")
                    except (ValueError, TypeError, parser.ParserError) as e:
                        log.warning("Could not parse session schedule date/time: %s - %s", dtstr, e)
                        return meetings
                    
                    # Add meeting status and stream link (defaults if API missing/fails).
                    # Both session and committee APIs return internalURL (Vimeo); we use /embed for playback.
                    meeting_status = "Upcoming"
                    meeting_stream_url = None
                    response = self.scraper.scrape_html(url=self.senate_session_api)
                    if response and response.strip():
                        try:
                            json_data = json.loads(response)
                            if isinstance(json_data, list):
                                for data in json_data:
                                    if isinstance(data, dict) and (data.get("entity") or "").lower() == "senate" and (data.get("status") or "").lower() == "online":
                                        internal_url = data.get("internalURL")
                                        if internal_url:
                                            meeting_stream_url = f"{internal_url.rstrip('/')}/embed"
                                        meeting_status = "In progress"
                                        break
                        except json.JSONDecodeError:
                            log.warning("Invalid JSON from status API for senate session")

                    # add meeting to list
                    meeting = {
                        "Meeting name": "Regular session",
                        "Scheduled time": meeting_datetime_iso,
                        "Meeting link": meeting_stream_url,
                        "Agenda link": None,
                        "Status": meeting_status,
                    }
                    meetings.append(meeting)
        return meetings

    def committee_hearings(self, soup: BeautifulSoup, timezone: str):
        """
        This method gets all committee hearing meetings from the calendar.
        """
        meetings = []
        month_pane = soup.find("div", id="pane-Month")
        if not month_pane:
            return meetings
        table = month_pane.select_one("table.table.table-striped.border")
        if not table:
            return meetings
        table_events = table.find_all("tr")
        if not table_events:
            return meetings

        for table_event in table_events:
            title = None
            meeting_datetime = None
            meeting_stream_url = None
            meeting_status = "Upcoming"
            room_number = None

            event_td = table_event.find_all("td")
            if not event_td:
                continue
            first_td = event_td[0]
            event_span = first_td.find_all("span")
            if not event_span:
                continue

            # get event name and time (use partition to handle missing "-")
            span_1 = event_span[0]
            span_text = span_1.get_text(strip=True).lower() if span_1 else ""
            date_time, _, title_part = span_text.partition("-")
            date_time = date_time.strip()
            title = title_part.strip() if title_part else None
            if not title:
                continue

            if re.search(r"Cancel(?:led|ed)", title, re.IGNORECASE):
                continue

            if date_time:
                try:
                    local_meeting_date = parser.parse(date_time, fuzzy=True)
                    event_time = datetime.strftime(local_meeting_date, TimeFormatter.desired_format())
                    utc_time = TimeFormatter(event_time, timezone).get_utc_time(as_datetime=True)
                    meeting_datetime = utc_time.isoformat().replace("+00:00", "Z")
                except (ValueError, TypeError):
                    continue

            # get event id from first span with id containing "-"
            event_id = None
            span_index = 0
            for i, span_i in enumerate(event_span):
                eid = span_i.get("id")
                if eid and "-" in eid:
                    parts = eid.strip().split("-", 1)
                    if len(parts) >= 2:
                        event_id = parts[1]
                        span_index = i
                        break
            if not event_id:
                continue

            span_2 = event_span[span_index]
            sibling = span_2.next_sibling
            while sibling and isinstance(sibling, NavigableString) and not str(sibling).strip():
                sibling = sibling.next_sibling

            if sibling:
                text_value = str(sibling).strip() if isinstance(sibling, NavigableString) else sibling.get_text(strip=True)
                if text_value:
                    room_number = text_value.split(" - ", 1)[0].strip()
                    # Remove "Room " prefix so API gets the room identifier (e.g. "212").
                    if room_number and room_number.lower().startswith("room"):
                        room_number = room_number[5:].strip()

            # if room number is not found, skip
            if not room_number:
                continue

            # get event status (event_id is from page DOM; quote for safe URL)
            status_url = f"{self.hearing_status_api}{quote(str(event_id), safe='')}"
            response = self.scraper.scrape_html(url=status_url)

            if response and response.strip():
                try:
                    json_data = json.loads(response)
                    status = json_data.get("name")
                    if status:
                        status = status.lower()
                        if status == "open":
                            meeting_status = "In progress"
                except json.JSONDecodeError:
                    log.warning("Invalid JSON from status API for hearing %s", event_id)

            # Get stream url
            api_response = "[]"
            if room_number not in self.response_cache:
                self.response_cache[room_number] = self.scraper.scrape_html(url=f"{self.hearing_details_api}{quote(str(room_number), safe='')}")
            api_response = self.response_cache[room_number]

            if not api_response or not api_response.strip() or api_response.strip() == "[]":
                continue
            try:
                api_json_response = json.loads(api_response)
            except json.JSONDecodeError:
                log.warning("Invalid JSON from stream API for room %s", room_number)
                continue
            if api_json_response and len(api_json_response) > 0:
                internal_url = api_json_response[0].get("internalURL")
                if internal_url:
                    meeting_stream_url = f"{internal_url.rstrip('/')}/embed"
            if meeting_stream_url is None:
                continue

            meeting = {
                "Meeting name": title,
                "Scheduled time": meeting_datetime,
                "Meeting link": meeting_stream_url,
                "Agenda link": None,
                "Status": meeting_status,
            }
            meetings.append(meeting)
        return meetings


    def illinois_table(self, url: str, timezone: str = "America/Chicago"):
        # Fetch HTML (rendered for JavaScript-heavy content)
        html_content = self.scraper.scrape_html(url=url, render="true")
        soup = self.scraper.convert_to_soup(html_content)

        self.meetings = []

        # Get session schedules
        try:
            meetings = self.session_schedules(soup, timezone)
            self.meetings.extend(meetings)
        except (ValueError, TypeError, parser.ParserError, json.JSONDecodeError, KeyError) as e:
            log.exception("Error fetching Illinois session schedule: %s", e)

        # Get committee hearings
        try:
            meetings = self.committee_hearings(soup, timezone)
            self.meetings.extend(meetings)
        except (ValueError, TypeError, parser.ParserError, json.JSONDecodeError, KeyError) as e:
            log.exception("Error fetching Illinois committee hearings: %s", e)

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://ilga.gov/Senate/Schedules",
        schedule_type="illinois_table",
        timezone="America/Chicago",
    )
