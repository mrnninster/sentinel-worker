# legistar.py
import re
import pytz
from datetime import datetime
from urllib.parse import urlparse
from dateutil import parser

if __name__ == "__main__":  # for local testing
    import sys
    import os
    from dotenv import load_dotenv

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test
    from datetime import timedelta
    from pytz import timezone as pytz_timezone

from utils.scrape_html import HtmlScraper


class Legistar:
    def __init__(self):
        self.meetings = []
        self.default_mapping = {
            "Meeting name": 0,
            "Meeting date_only": 1,
            "Meeting time_only": 3,
            "table_class": "rgMasterTable",
            "video_class": "videolink",
            "in progress string": "In progress",
            "Agenda link columns": [
                6,
                7,
            ],  # Default columns to try for 'Agenda link'
        }
        self.self_contained_parser = True
        self.render = "true"

    def parse_date_time(self, date_str, time_str, timezone):
        # Skip parsing for non-standard time formats like 'Deferred'
        if ":" not in time_str or not any(char.isdigit() for char in time_str):
            # print(f"Non-standard time format: {time_str}")
            return None
        try:
            # Create a timezone object for the given timezone string
            meeting_timezone = pytz.timezone(timezone)

            # Check if time_str has AM/PM indicator
            if "AM" in time_str.upper() or "PM" in time_str.upper():
                # Time is in 12-hour format
                meeting_datetime = datetime.strptime(
                    date_str + " " + time_str, "%m/%d/%Y %I:%M %p"
                )
            else:
                # Time is in 24-hour format
                meeting_datetime = datetime.strptime(
                    date_str + " " + time_str, "%m/%d/%Y %H:%M"
                )

            # Localize the datetime object to the meeting timezone
            localized_meeting_datetime = meeting_timezone.localize(meeting_datetime)

            # Convert to UTC
            utc_meeting_datetime = localized_meeting_datetime.astimezone(pytz.utc)
            return utc_meeting_datetime
        except ValueError:
            # Handle invalid date/time format
            return None

    def legistar_table(self, url, timezone="America/New_York", mapping_override=None):

        scraper = HtmlScraper()
        # Combine default mapping with overrides
        mapping = self.default_mapping.copy()
        if mapping_override:
            mapping.update(mapping_override)

        self.wait_for_selector = "table." + mapping["table_class"]
        response = scraper.scrape_html(
            url=url,
            render=self.render,
            wait_for_selector=self.wait_for_selector,
        )
        soup = scraper.convert_to_soup(string=response)

        # Extract the domain from the URL
        domain = urlparse(url).scheme + "://" + urlparse(url).netloc
        search_attributes = [{"class": mapping["table_class"]}]

        # Get the current date in current timezone
        now = datetime.now(pytz.timezone(timezone)).date()

        for attr in search_attributes:
            table = soup.find("table", attr)
            if table is not None:
                tbodies = table.find_all("tbody")
                for tbody in tbodies:
                    rows = tbody.find_all("tr")
                    valid_rows_found = (
                        False  # Flag to indicate if any valid rows are found
                    )

                    for row in rows:
                        columns = row.find_all("td")
                        if (
                            len(columns)
                            >= max(
                                mapping.get("Meeting date_only", 0),
                                mapping.get("Meeting time_only", 0),
                            )
                            + 1
                        ):
                            valid_rows_found = True  # A valid row is found

                            meeting_data = {}

                            # Extract and process meeting date and time
                            meeting_date_index = mapping.get("Meeting date_only")
                            meeting_time_index = mapping.get("Meeting time_only")
                            meeting_date = (
                                columns[meeting_date_index].get_text(strip=True)
                                if meeting_date_index is not None
                                else ""
                            )
                            meeting_time = (
                                columns[meeting_time_index].get_text(strip=True)
                                if meeting_time_index is not None
                                else ""
                            )
                            # print(f"meeting date {meeting_date}")
                            # print(f"meeting time: {meeting_time}")
                            # Call the parse_date_time function
                            meeting_date_time = self.parse_date_time(
                                meeting_date, meeting_time, timezone
                            )
                            # print(f"meeting date time: {meeting_date_time}")
                            # If the meeting date is not today or in the future, skip it
                            if not meeting_date_time or meeting_date_time.date() < now:
                                continue

                            # Check for 'Meeting link' and status
                            video_elements = row.select(f'a.{mapping["video_class"]}')
                            meeting_link = None
                            status_raw = None
                            for element in video_elements:
                                # print(f"element: {element}")
                                if "href" in element.attrs:
                                    meeting_link = element["href"]
                                    # print(f"meeting link: {meeting_link}")
                                    if not meeting_link.startswith("http"):
                                        meeting_link = (
                                            domain + "/" + meeting_link.lstrip("/")
                                        )
                                status_raw = element.get_text(strip=True).replace(
                                    "\xa0", " "
                                )

                            meeting_data["Meeting link"] = meeting_link

                            # Set status based on the extracted text
                            in_progress_string = mapping.get(
                                "in progress string", "In progress"
                            ).lower()
                            # Replace non-breaking spaces and make case-insensitive for regex match
                            status_raw_cleaned = (
                                status_raw.lower().replace("\xa0", " ")
                                if status_raw
                                else None
                            )
                            # List of phrases that indicate a meeting is in progress
                            # (checked as substrings, so "stream" will match "streaming")
                            phrases_to_check = [
                                in_progress_string,
                                "stream",
                                "live",
                                "progress",
                                "session",
                                "now",
                                "watch",
                            ]
                            if status_raw_cleaned and any(
                                phrase in status_raw_cleaned
                                for phrase in phrases_to_check
                            ):
                                meeting_data["Status"] = "In progress"
                            elif status_raw_cleaned and re.search(
                                r"cancelled", status_raw_cleaned, re.IGNORECASE
                            ):
                                meeting_data["Status"] = "Cancelled"
                            else:
                                meeting_data["Status"] = "Upcoming"

                            # Process 'Agenda link'
                            agenda_link = None
                            agenda_link_columns = mapping.get(
                                "Agenda link columns", [6, 7]
                            )
                            for col_index in agenda_link_columns:
                                if len(columns) > col_index:
                                    link_element = columns[col_index].find(
                                        "a", href=True
                                    )
                                    if link_element and "href" in link_element.attrs:
                                        agenda_link = link_element["href"]
                                        if not agenda_link.startswith("http"):
                                            agenda_link = (
                                                domain + "/" + agenda_link.lstrip("/")
                                            )
                                        break  # Stop after finding the first valid link
                            meeting_data["Agenda link"] = agenda_link

                            for key, value in mapping.items():
                                if key not in [
                                    "table_class",
                                    "video_class",
                                    "Meeting link",
                                    "Agenda link columns",
                                    "Meeting date_only",
                                    "Meeting time_only",
                                    "in progress string",
                                ]:
                                    # generic handling for extra columns
                                    try:
                                        # Ensure value is an integer or a list of integers for column index
                                        if isinstance(value, list):
                                            # Handle list of column indices (e.g., for 'Agenda link')
                                            for val in value:
                                                if len(columns) > val:
                                                    meeting_data[key] = columns[
                                                        val
                                                    ].get_text(strip=True)
                                                    break  # Use the first valid column in the list
                                        else:
                                            # Handle single column index
                                            index = int(value)
                                            meeting_data[key] = columns[index].get_text(
                                                strip=True
                                            )
                                    except (ValueError, IndexError):
                                        meeting_data[key] = None
                            if meeting_date_time:
                                meeting_data["Scheduled time"] = (
                                    meeting_date_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[
                                        :-3
                                    ]
                                    + "Z"
                                )

                            self.meetings.append(meeting_data)

        return self.meetings

    def legistar_table_nyc(self, url, timezone="America/New_York"):

        def normalize_text(input_text):
            # Replace non-breaking spaces and other invisible characters
            cleaned_text = re.sub(r"\s+", " ", input_text, flags=re.UNICODE)
            # Trim whitespace and convert to lowercase
            return cleaned_text.strip().lower()

        nyc_column_mapping = {
            "Agenda link columns": [7],
            "in progress string": "Live",
            "Meeting location": 4,
        }

        # use lower case for normalized matching
        link_mapping = {
            "council chambers - city hall": "https://legistar.council.nyc.gov/Webcasts/Default.aspx?From=InSite&LocationID=1",
            "committee room - city hall": "https://legistar.council.nyc.gov/Webcasts/Default.aspx?From=InSite&LocationID=2",
            "250 broadway - committee room, 14th floor": "https://legistar.council.nyc.gov/Webcasts/Default.aspx?From=InSite&LocationID=3",
            "250 broadway - committee room, 16th floor": "https://legistar.council.nyc.gov/Webcasts/Default.aspx?From=InSite&LocationID=4",
        }

        meetings = self.legistar_table(
            url, timezone=timezone, mapping_override=nyc_column_mapping
        )

        for meeting in meetings:
            location_text = meeting.get("Meeting location", "")
            meeting_link = meeting.get("Meeting link")
            if not meeting_link:
                meeting_link = ""
            if not location_text:
                location_text = ""

            # Update the split logic to look for "Jointly" or "Vote"
            parts = re.split(r"jointly|vote", location_text.lower())

            meeting["Meeting location"] = parts[0].strip() if parts else ""

            location_text_normalized = normalize_text(meeting["Meeting location"])

            # Check if "Meeting link" ends with a digit 1-4, if not, update based on "Meeting location"
            if not meeting_link or not re.search(r"[1-4]$", meeting_link):
                meeting["Meeting link"] = link_mapping.get(
                    location_text_normalized, meeting_link
                )

        return meetings


if __name__ == "__main__":
    from dotenv import load_dotenv
    import sys
    import os
    from pytz import timezone as pytz_timezone

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test

    url = "https://mwdh2o.legistar.com/Calendar.aspx"
    timezone = "America/New_York"

    # Make datetime.now() timezone aware
    tz = pytz_timezone(timezone)

    run_test(
        url=url,
        timezone=timezone,
        schedule_type="legistar_table",
        # get_date_start=datetime.now(tz) - timedelta(days=10),
        # get_date_end=datetime.now(tz) - timedelta(days=1),
    )
