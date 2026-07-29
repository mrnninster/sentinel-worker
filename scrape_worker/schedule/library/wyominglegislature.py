import os
import pytz
import requests
from datetime import datetime
from urllib.parse import urlparse
from dateutil import parser

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test
from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter


class Wyominglegislature:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True

    def wyominglegislature_api(self, url, timezone="America/Denver"):
        tz = pytz.timezone(timezone)
        now = datetime.now(tz)
        domain = urlparse(url).scheme + "://" + urlparse(url).netloc
        api_url = f"{url}/{now.strftime('%Y%m%d')}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Origin": domain,
            "Referer": f"{domain}/",
        }

        # Fetch data from the API
        response = requests.get(api_url, headers=headers)
        if response.status_code != 200:
            print(
                f"API request failed with status code {response.status_code}. Check the API endpoint and parameters."
            )
            return []

        data = response.json()

        for event in data:
            meeting_type = event.get("meetingType", "Unknown")
            committee_fullname = event.get("committee", {}).get("fullName", "Unknown")
            if committee_fullname.startswith("Senate") or committee_fullname.startswith(
                "House"
            ):
                meeting_name = committee_fullname
            else:
                meeting_name = f"{meeting_type} {committee_fullname}"
            start_date = event.get("startDate")
            media = event.get("meetingMedias", [])

            # Parse the start date and adjust for the timezone
            try:
                dt = parser.parse(start_date, ignoretz=True)
                if dt.tzinfo is None:
                    dt = tz.localize(dt)
            except ValueError as e:
                print(f"Error parsing date for {meeting_name}: {e}")
                continue

            formatted_naive_datetime = dt.strftime(TimeFormatter.desired_format())
            time_data = TimeFormatter(formatted_naive_datetime, timezone)
            utc_time_data = time_data.get_utc_time(as_datetime=True)
            isotime = utc_time_data.isoformat().replace("+00:00", "Z")

            # Determine status and meeting link
            meeting_link = None
            status = "Upcoming"
            for media_item in media:
                if media_item.get("filePath") and "youtube.com/live/" in media_item.get(
                    "filePath"
                ):
                    meeting_link = media_item.get("filePath")
                    break

            self.meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": isotime,
                    "Meeting link": meeting_link,
                    "Status": status,
                }
            )

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://lsoservice.wyoleg.gov/api/Calendar/Events/",
        schedule_type="wyominglegislature_api",
        timezone="America/Denver",
        get_full_archive_flag=False,
    )
