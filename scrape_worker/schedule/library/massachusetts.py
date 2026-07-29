# massachusetts.py
import os
import sys
import pytz
import logging
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from urllib.parse import urlparse
from datetime import datetime, timedelta
from dateutil import parser, relativedelta

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.format_time import TimeFormatter
from utils.scrape_html import HtmlScraper, HTMLTags, HTMLAttributes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"
MEETING_DATE_PARSING_TO_API_CALL = "%m-%d-%Y"


class Massachusetts:
    def __init__(self):
        self.timezone = None
        self.base_url = None
        self.url = None
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        self.scrapper_api_key = os.getenv("SCRAPERAPICOM_API_KEY")

    def massachusetts_table(self, url: str, local_timezone: str) -> list:
        self.timezone = local_timezone
        self.url = url
        self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc

        now = datetime.now(pytz.timezone(self.timezone))
        future = now + relativedelta.relativedelta(months=1)
        current_date = now.date().strftime(MEETING_DATE_PARSING_TO_API_CALL)
        future_date = future.date().strftime(MEETING_DATE_PARSING_TO_API_CALL)

        page_filter_url = self.base_url + f"/Events/List/{current_date}/{future_date}/"
        log.info(f"Page filter url: {page_filter_url}")

        soup_string = self.scraper.scrape_html(url=page_filter_url)
        soup = self.scraper.convert_to_soup(string=soup_string)

        table_row = soup.find(
            HTMLTags.TABLE_TAG,
            attrs={HTMLAttributes.CLASS_ATTRIBUTE: "table table-striped eventTable"},
        )

        all_table_row = table_row.find_all(HTMLTags.ROWS_TAG)
        meeting_date = None
        for table_row in all_table_row:
            all_blocks = table_row.find_all(HTMLTags.COLUMNS_TAG)
            if len(all_blocks) < 7:
                continue
            meeting_name_index_id = 4
            meeting_time_index_id = 1
            if len(all_blocks) == 8:
                meeting_name_index_id = 5
                meeting_time_index_id = 2
                if (
                    all_blocks[0].get(HTMLAttributes.CLASS_ATTRIBUTE)[0]
                    == "text-center"
                ):
                    year_month = table_row.find_all(
                        HTMLTags.SPAN_TAG,
                        attrs={HTMLAttributes.CLASS_ATTRIBUTE: "month"},
                    )
                    day = table_row.find(
                        HTMLTags.SPAN_TAG,
                        attrs={HTMLAttributes.CLASS_ATTRIBUTE: "day"},
                    )
                    meeting_date = f"{year_month[0].text.strip()} {year_month[1].text.strip()} {day.text.strip()}"
            meeting_time = all_blocks[meeting_time_index_id].text.strip()
            meeting_full_date = f"{meeting_date} {meeting_time}"
            meeting_time_parsed = parser.parse(
                meeting_full_date, fuzzy=True, ignoretz=True
            )
            formatted_naive_datetime = meeting_time_parsed.strftime(
                TimeFormatter.desired_format()
            )
            time_formatter = TimeFormatter(formatted_naive_datetime, self.timezone)
            formatted_date_time = time_formatter.get_utc_time(as_datetime=True)
            meeting_date_formatted = (
                formatted_date_time.strftime(
                    MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT
                )[:-3]
                + "Z"
            )

            meeting_link = (
                all_blocks[meeting_name_index_id]
                .find(HTMLTags.LINK_TAG)
                .get(HTMLAttributes.LINK_ATTRIBUTE)
            )
            meeting_name = (
                all_blocks[meeting_name_index_id].find(HTMLTags.SPAN_TAG).text.strip()
            )

            status_row = table_row.find(HTMLTags.COLUMNS_TAG, class_="video")
            status_row = status_row.find_all(HTMLTags.SPAN_TAG)
            status = "Upcoming"
            if len(status_row) >= 2:
                status = status_row[1].text.strip()
            if status == "Live":
                status = "In Progress"

            full_meeting_link = None
            if meeting_link:
                full_meeting_link = f"{self.base_url}{meeting_link}"

            self.meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date_formatted,
                    "Meeting link": full_meeting_link,
                    "Agenda link": None,
                    "Status": status,
                }
            )

        return self.meetings


if __name__ == "__main__":
    from schedule.schedule_scraper import run_test

    run_test(
        url="https://malegislature.gov/Events/List",
        schedule_type="massachusetts_table",
        get_full_archive_flag=True,
    )
