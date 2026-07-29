import logging

import pytz
from datetime import datetime
import requests

if __name__ == "__main__":  # for local testing
    import sys
    import os
    from dotenv import load_dotenv

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test
from utils.scrape_html import HtmlScraper, HTMLTags
from utils.utils_functions import to_utc_iso


CALENDAR_URL = "https://gwinnettcounty.mhsoftware.com/ViewMonth.html"
MEETING_LINK = "https://www.gwinnettcounty.com/departments/communications/tvgwinnett"


log = logging.getLogger(__name__)


class Gwinnett:
    self_contained_parser = True

    def __init__(self):
        self._scraper = HtmlScraper()

    def unique_gwinnett(self, url: str, timezone: str) -> list:
        meetings = []
        tz_info = pytz.timezone(timezone)

        response = requests.get(CALENDAR_URL)
        soup = self._scraper.convert_to_soup(response.text)

        for meeting_content in soup.select("div[data-dojo-type='dijit/Tooltip']"):
            try:
                tooltip_time = meeting_content.find(
                    HTMLTags.SPAN_TAG, class_="MHVMTooltipTime"
                )
                tooltip_title = meeting_content.find(
                    HTMLTags.SPAN_TAG, class_="MHVMTooltipTitle"
                )

                if not tooltip_time or not tooltip_title:
                    continue

                meeting_name = tooltip_title.get_text(strip=True)

                meeting_date, times_ = tooltip_time.get_text(strip=True).split(", ")
                meeting_start_time, meeting_end_time = times_.split(" - ")

                meeting_date_obj = datetime.strptime(meeting_date, "%m/%d/%y").date()
                meeting_start_time_obj = datetime.strptime(
                    meeting_start_time, "%I:%M %p"
                ).time()
                meeting_end_time_obj = datetime.strptime(
                    meeting_end_time, "%I:%M %p"
                ).time()

                meeting_start = datetime.combine(
                    meeting_date_obj, meeting_start_time_obj, tzinfo=tz_info
                )
                meeting_end = datetime.combine(
                    meeting_date_obj, meeting_end_time_obj, tzinfo=tz_info
                )

                meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": to_utc_iso(meeting_start),
                        "Meeting link": MEETING_LINK,
                        "Agenda link": None,
                        "Status": "Upcoming",
                    }
                )
            except Exception as exception:
                log.warning(f"Error parsing meeting content: {exception}")

        return meetings


if __name__ == "__main__":
    run_test(
        url="https://www.gwinnettcounty.com/calendar/general",
        schedule_type="unique_gwinnett",
    )
