# louisiana.py
import logging
import os
import sys

from bs4 import BeautifulSoup
from dateutil import parser
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from schedule.schedule_scraper import run_test  # noqa: E402
from utils.format_time import TimeFormatter  # noqa: E402
from utils.scrape_html import HtmlScraper, HTMLTags, HTMLAttributes  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


class Louisiana:
    def __init__(self):
        self.self_contained_parser = True
        self.scraper = HtmlScraper()
        self.scrapper_api_key = os.getenv("SCRAPERAPICOM_API_KEY")
        self.meetings = []
        self.timezone = None
        self.base_url = None

    def louisiana_house_table(self, url: str, timezone: str) -> list:
        self.meetings = []
        self.timezone = timezone
        self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc
        detail_page_soup = self._get_page_soup_using_scraperapi(url)
        upcoming_div_row = detail_page_soup.find(
            HTMLTags.DIV_TAG, {HTMLAttributes.ID_ATTRIBUTE: "appupcoming"}
        )
        meetings = (
            upcoming_div_row.find_all(HTMLTags.DIV_TAG, class_="row")
            if upcoming_div_row
            else []
        )
        for meeting in meetings:
            data_rows = meeting.find_all(HTMLTags.DIV_TAG, class_="col-md-4")
            if len(data_rows) != 3:
                log.warning(
                    f"Found {len(data_rows)} data rows in {meeting}. Skipping."
                )
                continue
            title_row = data_rows[0].find(HTMLTags.SPAN_TAG)
            date_and_channel_row = data_rows[1].find(HTMLTags.SPAN_TAG)
            agenda_and_stream_link_row = data_rows[2].find_all(HTMLTags.LINK_TAG)

            meeting_name = title_row.get_text(strip=True) if title_row else None

            meeting_date_unformatted = (
                date_and_channel_row.get_text(strip=True)
                if date_and_channel_row
                else None
            )
            meeting_date = None
            if meeting_date_unformatted:
                split_date = meeting_date_unformatted.split(",")
                meeting_date = split_date[0]
                meeting_time = split_date[1]
                if "Adj" in meeting_time:
                    log.warning("Adjourned meeting found - skipping.")
                    continue
                combined_time = f"{meeting_date} {meeting_time}"
                meeting_time_parsed = parser.parse(
                    combined_time, fuzzy=True, ignoretz=True
                )
                formatted_naive_datetime = meeting_time_parsed.strftime(
                    TimeFormatter.desired_format()
                )
                time_formatter = TimeFormatter(formatted_naive_datetime, self.timezone)
                formatted_date_time = time_formatter.get_utc_time(as_datetime=True)
                meeting_date = (
                    formatted_date_time.strftime(
                        MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT
                    )[:-3]
                    + "Z"
                )

            agenda_link = None
            meeting_link = None
            if len(agenda_and_stream_link_row) == 2:
                relative_path = (
                    agenda_and_stream_link_row[0]
                    .get(HTMLAttributes.LINK_ATTRIBUTE)
                    .strip()
                )
                clean_path = relative_path.lstrip("../")
                agenda_link = f"{self.base_url}{clean_path}"
                link_attr = (
                    agenda_and_stream_link_row[1]
                    .get(HTMLAttributes.LINK_ATTRIBUTE)
                    .strip()
                )
                meeting_link = f"{self.base_url}{link_attr}"
            elif len(agenda_and_stream_link_row) == 1:
                log.warning("Missing agenda link.")
                link_attr = (
                    agenda_and_stream_link_row[0]
                    .get(HTMLAttributes.LINK_ATTRIBUTE)
                    .strip()
                )
                meeting_link = f"{self.base_url}{link_attr}"

            status = "Upcoming"

            self.meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
            )

        return self.meetings

    def louisiana_senate_table(self, url: str, timezone: str) -> list:
        self.meetings = []
        self.timezone = timezone
        self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc
        detail_page_soup = self._get_page_soup_using_scraperapi(url)
        upcoming_div = detail_page_soup.find(
            HTMLTags.DIV_TAG, {HTMLAttributes.ID_ATTRIBUTE: "appupcoming"}
        )
        meetings = (
            upcoming_div.find_all(HTMLTags.DIV_TAG, class_="row")
            if upcoming_div
            else []
        )
        for meeting in meetings:
            cols = meeting.find_all(HTMLTags.DIV_TAG, recursive=False)
            if len(cols) < 3:
                continue

            # Column 1: committee name
            name_span = cols[0].find(HTMLTags.SPAN_TAG)
            meeting_name = name_span.get_text(strip=True) if name_span else None

            # Column 2: date, time, location (e.g. "Feb 18, 1:30 PM, Room A-B")
            datetime_span = cols[1].find(HTMLTags.SPAN_TAG)
            datetime_text = (
                datetime_span.get_text(strip=True) if datetime_span else None
            )
            meeting_date = None
            if datetime_text:
                # Split on comma — last part is the room, rest is date/time
                parts = [p.strip() for p in datetime_text.split(",")]
                if len(parts) >= 3:
                    date_time_str = f"{parts[0]}, {parts[1]}"
                else:
                    date_time_str = datetime_text
                try:
                    meeting_time_parsed = parser.parse(
                        date_time_str, fuzzy=True, ignoretz=True
                    )
                    formatted_naive_datetime = meeting_time_parsed.strftime(
                        TimeFormatter.desired_format()
                    )
                    time_formatter = TimeFormatter(
                        formatted_naive_datetime, self.timezone
                    )
                    formatted_date_time = time_formatter.get_utc_time(as_datetime=True)
                    meeting_date = (
                        formatted_date_time.strftime(
                            MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT
                        )[:-3]
                        + "Z"
                    )
                except (ValueError, TypeError) as e:
                    log.warning(f"Could not parse date '{date_time_str}': {e}")

            # Column 3: agenda and video links
            links = cols[2].find_all(HTMLTags.LINK_TAG)
            agenda_link = None
            meeting_link = None
            for link in links:
                href = link.get(HTMLAttributes.LINK_ATTRIBUTE, "").strip()
                if not href:
                    continue
                # Make absolute URL
                if href.startswith("/"):
                    full_url = f"{self.base_url}{href}"
                elif href.startswith("http"):
                    full_url = href
                else:
                    full_url = f"{self.base_url}/{href}"
                if "agenda" in href.lower() or "legis.la.gov" in href.lower():
                    agenda_link = full_url
                elif "video" in href.lower():
                    meeting_link = full_url

            self.meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_link,
                    "Status": "Upcoming",
                }
            )

        return self.meetings

    def _get_page_soup_using_scraperapi(self, url: str, **kwargs) -> BeautifulSoup:
        payload = {
            "api_key": self.scrapper_api_key,
            "url": url,
            "render": "true",
        }
        log.info("Calling for scraperapi")
        page_with_needed_data = self.scraper.fetch_with_scraperapi(payload=payload)

        return self.scraper.convert_to_soup(string=page_with_needed_data)


if __name__ == "__main__":
    run_test(
        url="https://house.louisiana.gov/H_Sched/Hse_MeetingSchedule",
        schedule_type="louisiana_house_table",
        timezone="America/Chicago",
    )
    run_test(
        url="https://senate.la.gov/Sched/S_Sched",
        schedule_type="louisiana_senate_table",
        timezone="America/Chicago",
    )
