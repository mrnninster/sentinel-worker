# westvirginia.py
import re
import logging
import pytz
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
from dateutil import parser

from utils.scrape_html import HtmlScraper, HTMLTags
from schedule.schedule_scraper import run_test

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

DATE_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.000Z"

# YouTube channel URLs for West Virginia legislature
# Verified: 2024-12-19
WV_HOUSE_YOUTUBE_URL = "https://www.youtube.com/@WVHouseofDelegates/live"
WV_SENATE_YOUTUBE_URL = "https://www.youtube.com/@WVSenate/live"


class Westvirginia:
    def __init__(self):
        self.timezone = None
        self.base_url = None
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True

        # Compile regex patterns at class level for better performance
        self.session_start_pattern = re.compile(
            r"1st\s+Day\s+Of\s+Session", re.IGNORECASE
        )
        self.session_end_pattern = re.compile(
            r"(?:60th\s+Day,?\s+)?Last\s+Day\s+of\s+Session", re.IGNORECASE
        )
        self.cancelled_pattern = re.compile(r"Cancel(?:led|ed)", re.IGNORECASE)

    def unique_westvirginia(self, url: str, local_timezone: str) -> list:
        self.timezone = local_timezone
        self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc

        # Get current year for the calendar
        current_year = datetime.now().year

        # The page has a form to submit year, we'll need to POST or GET with year parameter
        # Properly handle existing query parameters in the URL
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        params["year"] = [str(current_year)]
        page_url = urlunparse(parsed._replace(query=urlencode(params, doseq=True)))

        # Use render=true since the page may have JavaScript
        page_html = self.scraper.scrape_html(url=page_url, render="true")
        detail_page_soup = self.scraper.convert_to_soup(string=page_html)

        # Find calendar tables with id="calendar"
        # Each table represents one month
        calendar_tables = detail_page_soup.find_all(HTMLTags.TABLE_TAG, id="calendar")

        # Track session start and end dates
        session_start_date = None
        session_end_date = None
        milestone_dates = {}  # Track dates that have explicit milestone descriptions

        for table in calendar_tables:
            # Extract month and year from header row
            # Header format: <td class="header" colspan="5">January 2026</td>
            current_month = None
            current_year_parsed = current_year

            # Find the header row with month/year
            header_rows = table.find_all(HTMLTags.ROWS_TAG)
            for row in header_rows:
                header_cells = row.find_all(HTMLTags.COLUMNS_TAG, class_="header")
                for cell in header_cells:
                    header_text = cell.get_text(strip=True)
                    # Extract month and year from header text like "January 2026"
                    month_year_match = re.search(
                        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})",
                        header_text,
                        re.IGNORECASE,
                    )
                    if month_year_match:
                        current_month = month_year_match.group(1)
                        current_year_parsed = int(month_year_match.group(2))
                        break
                if current_month:
                    break

            if not current_month:
                continue

            # Find all date cells (cells with class="day-all")
            # These cells contain divs with class="day-digit"
            # First div is the day number, subsequent divs are event descriptions
            date_cells = table.find_all(
                HTMLTags.COLUMNS_TAG, class_=lambda x: x and "day-all" in x
            )

            for cell in date_cells:
                # Find all divs with class="day-digit" in this cell
                day_divs = cell.find_all("div", class_="day-digit")

                if not day_divs:
                    continue

                # First div contains the day number
                day_text = day_divs[0].get_text(strip=True)
                day_match = re.match(r"^(\d{1,2})$", day_text)
                if not day_match:
                    continue

                day = int(day_match.group(1))

                # Subsequent divs contain event descriptions
                # Example: "1st Day Of Session", "20th Day, Legislative Rule-Making Review bills due"
                event_descriptions = []
                for div in day_divs[1:]:
                    desc_text = div.get_text(strip=True)
                    if desc_text and desc_text != day_text:
                        event_descriptions.append(desc_text)

                # Process each event description
                for event_desc in event_descriptions:
                    if not event_desc or len(event_desc) < 3:
                        continue

                    try:
                        # Construct date string
                        date_str = f"{current_month} {day}, {current_year_parsed}"
                        meeting_date_time = parser.parse(date_str, fuzzy=True)

                        # Default to 9:00 AM if no time specified
                        if (
                            meeting_date_time.hour == 0
                            and meeting_date_time.minute == 0
                        ):
                            meeting_date_time = meeting_date_time.replace(
                                hour=9, minute=0
                            )

                        # Convert to UTC
                        meeting_date_utc = self._convert_to_utc(
                            meeting_date_time, self.timezone
                        )

                        # Track session start and end dates
                        if self.session_start_pattern.search(event_desc):
                            session_start_date = meeting_date_time.date()
                        elif self.session_end_pattern.search(event_desc):
                            session_end_date = meeting_date_time.date()

                        # Track milestone dates to avoid duplicates later
                        date_key = meeting_date_time.date()
                        # Mark this date as having milestone meetings (regardless of whether we add it)
                        if date_key not in milestone_dates:
                            milestone_dates[date_key] = []

                        # Extract meeting name from event description
                        meeting_name = self._extract_meeting_name(event_desc)

                        # Check for cancelled status
                        status = (
                            "Cancelled"
                            if self.cancelled_pattern.search(event_desc)
                            else "Upcoming"
                        )

                        # Extract meeting link from cell (check for links in the date cell)
                        meeting_link = self._extract_meeting_link(cell, event_desc)

                        # Check if we already have this meeting (avoid duplicates)
                        existing = any(
                            m.get("Scheduled time")
                            == meeting_date_utc.strftime(DATE_UTC_FORMAT)
                            and m.get("Meeting name") == meeting_name
                            for m in self.meetings
                        )

                        if not existing:
                            milestone_dates[date_key].append(meeting_name)
                            self.meetings.append(
                                {
                                    "Meeting name": meeting_name,
                                    "Scheduled time": meeting_date_utc.strftime(
                                        DATE_UTC_FORMAT
                                    ),
                                    "Meeting link": meeting_link,
                                    "Agenda link": None,
                                    "Status": status,
                                }
                            )
                    except (ValueError, AttributeError) as e:
                        log.debug(f"Failed to process event: {e}")
                        continue

        # If we found session start and end dates, create meetings for all days in between
        if session_start_date and session_end_date:
            current_date = session_start_date
            while current_date <= session_end_date:
                # Skip if this date already has a milestone meeting
                if current_date not in milestone_dates:
                    try:
                        # Create a datetime for this date at 9:00 AM
                        meeting_date_time = datetime.combine(
                            current_date,
                            datetime.min.time().replace(hour=9, minute=0),
                        )

                        # Convert to UTC
                        meeting_date_utc = self._convert_to_utc(
                            meeting_date_time, self.timezone
                        )

                        # Create a regular legislative session meeting
                        # Format: "Legislative Session - Day X" where X is the day number
                        day_number = (current_date - session_start_date).days + 1
                        day_suffix = self._get_day_suffix(day_number)
                        meeting_name = (
                            f"Legislative Session - {day_number}{day_suffix} Day"
                        )

                        # Check if we already have this meeting (avoid duplicates)
                        existing = any(
                            m.get("Scheduled time")
                            == meeting_date_utc.strftime(DATE_UTC_FORMAT)
                            and m.get("Meeting name") == meeting_name
                            for m in self.meetings
                        )

                        # Get meeting link for regular session days
                        # Use standard YouTube channel URLs based on meeting type
                        meeting_link = self._get_standard_meeting_link(meeting_name)

                        if not existing:
                            self.meetings.append(
                                {
                                    "Meeting name": meeting_name,
                                    "Scheduled time": meeting_date_utc.strftime(
                                        DATE_UTC_FORMAT
                                    ),
                                    "Meeting link": meeting_link,
                                    "Agenda link": None,
                                    "Status": "Upcoming",
                                }
                            )
                    except (ValueError, AttributeError) as e:
                        log.debug(
                            f"Failed to process session date {current_date}: {e}"
                        )
                        pass

                # Move to next day
                current_date += timedelta(days=1)

        return self.meetings

    def _get_day_suffix(self, day: int) -> str:
        """Get the ordinal suffix for a day number (st, nd, rd, th)."""
        if 10 <= day % 100 <= 20:
            suffix = "th"
        else:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        return suffix

    def _extract_meeting_link(self, cell, event_desc: str) -> str:
        """
        Extract meeting link from calendar cell or event description.
        Checks for:
        1. Links in the date cell (href attributes)
        2. YouTube URLs in text
        3. Standard channel URLs based on meeting type
        """
        meeting_link = None

        # Check for links in the cell
        links = cell.find_all("a", href=True)
        for link in links:
            href = link.get("href", "")
            if href:
                # Check if it's a YouTube link or stream link
                if any(
                    domain in href.lower()
                    for domain in ["youtube.com", "youtu.be", "stream", "live"]
                ):
                    # Make absolute URL if relative
                    if href.startswith("//"):
                        meeting_link = f"https:{href}"
                    elif href.startswith("/"):
                        meeting_link = f"{self.base_url}{href}"
                    else:
                        meeting_link = href
                    break

        # If no link found in cell, check event description for YouTube URLs
        if not meeting_link:
            youtube_pattern = (
                r"(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[\w-]+)"
            )
            youtube_match = re.search(youtube_pattern, event_desc)
            if youtube_match:
                meeting_link = youtube_match.group(1)

        # If still no link, use standard channel URLs based on meeting type
        if not meeting_link:
            meeting_link = self._get_standard_meeting_link(event_desc)

        return meeting_link

    def _get_standard_meeting_link(self, meeting_name: str) -> str:
        """
        Get standard meeting link based on meeting name/type.
        Returns standard YouTube channel URLs for House/Senate sessions.
        """
        meeting_name_lower = meeting_name.lower()

        # West Virginia House of Delegates YouTube channel
        if "house" in meeting_name_lower or "delegates" in meeting_name_lower:
            return WV_HOUSE_YOUTUBE_URL

        # West Virginia Senate YouTube channel
        if "senate" in meeting_name_lower:
            return WV_SENATE_YOUTUBE_URL

        # For general legislative sessions, default to House stream
        # (most common for full session meetings)
        if (
            "legislative session" in meeting_name_lower
            or "session" in meeting_name_lower
        ):
            return WV_HOUSE_YOUTUBE_URL

        return None

    def _extract_meeting_name(self, text: str) -> str:
        """Extract a clean meeting name from text."""
        # Remove extra whitespace
        text = re.sub(r"\s+", " ", text).strip()

        # The text format is typically: "Xth Day, Description" or "Xth Day Of Session"
        # We want to return the full description including the day number
        # Examples:
        # - "1st Day Of Session" -> "1st Day Of Session"
        # - "20th Day, Legislative Rule-Making Review bills due" -> "20th Day, Legislative Rule-Making Review bills due"
        # - "35th Day, Last day to introduce bills in the House" -> "35th Day, Last day to introduce bills in the House"

        # If the text already contains a day pattern, return it as-is (cleaned up)
        if re.search(r"\d+(?:st|nd|rd|th)\s+Day", text, re.IGNORECASE):
            return text

        # Look for common legislative event patterns
        patterns = [
            r"(\d+)(?:st|nd|rd|th)\s+Day\s+Of\s+Session",
            r"(\d+)(?:st|nd|rd|th)\s+Day,\s*(.+)",
            r"(\d+)(?:st|nd|rd|th)\s+Day",
            r"Last\s+day\s+to\s+introduce\s+bills\s+in\s+the\s+(House|Senate)",
            r"Last\s+day\s+to\s+consider\s+bill\s+on\s+third\s+reading",
            r"Legislative\s+Rule-Making\s+Review\s+bills\s+due",
            r"Bills\s+due\s+out\s+of\s+committees",
            r"Last\s+Day\s+of\s+Session",
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                # If pattern has groups, return the full match
                return match.group(0).strip()

        # Fallback: return first 100 chars or a default name
        if text:
            return text[:100] if len(text) > 100 else text

        return "Legislative Session"

    def _convert_to_utc(self, date_time: datetime, local_timezone: str) -> datetime:
        """Convert a naive datetime to UTC using the local timezone."""
        local_tz = pytz.timezone(local_timezone)
        # If datetime is naive, localize it
        if date_time.tzinfo is None:
            local_dt = local_tz.localize(date_time)
        else:
            local_dt = date_time.astimezone(local_tz)
        return local_dt.astimezone(pytz.UTC)


if __name__ == "__main__":
    run_test(
        url="https://www.wvlegislature.gov/nowcalendar_legis3.cfm",
        schedule_type="unique_westvirginia",
        timezone="America/New_York",
    )
