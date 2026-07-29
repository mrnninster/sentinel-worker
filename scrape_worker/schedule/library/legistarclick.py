import os
import sys
import asyncio
import logging
import re
from datetime import datetime, UTC
import pytz
from dateutil import tz
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from utils.playwright_utils import BrowserManager
from schedule.schedule_scraper import run_test

# Setup the log
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Legistarclick:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    async def legistarclick_table(self, url, timezone="America/New_York"):
        browser_manager = BrowserManager()
        try:
            await browser_manager.launch_browser()
            page = await browser_manager.context.new_page()
            await page.goto(url)
            await asyncio.sleep(5)

            year = str(datetime.now().year)

            await page.wait_for_selector("table.rgMasterTable", timeout=120000)

            # Scroll the table into view
            await page.evaluate(
                "document.querySelector('table.rgMasterTable').scrollIntoView()"
            )

            # Wait for a brief moment after scrolling (adjust as needed)
            await asyncio.sleep(2)

            await asyncio.sleep(10)
            # Click on the input field to focus it (assuming you need to do this)
            await page.click("#ctl00_ContentPlaceHolder1_lstYears_Input")

            # Wait for the dropdown options to appear (you might need to adjust the selector)
            await page.wait_for_selector(".rcbItem")

            # Click on the specific option using XPath
            await page.evaluate(
                """(year) => {
                const options = document.querySelectorAll('.rcbItem');
                for (const option of options) {
                    if (option.textContent.includes(year)) {
                        option.click();
                        break;
                    }
                }
            }""",
                year,
            )
            await asyncio.sleep(10)
            await page.wait_for_selector("table.rgMasterTable", timeout=120000)

            # Get the HTML content
            html_content = await page.content()

            # Use Beautiful Soup to parse the HTML
            soup = self.scraper.convert_to_soup(string=html_content)

            meetings = await self.table_scraper(soup, url, timezone)

            # Add the meeting info to the list
            self.meetings.extend(meetings)

        finally:
            await browser_manager.close_browser()

        return self.meetings

    async def table_scraper(self, soup, url, timezone="America/New_York"):
        meetings = []

        timezone = pytz.timezone(timezone)

        # Extract the domain from the URL
        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        # Define the search attributes
        search_attributes = [{"class": "rgMasterTable"}]

        # Get the current date in UTC
        now = datetime.now(UTC)

        for attr in search_attributes:
            table = soup.find("table", attr)
            if table is not None:
                rows = table.tbody.find_all("tr")

                for i, row in enumerate(rows):
                    columns = row.find_all("td")
                    class_names = ["videolink"]

                    # Iterate through the elements with any of the specified class names
                    for class_name in class_names:
                        elements = row.select(f"a.{class_name}")
                        for element in elements:
                            stat = element.get_text(strip=True)
                            if "href" in element.attrs:
                                meeting_link = element["href"]
                            else:
                                meeting_link = None

                    # Ensure there are at least 8 elements in the columns list
                    if len(columns) >= 1:
                        # Extract meeting name
                        meeting_name = columns[0].get_text(strip=True)
                        # Extract meeting date and time
                        meeting_date = columns[1].get_text(strip=True)
                        meeting_time = columns[3].get_text(strip=True)
                        if (
                            "pm" in meeting_time.lower()
                            and "12:" not in meeting_time.lower()
                        ):
                            meeting_time = (
                                str(int(meeting_time.split(":")[0]) + 12)
                                + ":"
                                + meeting_time.split(":")[1].strip()[:-3].strip()
                            )
                        elif (
                            "am" in meeting_time.lower()
                            and "12:" in meeting_time.lower()
                        ):
                            meeting_time = (
                                "00:" + meeting_time.split(":")[1].strip()[:-3].strip()
                            )
                        elif meeting_time.lower().split == "":
                            meeting_time = None
                        else:
                            meeting_time = meeting_time.strip()[:-3].strip()
                        try:
                            meeting_date_time = datetime.strptime(
                                meeting_date + " " + meeting_time,
                                "%m/%d/%Y %H:%M",
                            )
                        except ValueError:
                            meeting_date_time = datetime.strptime(
                                meeting_date + " " + meeting_time, "%m/%d/%Y "
                            )

                        # Convert to the specified timezone
                        meeting_date_time = timezone.localize(meeting_date_time)
                        meeting_date_time = meeting_date_time.astimezone(pytz.utc)
                        # If the meeting date is not today or in the future, skip it
                        if meeting_date_time < now:
                            continue

                        # Convert to JSON-friendly UTC date/time string

                        meeting_date_time = (
                            meeting_date_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                            + "Z"
                        )

                        if meeting_link and meeting_link.startswith("//"):
                            meeting_link = "https:" + meeting_link

                        try:
                            agenda_link = columns[6].find("a")["href"]
                        except (TypeError, KeyError):
                            try:
                                agenda_link = columns[7].find("a")["href"]
                            except (TypeError, KeyError):
                                agenda_link = None
                        # Prepend the domain to the links if they are not None
                        if agenda_link is not None:
                            agenda_link = domain + "/" + agenda_link

                        status_raw = stat
                        status_raw = status_raw.replace("\xa0", " ")
                        # Check if the updated string contains "In progress"
                        if re.search(r"In progress", status_raw):
                            status = "In progress"
                        # Check if the updated string contains "cancelled" (case-insensitive)
                        elif re.search(r"Canceled", status_raw, re.IGNORECASE):
                            status = "Cancelled"
                        else:
                            status = "Upcoming"

                        meetings.append(
                            {
                                "Meeting name": meeting_name,
                                "Scheduled time": meeting_date_time,
                                "Meeting link": meeting_link,
                                "Agenda link": agenda_link,
                                "Status": status,
                            }
                        )
        return meetings


if __name__ == "__main__":
    run_test(
        url="https://broward.legistar.com/Calendar.aspx",
        schedule_type="legistarclick_table",
        timezone="America/New_York",
    )
