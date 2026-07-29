import logging
import os
import re
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import urlparse, urljoin

import pytz
import requests
from bs4 import BeautifulSoup
from dateutil import parser

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    local_project_path = os.getenv("LOCAL_PROJECT_PATH")
    if local_project_path and local_project_path not in sys.path:
        sys.path.append(local_project_path)
    from schedule.schedule_scraper import run_test

log = logging.getLogger(__name__)

_MONTH_NAME_PATTERN = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2},?\s+\d{2,4}"
)

_DATE_PATTERNS = [
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}",
    _MONTH_NAME_PATTERN,
    r"\d{4}-\d{1,2}-\d{1,2}",
]

_DATE_PARENS_RE = re.compile(
    r"\(\s*(?:" + "|".join(_DATE_PATTERNS) + r")\s*\)", re.IGNORECASE
)

CITY_KEYWORDS = ["city", "vancouver"]
COUNTY_KEYWORDS = ["county", "regional", "c-tran"]


class Vancouver:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True

    def vancouver_table_city(self, url, timezone="America/Los_Angeles"):
        return self._parse_schedule(url, timezone, mode="city")

    def vancouver_table_county(self, url, timezone="America/Los_Angeles"):
        return self._parse_schedule(url, timezone, mode="county")

    def _parse_schedule(self, url: str, timezone: str, mode: str) -> List[Dict]:
        self.meetings = []
        if not url:
            log.warning("No URL provided for Vancouver schedule")
            return []

        tz = pytz.timezone(timezone)
        now_local = datetime.now(tz)

        markup = self._fetch_schedule_markup(url)
        if not markup:
            return []

        soup = BeautifulSoup(markup, "html.parser")
        for panel in soup.select(".tabs__panel"):
            for day in panel.select(".schedule__day"):
                date_el = day.select_one(".schedule-day-date")
                if not date_el:
                    continue
                date_text = date_el.get_text(" ", strip=True)
                broadcast_date = self._parse_broadcast_date(date_text)
                if not broadcast_date:
                    continue
                if broadcast_date < now_local.date():
                    continue

                for item in day.select("li.day-list-item"):
                    time_el = item.select_one(".time")
                    title_el = item.select_one(".title")
                    if not time_el or not title_el:
                        continue

                    time_text = time_el.get_text(strip=True)
                    title_text = title_el.get_text(" ", strip=True)
                    if not time_text or not title_text:
                        continue

                    title_date = self._extract_date_from_title(title_text)
                    if not title_date or title_date != broadcast_date:
                        continue

                    if not self._title_matches_mode(title_text, mode=mode):
                        continue

                    meeting_dt_local = self._combine_date_time(
                        broadcast_date, time_text, tz
                    )
                    if not meeting_dt_local:
                        continue

                    meeting_dt_utc = meeting_dt_local.astimezone(pytz.utc)
                    scheduled_time = meeting_dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

                    meeting_name = self._strip_date_from_title(title_text)
                    meeting_link = None
                    link_el = title_el.find("a")
                    if link_el and link_el.get("href"):
                        meeting_link = link_el.get("href")

                    self.meetings.append(
                        {
                            "Meeting name": meeting_name,
                            "Scheduled time": scheduled_time,
                            "Meeting link": meeting_link,
                            "Agenda link": None,
                            "Status": "Upcoming",
                        }
                    )

        return self.meetings

    def _fetch_schedule_markup(self, url: str) -> Optional[str]:
        ajax_url = self._build_admin_ajax_url(url)
        if not ajax_url:
            return None
        try:
            resp = requests.get(
                ajax_url,
                params={"action": "get_channel_schedule"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("Failed to fetch CVTV schedule: %s", exc)
            return None

        if not data or not data.get("success"):
            log.warning("CVTV schedule response missing success flag")
            return None

        return data.get("markup")

    @staticmethod
    def _build_admin_ajax_url(url: str) -> Optional[str]:
        try:
            parsed = urlparse(url)
            if not parsed.scheme or not parsed.netloc:
                return None
            base = f"{parsed.scheme}://{parsed.netloc}"
            return urljoin(base, "/wp-admin/admin-ajax.php")
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _parse_broadcast_date(date_text: str) -> Optional[datetime.date]:
        if not date_text:
            return None
        match = re.search(_MONTH_NAME_PATTERN, date_text, re.IGNORECASE)
        if match:
            try:
                return parser.parse(match.group(0), fuzzy=True).date()
            except (ValueError, TypeError, parser.ParserError):
                pass
        return Vancouver._extract_date_from_title(date_text)

    @staticmethod
    def _combine_date_time(
        broadcast_date: datetime.date, time_text: str, tz
    ) -> Optional[datetime]:
        if not broadcast_date or not time_text:
            return None
        try:
            time_val = parser.parse(time_text, fuzzy=True).time()
        except (ValueError, TypeError, parser.ParserError):
            return None
        try:
            dt = datetime.combine(broadcast_date, time_val)
            return tz.localize(dt)
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _extract_date_from_title(title: str) -> Optional[datetime.date]:
        if not title:
            return None
        for pattern in _DATE_PATTERNS:
            match = re.search(pattern, title, re.IGNORECASE)
            if not match:
                continue
            try:
                return parser.parse(match.group(0), fuzzy=True).date()
            except (ValueError, TypeError, parser.ParserError):
                continue
        return None

    @staticmethod
    def _strip_date_from_title(title: str) -> str:
        if not title:
            return title
        cleaned = _DATE_PARENS_RE.sub("", title)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned or title

    @staticmethod
    def _title_matches_mode(title: str, mode: str) -> bool:
        if not title:
            return False
        t = title.lower()
        if mode == "county":
            return any(keyword in t for keyword in COUNTY_KEYWORDS)
        return any(keyword in t for keyword in CITY_KEYWORDS)


if __name__ == "__main__":
    url = "https://www.cvtv.org/program-schedule/"
    timezone = "America/Los_Angeles"
    run_test(url=url, timezone=timezone, schedule_type="vancouver_table_city")
    #run_test(url=url, timezone=timezone, schedule_type="vancouver_table_county")
