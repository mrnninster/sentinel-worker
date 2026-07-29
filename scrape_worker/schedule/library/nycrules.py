import os
import sys
import asyncio
import re
from datetime import datetime
import pytz
import logging
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper, HTMLTags, HTMLAttributes
from utils.playwright_utils import BrowserManager
from schedule.schedule_scraper import run_test

BUTTON_HTML_XPATH = "//button[contains(text(), 'list')]"
DATE_COMBINED_FORMAT = "%B %d, %Y %I:%M%p"
DATE_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.000Z"
DATE_UTC_FORMAT_DATE_ONLY = "%Y-%m-%d"
GLOBAL_NUMBER_OF_YEAR_PER_MONTH_PAGE_CLICKS = 6

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Nycrules:
    def __init__(self, stream_type=""):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()
        self.stream_type = stream_type
        self.scrapper_api_key = os.getenv("SCRAPERAPICOM_API_KEY")

    async def nycrules_table(self, url, timezone="America/New_York"):
        browser_manager = BrowserManager()
        try:
            await browser_manager.launch_browser()
            page = await browser_manager.context.new_page()
            await page.goto(url)

            date_headers = await self.get_all_pages_data(page)

            await self.extract_meetings(date_headers, timezone)

        finally:
            await browser_manager.close_browser()

        return self.meetings

    async def get_all_pages_data(self, page):
        date_headers = []
        for page_num in range(0, GLOBAL_NUMBER_OF_YEAR_PER_MONTH_PAGE_CLICKS):
            await self.ensure_page_is_ready(page)

            html_content = await self.get_page_content(page)

            soup = self.scraper.convert_to_soup(string=html_content)
            page_date_headers = soup.find_all(
                HTMLTags.ROWS_TAG, class_="fc-list-heading"
            )

            if not page_date_headers:
                return date_headers

            date_headers += page_date_headers

            next_button = await page.query_selector("button.fc-next-button")
            if not next_button:
                return date_headers

            await next_button.click()

        return date_headers

    async def ensure_page_is_ready(self, page):
        if page is None or page.is_closed():
            raise Exception("Browser page is not available")

        await asyncio.sleep(1)

        await page.wait_for_selector(f"xpath={BUTTON_HTML_XPATH}")

        button = await page.query_selector_all(f"xpath={BUTTON_HTML_XPATH}")
        await button[0].click()

    async def get_page_content(self, page):
        await page.query_selector("section.hearing-calendar-section")
        return await page.content()

    async def extract_meetings(self, date_headers, timezone):
        for date_header in date_headers:
            meeting_date_row = date_header.find(
                HTMLTags.LINK_TAG, class_="fc-list-heading-main"
            ).text.strip()
            meeting_rows = date_header.find_next_siblings(
                HTMLTags.ROWS_TAG, class_="fc-list-item"
            )
            for meeting_row in meeting_rows:
                time = meeting_row.find(
                    HTMLTags.COLUMNS_TAG, class_="fc-list-item-time"
                ).text.strip()

                title_tag = meeting_row.find(
                    HTMLTags.COLUMNS_TAG, class_="fc-list-item-title"
                ).find(HTMLTags.LINK_TAG)

                url = title_tag[HTMLAttributes.LINK_ATTRIBUTE]

                start_time = time.split(" - ")[0]
                date_time_str = f"{meeting_date_row} {start_time}"
                meeting_date_time = datetime.strptime(
                    date_time_str, DATE_COMBINED_FORMAT
                )
                meeting_date = self.convert_to_utc(meeting_date_time, timezone)
                today_start = self.convert_to_utc(datetime.now(), timezone)
                local_today_start_utc = today_start.replace(
                    hour=0, minute=0, second=0, microsecond=0
                )

                if local_today_start_utc > meeting_date:
                    continue

                (
                    meeting_name,
                    agenda_link,
                    meeting_link,
                    phone_number,
                    access_id,
                    passcode,
                ) = await self.fetch_meeting_details(url)

                self.meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": meeting_date.strftime(DATE_UTC_FORMAT),
                        "Meeting link": meeting_link,
                        "Agenda link": agenda_link,
                        "Status": "Upcoming",
                        "Stream type": self.stream_type,
                        "Phone number": phone_number,
                        "Access ID": access_id,
                        "Passcode": passcode,
                    }
                )

                if meeting_row.find_next_sibling(
                    HTMLTags.ROWS_TAG, class_="fc-list-heading"
                ):
                    break

        return self.meetings

    def convert_to_utc(self, date_time, timezone):
        local_tz = pytz.timezone(timezone)
        local_dt = local_tz.localize(date_time)
        utc_dt = local_dt.astimezone(pytz.UTC)
        return utc_dt

    async def fetch_meeting_details(self, url):
        meeting_link = None

        payload = {"api_key": self.scrapper_api_key, "url": url}
        page_with_needed_data = self.scraper.fetch_with_scraperapi(payload=payload)

        detail_page_soup = self.scraper.convert_to_soup(string=page_with_needed_data)

        meeting_name = detail_page_soup.find(
            HTMLTags.H1_TAG, class_="display-4"
        ).text.strip()
        agenda_link = (
            detail_page_soup.find(HTMLTags.DIV_TAG, class_="mb-3")
            .find_all(HTMLTags.PARAGRAPH_TAG)[1]
            .find(HTMLTags.LINK_TAG)[HTMLAttributes.LINK_ATTRIBUTE]
        )
        card_body = detail_page_soup.find(HTMLTags.DIV_TAG, class_="card-body")

        phone = None
        meeting_id = None
        conference_id = None
        password = None

        if card_body:
            meeting_link = card_body.find(HTMLTags.LINK_TAG)[
                HTMLAttributes.LINK_ATTRIBUTE
            ]

            stream_types = {
                "zoom": "twilio_zoom",
                "teams": "twilio_teams",
                "webex": "twilio_webex",
                "tinyurl": "twilio_teams",
            }
            for key, value in stream_types.items():
                if key in meeting_link:
                    self.stream_type = value

            card_text = card_body.text.strip()

            patterns = {
                "phone": r"Phone(?:\s\(audio only\))?:\s*(\+?\d{1,3}?[-\s.]?\(?\d{1,3}\)?[-\s.]?\d{3}[-\s.]?\d{4})",
                "conference_id": r"Phone Conference ID:\s*([\d\s]+)#",
                "meeting_id": r"Meeting ID:\s*([\d\s]+)",
                "password": r"Password:\s*(\w+)",
            }

            phone = re.search(patterns["phone"], card_text)
            conference_id = re.search(patterns["conference_id"], card_text)
            meeting_id = re.search(patterns["meeting_id"], card_text)
            password = re.search(patterns["password"], card_text)

            if phone:
                phone = re.sub(r"[^\d]", "", phone.group(1))
                if not phone.startswith("1"):
                    phone = "1" + phone
                phone = f"+{phone}"
            else:
                phone = None

            conference_id = (
                conference_id.group(1).replace(" ", "") if conference_id else None
            )
            meeting_id = meeting_id.group(1).replace(" ", "") if meeting_id else None
            password = password.group(1).replace(" ", "") if password else None

        return (
            meeting_name,
            agenda_link,
            meeting_link,
            phone,
            meeting_id or conference_id,
            password,
        )


if __name__ == "__main__":
    run_test(
        url="https://rules.cityofnewyork.us/hearings/",
        schedule_type="nycrules_table",
        timezone="America/New_York",
    )
