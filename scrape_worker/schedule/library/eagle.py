import os
import sys
import pytz
import json
import logging
import xmltodict
from dateutil import parser
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

RELEVANT_KEYWORDS = [
    "Planning Commission",
    "BoCC",
    "Board of County Commissioners",
    "Advisory Committee",
    "Joint Work Session",
    "ECLUR",
    "Session",
    "Committee",
    "Commission",
]


class Eagle:
    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        self.base_url = "http://vod-eaglecounty.cablecast.tv:4020"
        self.stream_url = "https://reflect-vod-eaglecounty.cablecast.tv/live-4/live/stream-1/live.m3u8"

    def get_live_page(self):
        self.live_meeting_title = None
        schedule_page_url = (
            "http://vod-eaglecounty.cablecast.tv:4020/internetchannel/schedule"
        )
        page = self.scraper.scrape_html(url=schedule_page_url)
        soup = self.scraper.convert_to_soup(page)
        schedule_item = soup.find_all("li", class_="text-white")
        for item in schedule_item:
            indicator = item.find("p", class_="mr-4 py-2")
            title = item.find("div", class_="line-clamp-1")
            # log.info(f"Indicator: {indicator}")
            # log.info(f"Title: {title}")

            # Get live meeting name
            if indicator.text.strip().lower() == "now":
                self.live_meeting_title = title.text.strip().lower()

        # log.info(f"Live meeting title: {self.live_meeting_title}")
        return self.live_meeting_title

    def unique_eagle(self, url: str, timezone: str = "America/Denver"):
        # Set timezone
        self.timezone = timezone
        status = None

        # Get schedule data
        api_url = f"{self.base_url}/cablecastapi/publicsitedata?site=1"
        response = self.scraper.scrape_html("unique_eagle", api_url)
        xml_dict = xmltodict.parse(response)
        json_data = json.dumps(xml_dict)
        data = json.loads(json_data)

        # Get live meeting title
        self.get_live_page()

        # Get meetings
        meetings = data["PublicSiteConfig"]["ScheduleItems"]["PublicSiteScheduleItem"]
        # log.info(f"meetings => {meetings}")
        for meeting in meetings:
            # Get title
            title = meeting["Title"]
            # log.info(f"Title: {title}")

            # Keyword Validation
            is_relevant = any(
                keyword.lower() in title.lower() for keyword in RELEVANT_KEYWORDS
            )
            if not is_relevant:
                continue

            # Date Validation
            try:
                title_datetime = parser.parse(title, fuzzy=True)
                if title_datetime.date() is not None:
                    title_date = title_datetime.date()
                    local_timezone = pytz.timezone(self.timezone)
                    current_local_datetime = datetime.now(tz=local_timezone)
                    # log.info(f"Current Local Datetime: {current_local_datetime}")
                    if title_date < current_local_datetime.date():
                        continue
            except Exception as e:
                log.debug(f"No date found in title: {title}")

            # Get stream page
            if (
                self.live_meeting_title is not None
                and self.live_meeting_title.lower() == title.lower()
            ):
                status = "In progress"
            else:
                status = "Upcoming"

            # Get UTC time
            run_datetime = meeting["RunDateTime"]
            run_datetime = parser.parse(run_datetime, fuzzy=True)
            run_datetime = datetime.strftime(
                run_datetime, TimeFormatter.desired_format()
            )
            utc_time = TimeFormatter(run_datetime, timezone).get_utc_time()
            # log.info(f"UTC Time: {utc_time}")

            meeting = {
                "Meeting name": title,
                "Scheduled time": utc_time,
                "Meeting link": self.stream_url,
                "Agenda link": None,
                "Status": status,
            }

            self.meetings.append(meeting)

        log.info(f"Meeting {self.meetings}")
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="http://vod-eaglecounty.cablecast.tv:4020/CablecastPublicSite/schedule",
        schedule_type="unique_eagle",
        timezone="America/Denver",
    )
