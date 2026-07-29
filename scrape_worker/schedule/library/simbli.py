import os
import sys
import logging
import re
import pytz
from datetime import datetime
from enum import Enum
import requests
from fake_useragent import UserAgent
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper, HTMLTags
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test


logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://simbli.eboardsolutions.com/SB_Meetings"
MEETING_DATE_TIME_FORMAT = "%m/%d/%Y - %I:%M %p"
MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"
STRING_REGEX = r"ViewMeeting\((.*?)\)"
DEFAULT_HTML_TABLE_TAG_ID = "ContentPlaceHolder1_MeetingGrid"


class Simbli:
    def __init__(self, timezone="America/New_York"):
        self.timezone = timezone
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def parse_meeting_row(self, row):
        columns = row.find_all(HTMLTags.COLUMNS_TAG)

        if len(columns) < 2:
            return

        date_column, meeting_name_column, meeting_type_column = (
            columns[0],
            columns[1],
            columns[3],
        )
        try:
            meeting_name = meeting_name_column.get_text(strip=True)

            meeting_date_time = date_column.get_text(strip=True)
            meeting_date_time = datetime.strptime(
                meeting_date_time, MEETING_DATE_TIME_FORMAT
            )

            today_start = datetime.now(pytz.timezone(self.timezone))

            formatted_naive_datetime = meeting_date_time.strftime(
                TimeFormatter.desired_format()
            )
            time_formatter = TimeFormatter(formatted_naive_datetime, self.timezone)
            formatted_date_time = time_formatter.get_utc_time(as_datetime=True)

            if today_start.date() > meeting_date_time.date():
                return

            formatted_meeting_date_time = (
                formatted_date_time.strftime(
                    MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT
                )[:-3]
                + "Z"
            )
        except Exception as e:
            log.warning(f"Error parsing date or name: {e}")
            return

        agenda_link = self.extract_simbli_agenda_link(meeting_name_column)

        return {
            "Meeting name": meeting_name,
            "Scheduled time": formatted_meeting_date_time,
            "Meeting link": None,
            "Agenda link": agenda_link,
            "Status": "Upcoming",
        }

    @staticmethod
    def extract_simbli_agenda_link(column):
        try:
            agenda_div = column.find(HTMLTags.LINK_TAG).get("onclick")
            match = re.search(STRING_REGEX, agenda_div)
            if match:
                params = match.group(1).split(",")
                stream_id = (
                    params[0].replace('"', "") if params and len(params) > 1 else None
                )
                meeting_id = (
                    params[1].replace('"', "") if params and len(params) > 2 else None
                )
                return f"{DEFAULT_BASE_URL}/ViewMeeting.aspx?S={stream_id}&MID={meeting_id}"
        except Exception as e:
            log.warning(f"Error extracting agenda link: {e}")
        return None

    def simbli_table(self, url, timezone="America/New_York"):
        # Fetch HTML with JS rendering (rendered list)
        html_content = self.scraper.scrape_html(url=url, render="true")
        soup = self.scraper.convert_to_soup(html_content)
        self.timezone = timezone
        table = soup.find(HTMLTags.TABLE_TAG, {"id": DEFAULT_HTML_TABLE_TAG_ID})
        if not table:
            log.warning("No meeting table found")
            return []

        rows = table.tbody.find_all(HTMLTags.ROWS_TAG)
        for row in rows:
            table_meeting = self.parse_meeting_row(row)
            if table_meeting:
                self.meetings.append(table_meeting)

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://simbli.eboardsolutions.com/SB_Meetings/SB_MeetingListing.aspx?S=4082",
        schedule_type="simbli_table",
        timezone="America/New_York",
    )
