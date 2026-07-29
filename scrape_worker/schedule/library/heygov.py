import os
import re
import sys
import logging
from datetime import datetime, timedelta
from typing import List, Optional

if __name__ == "__main__":
    sys.path.append(
        os.getenv("LOCAL_PROJECT_PATH")
        or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    )

import pytz
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

from utils.scrape_html import HtmlScraper

log = logging.getLogger(__name__)

LOOKBACK_DAYS = 7
HEYGOV_API_BASE = "https://api.heygov.com"
HEYGOV_FILES_BASE = "https://files.heygov.com"


class Heygov:
    """
    Self-contained scraper for HeyGov SaaS meeting pages.

    HeyGov embeds a JS widget on government sites and exposes a public
    JSON API at https://api.heygov.com/{jurisdiction}/meetings.

    The jurisdiction slug is extracted from the `data-heygov-jurisdiction`
    attribute on the HeyGov script tag in the page HTML.
    """

    self_contained_parser = True

    def __init__(self):
        self.scraper = HtmlScraper()

    def heygov_table(self, url: str, timezone: str) -> List[dict]:
        jurisdiction = self._extract_jurisdiction(url)
        if not jurisdiction:
            log.warning("Heygov: could not find jurisdiction slug for %s", url)
            return []

        api_url = f"{HEYGOV_API_BASE}/{jurisdiction}/meetings"
        data = self._fetch_json(api_url)
        if not data:
            return []

        meetings = []
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)

        for item in data:
            meeting = self._parse_meeting(item, timezone, min_allowed)
            if meeting:
                meetings.append(meeting)

        return meetings

    def _extract_jurisdiction(self, url: str) -> Optional[str]:
        """Extract the HeyGov jurisdiction slug from the page's script tag."""
        try:
            html = self.scraper.scrape_html(url=url)
            if not html or (isinstance(html, dict) and "max_failure" in html):
                return None
            soup = BeautifulSoup(html, "html.parser")

            # Look for data-heygov-jurisdiction attribute
            for script in soup.find_all("script"):
                jurisdiction = script.get("data-heygov-jurisdiction")
                if jurisdiction:
                    return jurisdiction

            # Fallback: search all elements for the attribute
            elem = soup.find(attrs={"data-heygov-jurisdiction": True})
            if elem:
                return elem["data-heygov-jurisdiction"]

        except Exception as e:
            log.warning("Heygov: error extracting jurisdiction from %s: %s", url, e)
        return None

    def _parse_meeting(
        self, item: dict, timezone: str, min_allowed: datetime
    ) -> Optional[dict]:
        title = item.get("title", "").strip()
        if not title:
            return None

        starts_at = item.get("starts_at")
        if not starts_at:
            return None

        # starts_at is already UTC ISO format
        scheduled_time = self._normalize_iso(starts_at)
        if not scheduled_time:
            return None

        utc_dt = self._parse_iso_to_utc(scheduled_time)
        if utc_dt and utc_dt < min_allowed:
            return None

        # Build agenda link from file path
        agenda_link = None
        agenda_path = item.get("agenda_file_path") or item.get(
            "agenda_pack_file_path"
        )
        if agenda_path:
            agenda_link = f"{HEYGOV_FILES_BASE}/{agenda_path}"

        # Build meeting link from video URL or conferencing link
        meeting_link = (
            item.get("video_public_url")
            or item.get("conferencing_link")
            or None
        )
        if meeting_link and not meeting_link.strip():
            meeting_link = None

        status = self._determine_status(title, scheduled_time, item)

        return {
            "Meeting name": title,
            "Scheduled time": scheduled_time,
            "Meeting link": meeting_link,
            "Agenda link": agenda_link,
            "Status": status,
        }

    def _determine_status(
        self, title: str, scheduled_time: str, item: dict
    ) -> str:
        if re.search(r"cancel", title, re.I):
            return "Cancelled"
        api_status = item.get("status", "")
        if api_status == "draft":
            return "Upcoming"
        utc_dt = self._parse_iso_to_utc(scheduled_time)
        if utc_dt and utc_dt < datetime.now(pytz.UTC):
            return "Past"
        return "Upcoming"

    def _normalize_iso(self, iso_str: str) -> Optional[str]:
        """Normalize an ISO string to our standard format."""
        try:
            dt = dateutil_parser.parse(iso_str)
            if not dt:
                return None
            if dt.year < 2020 or dt.year > datetime.now().year + 2:
                return None
            if dt.tzinfo is None:
                dt = pytz.UTC.localize(dt)
            utc = dt.astimezone(pytz.UTC)
            return utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except (ValueError, TypeError):
            return None

    def _parse_iso_to_utc(self, iso_str: str) -> Optional[datetime]:
        try:
            dt = dateutil_parser.parse(iso_str)
            if dt and dt.tzinfo is None:
                dt = pytz.UTC.localize(dt)
            return dt
        except (ValueError, TypeError):
            return None

    def _fetch_json(self, url: str) -> Optional[list]:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/115.0",
                "Accept": "application/json",
            }
            r = requests.get(url, headers=headers, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("Heygov: error fetching API %s: %s", url, e)
            return None


if __name__ == "__main__":
    from schedule.schedule_scraper import run_test

    run_test(
        url="https://newrichlandmn.gov/meetings/",
        timezone="America/Chicago",
        schedule_type="heygov_table",
    )
