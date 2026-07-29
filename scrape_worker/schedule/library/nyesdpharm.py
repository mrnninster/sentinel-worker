# kaltura.py
import os
import sys
import asyncio
import logging
import re
from datetime import datetime, timedelta, UTC
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


class Nyesdpharm:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    async def unique_nyesdpharm(self, url, timezone="America/New_York"):
        browser_manager = BrowserManager()
        try:
            await browser_manager.launch_browser()
            page = await browser_manager.context.new_page()
            await page.goto(url)

            await page.wait_for_selector("a.expand.show")
            await page.click("a.expand.show")
            await asyncio.sleep(5)

            now = datetime.now(UTC)

            year = now.year.__str__()

            timezone_obj = pytz.timezone("America/New_York")

            # Get the HTML content
            html_content = await page.content()

            # Use Beautiful Soup to parse the HTML
            soup = self.scraper.convert_to_soup(string=html_content)
            div = soup.find("div", class_="layout-container")
            if not div:
                return self.meetings

            main = div.find("main", class_="main-content main-content--with-sidebar")
            if not main:
                return self.meetings

            block = main.find("div", class_="block views-element-container")
            if not block:
                return self.meetings

            items = block.find_all("div", class_="list list--accordion mb-4 accordion")

            for item in items:
                card = item.find("div", class_="card")
                if not card:
                    continue
                year_text_div = card.find("h5")
                if not year_text_div:
                    continue
                year_text = year_text_div.get_text().split("-")[1].strip()
                meeting_name = year_text_div.get_text().split("-")[0].strip()

                if year != year_text:
                    continue

                meeting_div = item.find("div", class_="text-long")
                if not meeting_div:
                    continue

                meeting_date_div = meeting_div.find("p")
                if not meeting_date_div:
                    continue
                meeting_date_text = meeting_date_div.get_text()

                # Define the regular expression pattern to match the desired date format
                date_pattern = r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s\d{1,2},\s\d{4}\b"

                # Find all matches of the date pattern in the text
                match = re.search(date_pattern, meeting_date_text)
                if match:
                    meeting_date = match[0]
                else:
                    continue

                meeting_time_div = item.find(
                    "p", string=lambda text: text and "Public Session" in text
                )
                if not meeting_time_div:
                    continue

                meeting_time_text = meeting_time_div.get_text(strip=True).replace(".", "")

                time_pattern = r"\b\d{1,2}:\d{2}\s(?:am|pm)\b"

                # Find all matches of the time pattern in the text
                matched_time = re.search(time_pattern, meeting_time_text)

                if matched_time:
                    meeting_time = matched_time[0]
                else:
                    print(f"Skipping Meeting ({meeting_name}): No time data yet...")
                    continue

                meeting_date_time_web = f"{meeting_date} {meeting_time}"

                meeting_date_time_web = meeting_date_time_web.replace(".", "")

                try:
                    # Try parsing the input time with 'a.m.' or 'p.m.'
                    meeting_date_time_web = datetime.strptime(
                        meeting_date_time_web, "%B %d, %Y %I:%M %p"
                    )
                except ValueError:
                    try:
                        # Try parsing the input time without 'am' or 'pm'
                        meeting_date_time_web = datetime.strptime(
                            meeting_date_time_web, "%B %d, %Y %I%p"
                        )
                    except ValueError:
                        meeting_date_time_web = datetime.strptime(
                            meeting_date_time_web, "%B %d, %Y "
                        )
                meeting_date_time_local = timezone_obj.localize(meeting_date_time_web)
                # Convert the time to UTC
                meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)
                # Format the UTC datetime object into the desired output string
                meeting_date_time = meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

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

        finally:
            await browser_manager.close_browser()

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://www.op.nysed.gov/professions/pharmacy/pharmacy-board-meetings",
        schedule_type="unique_nyesdpharm",
        timezone="America/New_York",
    )
