import os
import sys
import re
import pytz
import logging
import requests
from datetime import datetime, timedelta
from html import unescape
from typing import List, Optional

if __name__ == "__main__":
    sys.path.append(
        os.getenv("LOCAL_PROJECT_PATH")
        or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    )

from utils.scrape_html import HtmlScraper

logger = logging.getLogger(__name__)
LOOKBACK_DAYS = 7

# Tribe Events REST API base
API_BASE = "https://hutchinsonmn.gov/wp-json/tribe/events/v1/events"


class Hutchinson:
    """
    Scraper for City of Hutchinson meetings via WordPress Tribe Events REST API.

    The site at https://hutchinsonmn.gov uses The Events Calendar (Tribe Events)
    WordPress plugin, which exposes a REST API at:
        /wp-json/tribe/events/v1/events

    This parser fetches all upcoming events from the API rather than scraping HTML.
    The API returns structured JSON with event title, start/end dates, venue, and URL.

    Council meetings are broadcast on HCVN Channel 10 and the WebLink document site
    has agendas at:
        https://weblink.hutchinsonmn.gov/WebLink/Browse.aspx?id=39764
    """

    self_contained_parser = True

    def __init__(self):
        self.scraper = HtmlScraper()

    def unique_hutchinson(self, url: str, timezone: str) -> List[dict]:
        """
        Parse City of Hutchinson meetings from Tribe Events REST API.

        Args:
            url: The schedule URL (used as meeting link context; API URL is hardcoded)
            timezone: IANA timezone string (e.g. America/Chicago)

        Returns:
            List of meeting dicts
        """
        meetings = []
        tz = pytz.timezone(timezone)
        now = datetime.now(tz)
        lookback = now - timedelta(days=LOOKBACK_DAYS)

        # Fetch events from the API
        # The API returns events starting from today by default
        lookback_date = lookback.strftime("%Y-%m-%d")
        params = {
            "start_date": lookback_date,
            "per_page": 50,
            "page": 1,
        }

        all_events = []
        try:
            while True:
                response = requests.get(
                    API_BASE,
                    params=params,
                    timeout=15,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                response.raise_for_status()
                data = response.json()

                events = data.get("events", [])
                if not events:
                    break

                all_events.extend(events)

                # Check if there are more pages
                total_pages = data.get("total_pages", 1)
                if params["page"] >= total_pages:
                    break
                params["page"] += 1

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch events from API: {e}")
            return meetings
        except ValueError as e:
            logger.error(f"Failed to parse API response: {e}")
            return meetings

        for event in all_events:
            try:
                # Extract event data
                title = unescape(event.get("title", ""))
                if not title:
                    continue

                start_date_str = event.get("start_date", "")
                event_url = event.get("url", "")

                if not start_date_str:
                    continue

                # Parse start date (format: "2026-02-10 17:30:00")
                try:
                    meeting_dt = datetime.strptime(
                        start_date_str, "%Y-%m-%d %H:%M:%S"
                    )
                    meeting_dt = tz.localize(meeting_dt)
                except ValueError:
                    logger.warning(
                        f"Failed to parse date '{start_date_str}' for '{title}'"
                    )
                    continue

                # Skip old meetings beyond lookback
                if meeting_dt < lookback.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ):
                    continue

                # Determine status
                if meeting_dt.date() < now.date():
                    status = "Past"
                elif re.search(
                    r"cancel(?:led|ed)|reschedul", title, re.IGNORECASE
                ):
                    status = "Cancelled"
                else:
                    status = "Upcoming"

                # Convert to UTC ISO
                scheduled_time = meeting_dt.astimezone(pytz.UTC).strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                )

                meetings.append(
                    {
                        "Meeting name": title,
                        "Scheduled time": scheduled_time,
                        "Meeting link": event_url if event_url else None,
                        "Agenda link": None,
                        "Status": status,
                    }
                )

            except Exception as e:
                logger.warning(f"Error parsing event: {e}")
                continue

        return meetings


if __name__ == "__main__":
    from schedule.schedule_scraper import run_test

    run_test(
        url="https://hutchinsonmn.gov/events/",
        timezone="America/Chicago",
        schedule_type="unique_hutchinson",
    )
