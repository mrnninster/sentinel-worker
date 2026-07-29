import re
import os
import sys
import pytz
import logging
from datetime import datetime
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from schedule.schedule_scraper import run_test
from utils.scrape_html import HtmlScraper, HTMLTags
from utils.format_time import TimeFormatter

MEETING_LINK = "https://www.youtube.com/@isdeptmqtco2291"


class Marquette:
    self_contained_parser = True

    def __init__(self):
        self._scraper = HtmlScraper()

    def unique_marquette(self, url: str, timezone: str) -> list:
        meetings = []
        tz_info = pytz.timezone(timezone)

        response = self._scraper.scrape_html(url=url, render="true")
        soup = self._scraper.convert_to_soup(string=response)

        for meeting_content in soup.select("td.weekDay.eventDay"):
            try:
                event_details = meeting_content.select_one("a.eventName")
                if not event_details:
                    continue

                meeting_time = event_details.find(HTMLTags.SPAN_TAG, class_="meridiem")
                if not meeting_time:
                    continue

                meeting_time = meeting_time.get_text(strip=True)
                meeting_name = (
                    event_details.get_text(strip=True).replace(meeting_time, "").strip()
                )

                js_link = event_details["href"]
                date_str = js_link.split("'")[1]  # '04-15-2025'

                meeting_date_obj = datetime.strptime(date_str, "%m-%d-%Y").date()
                meeting_start_time_obj = datetime.strptime(
                    meeting_time, "%I:%M %p"
                ).time()

                meeting_start_raw = datetime.combine(
                    meeting_date_obj, meeting_start_time_obj, tzinfo=tz_info
                )
                meeting_start = datetime.strftime(
                    meeting_start_raw, TimeFormatter.desired_format()
                )
                utc_time = TimeFormatter(meeting_start, timezone).get_utc_time(
                    as_datetime=True
                )
                meeting_start = utc_time.isoformat().replace("+00:00", "Z")

                today = datetime.now(tz=tz_info).date()
                if meeting_start_raw.date() < today:
                    continue

                if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                    status = "Cancelled"
                else:
                    status = "Upcoming"

                meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": meeting_start,
                        "Meeting link": MEETING_LINK,
                        "Agenda link": None,
                        "Status": status,
                    }
                )
            except Exception as exception:
                log.warning(f"Error parsing meeting content: {exception}")

        return meetings


if __name__ == "__main__":
    run_test(
        url="https://www.co.marquette.mi.us/calendar_app/index.html",
        schedule_type="unique_marquette",
    )
