import os
import sys
import asyncio
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
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


class Hsd:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()
        self.agenda_url = None

    def is_future_meeting(self, time, timezone="America/New_York"):
        # Parse the scheduled time string
        scheduled_datetime = datetime.strptime(time, "%Y-%m-%dT%H:%M:%S.000Z")

        timezone = pytz.timezone(timezone)
        # Make the scheduled datetime timezone-aware (assuming it's in the same timezone as now)
        scheduled_datetime = timezone.localize(scheduled_datetime)

        scheduled_datetime = scheduled_datetime.astimezone(pytz.utc)

        # Compare with the current time
        now = datetime.now(UTC)
        return scheduled_datetime > now

    async def unique_hsd(self, url, timezone="America/New_York"):
        # Handle date substitution
        current_date = datetime.now().strftime("%Y%m%d")
        url = url.replace("%date%", current_date)

        browser_manager = BrowserManager()
        try:
            await browser_manager.launch_browser()
            page = await browser_manager.context.new_page()
            await page.goto(url)
            await asyncio.sleep(5)

            try:
                await page.wait_for_selector("li.wcm-calendar-this-month")
            except PlaywrightTimeoutError:
                log.warning("No meetings on this page for now, check later...")
                return self.meetings  # Return an empty meetings list

            await page.query_selector_all("li.wcm-calendar-this-month")

            if await page.wait_for_selector("button.bb-butt"):
                await page.click("button.bb-butt")
            await asyncio.sleep(10)

            # Get the HTML content
            html_content = await page.content()

            soup = self.scraper.convert_to_soup(string=html_content)

            timezone_obj = pytz.timezone(timezone)

            now = datetime.now(UTC)

            wrap = soup.find("div", class_="sp home")
            if not wrap:
                return self.meetings

            main = wrap.find("main")
            if not main:
                return self.meetings

            column = main.find("div", class_="sp-column two")
            if not column:
                return self.meetings

            div = column.find("div", class_="wcm-calendar-month")
            if not div:
                return self.meetings

            content = div.find("ul", class_="wcm-calendar-month-view")
            if not content:
                return self.meetings

            this_month = content.find("li", class_="wcm-calendar-this-month")
            if not this_month:
                return self.meetings

            list_items = this_month.find("ol")
            if not list_items:
                return self.meetings

            for item in list_items:
                meet = item.find("div", role="link")
                if meet is not None:
                    meeting_info = item.find("div").get_text(strip=True)
                    # Find the position of the colon
                    colon_index = meeting_info.find(":")

                    # Now urls should contain the two individual URLs
                    meeting_name = meeting_info[:colon_index].strip()
                    meeting_time = meeting_info[colon_index + 1 :].strip()
                    meeting_date = item.get("aria-label")

                    meeting_date_time_web = meeting_date + " " + meeting_time
                    meeting_date_time_web = datetime.strptime(
                        meeting_date_time_web, "%A, %B %d, %Y %I:%M %p"
                    )

                    # Convert each datetime object to the specified timezone
                    meeting_date_time_local = timezone_obj.localize(meeting_date_time_web)

                    meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

                    meeting_date_time = meeting_date_time_utc.strftime(
                        "%Y-%m-%dT%H:%M:%S.000Z"
                    )

                    start_of_today_local = datetime(
                        now.year, now.month, now.day, tzinfo=timezone_obj
                    )

                    if meeting_date_time_local < start_of_today_local:
                        continue

                    if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                        status = "Cancelled"
                    else:
                        status = "Upcoming"
                    meeting_link = None
                    agenda_link = None

                    meeting_info = {
                        "Meeting name": meeting_name,
                        "Scheduled time": meeting_date_time,
                        "Meeting link": meeting_link,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }

                    # Check if the meeting is in the future before adding it to the list
                    if self.is_future_meeting(meeting_info["Scheduled time"]):
                        # Add the meeting info to the list
                        self.meetings.append(meeting_info)

        finally:
            await browser_manager.close_browser()

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://hsd.senate.gov/hearings?date=%date%",
        schedule_type="unique_hsd",
        timezone="America/New_York",
    )
