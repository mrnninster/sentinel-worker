import os
import re
import sys
import json
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
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from utils.scrape_html import HtmlScraper

log = logging.getLogger(__name__)

LOOKBACK_DAYS = 7

# Patterns to identify agenda files vs minutes vs other docs
AGENDA_RE = re.compile(r"agenda|packet", re.I)
MINUTES_RE = re.compile(r"minute", re.I)
# Date patterns in filenames: MM-DD-YY or MM-DD-YYYY
FILENAME_DATE_RE = re.compile(r"(\d{1,2})-(\d{1,2})-(\d{2,4})")


class Munibit:
    """
    Self-contained scraper for Munibit (MembershipWare) CMS pages.

    Munibit sites use a JavaScript widget that loads document data from a
    server-side API at /api/public/mwjsResources. The response is a JS
    variable assignment containing JSON with a `files` array.

    Each file has: fileTitle, fileName, fileDateCreated, fileUrl, tags, etc.
    Meeting dates are extracted from filenames (e.g., "01-02-24 Council
    Agenda Packet.pdf").
    """

    self_contained_parser = True

    def __init__(self):
        self.scraper = HtmlScraper()

    def munibit_table(self, url: str, timezone: str) -> List[dict]:
        base_url = self._derive_base_url(url)
        api_url = self._extract_api_url(url, base_url)
        if not api_url:
            log.warning("Munibit: could not find API URL for %s", url)
            return []

        files = self._fetch_files(api_url)
        if not files:
            return []

        return self._build_meetings(files, base_url, timezone)

    def _extract_api_url(self, page_url: str, base_url: str) -> Optional[str]:
        """Extract the mwjsResources API URL from the page HTML."""
        try:
            html = self.scraper.scrape_html(url=page_url)
            if not html or (isinstance(html, dict) and "max_failure" in html):
                return None

            soup = BeautifulSoup(html, "html.parser")
            for script in soup.find_all("script"):
                text = script.string or ""
                match = re.search(
                    r"import\(['\"](/api/public/mwjsResources\?[^'\"]+)['\"]",
                    text,
                )
                if match:
                    return urljoin(base_url, match.group(1))

            # Fallback: check for membershipware.com import
            for script in soup.find_all("script"):
                text = script.string or ""
                match = re.search(
                    r"import\s+\w+\s+from\s+['\"]"
                    r"(https://app\.membershipware\.com/api/public/mwjsResources\?[^'\"]+)"
                    r"['\"]",
                    text,
                )
                if match:
                    return match.group(1)

        except Exception as e:
            log.warning("Munibit: error extracting API URL from %s: %s", page_url, e)
        return None

    def _fetch_files(self, api_url: str) -> Optional[list]:
        """Fetch and parse the JS-wrapped JSON from the Munibit API."""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/115.0",
            }
            r = requests.get(api_url, headers=headers, timeout=30)
            r.raise_for_status()
            text = r.text.strip()

            # Strip JS variable assignment prefix
            prefix = "var mwjsMemberData="
            if not text.startswith(prefix):
                log.warning("Munibit: unexpected API response format")
                return None

            json_part = text[len(prefix) :]

            # Use raw_decode to properly parse the JSON object,
            # handling braces inside string values correctly
            decoder = json.JSONDecoder()
            data, _ = decoder.raw_decode(json_part)
            return data.get("files", [])

        except Exception as e:
            log.warning("Munibit: error fetching API %s: %s", api_url, e)
            return None

    def _build_meetings(
        self, files: list, base_url: str, timezone: str
    ) -> List[dict]:
        """
        Build meeting dicts from file list.

        Group files by date and prefer agenda files over minutes.
        """
        min_allowed = datetime.now(pytz.UTC) - timedelta(days=LOOKBACK_DAYS)
        meetings_by_date = {}

        for f in files:
            title = (f.get("fileTitle") or "").strip()
            filename = (f.get("fileName") or "").strip()
            file_url = f.get("fileUrl") or ""

            # Skip non-agenda files (minutes, ordinances, etc.)
            combined = f"{title} {filename}"
            if not AGENDA_RE.search(combined):
                continue
            if MINUTES_RE.search(title) and not AGENDA_RE.search(title):
                continue

            # Extract date from title or filename
            date_str = self._extract_date(title) or self._extract_date(
                filename
            )
            if not date_str:
                continue

            scheduled_time = self._to_utc_iso(date_str, timezone)
            if not scheduled_time:
                continue

            utc_dt = self._parse_iso_to_utc(scheduled_time)
            if utc_dt and utc_dt < min_allowed:
                continue

            date_key = scheduled_time[:10]
            if date_key not in meetings_by_date:
                # Extract meeting name from title (prefer fileTitle over
                # filename to avoid duplication)
                meeting_name = self._extract_meeting_name(title or filename)

                meetings_by_date[date_key] = {
                    "Meeting name": meeting_name,
                    "Scheduled time": scheduled_time,
                    "Meeting link": None,
                    "Agenda link": file_url if file_url else None,
                    "Status": self._determine_status(combined, scheduled_time),
                }

        return list(meetings_by_date.values())

    def _extract_date(self, text: str) -> Optional[str]:
        """Extract a date from a filename like '01-02-24 Council Agenda'."""
        match = FILENAME_DATE_RE.search(text)
        if not match:
            return None
        month, day, year = match.group(1), match.group(2), match.group(3)
        if len(year) == 2:
            year = f"20{year}"
        return f"{month}/{day}/{year}"

    def _extract_meeting_name(self, text: str) -> str:
        """Extract meeting name by removing date and 'Agenda Packet' suffix."""
        cleaned = FILENAME_DATE_RE.sub("", text)
        cleaned = re.sub(
            r"\s*(?:Agenda\s*Packet|Agenda|Packet|\.pdf)\s*",
            "",
            cleaned,
            flags=re.I,
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—:,.")
        return cleaned if cleaned else "Council Meeting"

    def _determine_status(self, title: str, scheduled_time: str) -> str:
        if re.search(r"cancel", title, re.I):
            return "Cancelled"
        utc_dt = self._parse_iso_to_utc(scheduled_time)
        if utc_dt and utc_dt < datetime.now(pytz.UTC):
            return "Past"
        return "Upcoming"

    def _derive_base_url(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _to_utc_iso(self, time_str: str, timezone: str) -> Optional[str]:
        try:
            default_dt = datetime.now().replace(
                hour=12, minute=0, second=0, microsecond=0, tzinfo=None
            )
            dt = dateparser.parse(time_str, fuzzy=True, default=default_dt)
            if not dt:
                return None
            if dt.year < 2020 or dt.year > datetime.now().year + 2:
                return None
            if dt.tzinfo is None:
                local_tz = pytz.timezone(timezone)
                dt = local_tz.localize(dt)
            utc = dt.astimezone(pytz.UTC)
            return utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except (ValueError, TypeError):
            return None

    def _parse_iso_to_utc(self, iso_str: str) -> Optional[datetime]:
        try:
            dt = dateparser.parse(iso_str)
            if dt and dt.tzinfo is None:
                dt = pytz.UTC.localize(dt)
            return dt
        except (ValueError, TypeError):
            return None


if __name__ == "__main__":
    from schedule.schedule_scraper import run_test

    run_test(
        url="https://fergusfallsmn.gov/agendasandminutes",
        timezone="America/Chicago",
        schedule_type="munibit_table",
    )
