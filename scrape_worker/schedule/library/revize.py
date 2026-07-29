import re
import pytz
import json
import logging
import urllib.parse
from dateutil import parser
from datetime import datetime
from bs4 import BeautifulSoup
from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter


# Setup the log
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Revize:
    def __init__(self):
        self.meetings = []
        self.render = "true"
        self.browser = None
        self.scraper = HtmlScraper()
        self.self_contained_parser = True

        # For St. Pete
        self.stpete_domain = "https://www.stpete.org/"

    def revize_table(self, url, timezone="America/New_York", agenda_url=None):
        print(f"url => {url}")
        print(f"agenda_url => {agenda_url}")
        print(f"timezone => {timezone}")

        if "stpete" in str(url).lower():
            # Get api data
            response = self.scraper.scrape_html(url=url)
            json_response = json.loads(response)

            if agenda_url:
                # Get agenda page data
                soup_string = self.scraper.fetch_with_bs(agenda_url)
                soup_new = self.scraper.convert_to_soup(string=soup_string)
                table = soup_new.find("table", class_="agenda-entry")
                table_items = table.find_all("tr")

            # Generate meetings
            for item in json_response:
                if "start" in item.keys():
                    meeting_date_time = parser.parse(item["start"], fuzzy=True)
                    meeting_date_time = datetime.strftime(
                        meeting_date_time, TimeFormatter.desired_format()
                    )
                    utc_time = TimeFormatter(meeting_date_time, timezone).get_utc_time(
                        as_datetime=True
                    )
                    schedule_time = utc_time.isoformat().replace("+00:00", "Z")
                    primary_name = item["primary_calendar_name"]
                    description = item["desc"]

                    # clean description
                    decoded_text = urllib.parse.unquote(description)
                    soup = BeautifulSoup(decoded_text, "html.parser")
                    description = soup.get_text()

                    if (
                        primary_name == "Official City Meetings"
                        and "televised live on sptv" in description.lower()
                    ):

                        # Check for cancellation
                        if re.search(
                            r"cancel(?:led|ed)",
                            str(item["title"]).lower(),
                            re.IGNORECASE,
                        ):
                            status = "Cancelled"
                        elif re.search(
                            r"cancel(?:led|ed)",
                            description.lower(),
                            re.IGNORECASE,
                        ):
                            status = "Cancelled"
                        else:
                            status = "Upcoming"

                        # Add agenda link
                        if agenda_url:
                            for table_item in table_items:
                                agenda_link = table_item.find("a").get("href")
                                agenda_header = table_item.find(
                                    "td", class_="agenda-td-headers"
                                ).get_text(strip=True)
                                str(agenda_header).replace("\n", "")
                                date_pattern = re.compile(r"(\d{2}/\d{2}/\d{2})")

                                # Search for the date in the URL
                                match = date_pattern.search(agenda_header)

                                # Generate agenda link or set as None
                                if match:
                                    extracted_date = match.group(1)
                                    original_date = datetime.strptime(
                                        extracted_date, "%m/%d/%y"
                                    )
                                    agenda_date = pytz.utc.localize(original_date)
                                    agenda_date_str = agenda_date.strftime("%Y-%m-%d")

                                    meeting_date = utc_time.date()
                                    meeting_date_str = meeting_date.strftime("%Y-%m-%d")

                                    if (
                                        agenda_date_str == meeting_date_str
                                        and "city council meeting"
                                        in str(item["title"]).lower()
                                        and "agenda packet" in str(agenda_link).lower()
                                    ):
                                        agenda_link = (
                                            f"{self.stpete_domain}{agenda_link}"
                                        )
                                        break

                                    else:
                                        agenda_link = None

                        # Create meeting object
                        meeting = {
                            "Meeting name": item["title"],
                            "Scheduled time": schedule_time,
                            "Meeting link": None,
                            "Agenda link": agenda_link if agenda_url else None,
                            "Status": status,
                        }

                        # Add meeting to list
                        self.meetings.append(meeting)
        # Return meetings
        return self.meetings
