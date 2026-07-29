# alaska.py
from datetime import datetime
import logging
import os
from urllib.parse import urlparse

import pytz
import requests
from dateutil import parser
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test

from utils.format_time import TimeFormatter
from utils.scrape_html import HtmlScraper, HTMLTags

MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"
API_CALL_CLIENT_ID = "2147483647"
API_CALL_AUTHORIZATION_WSC_API_KEY = "7WhiEBzijpritypp8bqcU7pfU9uicDR"
API_CALL_AUTHORIZATION_TYPE = "embedder"
API_CALL_DEFAULT_SORT = "ASC"
API_CALL_MAX_RESULT_PARAM = "100"
API_CALL_PAYLOAD_DATE_FORMAT = "%Y-%m-%d"


class Alaska:
    def __init__(self):
        self.timezone = None
        self.base_url = None
        self.url = None
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True

    def unique_alaska(self, url: str, local_timezone: str) -> list:
        current_date = datetime.now(tz=pytz.UTC).date()
        self.timezone = local_timezone
        self.url = url
        self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc

        api_url = "https://api.v3.invintus.com/v2/Search/general"
        video_api_url = "https://www.ktoo.org/video/gavel/watch"
        event_detail_api_url = "https://api.v3.invintus.com/v2/Event/getDetailed"
        tz = pytz.timezone(local_timezone)
        now = datetime.now(tz)
        today = now.date()
        end_of_year = tz.localize(datetime(today.year, 12, 31, 23, 59, 59))

        today_str = today.strftime(API_CALL_PAYLOAD_DATE_FORMAT)
        end_of_year_str = end_of_year.strftime(API_CALL_PAYLOAD_DATE_FORMAT)

        body = {
            "clientID": API_CALL_CLIENT_ID,
            "sortBy": API_CALL_DEFAULT_SORT,
            "startDateTime": today_str,
            "stopDateTime": end_of_year_str,
            "resultMax": API_CALL_MAX_RESULT_PARAM,
        }
        payload = {
            "Wsc-api-key": API_CALL_AUTHORIZATION_WSC_API_KEY,
            "authorization": API_CALL_AUTHORIZATION_TYPE,
        }

        response = requests.post(api_url, json=body, headers=payload)
        meetings = response.json().get("data")

        for meeting in meetings:
            meeting_name = meeting.get("title")
            meeting_date = meeting.get("startDateTime")
            video_id = meeting.get("id")
            client_id = meeting.get("clientID")

            meeting_date_time_web = parser.parse(
                meeting_date, fuzzy=True, ignoretz=True
            )

            formatted_naive_datetime = meeting_date_time_web.strftime(
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

            meeting_link = f"{video_api_url}?clientID={client_id}&eventID={video_id}"
            status = "Upcoming"
            
            if formatted_date_time.date() == current_date:
                
                # Create event payload
                event_payload = {
                    "eventID": video_id,
                    "clientID": client_id,
                    "simple": "",
                    "showEncoder": True,
                    "showStreams": True,
                    "includePrivate": False,
                    "VAST": True,
                    "checkRecentBreak": True,
                    "showDownloadLinks": True,
                    "showDocumentAssets": True
                }
                
                # make event details request
                event_details_resp = None
                try:
                    event_details_resp = requests.post(
                        event_detail_api_url,
                        json=event_payload,
                        headers=payload,
                        timeout=60,
                    )
                    
                except Exception as e:
                    log.info(f"Failed to get event status for {meeting_link}")
                
                if event_details_resp and event_details_resp.status_code == 200:
                    try:
                        event_details = event_details_resp.json()
                        if event_details.get("data", {}).get("eventStatus") == "live":
                            status = "In progress"
                    except (ValueError, KeyError) as e:
                        log.warning(f"Failed to parse event details for {meeting_link}: {e}")

            self.meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date,
                    "Meeting link": meeting_link,
                    "Agenda link": None,
                    "Status": status,
                }
            )
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://akleg.gov/index.php?tab3=Date%3D2%2F13%2F2025%26chamber%3D",
        schedule_type="unique_alaska",
        timezone="America/Anchorage",
        get_full_archive_flag=False,
    )
