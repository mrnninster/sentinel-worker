import re
import os
import sys
import pytz
import logging
from datetime import datetime
from dotenv import load_dotenv
from urllib.parse import urlparse

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


class Ohiolegislature:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()
        self.timezone = None
        self.base_url = None
        self.url = None
        self.api_base_url = None

    def unique_ohiolegislature(self, url, timezone="America/New_York"):
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        year = now.year

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        div = soup.find("div", id="layout")
        content = div.find("div", id="ohioChannelLive")

        main = content.find("div", class_="mediaGroupModule")
        list = main.find_all("div", class_="mediaContainer")

        for item in list:
            name_div = item.find("div", class_="mediaTitle")
            meeting_name = name_div.get_text().strip()

            details_div = item.find("div", class_="mediaDetails")
            # Extract all <br> tags
            br_tags = details_div.find_all("br")

            if len(br_tags) > 1:
                # Get the text after the last <br> tag
                last_br_index = len(br_tags) - 1
                text_after_last_br = (
                    br_tags[last_br_index].find_next_sibling(string=True).strip()
                )

                meeting_date_time_web = text_after_last_br + f" {str(year)}"

                meeting_date_time_web = datetime.strptime(
                    meeting_date_time_web, "%b %d - %I:%M %p %Y"
                )

                meeting_date_time_local = timezone.localize(meeting_date_time_web)

                meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

                meeting_date_time = meeting_date_time_utc.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                )

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
                self.meetings.append(dictionary)

            live_link = details_div.find("a", class_="liveLink")
            live_link_text = live_link.get_text() if live_link else None

            if live_link is not None and live_link_text.lower() == "live":

                meeting_date_time_web = datetime.now(timezone).replace(
                    second=0, microsecond=0
                )

                meeting_date_time_utc = meeting_date_time_web.astimezone(pytz.utc)

                meeting_date_time = meeting_date_time_utc.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                )

                status = "In progress"
                meeting_link = domain + live_link.get("href").replace("..", "")
                agenda_link = None

                dictionary = {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date_time,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
                self.meetings.append(dictionary)
        exempt = [
            "the ohio channel",
            "the sound of ideas",
            "all sides",
            "special events",
        ]

        self.meetings = [
            meet
            for meet in self.meetings
            if (meet["Meeting name"]).lower() not in exempt
        ]

        # log.info(f"Meetings: {self.meetings}")

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://www.ohiochannel.org/live",
        schedule_type="unique_ohiolegislature",
        timezone="America/New_York",
    )
