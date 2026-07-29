import logging
import re
import os
import sys
from datetime import datetime, timedelta
import pytz
import requests
from dotenv import load_dotenv
from utils.pdf_text import extract_pdf_text_from_bytes

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from schedule.schedule_scraper import run_test
from utils.scrape_html import HtmlScraper
from utils.utils_functions import to_utc_iso

log = logging.getLogger(__name__)


class Maurycounty:
    """
    Scraper for Maury County Public Schools Board of Education meetings page.

    This scraper extracts meeting agendas and videos from:
        https://www.mauryk12.org/boemeetings

    The page uses year and month selections to display agendas and on-demand videos.
    """

    def __init__(self):
        self._scraper = HtmlScraper()
        self.self_contained_parser = True
        self.meetings = []
        self.meeting_link = "https://play.champds.com/maurycoschoolstn/live/5"

    def _fetch_event_details(self, event_id: str, timezone: str) -> dict:
        """
        Fetch additional details from agenda PDF including meeting date and time.

        Args:
            event_id (str): The event ID
            timezone (str): Timezone string

        Returns:
            dict: Dictionary with 'agenda_pdf', 'meeting_time', 'meeting_date' keys
        """
        details = {
            "agenda_pdf": None,
            "meeting_time": None,
            "meeting_date": None,
        }
        agenda_link = f"https://play.champds.com/maurycoschoolstn/agendapdf/{event_id}"

        try:
            # Download the PDF
            response = requests.get(agenda_link, timeout=15)
            response.raise_for_status()

            # Extract text from PDF
            text = extract_pdf_text_from_bytes(response.content)
            text = text.strip()

            if not text:
                log.debug(f"No text extracted from PDF for event {event_id}")
                return details

            # Store the agenda link
            details["agenda_pdf"] = agenda_link

            # Parse meeting date and time from PDF text
            lines = text.split("\n")

            # First, try to match combined date-time pattern (e.g., "NOVEMBER 4, 2025, 6:00 PM")
            combined_pattern = r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(\d{4}),\s+(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)"

            meeting_date = None
            meeting_time = None

            for line in lines:
                line = line.strip()
                combined_match = re.search(combined_pattern, line, re.IGNORECASE)
                if combined_match:
                    # Extract date and time from combined match
                    month = combined_match.group(
                        1
                    ).capitalize()  # Capitalize first letter (NOVEMBER -> November)
                    day = combined_match.group(2)
                    year = combined_match.group(3)
                    hour = combined_match.group(4)
                    minute = combined_match.group(5)
                    period = combined_match.group(6)

                    meeting_date = f"{month} {day}, {year}"
                    meeting_time = f"{hour}:{minute} {period.upper()}"
                    details["meeting_date"] = meeting_date
                    details["meeting_time"] = meeting_time
                    break

            # If combined pattern didn't match, try separate date and time patterns
            if not meeting_date:
                # Look for date patterns (e.g., "Monday, November 4, 2024", "November 4, 2024", "Nov 4, 2024", "11/4/2024")
                date_patterns = [
                    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(\d{4})",
                    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(\d{4})",
                    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})",
                    r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})",
                    r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})",
                ]

                for line in lines:
                    line = line.strip()
                    for pattern in date_patterns:
                        date_match = re.search(pattern, line, re.IGNORECASE)
                        if date_match:
                            meeting_date = date_match.group(0)
                            details["meeting_date"] = meeting_date
                            break
                    if meeting_date:
                        break

            # Look for time patterns if not already found (e.g., "6:00 PM", "6 PM", "18:00")
            if not meeting_time:
                time_patterns = [
                    r"(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)",
                    r"(\d{1,2})\s*(AM|PM|am|pm)",
                    r"(\d{1,2}):(\d{2})",
                ]

                for line in lines:
                    line = line.strip()
                    for pattern in time_patterns:
                        time_match = re.search(pattern, line, re.IGNORECASE)
                        if time_match:
                            meeting_time = time_match.group(0)
                            details["meeting_time"] = meeting_time
                            break
                    if meeting_time:
                        break

        except requests.exceptions.RequestException as e:
            log.debug(f"Error downloading PDF for event {event_id}: {e}")
        except Exception as e:
            log.debug(f"Error fetching event details for {event_id}: {e}")

        return details

    def unique_maurycounty(self, url: str, timezone: str) -> list:
        """
        Extract meeting data from Maury County Public Schools Board of Education.

        Args:
            url (str): Target webpage URL (https://www.mauryk12.org/boemeetings).
            timezone (str): Timezone (e.g., 'America/Chicago').

        Returns:
            list: A list of meeting dictionaries with keys:
                  'Meeting name', 'Scheduled time', 'Meeting link', 'Agenda link', 'Status'
        """
        tz_info = pytz.timezone(timezone)

        # Fetch and parse HTML with rendering for dynamic content
        # Wait for the page to fully load (events are loaded dynamically via JavaScript)
        response = self._scraper.scrape_html(url=url, render="true", wait_for_seconds=5)
        soup = self._scraper.convert_to_soup(string=response)
        # Get current year from the year selector or use current year
        year_select = soup.select_one("select.cdsYearSelect option[selected]")
        current_year = (
            int(year_select.get("value")) if year_select else datetime.now(tz_info).year
        )

        # Find all event rows (class cdsEvent)
        event_rows = soup.find_all("tr", class_="cdsEvent")

        if not event_rows:
            log.warning("No event rows found on the page")
            # Debug: Save HTML for inspection
            log.debug(f"Page contains {len(response)} characters")
            log.debug(f"Found year select: {year_select is not None}")
            return self.meetings

        for row in event_rows:
            try:
                # Extract event ID from row ID (e.g., "trEvent_167" -> "167")
                event_id = None
                row_id = row.get("id", "")
                if row_id and "trEvent_" in row_id:
                    event_id = row_id.replace("trEvent_", "")

                title_cell = row.find("td", class_="tdEventTitle")

                meeting_name = title_cell.get_text(strip=True)
                # Fetch event details to get agenda PDF, meeting date and time
                event_details = {}
                if event_id:
                    event_details = self._fetch_event_details(event_id, timezone)

                # Get meeting_date and meeting_time from PDF
                meeting_date = event_details.get("meeting_date")
                meeting_time = event_details.get("meeting_time")

                # Check if either date or time is missing
                if not meeting_date or not meeting_time:
                    log.info(
                        f"Meeting '{meeting_name}' detected but no time present (date: {meeting_date}, time: {meeting_time}), check back later"
                    )
                    continue

                # Combine meeting date and time
                meeting_date_time = f"{meeting_date} {meeting_time}"

                try:
                    local_dt = datetime.strptime(
                        meeting_date_time, "%B %d, %Y %I:%M %p"
                    )
                    local_dt = tz_info.localize(local_dt)
                    scheduled_time = to_utc_iso(local_dt)
                except ValueError as e:
                    log.warning(
                        f"Failed to parse datetime '{meeting_date_time}' for '{meeting_name}': {e}"
                    )
                    continue

                # Skip past meetings
                tz_now = datetime.now(tz_info)
                if local_dt < tz_now:
                    log.warning(
                        f"Skipping past meeting: {meeting_name} at {meeting_date_time}"
                    )
                    continue

                # Get agenda link from event details (PDF) if available
                agenda_link = event_details.get("agenda_pdf")
                meeting_link = self.meeting_link

                # Determine status
                if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                    status = "Cancelled"
                else:
                    status = "Upcoming"

                self.meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": scheduled_time,
                        "Meeting link": meeting_link,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }
                )
            except Exception as e:
                log.warning(f"Error parsing event row: {e}")
                continue
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://play.champds.com/maurycoschoolstn/archive/1",
        schedule_type="unique_maurycounty",
    )
