# civicclerk.py
import os
import pytz
import requests
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
from dateutil import parser

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test
from utils.format_time import TimeFormatter


class Civicclerk:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True

    def civicclerk_table(self, url, timezone="America/New_York"):
        now = datetime.now(tz=pytz.utc)
        domain = urlparse(url).scheme + "://" + urlparse(url).netloc
        api_domain = f"{domain.replace('portal', 'api')}/v1/Events"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            "accept": "application/json, text/plain, */*",
            "origin": domain,
            "referer": f"{domain}/",
        }

        # Get the current UTC time formatted to use as a filter in the API request
        current_utc = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        parsed_url = urlparse(url)
        qs = parse_qs(parsed_url.query)
        category_id = qs.get("category_id", [None])[0]
        base_filter = f"startDateTime gt {(now - timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')}"
        if category_id:
            filter_clause = f"{base_filter} and categoryId in ({category_id})"
        else:
            filter_clause = base_filter
        params = {
            "$filter": filter_clause,
            "$orderby": "EventDate asc, EventName asc",
        }
        print(f"params: {params}")

        # Fetch data and handle pagination
        next_url = api_domain
        while next_url:
            response = requests.get(
                next_url,
                headers=headers,
                params=params if next_url == api_domain else None,
            )
            if response.status_code == 200:
                data = response.json()
                events = data.get("value", [])

                # Process each event
                for event in events:
                    meeting_name = event.get("eventName")
                    event_date = event.get("startDateTime")
                    agenda_id = event.get("agendaId")
                    event_id = event.get("id")

                    # Construct the datetime object from the API response
                    try:
                        dt = parser.parse(event_date, ignoretz=True)
                        local_tz = pytz.timezone(timezone)
                        dt = local_tz.localize(dt)
                    except ValueError as e:
                        print(f"Error parsing date and time for {meeting_name}: {e}")
                        continue

                    formatted_naive_datetime = dt.strftime(
                        TimeFormatter.desired_format()
                    )
                    time_data = TimeFormatter(formatted_naive_datetime, timezone)
                    utc_time_data = time_data.get_utc_time(as_datetime=True)
                    isotime = utc_time_data.isoformat().replace("+00:00", "Z")

                    # Determine the status of the meeting
                    status = "In progress" if event.get("isLive") else "Upcoming"

                    meeting_link = None
                    if status == "In progress":
                        meeting_info = requests.get(
                            f"{api_domain}Media/GetEventMediaSummary(eventId={event_id})",
                            headers=headers,
                        ).json()
                        meeting_link = (
                            meeting_info.get("videoUrl", "") + "/playlist.m3u8"
                        )
                        print(f"Meeting is in progress. Media link: {meeting_link}")

                    # Construct the agenda link based on event and agenda IDs
                    agenda_link = (
                        f"{domain}/event/{event_id}/files/agenda/{agenda_id}"
                        if agenda_id
                        else None
                    )

                    # Construct the user live link based on event ID
                    user_live_link = f"{domain}/event/{event_id}/media"

                    self.meetings.append(
                        {
                            "Meeting name": meeting_name,
                            "Scheduled time": isotime,
                            "Meeting link": meeting_link,  # Update this if media link logic is provided
                            "user_live_link": user_live_link,
                            "Agenda link": agenda_link,
                            "Status": status,
                        }
                    )

                # Check if there is a next page
                next_url = data.get("@odata.nextLink")
                if next_url:
                    print(f"Fetching next page of results from: {next_url}")
            else:
                print(
                    f"API request failed with status code {response.status_code}. Check the API endpoint and parameters."
                )
                break

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://melbournefl.portal.civicclerk.com/?category_id=26",
        schedule_type="civicclerk_table",
        timezone="America/New_York",
        get_full_archive_flag=False,
    )
