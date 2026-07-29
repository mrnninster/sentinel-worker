import os
import re
from datetime import datetime
from urllib.parse import urlparse
import pytz
import logging

if __name__ == "__main__":
    import sys
    import os
    from dotenv import load_dotenv

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
from utils.scrape_html import HtmlScraper, HTMLTags, HTMLAttributes

DATE_FORMAT_VARIATE_1 = "%B %d, %Y at %I:%M %p"
DATE_FORMAT_VARIATE_2 = "%B %d, %Y"
DATE_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.000Z"

AGENDA_TEXT = "Agenda Packet"

DATE_REGEX = r"\b\w+\b\s+\d{1,2},\s+\d{4}\s+at\s+\d{1,2}:\d{2}\s*[ap]\.m\."

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Civicplus:
    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        self.scrapper_api_key = os.getenv("SCRAPERAPICOM_API_KEY")
        self.base_url = None

    def civicplus_table(self, url, timezone):

        self.timezone = timezone

        self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc

        detail_page_soup = self.get_page_soup_using_scraperapi(url)

        meeting_links = detail_page_soup.find(
            HTMLTags.MARKED_COLUMNS_TAG, id="secondaryMenusecondaryNav"
        ).find_all(HTMLTags.MARKED_ROWS_TAG)

        for meeting_link in meeting_links:
            meeting_title = meeting_link.find(HTMLTags.LINK_TAG).text.strip().split("-")

            meeting_name = meeting_title[0].strip()
            meeting_date = meeting_title[1].strip()
            try:
                if "at" in meeting_date:
                    meeting_date_time = datetime.strptime(
                        meeting_date, DATE_FORMAT_VARIATE_1
                    )
                else:
                    meeting_date_time = datetime.strptime(
                        meeting_date, DATE_FORMAT_VARIATE_2
                    )
            except ValueError as e:
                log.warning(f"Error parsing date '{meeting_date}': {e}")
                raise

            meeting_date = self._convert_to_utc(meeting_date_time)

            agenda_meeting_link_row = meeting_link.find(HTMLTags.LINK_TAG)
            agenda_meeting_link = (
                agenda_meeting_link_row[HTMLAttributes.LINK_ATTRIBUTE]
                if agenda_meeting_link_row
                else None
            )
            full_agenda_link = (
                f"{self.base_url}{agenda_meeting_link}" if agenda_meeting_link else None
            )

            agenda_link, additional_meeting_date = self._receive_detail_page_data(
                full_agenda_link
            )

            if additional_meeting_date:
                meeting_date_time = datetime.strptime(
                    additional_meeting_date, DATE_FORMAT_VARIATE_1
                )
                meeting_date = self._convert_to_utc(meeting_date_time)

            status = "Past"
            if not self._is_past_meeting(meeting_date):
                status = "Upcoming"

            self.meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date.strftime(DATE_UTC_FORMAT),
                    "Meeting link": None,
                    "Agenda link": (agenda_link if agenda_link else full_agenda_link),
                    "Status": status,
                }
            )

        return self.meetings

    def _convert_to_utc(self, date_time):
        local_tz = pytz.timezone(self.timezone)
        local_dt = local_tz.localize(date_time)
        return local_dt.astimezone(pytz.UTC)

    def _receive_detail_page_data(
        self, meeting_link: str
    ) -> tuple[str | None, str | None]:
        detail_page_soup = self.get_page_soup_using_scraperapi(meeting_link)

        link_tag = detail_page_soup.find(
            HTMLTags.LINK_TAG,
            string=lambda text: AGENDA_TEXT in text if text else False,
        )
        link = link_tag[HTMLAttributes.LINK_ATTRIBUTE] if link_tag else None
        agenda_link = f"{self.base_url}{link}" if link else None

        title_desc_tag = detail_page_soup.find(
            "span", string=lambda text: ".m." in text if text else None
        )

        title_desc_text = title_desc_tag.text.strip() if title_desc_tag else None

        match = re.search(DATE_REGEX, title_desc_text if title_desc_text else "")
        adjusted_meeting_time = (
            match.group().strip().replace(".", "") if match else None
        )

        return agenda_link, adjusted_meeting_time

    def get_page_soup_using_scraperapi(self, url):
        payload = {"api_key": self.scrapper_api_key, "url": url}
        page_with_needed_data = self.scraper.fetch_with_scraperapi(payload=payload)

        return self.scraper.convert_to_soup(string=page_with_needed_data)

    def _is_past_meeting(self, meeting_date: datetime) -> bool:
        local_now = datetime.now(pytz.timezone(self.timezone))
        return local_now.date() > meeting_date.date()

    # url = "https://fl-titusville.civicplus.com/739/City-Council-Meetings"
    # timezone = "America/New_York"
    # schedule_type = "civicplus_table"
    # tz = pytz_timezone(timezone)
    #
    # run_test(url=url,timezone=timezone, schedule_type=schedule_type,
    #         get_date_start=datetime.now(tz) - timedelta(days=10),
    #         #get_date_end=datetime.now(tz) - timedelta(days=1),
    #          )

    if __name__ == "__main__":
        from schedule.schedule_scraper import run_test
        from datetime import timedelta
        from pytz import timezone as pytz_timezone

        url = "https://fl-titusville.civicplus.com/739/City-Council-Meetings"
        timezone = "America/New_York"
        schedule_type = "civicplus_table"

        tz = pytz_timezone(timezone)

        run_test(
            url=url,
            timezone=timezone,
            schedule_type=schedule_type,
            get_date_start=datetime.now(tz) - timedelta(days=10),
            # get_date_end=datetime.now(tz) - timedelta(days=1),
        )
