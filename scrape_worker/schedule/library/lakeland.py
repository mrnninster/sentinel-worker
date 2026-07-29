import logging
import re
import os
import sys
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from schedule.schedule_scraper import run_test
from utils.scrape_html import HtmlScraper

log = logging.getLogger(__name__)


class Lakeland:
    """
    Scraper for Lakeland Government's Upcoming Broadcasts page.

    This scraper extracts each meeting's name and scheduled time
    from the repeating group of broadcast entries found on:
        https://media.lakelandgov.net/

    """

    self_contained_parser = True

    def __init__(self):
        self._scraper = HtmlScraper()

    def unique_lakeland(self, url: str, timezone: str) -> list:
        """
        Extract meeting data from LakelandGov .

        Args:
            url (str): Target webpage URL (https://media.lakelandgov.net/).
            timezone (str): Timezone (e.g., 'America/New_York').

        Returns:
            list: A list of meeting dictionaries with keys:
                  'Meeting name', 'Scheduled time', 'Meeting link', 'Status'
        """
        meetings = []
        tz_info = pytz.timezone(timezone)

        # Fetch and parse HTML
        response = self._scraper.scrape_html(url=url, render="true")
        soup = self._scraper.convert_to_soup(string=response)

        # Select all meeting entries under "Upcoming Broadcasts"
        entries = soup.select("div.bubble-element.group-item")

        for entry in entries:
            try:
                # Extract name and date/time
                name_tag = entry.select_one("div.baTaIaMh div")
                date_tag = entry.select_one("div.baTaIaMd div")

                if not (name_tag and date_tag):
                    continue

                meeting_name = name_tag.get_text(strip=True)
                date_time_text = date_tag.get_text(strip=True)

                # Example format: "Tue, Oct 28 | 6:30 PM"
                date_match = re.search(
                    r"([A-Za-z]{3}), ([A-Za-z]{3}) (\d{1,2}) \| (\d{1,2}:\d{2} [APM]{2})",
                    date_time_text,
                )
                if not date_match:
                    log.warning(f"Unrecognized date/time format: {date_time_text}")
                    continue

                # Build date string with current year
                tz_now = datetime.now(tz_info)
                today_year = tz_now.year
                _, month_abbr, day, time_str = date_match.groups()
                date_str = f"{month_abbr} {day}, {today_year} {time_str}"

                # Parse localized time
                try:
                    local_dt = datetime.strptime(date_str, "%b %d, %Y %I:%M %p")
                    local_dt = tz_info.localize(local_dt)
                except Exception as e:
                    log.warning(f"Time parsing failed for '{date_str}': {e}")
                    continue

                # If the date is more than 30 days in the past, assume it's next year
                if local_dt < tz_now - timedelta(days=30):
                    local_dt = local_dt.replace(year=local_dt.year + 1)

                # Convert to UTC
                utc_dt = local_dt.astimezone(pytz.utc)
                meeting_time = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

                if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                    status = "Cancelled"
                elif utc_dt < datetime.now(pytz.utc):
                    status = "Past"
                else:
                    status = "Upcoming"

                meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": meeting_time,
                        "Meeting link": None,
                        "Agenda link": None,
                        "Status": status,
                    }
                )

            except Exception as e:
                log.warning(f"Error parsing entry: {e}")
        return meetings


if __name__ == "__main__":
    run_test(
        url="https://media.lakelandgov.net/",
        schedule_type="unique_lakeland",
    )
