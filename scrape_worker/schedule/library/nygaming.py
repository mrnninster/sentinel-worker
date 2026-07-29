import os
import re
import sys
import pytz
from urllib.parse import urlparse
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Nygaming:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_nygaming(self, url, timezone="America/New_York"):
        # Fetch HTML with JS rendering (rendered list)
        html_content = self.scraper.scrape_html(url=url, render="true")
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc
        div = soup.find("ul", class_="verticalslider_contents")
        if not div:
            print("No slider contents found...")
            return self.meetings

        list_item = div.find("li", class_="activeContent")
        if not list_item:
            print("No active content found...")
            return self.meetings

        meeting_name_tag = list_item.find("h3")
        if not meeting_name_tag:
            return self.meetings

        meeting_date_tag = meeting_name_tag.find_next("p")
        if not meeting_date_tag:
            return self.meetings

        meeting_date_text = meeting_date_tag.get_text(strip=True)
        # Regular expression pattern to match the date and time
        pattern = r"(\b\w+\s\d{1,2},\s\d{4}\sat\s\d{1,2}:\d{2}[AP]M\b)"

        # Search for the pattern in the text
        match = re.search(pattern, meeting_date_text)

        if match:
            meeting_name = meeting_name_tag.get_text(strip=True)
            next_p = meeting_date_tag.find_next("p")
            agenda_tag = next_p.find(
                "a", string=lambda text: text and "Agenda" in text
            ) if next_p else None
            agenda_link = agenda_tag.get("href") if agenda_tag else None
            if agenda_link is not None:
                agenda_link = domain + agenda_link

            extracted_text = match.group(1)
            meeting_date, meeting_time = extracted_text.split(" at ")
            meeting_date_time_web = f"{meeting_date} {meeting_time}"

            meeting_date_time_web = datetime.strptime(
                meeting_date_time_web, "%B %d, %Y %I:%M%p"
            )

            # Convert each datetime object to the specified timezone
            meeting_date_time_local = timezone.localize(meeting_date_time_web)

            meeting_date_text_utc = meeting_date_time_local.astimezone(pytz.utc)

            meeting_date_time = meeting_date_text_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            if meeting_date_time_local.date() < now.date():
                print("Skipping past meetings currently on the calendar...")
                return self.meetings

            if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                status = "Cancelled"
            else:
                status = "Upcoming"
            meeting_link = None

            self.meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date_time,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
            )
        else:
            print("No meetings currently on the calendar...")

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://www.gaming.ny.gov/about/index.php?ID=2",
        schedule_type="unique_nygaming",
        timezone="America/New_York",
    )
