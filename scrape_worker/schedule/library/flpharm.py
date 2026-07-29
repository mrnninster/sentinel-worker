import os
import sys
import logging
import re
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from utils.playwright_utils import BrowserManager
from schedule.schedule_scraper import run_test

# Setup the log
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Flpharm:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def process_date_text(self, date_text):
        date_format_1 = "%B %d, %Y"  # Example 1 format
        date_format_2 = "%B %d-%d, %Y"  # Example 2 format

        if "-" in date_text:
            # Example 1 or Example 2 format
            date_parts = date_text.split("-")
            if len(date_parts) == 2:
                try:
                    # Example 1 format
                    date1 = datetime.strptime(
                        date_parts[0].strip(), date_format_1
                    ).strftime("%Y-%m-%d")
                    date2 = datetime.strptime(
                        date_parts[1].strip(), date_format_1
                    ).strftime("%Y-%m-%d")
                except ValueError:
                    # Example 2 format
                    text_range = date_text.split()
                    date_range = date_parts[0].split()

                    date1 = f"{date_range[0]} {date_range[1]}, {text_range[5]}"
                    date2 = f"{date_parts[1].strip()}"

                    # Parse the input date text
                    date1 = datetime.strptime(date1, date_format_1)
                    date2 = datetime.strptime(date2, date_format_1)

                    # Format the date in the desired format
                    date1 = date1.strftime("%Y-%m-%d")
                    date2 = date2.strftime("%Y-%m-%d")
                return date1, date2
        else:
            date_text = datetime.strptime(date_text, date_format_1)

            # Format the date in the desired format
            date_text = date_text.strftime("%Y-%m-%d")
            return date_text

    def create_meeting(
        self,
        meeting_date_time_web,
        meeting_name,
        agenda_link,
        meeting_link=None,
        timezone="America/New_York",
    ):

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        meeting_date_time_local = timezone.localize(meeting_date_time_web)

        meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

        meeting_date_time = meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        start_of_today_local = datetime(now.year, now.month, now.day, tzinfo=timezone)

        if meeting_date_time_local < start_of_today_local:
            return None

        if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
            status = "Cancelled"
        else:
            status = "Upcoming"

        dictionary = {
            "Meeting name": meeting_name,
            "Scheduled time": meeting_date_time,
            "Meeting link": meeting_link,
            "Agenda link": agenda_link,
            "Status": status,
        }
        return dictionary

    async def unique_flpharm(self, url, timezone="America/New_York"):
        browser_manager = BrowserManager()
        try:
            await browser_manager.launch_browser()
            page = await browser_manager.context.new_page()
            await page.goto(url)

            await page.query_selector_all("div.entry-content")

            # Get the HTML content
            html_content = await page.content()

            # Use Beautiful Soup to parse the HTML
            soup = self.scraper.convert_to_soup(string=html_content)

            # Wait for the page content to load
            container = soup.find("div", id="container")
            if not container:
                return self.meetings

            content = container.find("div", id="main-content")
            if not content:
                return self.meetings

            meeting_divs = content.find_all("div", class_="entry-content")
            for meet in meeting_divs:
                table = meet.find("table")
                if table:
                    table = table.find("tbody")
                    if not table:
                        continue
                    row = table.find("tr")
                    if not row:
                        continue
                    column = row.find("td")
                    if not column:
                        continue
                    next_column = column.find_next("td")

                    meeting_name_div = column.find("em")
                    if not meeting_name_div:
                        continue
                    meeting_name = meeting_name_div.get_text(strip=True)

                    date_span = column.find("span")
                    if not date_span:
                        continue
                    date_div = date_span.find("strong")
                    if not date_div:
                        continue
                    meeting_date = date_div.get_text().strip().replace("           ", "")

                    meeting_date = self.process_date_text(meeting_date)

                    time_div = column.find("div")
                    if not time_div:
                        continue
                    time_div = time_div.find("strong")
                    if not time_div:
                        continue
                    time_text = time_div.get_text().strip().replace(".", "")

                    # Define the regular expression pattern to match the desired time format
                    time_pattern = r"\b\d{1,2}:\d{2}\s(?:am|pm)\b"
                    time_pattern2 = r"\b\d{1,2}:\d{2}(?:am|pm)\b"

                    # Find all matches of the time pattern in the text
                    match = re.search(time_pattern, time_text)
                    match2 = re.search(time_pattern2, time_text)

                    if match:
                        meeting_time = match[0]
                    elif match2:
                        meeting_time = match2[0]

                        # Parse the original string into a datetime object
                        meeting_time = datetime.strptime(meeting_time, "%I:%M%p")

                        # Format the datetime object back into a string with the desired format
                        meeting_time = meeting_time.strftime("%I:%M %p")
                    else:
                        print(f"Skipping Meeting ({meeting_name}): No time data yet...")
                        continue

                    agenda_link_div = next_column.find(
                        "a", string=lambda text: text and "Agenda" in text
                    ) if next_column else None
                    agenda_link = agenda_link_div.get("href") if agenda_link_div else None

                    if isinstance(meeting_date, tuple):
                        for meet_date in meeting_date:
                            meeting_date_time_web = meet_date + " " + meeting_time
                            try:
                                meeting_date_time_web = datetime.strptime(
                                    meeting_date_time_web, "%Y-%m-%d %I:%M %p"
                                )
                            except ValueError:
                                meeting_date_time_web = datetime.strptime(
                                    meeting_date_time_web, "%Y-%m-%d "
                                )

                            dictionary = self.create_meeting(
                                meeting_date_time_web=meeting_date_time_web,
                                agenda_link=agenda_link,
                                meeting_name=meeting_name,
                            )

                            if dictionary is not None:
                                self.meetings.append(dictionary)
                    else:
                        meeting_date_time_web = meeting_date + " " + meeting_time
                        try:
                            meeting_date_time_web = datetime.strptime(
                                meeting_date_time_web, "%Y-%m-%d %I:%M %p"
                            )
                        except ValueError:
                            meeting_date_time_web = datetime.strptime(
                                meeting_date_time_web, "%Y-%m-%d "
                            )

                        dictionary = self.create_meeting(
                            meeting_date_time_web=meeting_date_time_web,
                            agenda_link=agenda_link,
                            meeting_name=meeting_name,
                        )

                        if dictionary is not None:
                            self.meetings.append(dictionary)

        finally:
            await browser_manager.close_browser()

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://floridaspharmacy.gov/meetings/",
        schedule_type="unique_flpharm",
        timezone="America/New_York",
    )
