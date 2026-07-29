import os
import sys
import pytz
import requests
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class Txhouse:

    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_txhouse(self, url, timezone="America/Chicago"):
        # Fetch HTML with JS rendering (rendered list)
        html_content = self.scraper.scrape_html(url=url, render="true")
        soup = self.scraper.convert_to_soup(html_content)

        # Find the tbody containing the rows of meeting data
        main = soup.body.find("main", id="main-content") if soup.body else None
        if not main:
            return self.meetings

        grid = main.find("div", class_="grid-container")
        if not grid:
            return self.meetings

        table = grid.find("table")
        if not table:
            return self.meetings

        table_body = table.find("tbody")
        if not table_body:
            return self.meetings

        rows = table_body.find_all("tr")

        # Set timezone
        local_tz = pytz.timezone(timezone)

        # Process each row in the table
        for row in rows:
            columns = row.find_all("td")

            if len(columns) >= 3:
                # Extract meeting details
                start_time = columns[0].get_text(strip=True)
                date_str = columns[1].get_text(strip=True)
                event_name = columns[2].get_text(strip=True)

                # Check if the first column is a link (indicating the meeting is live)
                status = "Upcoming"
                meeting_link = None
                try:
                    a_tag = (
                        columns[0]
                        .find("a", href=True)
                        .find(
                            "span",
                            string=lambda text: text and "live" in text.lower(),
                        )
                    )
                except AttributeError:
                    a_tag = None

                if a_tag is not None:
                    status = "In progress"
                    meeting_link = columns[0].find("a")["href"]
                    if meeting_link.startswith("//"):
                        meeting_link = "https:" + meeting_link

                    meeting_id = meeting_link.split("/")[-1]
                    event_url = (
                        f"https://house.texas.gov/api/GetVideoEvent/{meeting_id}"
                    )
                    try:
                        event_response = requests.get(event_url, timeout=10)
                        event_data = event_response.json()
                        meeting_link = event_data.get("url", None)
                    except Exception:
                        pass

                if status == "In progress":
                    local_meeting_date_time = datetime.now(local_tz).replace(
                        second=0, microsecond=0
                    )
                else:
                    # Combine date and time into a single datetime object
                    date_time_str = f"{date_str} {start_time}"
                    try:
                        # Parse the datetime string with timezone
                        naive_meeting_date_time = datetime.strptime(
                            date_time_str, "%m/%d/%y %I:%M%p"
                        )
                        local_meeting_date_time = local_tz.localize(
                            naive_meeting_date_time
                        )
                    except ValueError:
                        continue  # Skip if date parsing fails

                # Convert to UTC
                meeting_date_time_utc = local_meeting_date_time.astimezone(pytz.utc)
                meeting_date_time_iso = (
                    meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                )

                # Extract other relevant information (e.g., agenda link, if available)
                agenda_link = None

                # Add the meeting to the list
                self.meetings.append(
                    {
                        "Meeting name": event_name,
                        "Scheduled time": meeting_date_time_iso,
                        "Meeting link": meeting_link,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }
                )

        log.info(f"Meetings: {self.meetings}")
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://house.texas.gov/videos",
        schedule_type="unique_txhouse",
        timezone="America/Chicago",
    )
