import os
import re
import sys
import json
import logging
import time
from dateutil import parser
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test
from utils.utils_functions import get_api_json_call

MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"
SLIQ_EPOCH_TIMESTAMP = "20000101000000000"
scraper = HtmlScraper()


class Sliqmedia:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    @staticmethod
    def get_meeting_status(entity_status: int) -> str:
        """
        Return a human-readable meeting status based on the entity status code.

        -1 → "Ended"
        1 → "In Progress"
        0 → "Upcoming"
        """
        if entity_status == -1:
            return "Ended"
        return "In Progress" if entity_status else "Upcoming"

    def sliqmedia_table(self, url: str, timezone: str) -> list:
        """
        Extracts and processes upcoming meeting data from the SliqMedia API.
        """
        self.timezone = timezone
        params = {"lastModified": SLIQ_EPOCH_TIMESTAMP}
        payload = {
            "api_key": self.scraper.SCRAPERAPICOM_API_KEY,
            "url": f"{url}api/Data/GetUpcomingEvents/?lastModified={SLIQ_EPOCH_TIMESTAMP}",
        }
        json_response = self.scraper.fetch_with_scraperapi(payload=payload)
        try:
            json_data = json.loads(json_response)
            log.info(f"SliqMedia Raw Response keys: {json_data.keys()}")
            content_entity_datas = json_data.get("ContentEntityDatas", [])
            log.info(f"Found {len(content_entity_datas)} ContentEntityDatas groups")
        except Exception as e:
            log.warning(
                f"Failed to parse SliqMedia JSON. Response snippet: {json_response[:200]}... Error: {e}"
            )
            return []

        for content_entity_data in content_entity_datas:
            if content_entity_data:
                # log.info(f"Content entity data: {json.dumps(content_entity_data, indent=4)}")
                for data in content_entity_data:
                    meeting_name = data.get("Title")
                    entity_status = data.get("EntityStatus")
                    # log.info(f"Processing meeting: {meeting_name}, EntityStatus: {entity_status}")

                    if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                        status = "Cancelled"
                    else:
                        status = "Upcoming"

                    meeting_date = data.get("ScheduledStart")
                    meeting_time_parsed = parser.parse(
                        meeting_date, fuzzy=True, ignoretz=True
                    )
                    formatted_naive_datetime = meeting_time_parsed.strftime(
                        TimeFormatter.desired_format()
                    )
                    time_formatter = TimeFormatter(
                        formatted_naive_datetime, self.timezone
                    )
                    utc_time = time_formatter.get_utc_time()

                    meeting_id = data.get("Id")
                    meeting_link = (
                        f"{url}PowerBrowser/PowerBrowserV2/20191211/-1/{meeting_id}"
                    )
                    meeting_entity_status = data.get("EntityStatus")

                    self.meetings.append(
                        {
                            "Meeting name": meeting_name,
                            "Scheduled time": utc_time,
                            "Meeting link": meeting_link,
                            "Agenda link": None,
                            "Status": self.get_meeting_status(meeting_entity_status),
                        }
                    )

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://sg001-harmony.sliq.net/00325/Harmony/en/",
        schedule_type="sliqmedia_table",
        timezone="America/Chicago",
    )
