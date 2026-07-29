"""
Virginia Senate Committee Meetings Scraper

Parses https://apps.senate.virginia.gov/Senator/CommitteeMeetings.php to extract
committee meeting information.

Special handling for:
- Fixed-time meetings (e.g., "9:30 AM") - returned as normal upcomingsessions
- Adjournment meetings (e.g., "30 Minutes After Adjournment") - triggers YouTube Watcher

Stream type: ts_youtube
Detect start: youtube
Detect end: streamdetect
"""

import os
import re
import sys
import pytz
import logging
import requests
from dateutil import parser
from datetime import datetime
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper  # noqa: E402
from schedule.schedule_scraper import run_test  # noqa: E402


class VirginiaSenate:
    """Scraper for Virginia Senate committee meetings."""

    # YouTube channel URL for Virginia Senate
    YOUTUBE_CHANNEL_URL = "https://www.youtube.com/@SenateofVirginia/streams"

    # Patterns that indicate an "upon adjournment" meeting
    ADJOURNMENT_PATTERNS = [
        r"after\s+adjournment",
        r"upon\s+adjournment",
        r"minutes?\s+after\s+adjournment",
        r"immediately\s+upon\s+adjournment",
    ]

    def __init__(self):
        self.meetings = []
        self.adjournment_meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True

    def _is_adjournment_meeting(self, time_str: str) -> bool:
        """
        Check if a time string indicates an adjournment-based meeting.

        Args:
            time_str: The time string from the calendar (e.g., "30 Minutes After
                      Adjournment")

        Returns:
            bool: True if this is an adjournment meeting
        """
        time_lower = time_str.lower()
        for pattern in self.ADJOURNMENT_PATTERNS:
            if re.search(pattern, time_lower):
                return True
        return False

    def _parse_fixed_time(
        self, date_str: str, time_str: str, timezone: str
    ) -> datetime:
        """
        Parse a fixed time string into a UTC datetime.

        Args:
            date_str: Date string (e.g., "Wednesday, January 28, 2026")
            time_str: Time string (e.g., "9:30 AM" or "12 Noon")
            timezone: Timezone string (e.g., "America/New_York")

        Returns:
            datetime: UTC datetime object
        """
        # Handle "12 Noon" format
        if "noon" in time_str.lower():
            time_str = "12:00 PM"

        # Combine date and time
        datetime_str = f"{date_str} {time_str}"

        try:
            # Parse with fuzzy matching
            datetime_obj = parser.parse(datetime_str, fuzzy=True)

            # Convert to UTC
            tz = pytz.timezone(timezone)
            local_dt = tz.localize(datetime_obj)
            utc_dt = local_dt.astimezone(pytz.UTC)

            return utc_dt
        except Exception as e:
            log.warning(f"Failed to parse datetime '{datetime_str}': {e}")
            return None

    def _extract_adjournment_type(self, time_str: str) -> str:
        """
        Extract the type of adjournment dependency.

        Args:
            time_str: Time string (e.g., "30 Minutes After Adjournment")

        Returns:
            str: Adjournment type identifier
        """
        time_lower = time_str.lower()

        # Check for "Immediately Upon Adjournment of X"
        match = re.search(r"immediately\s+upon\s+adjournment\s+of\s+(.+)", time_lower)
        if match:
            return f"immediately_after_{match.group(1).strip()}"

        # Check for "X Minutes After Adjournment"
        match = re.search(r"(\d+)\s+minutes?\s+after\s+adjournment", time_lower)
        if match:
            return f"{match.group(1)}_min_after_adjournment"

        # Default
        if "immediately" in time_lower:
            return "immediately_after_adjournment"

        return "after_adjournment"

    def unique_virginiasenate(self, url, timezone="America/New_York"):
        """
        Parse Virginia Senate committee meetings page.

        Args:
            url: URL of the page
            timezone: Timezone for datetime conversion

        Returns:
            list: List of meeting dicts with fixed times only.
                  Adjournment meetings are stored in self.adjournment_meetings
                  for the YouTube Watcher to handle.
        """
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        current_datetime = datetime.now(tz=pytz.UTC)
        current_date = current_datetime.date()

        # Find all h5 elements (committee names)
        # The structure is: h5 (committee name), then date/time/location text
        h5_elements = soup.find_all("h5")

        for h5 in h5_elements:
            try:
                # Get committee name
                committee_name = h5.get_text(strip=True)

                # Skip non-committee headers
                if not committee_name or "YouTube" in committee_name:
                    continue

                # Skip the adjournment notice header
                if "Senate Adjourned" in committee_name:
                    continue

                # Get the next sibling text which contains date/time/location
                # The structure is: committee name followed by text node
                next_sibling = h5.next_sibling
                if not next_sibling:
                    continue

                # Get all text until the next hr or h5
                meeting_info = ""
                current = next_sibling
                while current and current.name not in ["hr", "h5"]:
                    if hasattr(current, "get_text"):
                        meeting_info += current.get_text(strip=True)
                    elif isinstance(current, str):
                        meeting_info += current.strip()
                    current = current.next_sibling

                if not meeting_info:
                    continue

                # Parse date and time from meeting_info
                # Format: "Wednesday, January 28, 2026 - 9:30 AMSenate Room A (305), GAB"
                # or: "Wednesday, January 28, 2026 - 30 Minutes After AdjournmentSenate Room..."

                # Split on the dash to separate date from time
                parts = meeting_info.split(" - ", 1)
                if len(parts) < 2:
                    log.debug(f"Could not parse meeting info: {meeting_info}")
                    continue

                date_str = parts[0].strip()
                time_and_location = parts[1].strip()

                # Extract time - it's before "Senate" or the location
                time_match = re.match(
                    r"^(.+?)(?=Senate|GAB|Capitol|Room)", time_and_location
                )
                if time_match:
                    time_str = time_match.group(1).strip()
                else:
                    # Fallback: take everything before common location words
                    time_str = time_and_location.split("Senate")[0].strip()
                    if not time_str:
                        time_str = time_and_location

                # Determine if this is an adjournment meeting
                is_adjournment = self._is_adjournment_meeting(time_str)

                if is_adjournment:
                    # Store for YouTube Watcher
                    adjournment_type = self._extract_adjournment_type(time_str)

                    # Try to extract the date for filtering
                    try:
                        meeting_date = parser.parse(date_str, fuzzy=True).date()
                    except Exception:
                        meeting_date = current_date

                    # Only include today's adjournment meetings
                    if meeting_date == current_date:
                        self.adjournment_meetings.append(
                            {
                                "Meeting name": committee_name,
                                "relative_start": adjournment_type,
                                "expected_date": meeting_date.isoformat(),
                                "raw_time_str": time_str,
                            }
                        )
                        log.info(
                            f"Adjournment meeting found: {committee_name} "
                            f"({adjournment_type})"
                        )
                else:
                    # Fixed-time meeting
                    utc_time = self._parse_fixed_time(date_str, time_str, timezone)
                    if not utc_time:
                        continue

                    # Skip past meetings
                    if utc_time < current_datetime:
                        continue

                    utc_time_str = utc_time.isoformat().replace("+00:00", "Z")

                    self.meetings.append(
                        {
                            "Meeting name": committee_name,
                            "Scheduled time": utc_time_str,
                            "Meeting link": None,
                            "Agenda link": None,
                            "Status": "Upcoming",
                            "Stream type": "ts_youtube",
                        }
                    )
                    log.debug(
                        f"Fixed-time meeting: {committee_name} at {utc_time_str}"
                    )

            except Exception as e:
                log.warning(f"Error parsing meeting: {e}")
                continue

        # If there are adjournment meetings for today, trigger YouTube Watcher
        if self.adjournment_meetings:
            log.info(
                f"Found {len(self.adjournment_meetings)} adjournment meeting(s) "
                f"for today - YouTube Watcher should be spawned"
            )
            self._trigger_youtube_watcher(timezone)

        return self.meetings

    def _trigger_youtube_watcher(self, timezone: str):
        """
        Trigger the YouTube Watcher dyno to monitor for adjournment meetings.

        This makes an API call to spawn the watcher with the list of expected
        adjournment meetings.
        """
        # Get API configuration
        base_url = os.getenv("HEROKU_APP_DEFAULT_DOMAIN_NAME") or os.getenv(
            "HKU_BASEURL"
        )
        if not base_url:
            log.warning("Cannot trigger YouTube Watcher - no base URL configured")
            return

        if not base_url.startswith("http"):
            base_url = f"https://{base_url}"

        # Get geo information (passed via environment in production)
        geo_id = os.getenv("ARG_GEO_ID")
        geo_name = os.getenv("ARG_LOCATION", "Virginia Senate")

        # Also need to get active fixed-time meetings for the watcher to avoid
        # We'll pass these as well
        fixed_time_meeting_names = [m["Meeting name"] for m in self.meetings]

        payload = {
            "channel_url": self.YOUTUBE_CHANNEL_URL,
            "expected_meetings": self.adjournment_meetings,
            "fixed_time_meetings": fixed_time_meeting_names,
            "geo_id": geo_id,
            "geo_name": geo_name,
            "timezone": timezone,
        }

        try:
            api_key = os.getenv("BUBBLE_HKU_KEY")
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

            response = requests.post(
                f"{base_url}/start_youtube_watcher",
                json=payload,
                headers=headers,
                timeout=30,
            )

            if response.status_code == 200:
                log.info("YouTube Watcher triggered successfully")
            else:
                log.warning(
                    f"Failed to trigger YouTube Watcher: {response.status_code} "
                    f"- {response.text}"
                )
        except Exception as e:
            log.warning(f"Error triggering YouTube Watcher: {e}")

    def get_adjournment_meetings(self) -> list:
        """
        Get the list of adjournment meetings found during parsing.

        Returns:
            list: Adjournment meeting dicts
        """
        return self.adjournment_meetings


if __name__ == "__main__":
    url = "https://apps.senate.virginia.gov/Senator/CommitteeMeetings.php"
    timezone = "America/New_York"
    schedule_type = "unique_virginiasenate"
    run_test(url=url, timezone=timezone, schedule_type=schedule_type)
