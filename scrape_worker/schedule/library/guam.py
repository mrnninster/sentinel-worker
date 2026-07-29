import logging
import os
import re
from typing import Optional
from urllib.parse import urlparse, quote

from bs4 import BeautifulSoup
from dateutil import parser

if __name__ == "__main__":  # for local testing
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test
from utils.scrape_html import HtmlScraper, HTMLTags
from utils.format_time import TimeFormatter

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"
MEETING_NAME_PATTERN = r"^(.*?)\s+-"
MEETING_DATE_TIME_PATTERN = r"-\s+(.*)"


class Guam:
    def __init__(self):
        self.meetings = []
        self.timezone = None
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        self.base_url = None
        self.scraper_api_key = os.getenv("SCRAPERAPICOM_API_KEY")

    def guam_island(self, url: str, local_timezone: str) -> list:
        self.timezone = local_timezone
        self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc

        soup = self._get_page_soup_using_scraperapi(url=url)

        meetings = soup.find_all(HTMLTags.ROWS_TAG)

        for meeting in meetings:
            meeting_name_td = meeting.find(HTMLTags.COLUMNS_TAG, class_="meeting_name")
            full_meeting_name = (
                meeting_name_td.get_text(strip=True) if meeting_name_td else None
            )

            if not full_meeting_name:
                continue

            meeting_name_match = (
                re.search(MEETING_NAME_PATTERN, full_meeting_name)
                if full_meeting_name
                else None
            )
            meeting_name = meeting_name_match.group(1) if meeting_name_match else None

            datetime_match = (
                re.search(MEETING_DATE_TIME_PATTERN, full_meeting_name)
                if full_meeting_name
                else None
            )
            meeting_date_time = datetime_match.group(1) if datetime_match else None

            meeting_time_formatted = None
            if meeting_date_time:
                try:
                    start_time = parser.parse(
                        meeting_date_time, fuzzy=True, ignoretz=True
                    )
                except (ValueError, TypeError):
                    continue
                desired_format = TimeFormatter.desired_format()
                converted_start_time = start_time.strftime(desired_format)

                time_formatter = TimeFormatter(converted_start_time, self.timezone)
                meeting_start_time = time_formatter.get_utc_time(as_datetime=True)
                meeting_time_formatted = (
                    meeting_start_time.strftime(
                        MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT
                    )[:-3]
                    + "Z"
                )

            agenda_link = None
            action_agenda_link = meeting.find_all(
                HTMLTags.LINK_TAG, class_="tab_btn color3 color_gray3"
            )
            if action_agenda_link:
                link = action_agenda_link[0].get("data-load-remote")
                if link:
                    relative_link = self.get_agenda_link(link)
                    agenda_link = (
                        f"{self.base_url}{quote(relative_link, safe='/:')}"
                        if relative_link
                        else None
                    )

            meeting_link = None
            action_meeting_link = meeting.find_all(
                HTMLTags.LINK_TAG, class_="tab_btn color3 color_gray4"
            )
            if action_meeting_link:
                link = action_meeting_link[1].get("href")
                meeting_link = link if link else None

            meeting_text = meeting.get_text(" ", strip=True)
            if re.search(
                r"\b(cancel(?:led|ed)|adjourned|pas(?:sed|ed))\b",
                meeting_text,
                re.IGNORECASE,
            ):
                continue
            elif re.search(r"\b(ongoing)\b", meeting_text, re.IGNORECASE):
                status = "In progress"
            else:
                status = "Upcoming"

            if meeting_name is not None and meeting_time_formatted is not None:
                self.meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": meeting_time_formatted,
                        "Meeting link": meeting_link,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }
                )
        return self.meetings

    def get_agenda_link(self, url: str) -> Optional[str]:
        soup = self._get_page_soup_using_scraperapi(url=url)
        table_details = soup.find(HTMLTags.DIV_TAG, class_="table_details")
        if not table_details:
            return None

        for item in table_details.find_all(HTMLTags.DIV_TAG, class_="item"):
            label = item.find(HTMLTags.DIV_TAG, class_="name-coloumn")
            if label and "Agenda Documents/File" in label.get_text(strip=True):
                agenda_div = item.find(HTMLTags.DIV_TAG, class_="name-entity")
                if agenda_div:
                    link_tag = agenda_div.find(HTMLTags.DIV_TAG.LINK_TAG, href=True)
                    if link_tag:
                        return link_tag.get(HTMLTags.LINK_ATTRIBUTE)

        return None

    def _get_page_soup_using_scraperapi(self, url: str, **kwargs) -> BeautifulSoup:
        payload = {
            "api_key": self.scraper_api_key,
            "url": url,
            "render": "false",
        }
        log.info(f"Calling for scraperapi with url {url}")
        page_with_needed_data = self.scraper.fetch_with_scraperapi(payload=payload)

        return self.scraper.convert_to_soup(string=page_with_needed_data)


if __name__ == "__main__":
    run_test(
        url="https://go.opengovguam.com/meetings_list/?upcoming_meeting=1",
        schedule_type="guam_island",
        timezone="Pacific/Guam",
    )
