# Granicus Table Library
import os
import sys
import re
import pytz
import json
from dateutil import tz
from dateutil.parser import parse
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Granicus:

    def __init__(self):
        self.meetings = []
        self.look_for_date_column = False
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def parse_datetime(self, datetime_str, fuzzy=True):
        try:
            # Parse the datetime string with fuzzy logic if enabled
            dt = parse(datetime_str, fuzzy=fuzzy)
            # Convert to naive datetime (no timezone info)
            naive_dt = dt.replace(tzinfo=None)
            return naive_dt
        except Exception as e:
            print(f"Error parsing datetime string: {datetime_str}, error: {e}")
            return None

    def parse_and_localize_datetime(self, datetime_str, timezone):
        dt = parse(datetime_str, fuzzy=True)
        naive_dt = dt.replace(tzinfo=None)
        local_tz = tz.gettz(timezone)
        localized = naive_dt.replace(tzinfo=local_tz)
        return localized.astimezone(tz.gettz("UTC")).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def granicus_1_table_tlc(self, url, timezone="America/Chicago"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        if soup.find("table"):
            self.look_for_date_column = True
            return self.granicus_1_table(url, timezone, soup=soup)
        else:
            # Directly extract the text from the soup
            json_text = soup.text.strip()
            schedule_data = json.loads(json_text)

            # Now you can access the events and archives data
            events = schedule_data.get("events", [])

            # Initialize the list to store meetings
            self.meetings = []

            for event in events:
                if "date" in event and "time" in event:
                    # Combine date and time into a single string
                    event_datetime_str = f"{event['date']} {event['time']}"

                    # Parse the datetime string and set the timezone explicitly
                    event_datetime = parse(event_datetime_str)
                    local_timezone = pytz.timezone(timezone)
                    event_datetime = local_timezone.localize(event_datetime)

                    # Convert to UTC
                    utc_event_datetime = event_datetime.astimezone(pytz.utc)

                    # Convert to ISO 8601 format
                    iso_datetime_string = utc_event_datetime.isoformat()

                # Determine the status without altering the scheduled time
                status = (
                    "In progress" if event.get("in_progress") == "true" else "Upcoming"
                )

                # Add the meeting details to the list
                self.meetings.append(
                    {
                        "Meeting name": event["name"],
                        "Scheduled time": iso_datetime_string,
                        "Meeting link": event.get("media_link", ""),
                        "Agenda link": event.get("agenda_link", ""),
                        "Status": status,
                    }
                )

            return self.meetings

    def granicus_1_table(self, url, timezone="America/New_York", soup=None):
        # Fetch HTML if not provided (when called from granicus_1_table_tlc, soup is passed)
        if soup is None:
            html_content = self.scraper.scrape_html(url=url)
            soup = self.scraper.convert_to_soup(html_content)

        # Define the search attributes
        search_attributes = [
            {"class": "listingTable"},
            {"class": "listingtable"},
            {"class": "tableData"},
        ]

        # Flag to check if the table has been found
        table_found = False

        for attr in search_attributes:
            table = soup.find("table", attrs={"class": attr["class"]})
            if table is not None:
                table_found = True
                break  # Exit the loop if the table is found

        if table_found:
            if self.look_for_date_column:
                header_cells = table.find("tr").find_all("th")
                date_column_index = None

                # Find the index of the 'EventDate' column
                for index, header in enumerate(header_cells):
                    if "EventDate" in header.get("id", ""):
                        date_column_index = index
                        break
            else:
                date_column_index = 1

            rows = table.tbody.find_all("tr")

            for i, row in enumerate(rows):
                columns = row.find_all("td")

                # Ensure there are at least 2 elements in the columns list
                if len(columns) >= 2:
                    # Extract meeting name and remove the date
                    meeting_name = columns[0].get_text(strip=True)
                    meeting_name = re.sub(
                        r" on \d{4}-\d{2}-\d{2} \d{1,2}:\d{2} (AM|PM)",
                        "",
                        meeting_name,
                    )

                    # Extract meeting date and time
                    raw_meeting_date_time = columns[date_column_index].get_text(
                        strip=True
                    )
                    status = "Upcoming"
                    meeting_link = None
                    a_tag = columns[1].find("a", href=True)
                    if a_tag:
                        if (
                            a_tag["href"] != "javascript:void(0);"
                        ):  # Regular link exists and it's not a placeholder
                            meeting_link = a_tag["href"]
                            # If the link is protocol-relative (starts with '//'), prepend 'https:'
                            if meeting_link.startswith("//"):
                                meeting_link = "https:" + meeting_link
                    # If there's no regular link or it's a placeholder, look for on-click link
                    if not meeting_link:
                        a_tag = columns[1].find("a", href="javascript:void(0);")
                        if a_tag:
                            meeting_link_onclick = a_tag.get("onclick")
                            # The link is embedded in a javascript function, so we need to extract it
                            meeting_link = (
                                re.findall(r"'(.*?)'", meeting_link_onclick)[0]
                                if meeting_link_onclick
                                else None
                            )
                            if meeting_link and meeting_link.startswith("//"):
                                meeting_link = "https:" + meeting_link

                    # Confirm meeting not in progress
                    phrases_to_check = [
                        "inprogress",
                        "insession",
                        "viewmeetinglive",
                        "viewnow",
                    ]
                    lowercase_string = re.sub(r"\W+", "", raw_meeting_date_time.lower())
                    if any(phrase in lowercase_string for phrase in phrases_to_check):
                        meeting_date_time = datetime.now(pytz.UTC).replace(
                            second=0, microsecond=0
                        )
                        meeting_date_time = (
                            meeting_date_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                            + "Z"
                        )
                        status = "In progress"
                    else:
                        # attempt to parse the date time
                        try:
                            meeting_date_time = self.parse_datetime(
                                raw_meeting_date_time, timezone
                            )
                        except ValueError:
                            # remove any leading unix timestamp,  try again
                            meeting_date_time = re.sub(
                                r"^\d+", "", raw_meeting_date_time
                            ).strip()
                            try:
                                meeting_date_time = self.parse_datetime(
                                    meeting_date_time, timezone
                                )
                            except ValueError:
                                print(
                                    "can't automatically parse date, attempting structured parsers"
                                )

                                meeting_date_time = re.sub(r"\s", "", meeting_date_time)

                                # Define a regular expression pattern to match the date and time components
                                pattern = r"(\w+),(\w+)(\d+,\d+)-(\d+:\d+[APap][Mm])"
                                pattern_2 = r"(\d+)(\w+)(\d+,\d+)-(\d+:\d+[APap][Mm])"

                                # Use regex to extract the components
                                match = re.search(pattern, meeting_date_time)
                                match2 = re.search(pattern_2, meeting_date_time)

                                # Strip non-alphanumeric characters and convert to lowercase
                                clean_meeting_date_time = re.sub(
                                    r"[^\w\s]", "", meeting_date_time
                                ).lower()
                                meeting_date_time = self.parse_and_localize_datetime(
                                    meeting_date_time, timezone
                                )

                                if match:
                                    meeting_date_time = meeting_date_time.replace(
                                        match.group(0),
                                        match.group(0)[match.group(0).index(",") + 1 :],
                                    )
                                elif match2:
                                    meeting_date_time = meeting_date_time[10:]

                                else:
                                    # Convert to datetime
                                    try:
                                        meeting_date_time = datetime.strptime(
                                            meeting_date_time.replace("\xa0", " "),
                                            "%B%d,%Y-%I:%M%p",
                                        )
                                    except ValueError:
                                        print(
                                            f"value error in meeting_date_time => {meeting_date_time}"
                                        )
                                        meeting_date_time = datetime.strptime(
                                            meeting_date_time.replace("\xa0", " "),
                                            "%b%d,%Y-%I:%M%p",
                                        )

                        # Convert to the specified timezone
                        meeting_date_time = meeting_date_time.replace(
                            tzinfo=tz.gettz(timezone)
                        ).astimezone(tz.gettz("UTC"))
                        # Convert to JSON-friendly UTC date/time string
                        meeting_date_time = (
                            meeting_date_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                            + "Z"
                        )

                    # Extract agenda link
                    a_tag = columns[2].find("a", href=True)
                    agenda_link = (
                        a_tag["href"] if a_tag else None
                    )  # Extracts agenda link from 3rd column
                    if agenda_link and agenda_link.startswith("//"):
                        agenda_link = "https:" + agenda_link

                    self.meetings.append(
                        {
                            "Meeting name": meeting_name,
                            "Scheduled time": meeting_date_time,
                            "Meeting link": meeting_link,
                            "Agenda link": agenda_link,
                            "Status": status,
                        }
                    )
            return self.meetings

    def granicus_1_table_v2(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        search_attributes = [{"class": "listingTableUpcoming", "id": "table-upcoming"}]
        meeting_terms = {
            "County Commission",
            "Commission Meeting",
            "County Commission Meeting",
        }
        timezone = pytz.timezone(timezone)

        for attr in search_attributes:
            table = soup.find("table", attr)
            if table is not None:
                rows = table.tbody.find_all("tr")

                for i, row in enumerate(rows):
                    columns = row.find_all("td")

                    if len(columns) >= 1:
                        meeting_name = columns[1].get_text(strip=True)
                        meeting_name = re.sub(
                            r" on \d{4}-\d{2}-\d{2} \d{1,2}:\d{2} (AM|PM)",
                            "",
                            meeting_name,
                        )
                        if meeting_name.lower().endswith("meeting"):
                            for term in meeting_terms:
                                if term.lower() in meeting_name.lower():
                                    meeting_name = meeting_name[
                                        :-6
                                    ].strip()  # Remove "meeting" and strip spaces

                        status = (
                            "In progress"
                            if "in progress" in columns[0].get_text(strip=True).lower()
                            else "Upcoming"
                        )
                        meeting_link = None

                        if status == "In progress":
                            a_tag = columns[0].find("a", href=True)
                            if a_tag is not None:
                                if (
                                    a_tag["href"] != "javascript:void(0);"
                                ):  # Regular link exists and it's not a placeholder
                                    meeting_link = a_tag["href"]
                                    # If the link is protocol-relative (starts with '//'), prepend 'https:'
                                    if meeting_link.startswith("//"):
                                        meeting_link = "https:" + meeting_link
                                else:
                                    a_tag = columns[0].find(
                                        "a", href="javascript:void(0);"
                                    )

                                    meeting_link_onclick = a_tag.get("onclick")

                                    # The link is embedded in a javascript function, so we need to extract it
                                    meeting_link = (
                                        re.findall(r"'(.*?)'", meeting_link_onclick)[0]
                                        if meeting_link_onclick
                                        else None
                                    )
                                    if meeting_link and meeting_link.startswith("//"):
                                        meeting_link = "https:" + meeting_link

                        meeting_date_time = columns[0].get_text(strip=True)
                        if meeting_date_time == "In Progress":
                            meeting_date_time = datetime.now(pytz.utc).replace(
                                second=0, microsecond=0
                            )
                        else:
                            # Convert string to datetime
                            try:
                                meeting_date_time = datetime.strptime(
                                    meeting_date_time.replace("\xa0", " "),
                                    "%m/%d/%y %I:%M%p",
                                )
                            except ValueError:
                                meeting_date_time = datetime.strptime(
                                    meeting_date_time.replace("\xa0", " "),
                                    "%m/%d/%y %I:%M%p",
                                )
                            # Convert to the specified timezone
                            Aware_time = timezone.localize(meeting_date_time)
                            meeting_date_time = Aware_time.astimezone(pytz.UTC)

                        # Convert to JSON-friendly UTC date/time string
                        meeting_date_time = (
                            meeting_date_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                            + "Z"
                        )
                        if "href" in columns[2].attrs:
                            a_tag = columns[2].find("a", href=True)
                        else:
                            a_tag = columns[3].find("a", href=True)

                        agenda_link = (
                            a_tag["href"] if a_tag else None
                        )  # Extracts agenda link from 3rd column
                        if agenda_link and agenda_link.startswith("//"):
                            agenda_link = "https:" + agenda_link

                        self.meetings.append(
                            {
                                "Meeting name": meeting_name,
                                "Scheduled time": meeting_date_time,
                                "Meeting link": meeting_link,
                                "Agenda link": agenda_link,
                                "Status": status,
                            }
                        )
        return self.meetings

    def granicus_1_table_v3(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        # Define the search attributes
        search_attributes = [{"class": "tableData"}]

        self.meetings = []

        for attr in search_attributes:
            table = soup.find("table", attr)
            if table is not None:
                rows = table.tbody.find_all("tr")

                for i, row in enumerate(rows):
                    columns = row.find_all("td")

                    if len(columns) >= 4:
                        # Extract meeting name
                        meeting_name = columns[0].get_text(strip=True)

                        # Extract meeting date and time
                        meeting_date_time = columns[1].get_text(strip=True)
                        # Strip off the Unix timestamp at the beginning (if present)
                        meeting_date_time = re.sub(
                            r"^\d+", "", meeting_date_time
                        ).strip()
                        status = "Upcoming"
                        meeting_link = None

                        # Check if the meeting is in progress and extract the meeting link
                        if "in progress" in meeting_date_time.lower():
                            status = "In progress"
                            a_tag = columns[1].find("a", href=True)
                            if a_tag and a_tag["href"] != "javascript:void(0);":
                                meeting_link = a_tag["href"]
                                if meeting_link.startswith("//"):
                                    meeting_link = "https:" + meeting_link

                            now = datetime.now(pytz.timezone(timezone))
                            meeting_date_time = (
                                now.astimezone(pytz.UTC).strftime(
                                    "%Y-%m-%dT%H:%M:%S.%f"
                                )[:-3]
                                + "Z"
                            )
                        else:
                            # Parse the date and time for upcoming meetings
                            try:
                                meeting_date_time = datetime.strptime(
                                    meeting_date_time, "%B %d, %Y - %I:%M %p"
                                )
                            except ValueError:
                                meeting_date_time = datetime.strptime(
                                    meeting_date_time, "%b %d, %Y - %I:%M %p"
                                )
                            # Convert to the specified timezone
                            meeting_date_time = meeting_date_time.replace(
                                tzinfo=tz.gettz(timezone)
                            ).astimezone(tz.gettz("UTC"))
                            # Convert to JSON-friendly UTC date/time string
                            meeting_date_time = (
                                meeting_date_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                                + "Z"
                            )

                        # Extract agenda link
                        agenda_link = None
                        a_tag = columns[2].find("a", href=True)
                        if a_tag:
                            agenda_link = a_tag["href"]
                            if agenda_link.startswith("//"):
                                agenda_link = "https:" + agenda_link

                        self.meetings.append(
                            {
                                "Meeting name": meeting_name,
                                "Scheduled time": meeting_date_time,
                                "Meeting link": meeting_link,
                                "Agenda link": agenda_link,
                                "Status": status,
                            }
                        )
        return self.meetings

    def granicus_2_tables(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        # Define the search attributes
        search_attributes = [
            {"id": "inprogress"},
            {"id": "upcoming"},
            {"id": "live"},
        ]

        for attr in search_attributes:
            tables = soup.find_all("table", attr)
            for table in tables:
                if not table.tbody:
                    continue
                rows = table.tbody.find_all("tr")

                for i, row in enumerate(rows):
                    meeting_date_time = None
                    columns = row.find_all("td", {"class": "listItem"})

                    # Extract meeting name
                    meeting_name = columns[0].get_text(strip=True) if columns else None

                    # If no meeting is in progress or upcoming, continue to the next row
                    if not meeting_name or meeting_name in [
                        "No meetings are currently in session.",
                        "Currently there are no upcoming events.",
                        "No meeting in session",
                    ]:
                        continue

                    # Initialize meeting link and agenda link as None
                    meeting_link = None
                    agenda_link = None

                    # If there are at least two columns, extract the meeting link
                    if len(columns) >= 2:
                        meeting_link_tag = columns[1].find("a", onclick=True)
                        if meeting_link_tag:
                            onclick_text = meeting_link_tag.get("onclick")
                            link_match = re.search(r"//.*?['\"]", onclick_text)
                            if link_match:
                                meeting_link = "https:" + link_match.group(0).rstrip(
                                    "'\""
                                )

                    # If there are at least three columns, extract the agenda link
                    if len(columns) >= 3:
                        agenda_link_tag = columns[2].find("a", onclick=True)
                        if agenda_link_tag:
                            onclick_text = agenda_link_tag.get("onclick")
                            link_match = re.search(r"//.*?['\"]", onclick_text)
                            if link_match:
                                agenda_link = "https:" + link_match.group(0).rstrip(
                                    "'\""
                                )

                    # Set status and meeting date/time
                    if len(columns) >= 2:
                        meeting_date_time = columns[1].get_text(strip=True)
                        if (
                            attr["id"] == "inprogress"
                            or "in progress" in meeting_date_time.lower()
                            or "in session" in meeting_date_time.lower()
                            or "view meeting live" in meeting_date_time.lower()
                        ):
                            status = "In progress"
                            now = datetime.now(pytz.timezone(timezone))
                            meeting_date_time = now.astimezone(pytz.UTC)
                        else:
                            status = "Upcoming"
                            # Remove the leading timestamp and convert to datetime
                            meeting_date_time = meeting_date_time[10:]
                            meeting_date_time = datetime.strptime(
                                meeting_date_time, "%B %d, %Y - %I:%M %p"
                            )

                            # Convert to the specified timezone
                            meeting_date_time = meeting_date_time.replace(
                                tzinfo=tz.gettz(timezone)
                            ).astimezone(tz.gettz("UTC"))

                    # Convert to JSON-friendly UTC date/time string
                    meeting_date_time = (
                        meeting_date_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                    )

                    self.meetings.append(
                        {
                            "Meeting name": meeting_name,
                            "Scheduled time": meeting_date_time,
                            "Meeting link": meeting_link,
                            "Agenda link": agenda_link,
                            "Status": status,
                        }
                    )
        return self.meetings

    def granicus_no_table(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        self.meetings = []

        # Find the div with the id 'upcoming'
        upcoming_div = soup.find("div", {"id": "upcoming"})

        if upcoming_div:
            # Find all listings within the upcoming div
            listings = upcoming_div.find_all("div", {"class": "listing"})

            for listing in listings:
                # Extract event name
                event_name_div = listing.find("div", {"class": "eventName"})
                event_name = (
                    event_name_div.get_text(strip=True) if event_name_div else None
                )
                event_name = re.sub(
                    r"^[^a-zA-Z0-9]+", "", event_name
                ).strip()  # Strip leading non-alphanumeric characters and spaces

                if event_name:
                    event_name = event_name.split(",")[
                        0
                    ]  # Keep only the part before the first comma

                # Extract event description
                event_desc_div = listing.find("div", {"class": "eventDesc"})
                event_desc = (
                    event_desc_div.get_text(strip=True) if event_desc_div else None
                )

                # Initialize variables
                event_link = None
                event_date_time = None
                event_date_time_str = None

                # Check if the listing contains an anchor tag and text indicating 'In Progress'
                event_link_tag = listing.find("a", href=True)
                event_link_tag_text = (
                    event_link_tag.get_text().strip() if event_link_tag else None
                )
                if event_link_tag and "in progress" in event_link_tag_text.lower():
                    status = "In progress"
                    event_link = event_link_tag["href"]
                    if event_link.startswith("//"):
                        event_link = "https:" + event_link
                    event_date_time = datetime.now(pytz.timezone(timezone)).astimezone(
                        pytz.UTC
                    )
                else:
                    # Extract and parse the date and time string for 'Upcoming' events
                    status = "Upcoming"
                    date_time_str = listing.contents[-1].strip()
                    try:
                        event_date_time = datetime.strptime(
                            date_time_str.replace("\xa0", " "),
                            "%B %d, %Y - %I:%M %p",
                        )
                    except ValueError:
                        event_date_time = datetime.strptime(
                            date_time_str.replace("\xa0", " "),
                            "%b %d, %Y - %I:%M %p",
                        )
                    # Convert to the specified timezone
                    event_date_time = event_date_time.replace(
                        tzinfo=tz.gettz(timezone)
                    ).astimezone(tz.gettz("UTC"))
                    # Convert to JSON-friendly UTC date/time string

                event_date_time_str = (
                    event_date_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                )
                # Append the extracted information to the meetings list
                now = datetime.now(pytz.timezone(timezone)).date()
                if status == "In progress" or event_date_time.date() >= now:
                    self.meetings.append(
                        {
                            "Meeting name": event_name,
                            "Scheduled time": event_date_time_str,
                            "Meeting link": event_link,
                            "Status": status,
                        }
                    )

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://cityofno.granicus.com/ViewPublisher.php?view_id=2",
        schedule_type="granicus_2_tables",
        timezone="America/Chicago",
    )
