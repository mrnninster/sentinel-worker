import os
import re
import sys
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class House:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_house(self, url, timezone="America/New_York"):
        # Fetch HTML with JS rendering (rendered list)
        html_content = self.scraper.scrape_html(url=url, render="true")
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        table = soup.find("table", id="activity-table")
        if not table:
            return self.meetings

        tbody = table.find("tbody")
        if not tbody:
            return self.meetings

        meeting_div = tbody.find("tr")
        if not meeting_div:
            return self.meetings

        meeting_name = "House Convenes"
        columns = meeting_div.find_all("td")
        if len(columns) < 3:
            return self.meetings

        meeting_info = columns[2]
        a_tag = meeting_info.find("a")
        if a_tag:
            next_sibling = a_tag.find_next_sibling(string=True)
            if next_sibling:
                extracted_text = next_sibling.strip()
                extracted_text = extracted_text.replace(".", "")
                # Extract time and date using a single regex pattern
                match = re.search(
                    r"(\d+:\d+ [ap]m) on (\w+ \d+, \d{4})", extracted_text
                )

                if match:
                    meeting_time = match.group(1)
                    meeting_date = match.group(2)

                    meeting_date_time_web = f"{meeting_date} {meeting_time}"

                    meeting_date_time_web = datetime.strptime(
                        meeting_date_time_web, "%B %d, %Y %I:%M %p"
                    )

                    # Convert each datetime object to the specified timezone
                    meeting_date_time_local = timezone.localize(meeting_date_time_web)

                    meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

                    meeting_date_time = meeting_date_time_utc.strftime(
                        "%Y-%m-%dT%H:%M:%S.000Z"
                    )

                    if meeting_date_time_local.date() < now.date():
                        print("Calendar has only past meetings, try later...")
                        return self.meetings

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
                    self.meetings.append(dictionary)
        else:
            print("No meetings on this calendar presently, try later...")
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://live.house.gov/",
        schedule_type="unique_house",
        timezone="America/New_York",
    )
