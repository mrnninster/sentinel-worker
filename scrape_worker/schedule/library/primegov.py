# primegov.py
import re
import os
import sys
import pytz
import json
import logging
import requests
from dateutil import parser
from datetime import datetime
from dotenv import load_dotenv
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test


class Primegov:
    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True

    def primegov_table(self, url, timezone="America/Los_Angeles"):
        try:
            raw = self.scraper.scrape_html(url=url)
            if not isinstance(raw, str) or not raw.strip():
                log.warning(
                    "Empty PrimeGov response for %s (Cloudflare block or bad ScraperAPI key?)",
                    url,
                )
                return []
            stripped = raw.strip()
            if stripped[0] not in "[{":
                log.warning(
                    "Non-JSON PrimeGov response for %s (len=%s): %s",
                    url,
                    len(raw),
                    raw[:200].replace("\n", " "),
                )
                return []
            data = json.loads(raw)

        except Exception as e:
            log.warning(f"Error processing api response: {e}")
            log.exception(f"Exception: {e}")
            return []

        if len(data) > 0:
            for datum in data:
                try:
                    status = "Upcoming"
                    title = datum["title"]
                    document_list = datum["documentList"]

                    if document_list:
                        template_id = document_list[0]["templateId"]
                        parsed = urlparse(url)
                        domain_with_scheme = f"{parsed.scheme}://{parsed.netloc}"
                        agenda_link = f"{domain_with_scheme}/Portal/Meeting?meetingTemplateId={template_id}"
                    else:
                        agenda_link = None

                    date_time_string = f"{datum['date']} {datum['time']}"
                    date_time = parser.parse(date_time_string, fuzzy=True)
                    date_time = datetime.strftime(
                        date_time, TimeFormatter.desired_format()
                    )
                    meeting_datetime = TimeFormatter(date_time, timezone).get_utc_time(
                        as_datetime=True
                    )
                    schedule_time = meeting_datetime.isoformat().replace("+00:00", "Z")

                    meeting = {
                        "Meeting name": title,
                        "Scheduled time": schedule_time,
                        "Meeting link": None,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }
                    self.meetings.append(meeting)

                except Exception as e:
                    log.warning(f"Error processing meeting datum: {e}, datum: {datum}")
                    continue

        return self.meetings


if __name__ == "__main__":

    url = "https://palmbayflorida.primegov.com/api/v2/PublicPortal/ListUpcomingMeetings"
    timezone = "America/New_York"
    schedule_type = "primegov_table"
    run_test(
        url=url,
        timezone=timezone,
        schedule_type=schedule_type,
    )
