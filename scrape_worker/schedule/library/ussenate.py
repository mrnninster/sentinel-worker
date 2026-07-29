import os
import sys
import pytz
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone as dt_timezone

from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


# Map full SSXX committee code prefixes to short codes used in ISVP URLs
CMTE_CODE_MAP = {
    "SSAG": "ag",
    "SSAP": "approps",
    "SSAS": "armed",
    "SSBK": "banking",
    "SSBU": "budget",
    "SSCM": "commerce",
    "SSEG": "energy",
    "SSEV": "epw",
    "SSFI": "finance",
    "SSFR": "foreign",
    "SSGA": "govtaff",
    "SSHR": "help",
    "SSIA": "indian",
    "SSIN": "intel",
    "SSJU": "judiciary",
    "SSRA": "rules",
    "SSSB": "smbiz",
    "SSVA": "vetaff",
}


class Ussenate:
    """
    Scraper for US Senate committee hearings.

    Parses the Senate committee hearings XML feed at
    senate.gov/general/committee_schedules/hearings.xml to extract
    upcoming committee hearing information.
    """

    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True

    def _extract_short_code(self, cmte_code: str) -> str:
        """Extract short committee code from full code (e.g., SSAG00 -> ag)."""
        if not cmte_code:
            return None
        prefix = cmte_code[:4].upper()
        return CMTE_CODE_MAP.get(prefix)

    def _build_isvp_url(self, short_code: str, date_obj: datetime) -> str:
        """Build ISVP URL from committee short code and date."""
        if not short_code:
            return None
        filename = f"{short_code}{date_obj.strftime('%m%d%y')}"
        return f"https://www.senate.gov/isvp/?comm={short_code}&filename={filename}"

    def unique_ussenate(self, url: str, timezone: str):
        """
        Scrape US Senate committee hearings from XML feed.

        Args:
            url: XML feed URL (https://www.senate.gov/general/committee_schedules/hearings.xml)
            timezone: Timezone string (e.g., "America/New_York")

        Returns:
            list: Meeting dicts with standard keys.
        """
        xml_text = self.scraper.scrape_html(url=url)

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            log.warning(f"Failed to parse Senate hearings XML: {e}")
            return self.meetings

        now_utc = datetime.now(tz=dt_timezone.utc)

        for meeting_elem in root.findall(".//meeting"):
            committee = (meeting_elem.findtext("committee") or "").strip()
            sub_cmte = (meeting_elem.findtext("sub_cmte") or "").strip()
            date_str = (meeting_elem.findtext("date_iso_8601") or "").strip()
            time_str = (meeting_elem.findtext("time_iso_8601") or "").strip()
            matter = (meeting_elem.findtext("matter") or "").strip()
            cmte_code = (meeting_elem.findtext("cmte_code") or "").strip()

            if not date_str or not committee:
                continue

            # Skip closed hearings
            if "closed" in matter.lower():
                continue

            # Parse datetime
            try:
                if time_str:
                    dt_str = f"{date_str}T{time_str}"
                else:
                    dt_str = f"{date_str}T00:00:00"

                meeting_dt = datetime.fromisoformat(dt_str)

                # If naive, localize to the geo's timezone
                if meeting_dt.tzinfo is None:
                    local_tz = pytz.timezone(timezone)
                    meeting_dt = local_tz.localize(meeting_dt)

                utc_dt = meeting_dt.astimezone(dt_timezone.utc)
                meet_date_time = utc_dt.isoformat().replace("+00:00", "Z")
            except Exception as e:
                log.warning(f"Error parsing date/time '{date_str} {time_str}': {e}")
                continue

            # Skip past meetings
            if utc_dt < now_utc:
                continue

            # Build meeting name
            meeting_name = committee
            if sub_cmte:
                meeting_name = f"{committee} - {sub_cmte}"

            # Build ISVP URL
            short_code = self._extract_short_code(cmte_code)
            meeting_link = self._build_isvp_url(short_code, meeting_dt)

            self.meetings.append({
                "Meeting name": meeting_name,
                "Scheduled time": meet_date_time,
                "Meeting link": meeting_link,
                "Agenda link": None,
                "Status": "Upcoming",
            })

        return self.meetings


if __name__ == "__main__":
    url = "https://www.senate.gov/general/committee_schedules/hearings.xml"
    tz = "America/New_York"
    schedule_type = "unique_ussenate"
    run_test(url=url, timezone=tz, schedule_type=schedule_type)
