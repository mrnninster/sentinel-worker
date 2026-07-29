import os
import sys
import re
import pytz
import logging
from datetime import datetime, timedelta
from urllib.parse import urljoin
from typing import List, Optional

if __name__ == "__main__":
    sys.path.append(
        os.getenv("LOCAL_PROJECT_PATH")
        or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    )

from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from utils.scrape_html import HtmlScraper

logger = logging.getLogger(__name__)
LOOKBACK_DAYS = 7


class Hoytlakes:
    """
    Scraper for City of Hoyt Lakes council meeting agendas.

    Parses the GoAskRob CMS page at:
        https://www.hoytlakes.com/html/city-departments/city-council-agenda.html

    The page contains a table with three columns:
      - Regular Council Meeting Agenda (dates link to PDF)
      - EDA Meeting Agenda (dates link to PDF)
      - Special Council Meeting Notice (dates link to PDF)

    Dates are in MM/DD/YYYY format. No meeting times listed on the page;
    we default to 5:30 PM (typical for city council meetings).
    """

    self_contained_parser = True

    def __init__(self):
        self.scraper = HtmlScraper()

    def _to_utc_iso(self, time_str: str, timezone: str) -> Optional[str]:
        """Convert a date string to UTC ISO format."""
        try:
            default_dt = datetime.now().replace(
                hour=17, minute=30, second=0, microsecond=0, tzinfo=None
            )
            dt = dateparser.parse(time_str, fuzzy=True, default=default_dt)
            if not dt:
                return None
            if dt.year < 2020 or dt.year > datetime.now().year + 2:
                return None
            if dt.tzinfo is None:
                dt = pytz.timezone(timezone).localize(dt)
            return dt.astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except (ValueError, TypeError):
            return None

    def unique_hoytlakes(self, url: str, timezone: str) -> List[dict]:
        """
        Parse City of Hoyt Lakes council meeting schedule.

        Args:
            url: The schedule URL
            timezone: IANA timezone string (e.g. America/Chicago)

        Returns:
            List of meeting dicts
        """
        meetings = []
        tz = pytz.timezone(timezone)
        now = datetime.now(tz)
        lookback = now - timedelta(days=LOOKBACK_DAYS)

        response = self.scraper.fetch_with_bs(url=url)
        soup = self.scraper.convert_to_soup(string=response)

        # The meeting data is in a nested table with headers:
        # "Regular Council Meeting Agenda", "EDA Meeting Agenda", "Special Council Meeting Notice"
        # Find the inner table (it has the column headers)
        tables = soup.find_all("table")

        # We want the table that has "Regular Council Meeting Agenda" header
        meeting_table = None
        for table in tables:
            header_row = table.find("tr")
            if header_row:
                header_text = header_row.get_text(strip=True)
                if "Regular Council Meeting Agenda" in header_text:
                    meeting_table = table
                    break

        if not meeting_table:
            logger.warning("Could not find meeting table on page")
            return meetings

        # Get all rows (skip header row)
        rows = meeting_table.find_all("tr")
        if len(rows) < 2:
            logger.warning("No data rows found in meeting table")
            return meetings

        # Column mapping: index 0 = Regular, 1 = EDA, 2 = Special
        column_names = [
            "Regular Council Meeting",
            "EDA Meeting",
            "Special Council Meeting",
        ]

        # Default times for each meeting type
        default_times = {
            "Regular Council Meeting": (17, 30),  # 5:30 PM
            "EDA Meeting": (17, 0),  # 5:00 PM
            "Special Council Meeting": (17, 30),  # 5:30 PM
        }

        for row in rows[1:]:  # Skip header row
            cells = row.find_all("td")
            for col_idx, cell in enumerate(cells):
                if col_idx >= len(column_names):
                    break

                meeting_type = column_names[col_idx]

                # Find links with dates
                links = cell.find_all("a", href=True)
                if not links:
                    # Check for plain text dates too
                    cell_text = cell.get_text(strip=True)
                    if not cell_text or cell_text == "\xa0":
                        continue

                for link in links:
                    try:
                        link_text = link.get_text(strip=True)
                        if not link_text:
                            continue

                        # Parse date from link text (MM/DD/YYYY format)
                        date_match = re.search(
                            r"(\d{1,2})/(\d{1,2})/(\d{4})", link_text
                        )
                        if not date_match:
                            continue

                        month = int(date_match.group(1))
                        day = int(date_match.group(2))
                        year = int(date_match.group(3))

                        # Default meeting time
                        hour, minute = default_times.get(meeting_type, (17, 30))

                        meeting_dt = datetime(
                            year, month, day, hour, minute, 0
                        )
                        meeting_dt = tz.localize(meeting_dt)

                        # Skip old meetings beyond lookback
                        if meeting_dt < lookback.replace(
                            hour=0, minute=0, second=0, microsecond=0
                        ):
                            continue

                        # Get agenda PDF link
                        href = link.get("href", "")
                        if href and not href.startswith("http"):
                            agenda_link = urljoin(url, href)
                        else:
                            agenda_link = href if href else None

                        # Determine status
                        if meeting_dt.date() < now.date():
                            status = "Past"
                        else:
                            status = "Upcoming"

                        meeting_name = f"Hoyt Lakes {meeting_type}"

                        # Convert to UTC ISO
                        scheduled_time = meeting_dt.astimezone(pytz.UTC).strftime(
                            "%Y-%m-%dT%H:%M:%S.000Z"
                        )

                        meetings.append(
                            {
                                "Meeting name": meeting_name,
                                "Scheduled time": scheduled_time,
                                "Meeting link": None,
                                "Agenda link": agenda_link,
                                "Status": status,
                            }
                        )

                    except Exception as e:
                        logger.warning(
                            f"Error parsing meeting entry in column {col_idx}: {e}"
                        )
                        continue

        return meetings


if __name__ == "__main__":
    from schedule.schedule_scraper import run_test

    run_test(
        url="https://www.hoytlakes.com/html/city-departments/city-council-agenda.html",
        timezone="America/Chicago",
        schedule_type="unique_hoytlakes",
    )
