# No longer used

import os
import re
import sys
import pytz
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Orlando:

    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()
        self.main_url = "https://www.orlando.gov/Our-Government/Mayor-City-Council/City-Council-Meetings"

    def is_duplicate(self, new_meeting):
        for existing_meeting in self.meetings:
            if (
                existing_meeting["Meeting name"] == new_meeting["Meeting name"]
                and existing_meeting["Scheduled time"] == new_meeting["Scheduled time"]
            ):
                return True
        return False

    def scrape_main(self, url, timezone="America/New_York"):
        soup = self.scraper.fetch_with_bs(url=url)
        soup = self.scraper.convert_to_soup(string=soup)

        main_meetings = []
        timezone = pytz.timezone(timezone)
        now = datetime.now(timezone)
        divs = soup.find_all("div", class_="list-item-container homepage-pin")
        for div in divs:
            name_div = div.find("h2", class_="list-item-title")
            date_div = div.find("p", class_="event-date published-on small-text")

            # Skip if required elements are missing (page structure may have changed)
            if not name_div or not date_div:
                continue

            meeting_name = name_div.get_text().strip().split("-")[0]
            date_details = date_div.get_text().strip()

            # Expect format: "date | time"
            if "|" not in date_details:
                continue

            try:
                meeting_date, meeting_time = [
                    part.strip() for part in date_details.split("|")
                ]

                if "to" in meeting_time.lower():
                    meeting_time = meeting_time.split(" to ")[0]

                meeting_date_time_web = f"{meeting_date} {meeting_time}"
                meeting_date_time_web = meeting_date_time_web.strip()
                meeting_date_time_web = datetime.strptime(
                    meeting_date_time_web, "%A, %B %d, %Y %I:%M %p"
                )
                meeting_date_time_local = timezone.localize(meeting_date_time_web)
                meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

                meeting_date_time = meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

                if meeting_date_time_local.date() < now.date():
                    continue

                if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                    status = "Cancelled"
                else:
                    status = "Upcoming"
                meeting_link = None
                agenda_link = None

                dictionary = {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date_time,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
                main_meetings.append(dictionary)
            except (ValueError, IndexError):
                # Skip items that don't match expected format
                continue

        return main_meetings

    def unique_orlando(self, url, timezone="America/New_York"):
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        # Try to find multi-date-list container (may not exist on all pages)
        div_element = None
        container = soup.find("div", class_="multi-date-list-container")
        if container:
            div_element = container.find("ul", class_="multi-date-list future-events-list")

        timezone = pytz.timezone(timezone)
        now = datetime.now(timezone)
        # Extract the text from the <div> element if it exists
        if div_element:
            # Find all <li> elements within the <div> element
            li_elements = div_element.find_all("li")

            for li_element in li_elements:
                agenda_link = None
                # Extract the text from the <li> element
                li_text = li_element.get_text(strip=True)

                # Use regex to extract date, time, and period (AM/PM)
                match = re.search(r", (.+\d{4}) \| (\d+:\d+ [APM]+)", li_text)

                if match:
                    day_month = match.group(1)

                    time_data = match.group(2)

                    # Reformat the extracted parts
                    meeting_date_time_web = f"{day_month} {time_data}"

                    meeting_date_time_web = datetime.strptime(
                        meeting_date_time_web, "%B %d, %Y %I:%M %p"
                    )
                    meeting_date_time_local = timezone.localize(meeting_date_time_web)
                    meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)
                    # Format it to the desired ISO 8601 format
                    meeting_date_time = meeting_date_time_utc.strftime(
                        "%Y-%m-%dT%H:%M:%S.000Z"
                    )

                    if meeting_date_time_local.date() < now.date():
                        continue

                    # Find the element by its class
                    element = soup.find(class_="oc-page-title")
                    meeting_name = element.get_text(strip=True)

                    # Find the <a> element by its text content
                    link_element = soup.find("a", string="Click to join the meeting")

                    # Extract the link from the href attribute
                    if link_element:
                        meeting_link = link_element["href"]
                    else:
                        meeting_link = None

                    # Check if the time difference is exactly 60 minutes (1 hour)
                    if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                        status = "Cancelled"
                    else:
                        status = "Upcoming"

                    self.meetings.append(
                        {
                            "Meeting name": meeting_name,
                            "Scheduled time": meeting_date_time,
                            "Meeting link": meeting_link,
                            "Agenda link": agenda_link,
                            "Status": status,
                        }
                    )
        try:
            main_meetings = self.scrape_main(self.main_url)
            for meeting in main_meetings:
                if not self.is_duplicate(meeting):
                    self.meetings.append(meeting)
        except ValueError:
            main_meetings = None
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://www.orlando.gov/Our-Government/Mayor-City-Council/City-Council-Meetings",
        schedule_type="unique_orlando",
        timezone="America/New_York",
    )
