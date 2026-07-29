import os
import re
import sys
import json
import pytz
import logging
from dateutil import parser
from datetime import datetime
from dotenv import load_dotenv
from urllib.parse import urlparse

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Eduvision:

    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True

    def eduvision_table(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        script_tags = soup.find_all("script")
        pattern = re.compile(r"ViewModelData =")

        for script in script_tags:
            if script.string and pattern.search(script.string):
                start_pos = script.string.find("ViewModelData =") + len(
                    "ViewModelData ="
                )
                json_str = script.string[start_pos:]

                # Find the end of the JSON object
                brace_count = 0
                for i, char in enumerate(json_str):
                    if char == "{":
                        brace_count += 1
                    elif char == "}":
                        brace_count -= 1
                    if brace_count == 0 and i > 0:
                        json_str = json_str[: i + 1]
                        break
                data = json.loads(json_str)
                schedules = data["Schedules"]

                # Process schedules
                for schedule in schedules:
                    agenda_url = None
                    title = schedule["Title"]
                    date_time_string = schedule["StartTime"]
                    # log.info(f"Time string: {date_time_string}")

                    meeting_date_time = parser.parse(date_time_string, fuzzy=True)
                    meeting_date_time = datetime.strftime(
                        meeting_date_time, TimeFormatter.desired_format()
                    )
                    utc_time = TimeFormatter(meeting_date_time, timezone).get_utc_time(
                        as_datetime=True
                    )
                    schedule_time = utc_time.isoformat().replace("+00:00", "Z")
                    # log.info(f"Schedule time: {schedule_time}")

                    if str(schedule["Action"]).lower() == "watch now":
                        status = "In progress"

                        # Get the data for the meeting
                        item_id = schedule["Id"]
                        action = schedule["Action"]
                        password = schedule["Password"]

                        # Construct data object
                        data = {
                            "scheduleId": item_id,
                            "action": action,
                            "password": password,
                        }

                        # Extract base URL and use correct API endpoint
                        # (not /LiveSched.aspx/WatchNow - that returns 404)
                        parsed = urlparse(url)
                        base_url = f"{parsed.scheme}://{parsed.netloc}"
                        watch_now_url = f"{base_url}/liveEvents/WatchNow"

                        log.info(f"Posting to WatchNow: {watch_now_url}")
                        log.debug(f"WatchNow scheduleId={item_id}, action={action}")
                        response = self.scraper.post_with_scraperapi(
                            url=watch_now_url, data=data
                        )
                        if not response or not response.strip():
                            log.warning(f"Empty WatchNow response: {title}")
                            status = "Upcoming"
                            meeting_page_url = None
                        else:
                            try:
                                response_json = json.loads(response)
                                meeting_page_url = response_json.get("RedirectUrl")
                                if not meeting_page_url:
                                    log.warning(f"No RedirectUrl: {title}")
                                    status = "Upcoming"
                            except json.JSONDecodeError as e:
                                log.warning(f"WatchNow JSON parse error: {e}")
                                log.debug(f"Response: {response[:500]}")
                                status = "Upcoming"
                                meeting_page_url = None
                    else:
                        status = "Upcoming"
                        meeting_page_url = None

                    meeting = {
                        "Meeting name": title,
                        "Scheduled time": schedule_time,
                        "Meeting link": meeting_page_url,
                        "Agenda link": agenda_url,
                        "Status": status,
                    }

                    self.meetings.append(meeting)

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://mps.eduvision.tv/LiveEvents",
        schedule_type="eduvision_table",
        timezone="America/New_York",
    )
