# phoenix.py
import os
import re
import pytz
from datetime import datetime
from urllib.parse import urlparse
import requests
from utils.scrape_html import HtmlScraper

DATE_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
DATE_PATTERN = r"\d{2}/\d{2}/\d{4} \d{1,2}:\d{2} [APM]{2} "


class Phoenix:
    def __init__(self):
        self.timezone = None
        self.base_url = None
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        self.item_meeting_link = "https://www.phoenix.gov/cityclerksite/_api/web/lists/GetByTitle('City%20Council%20Meetings')/Items?$top=1000"
        self.scrapper_api_key = os.getenv("SCRAPERAPICOM_API_KEY")

    def phoenix_table(self, url: str, timezone: str) -> list:
        self.timezone = timezone
        self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc

        meeting_data_list = self._get_page_data_using_direct_url_json_to_list(
            self.item_meeting_link
        )

        for meeting_data in meeting_data_list:
            meeting_name = meeting_data.get("Title", None)
            meeting_name = (
                re.sub(DATE_PATTERN, "", meeting_name) if meeting_name else meeting_name
            )

            meeting_date = meeting_data.get("Meeting_x0020_Time", None)
            meeting_date_time = (
                datetime.strptime(meeting_date, DATE_UTC_FORMAT)
                if meeting_date
                else None
            )

            agenda_link = meeting_data.get("AgendaLink", None)
            agenda_link = agenda_link.replace(" ", "%20") if agenda_link else None
            agenda_full_link = f"{self.base_url}{agenda_link}" if agenda_link else None

            meeting_link = meeting_data.get("OnlineMeetingLink", None)

            if meeting_name and re.search(
                r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE
            ):
                status = "Cancelled"
            elif meeting_date_time and not self._is_past_meeting(meeting_date_time):
                status = "Upcoming"
            elif not self._is_valid_meeting_date(meeting_date_time):
                continue
            else:
                status = "Past"

            self.meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_full_link,
                    "Status": status,
                }
            )
        return self.meetings

    def _get_page_data_using_direct_url_json_to_list(self, url: str) -> list:
        headers = {
            "Accept": "application/json; odata=verbose",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        }
        request = requests.get(url, headers=headers)
        request.raise_for_status()

        data = request.json()

        return data["d"]["results"] if data else []

    def _convert_to_utc(self, date_time: datetime, local_timezone: str) -> datetime:
        local_tz = pytz.timezone(local_timezone)
        local_dt = local_tz.localize(date_time)
        return local_dt.astimezone(pytz.UTC)

    def _is_past_meeting(self, meeting_date: datetime) -> bool:
        today_start = self._convert_to_utc(datetime.now(), self.timezone).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return today_start > meeting_date

    def _is_valid_meeting_date(self, meeting_date: datetime) -> bool:
        if meeting_date is None:
            return False
        if meeting_date.tzinfo is None:
            return False
        if meeting_date.tzinfo.utcoffset(meeting_date) is None:
            return False
        # This is because of timezone.
        if meeting_date.hour == 7:
            return False

        return True
