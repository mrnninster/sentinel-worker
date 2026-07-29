import os
import sys
import re
import pytz
import logging
import requests
from datetime import datetime, timedelta, timezone
from typing import List, Optional

if __name__ == "__main__":
    sys.path.append(
        os.getenv("LOCAL_PROJECT_PATH")
        or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    )

logger = logging.getLogger(__name__)
LOOKBACK_DAYS = 7

# Finalsite calendar metadata API — returns the live Google Calendar iCal URL
CALENDAR_META_URL = (
    "https://www.isd47.org/cf_endpoints/routes.cfm/calendars.json?calendar_ids=354"
)

# Fallback iCal URL in case the metadata API is unreachable
ICAL_URL_FALLBACK = (
    "https://calendar.google.com/calendar/ical/"
    "isd47.org_u2s42r9tbbuuoeq51rfh0nu5u4%40group.calendar.google.com/public/basic.ics"
)


class Isd47:
    """
    Scraper for Sauk Rapids-Rice School District (ISD 47) board meetings.

    The Finalsite page at https://www.isd47.org/about/school-board uses calendar
    ID 353, which only contains 3 hardcoded organizational meetings. The district
    calendar (ID 354) is backed by a public Google Calendar iCal feed that has
    all regular board meetings going back to 2013.

    This parser:
    1. Fetches the calendar metadata from Finalsite to get the live iCal URL
       (falls back to the hardcoded URL if the metadata call fails)
    2. Parses the iCal VEVENT blocks, filtering for events whose summary
       contains "board" (case-insensitive)
    3. Returns upcoming meetings within the standard lookback window
    """

    self_contained_parser = True

    def _get_ical_url(self) -> str:
        """Fetch the iCal URL from the Finalsite calendar metadata API."""
        try:
            resp = requests.get(
                CALENDAR_META_URL,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            resp.raise_for_status()
            calendars = resp.json()
            for cal in calendars:
                if cal.get("calendarid") == 354 and cal.get("liveURL"):
                    return cal["liveURL"]
        except Exception as e:
            logger.warning(f"Could not fetch Finalsite calendar metadata: {e}")
        return ICAL_URL_FALLBACK

    def _parse_ical_dt(
        self, dtstart_line: str, default_tz: Optional[pytz.BaseTzInfo] = None
    ) -> Optional[datetime]:
        """
        Parse a DTSTART line from an iCal VEVENT.

        Handles:
          DTSTART:20260309T230000Z          — UTC datetime
          DTSTART;TZID=America/Chicago:...  — local datetime
          DTSTART;VALUE=DATE:20260105       — all-day date (default to 18:00 local)

        Args:
            dtstart_line: Raw iCal DTSTART line
            default_tz: Timezone for all-day events (defaults to UTC if not provided)
        """
        try:
            if "VALUE=DATE" in dtstart_line:
                date_str = dtstart_line.split(":")[-1].strip()
                dt = datetime.strptime(date_str, "%Y%m%d")
                dt = dt.replace(hour=18, minute=0, second=0)
                tz = default_tz or pytz.UTC
                return tz.localize(dt).astimezone(pytz.UTC)

            raw = dtstart_line.split(":", 1)[-1].strip()

            if "TZID=" in dtstart_line:
                tzid = re.search(r"TZID=([^:;]+)", dtstart_line)
                tz = pytz.timezone(tzid.group(1)) if tzid else pytz.UTC
                dt = datetime.strptime(raw, "%Y%m%dT%H%M%S")
                return tz.localize(dt).astimezone(pytz.UTC)

            if raw.endswith("Z"):
                return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc
                )

            # Bare local time with no timezone — treat as UTC
            dt = datetime.strptime(raw, "%Y%m%dT%H%M%S")
            return dt.replace(tzinfo=timezone.utc)

        except (ValueError, AttributeError) as e:
            logger.debug(f"Could not parse DTSTART '{dtstart_line}': {e}")
            return None

    def _parse_ical(self, ical_text: str, tz: pytz.BaseTzInfo, lookback: datetime) -> List[dict]:
        """Parse VEVENT blocks from an iCal string, returning board meeting dicts."""
        meetings = []
        now = datetime.now(timezone.utc)
        lookback_utc = lookback.astimezone(timezone.utc)

        # Split into VEVENT blocks
        blocks = re.split(r"BEGIN:VEVENT", ical_text)[1:]

        for block in blocks:
            # Unfold continued lines (RFC 5545: CRLF + whitespace = continuation)
            block = re.sub(r"\r?\n[ \t]", "", block)

            lines = block.splitlines()
            props = {}
            for line in lines:
                if ":" in line:
                    key, _, value = line.partition(":")
                    props[key.strip()] = value.strip()

            summary = props.get("SUMMARY", "")
            if not re.search(r"\bboard\b", summary, re.IGNORECASE):
                continue

            dtstart_line = next(
                (l for l in lines if l.startswith("DTSTART")), ""
            )
            if not dtstart_line:
                continue

            dt_utc = self._parse_ical_dt(dtstart_line, default_tz=tz)
            if not dt_utc:
                continue

            if dt_utc < lookback_utc:
                continue

            status_prop = props.get("STATUS", "CONFIRMED")
            if status_prop == "CANCELLED" or re.search(
                r"cancel(?:led|ed)", summary, re.IGNORECASE
            ):
                status = "Cancelled"
            elif dt_utc.date() < now.astimezone(tz).date():
                status = "Past"
            else:
                status = "Upcoming"

            scheduled_time = dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            meetings.append(
                {
                    "Meeting name": summary,
                    "Scheduled time": scheduled_time,
                    "Meeting link": None,
                    "Agenda link": None,
                    "Status": status,
                }
            )

        meetings.sort(key=lambda m: m["Scheduled time"])
        return meetings

    def unique_isd47(self, url: str, timezone: str) -> List[dict]:
        """
        Parse Sauk Rapids-Rice SD board meeting schedule from the district
        Google Calendar iCal feed.

        Args:
            url: The schedule page URL (kept for interface compatibility)
            timezone: IANA timezone string (e.g. America/Chicago)

        Returns:
            List of meeting dicts
        """
        tz = pytz.timezone(timezone)
        now = datetime.now(tz)
        lookback = now - timedelta(days=LOOKBACK_DAYS)

        ical_url = self._get_ical_url()

        try:
            resp = requests.get(
                ical_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            resp.raise_for_status()
            ical_text = resp.text
        except Exception as e:
            logger.error(f"Failed to fetch iCal feed from {ical_url}: {e}")
            return []

        return self._parse_ical(ical_text, tz, lookback)


if __name__ == "__main__":
    from schedule.schedule_scraper import run_test

    run_test(
        url="https://www.isd47.org/about/school-board",
        timezone="America/Chicago",
        schedule_type="unique_isd47",
    )
