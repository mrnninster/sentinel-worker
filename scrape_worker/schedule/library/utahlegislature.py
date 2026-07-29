import os
import pytz
import requests
from datetime import datetime
from urllib.parse import urlparse, urljoin
from dateutil import parser

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test

from utils.format_time import TimeFormatter


class Utahlegislature:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True

    def unique_utahlegislature(self, url, timezone="America/Denver"):
        """
        Scrapes the Utah Legislature calendar page.

        Args:
            url: The base URL for the calendar (e.g., https://le.utah.gov/calendar.html)
            timezone: The timezone for the meetings (default: America/Denver for Utah)

        Returns:
            List of meeting dictionaries with standard format
        """
        # Use the correct domain for building URLs (le.utah.gov)
        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        # Get current month and year, and also fetch next month for comprehensive coverage
        now = datetime.now(tz=pytz.utc)
        current_month = now.month
        current_year = now.year

        # Calculate next month
        if current_month == 12:
            next_month = 1
            next_year = current_year + 1
        else:
            next_month = current_month + 1
            next_year = current_year

        # Fetch data for current and next month
        months_to_fetch = [
            (current_month, current_year),
            (next_month, next_year),
        ]

        for month, year in months_to_fetch:
            # API endpoint is at www.utleg.gov
            api_url = f"https://www.utleg.gov/Calendar/month?month={month}&year={year}"

            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Referer": url,
            }

            try:
                response = requests.get(api_url, headers=headers, timeout=30)
                response.raise_for_status()
            except requests.RequestException as e:
                print(f"Error fetching calendar data for {month}/{year}: {e}")
                continue

            try:
                data = response.json()
            except ValueError as e:
                print(f"Error parsing JSON response for {month}/{year}: {e}")
                continue

            # Process each day's events
            try:
                for day_data in data.get("days", []):
                    for event in day_data.get("events", []):
                        # Only process meetings (not other event types)
                        if event.get("type") != "meeting":
                            continue

                        # Extract meeting information
                        meeting_name = event.get("description", "").strip()
                        item_time = event.get("itemtime")
                        location = event.get("location", "")
                        elec_mtg_link = event.get("elecMtgLink", "")

                        agenda = event.get("agenda", "")
                        status_code = event.get("status", "")
                        is_live = event.get("live", False)

                        # Validate required fields
                        if not meeting_name:
                            print(
                                f"Skipping meeting with missing name for event: {event.get('mtgID', 'unknown')}"
                            )
                            continue

                        if not item_time:
                            print(
                                f"Skipping meeting '{meeting_name}' with missing time"
                            )
                            continue

                        if not isinstance(item_time, str):
                            print(
                                f"Skipping meeting '{meeting_name}' with invalid time format: {type(item_time)}"
                            )
                            continue

                        # Parse the datetime
                        try:
                            # Parse the ISO timestamp (API returns UTC timestamps)
                            dt = parser.parse(item_time)

                            # The API returns UTC timestamps, so convert to local timezone
                            if dt.tzinfo is None:
                                # If timezone-naive, assume UTC
                                dt = pytz.utc.localize(dt)

                            # Convert UTC to local timezone for formatting
                            local_tz = pytz.timezone(timezone)
                            local_dt = dt.astimezone(local_tz)

                            # Format for TimeFormatter (needs naive datetime in local timezone)
                            formatted_time = local_dt.strftime(
                                TimeFormatter.desired_format()
                            )
                            time_formatter = TimeFormatter(formatted_time, timezone)
                            utc_time = time_formatter.get_utc_time(as_datetime=True)
                            isotime = utc_time.isoformat().replace("+00:00", "Z")

                        except (ValueError, TypeError) as e:
                            print(f"Error parsing date for '{meeting_name}': {e}")
                            continue

                        # Build meeting link - prefer elecMtgLink (Zoom), then mediaUrl, fallback to itemUrl
                        meeting_link = None
                        if elec_mtg_link and elec_mtg_link.strip():
                            meeting_link = elec_mtg_link

                        # Build agenda link
                        agenda_link = None
                        if agenda:
                            if agenda.startswith("http"):
                                agenda_link = agenda
                            else:
                                agenda_link = urljoin(domain, agenda)

                        # Determine status
                        # Check cancelled status first
                        if status_code == "C":  # Cancelled
                            status = "Cancelled"
                        elif is_live:
                            status = "In progress"
                        else:
                            status = "Upcoming"

                        # Add location to meeting name if available
                        if location:
                            meeting_name = f"{meeting_name} - {location}"

                        self.meetings.append(
                            {
                                "Meeting name": meeting_name,
                                "Scheduled time": isotime,
                                "Meeting link": meeting_link,
                                "Agenda link": agenda_link,
                                "Status": status,
                            }
                        )
            except (KeyError, AttributeError) as e:
                print(f"Error parsing calendar data structure for {month}/{year}: {e}")
                continue

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://le.utah.gov/calendar.html",
        schedule_type="unique_utahlegislature",
        timezone="America/Denver",
        get_full_archive_flag=False,
    )
