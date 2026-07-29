import os
import re
import sys
import logging
from datetime import datetime, timedelta
from typing import List, Optional
from urllib.parse import urljoin, urlparse

if __name__ == "__main__":
    sys.path.append(
        os.getenv("LOCAL_PROJECT_PATH")
        or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    )

import pytz
import requests

log = logging.getLogger(__name__)

LOOKBACK_DAYS = 7


class Squarespace:
    """
    Self-contained scraper for Squarespace-based government meeting pages.

    Squarespace exposes a JSON API at {page_url}?format=json that returns
    calendar events in `upcoming` and `past` arrays. Each event has:
    - title: Event name
    - startDate / endDate: Epoch milliseconds (UTC)
    - fullUrl: Path to event detail page
    - body: HTML body content
    """

    self_contained_parser = True

    def __init__(self):
        pass

    def squarespace_table(self, url: str, timezone: str) -> List[dict]:
        base_url = self._derive_base_url(url)
        data = self._fetch_json(url)
        if not data:
            return []

        meetings = []
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        # Process both upcoming and past events
        all_events = data.get("upcoming", []) + data.get("past", [])
        # Also check items in case the page uses a different layout
        all_events += data.get("items", [])

        seen_ids = set()
        for event in all_events:
            event_id = event.get("id")
            if event_id in seen_ids:
                continue
            seen_ids.add(event_id)

            meeting = self._parse_event(event, base_url, timezone, min_allowed)
            if meeting:
                meetings.append(meeting)

        return meetings

    def _parse_event(
        self, event: dict, base_url: str, timezone: str, min_allowed: datetime
    ) -> Optional[dict]:
        title = event.get("title", "").strip()
        if not title:
            return None

        start_ms = event.get("startDate")
        if not start_ms:
            return None

        # Convert epoch milliseconds to UTC datetime
        try:
            utc_dt = datetime.fromtimestamp(start_ms / 1000, tz=pytz.UTC)
        except (ValueError, TypeError, OSError):
            return None

        if utc_dt < min_allowed:
            return None

        scheduled_time = utc_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        # Build meeting link from fullUrl
        meeting_link = None
        full_url = event.get("fullUrl")
        if full_url:
            meeting_link = urljoin(base_url, full_url)

        status = self._determine_status(title, utc_dt)

        return {
            "Meeting name": title,
            "Scheduled time": scheduled_time,
            "Meeting link": meeting_link,
            "Agenda link": None,
            "Status": status,
        }

    def _determine_status(self, title: str, utc_dt: datetime) -> str:
        if re.search(r"cancel", title, re.I):
            return "Cancelled"
        if utc_dt < datetime.now(pytz.UTC):
            return "Past"
        return "Upcoming"

    def _fetch_json(self, url: str) -> Optional[dict]:
        """Fetch the Squarespace JSON API for a calendar page."""
        json_url = f"{url.rstrip('/')}?format=json"
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/115.0",
                "Accept": "application/json",
            }
            r = requests.get(json_url, headers=headers, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning(
                "Squarespace: error fetching %s: %s", json_url, e
            )
            return None

    def _derive_base_url(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"


if __name__ == "__main__":
    from schedule.schedule_scraper import run_test

    run_test(
        url="https://www.cityofmendota.org/city-calendar",
        timezone="America/Chicago",
        schedule_type="squarespace_table",
    )
