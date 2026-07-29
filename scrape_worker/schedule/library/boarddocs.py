# boarddocs.py
import logging
import re
import requests
from datetime import datetime
import pytz
from dateutil import parser as date_parser

# Setup the log
logging.basicConfig(level=logging.ERROR)
log = logging.getLogger(__name__)


class Boarddocs:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True

    def boarddocs_table(self, url, timezone="America/New_York"):
        """
        Fetches meeting data from Boarddocs JSON endpoint and processes it.

        Args:
            url (str): The base URL of the Boarddocs page.
            timezone (str): The timezone for meeting times.

        Returns:
            list: A list of dictionaries containing processed meeting information.
        """

        # Ensure the URL ends with "BD-GETMeetingsListForSEO"
        if not url.endswith("BD-GETMeetingsListForSEO"):
            url = url.lower()
            # Replace everything after "/Board.nsf" with "/BD-GETMeetingsListForSEO"
            url = re.sub(r"(board\.nsf).*$", r"\1/BD-GETMeetingsListForSEO", url)

        try:
            response = requests.post(url, timeout=30)
            response.raise_for_status()
            data = response.json()
            log.info(f"Successfully fetched data from {url}")
        except requests.exceptions.RequestException as e:
            log.warning(f"Error fetching data from {url}: {e}")
            return []
        except ValueError as e:
            log.warning(f"Error parsing JSON from {url}: {e}")
            return []

        for meeting in data:
            meeting_info = self.extract_meeting_info(meeting, timezone)
            if meeting_info:
                self.meetings.append(meeting_info)

        return self.meetings

    def extract_meeting_info(self, meeting_data, timezone):
        """
        Processes individual meeting data to extract and format necessary information.

        Args:
            meeting_data (dict): A dictionary containing raw meeting information.
            timezone (str): The timezone for meeting times.

        Returns:
            dict or None: A dictionary with formatted meeting information or None if parsing fails.
        """
        try:
            timezone_obj = pytz.timezone(timezone)
        except pytz.UnknownTimeZoneError:
            log.warning(f"Unknown timezone: {timezone}")
            return None

        # Extract basic information
        meeting_name = meeting_data.get("Name", "").strip().replace(".", "")
        meeting_description = meeting_data.get("Description", "")
        meeting_date_str = meeting_data.get("Date", "")

        if not meeting_date_str:
            log.info("Meeting date is missing.")
            return None

        try:
            # Parse the date string (assuming it's in ISO format UTC)
            meeting_date = datetime.strptime(meeting_date_str, "%Y-%m-%dT%H:%M:%SZ")
            meeting_date = meeting_date.replace(tzinfo=pytz.utc)
        except ValueError as e:
            log.warning(f"Error parsing meeting date '{meeting_date_str}': {e}")
            return None

        def insert_space(text):
            """
            Inserts a space between a letter and a digit or after the first 5 digits in a string
            of 6 or 7 digits followed by ':' or an am/pm variant.

            Args:
                text (str): The input string to preprocess.

            Returns:
                str: The preprocessed string with spaces inserted.
            """
            # Insert space between letter and digit
            text = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", text)

            # Insert space after 5 digits if followed by another digit and a colon or am/pm
            text = re.sub(r"(\d{5})(\d)(?=[:apm])", r"\1 \2", text)

            return text

        meeting_name = insert_space(meeting_name)
        meeting_description = insert_space(meeting_description)

        # Define enhanced time pattern to extract times
        time_pattern = re.compile(
            r"\b\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?\b", re.IGNORECASE
        )

        times = []
        # Search 'Name' and 'Description' for time strings
        for text in [meeting_name, meeting_description]:
            matches = time_pattern.findall(text)
            times.extend(matches)

        if not times:
            log.info(
                f"No meeting time found for meeting '{meeting_name}'. Attempting to parse entire description."
            )
            # Attempt to parse any datetime from the description using fuzzy parsing
            try:
                combined_text = (
                    f"{meeting_date.strftime('%Y-%m-%d')} {meeting_description}"
                )
                parsed_datetime = date_parser.parse(
                    combined_text,
                    fuzzy=True,
                    default=meeting_date,
                    ignoretz=True,
                )
                times.append(parsed_datetime.strftime("%I:%M %p").lower())
            except (ValueError, TypeError):
                log.warning(
                    f"No parseable time in description for '{meeting_name}'. Skipping meeting."
                )
                return None

        # List to store parsed datetimes
        meeting_datetimes = []
        for time_str in times:
            combined_datetime_str = f"{meeting_date.strftime('%Y-%m-%d')} {time_str}"
            try:
                # Parse datetime with fuzzy=False to avoid incorrect parsing
                parsed_datetime = date_parser.parse(
                    combined_datetime_str, fuzzy=False, ignoretz=True
                )
                meeting_datetimes.append(parsed_datetime)
            except (ValueError, TypeError) as e:
                log.warning(
                    f"Error parsing meeting datetime '{combined_datetime_str}': {e}"
                )
                continue

        if not meeting_datetimes:
            log.warning(
                f"Could not parse any meeting times for meeting '{meeting_name}'."
            )
            return None

        # Pick the earliest datetime
        earliest_datetime = min(meeting_datetimes)

        # Localize the datetime to the specified timezone
        try:
            earliest_datetime = timezone_obj.localize(earliest_datetime, is_dst=None)
        except pytz.exceptions.AmbiguousTimeError:
            # Handle ambiguous times (e.g., during DST transitions)
            earliest_datetime = timezone_obj.localize(earliest_datetime, is_dst=True)
        except pytz.exceptions.NonExistentTimeError:
            # Handle non-existent times (e.g., during DST transitions)
            earliest_datetime = timezone_obj.localize(earliest_datetime, is_dst=False)
        except Exception as e:
            log.warning(f"Error localizing datetime '{earliest_datetime}': {e}")
            return None

        # Convert to UTC
        meeting_date_time_utc = earliest_datetime.astimezone(pytz.utc)

        # Format as ISO 8601 string
        formatted_date_time = meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        # Determine meeting status
        if re.search(r"Canceled", meeting_name, re.IGNORECASE):
            status = "Cancelled"
        else:
            status = "Upcoming"

        # Construct the meeting information dictionary
        meeting_info = {
            "Meeting name": meeting_name,
            "Scheduled time": formatted_date_time,
            "Meeting link": None,
            "Agenda link": None,
            "Status": status,
        }

        log.info(f"Extracted meeting: {meeting_info}")
        return meeting_info


if __name__ == "__main__":
    from dotenv import load_dotenv
    import sys
    import os
    from pytz import timezone as pytz_timezone

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test

    url = "https://go.boarddocs.com/fl/putnam/Board.nsf/Public"
    timezone = "America/New_York"

    # Make datetime.now() timezone aware
    tz = pytz_timezone(timezone)
    schedule_type = "boarddocs_table"

    run_test(
        url=url,
        timezone=timezone,
        schedule_type=schedule_type,
        # get_date_start=datetime.now(tz) - timedelta(days=10),
        # get_date_end=datetime.now(tz) - timedelta(days=1),
    )
