import os
import sys
import re
import pytz
import logging
from datetime import datetime, timedelta
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

MEETING_LINK = "https://www.youtube.com/@NaturallyBetterHere/streams"


class Aitkincounty:
    """
    Scraper for Aitkin County Board Meetings.

    Parses the custom county website at https://www.co.aitkin.mn.us/board/{year}
    which lists meetings by month with date links and PDF packet links.

    Meetings are listed as:
      <ul class="months">
        <li>January
          <ul>
            <li><a href="01-06-2026/">01-06-2026</a> -- <a href="01-06-2026/pdf/...">Full Board Packet</a></li>
          </ul>
        </li>
      </ul>

    Some entries have suffixes like "-- COTW", "-- BAE", "-- Budget" appended
    as bold text indicating the meeting type.
    """

    self_contained_parser = True

    def __init__(self):
        self.scraper = HtmlScraper()

    def _to_utc_iso(self, time_str: str, timezone: str) -> Optional[str]:
        """Convert a date string to UTC ISO format."""
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
                dt = pytz.timezone(timezone).localize(dt)
            return dt.astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except (ValueError, TypeError):
            return None

    def unique_aitkincounty(self, url: str, timezone: str) -> List[dict]:
        """
        Parse Aitkin County board meeting schedule.

        Args:
            url: The schedule URL (e.g. https://www.co.aitkin.mn.us/board/2026)
            timezone: IANA timezone string (e.g. America/Chicago)

        Returns:
            List of meeting dicts with keys: Meeting name, Scheduled time,
            Meeting link, Agenda link, Status
        """
        meetings = []
        tz = pytz.timezone(timezone)
        now = datetime.now(tz)
        lookback = now - timedelta(days=LOOKBACK_DAYS)

        # Determine current year from URL or use current year
        year_match = re.search(r"/(\d{4})/?$", url)
        current_year = int(year_match.group(1)) if year_match else now.year

        response = self.scraper.fetch_with_bs(url=url)
        soup = self.scraper.convert_to_soup(string=response)

        # Find the months list
        months_ul = soup.find("ul", class_="months")
        if not months_ul:
            logger.warning("No months list found on page")
            return meetings

        # Each top-level <li> is a month containing a nested <ul> of meeting dates
        for month_li in months_ul.find_all("li", recursive=False):
            month_text = month_li.contents[0].strip() if month_li.contents else ""
            # Clean up month name (remove trailing whitespace)
            month_name = month_text.strip().rstrip(":")

            # Find nested <ul> with meeting dates
            inner_ul = month_li.find("ul")
            if not inner_ul:
                continue

            for date_li in inner_ul.find_all("li", recursive=False):
                try:
                    # Extract the date string from the <li> content
                    # It could be a plain text date or a linked date
                    date_str = None
                    agenda_link = None
                    meeting_suffix = ""

                    # Get all text content to find the date
                    li_text = date_li.get_text(strip=True)

                    # Look for date pattern MM-DD-YYYY
                    date_match = re.search(r"(\d{2}-\d{2}-\d{4})", li_text)
                    if not date_match:
                        continue
                    date_str = date_match.group(1)

                    # Check for meeting type suffix (COTW, BAE, Budget, etc.)
                    bold_tags = date_li.find_all("b")
                    for b in bold_tags:
                        b_text = b.get_text(strip=True).strip("— ").strip()
                        if b_text and b_text != "—":
                            meeting_suffix = b_text

                    # Look for PDF links (Full Board Packet, Signed Minutes, etc.)
                    pdf_links = date_li.find_all(
                        "a", href=lambda h: h and "pdf" in h.lower()
                    )
                    for pdf_link in pdf_links:
                        link_text = pdf_link.get_text(strip=True).lower()
                        if "packet" in link_text or "agenda" in link_text:
                            href = pdf_link.get("href", "")
                            if not href.startswith("http"):
                                # Relative URL - construct full URL
                                base_url = url.rstrip("/") + "/"
                                agenda_link = base_url + href
                            else:
                                agenda_link = href
                            break

                    # Parse the date - meetings are at 9:00 AM by default
                    # (Aitkin County board meetings typically start at 9:00 AM)
                    try:
                        meeting_date = datetime.strptime(date_str, "%m-%d-%Y")
                    except ValueError:
                        logger.warning(f"Failed to parse date: {date_str}")
                        continue

                    # Set default meeting time to 9:00 AM
                    meeting_dt = meeting_date.replace(hour=9, minute=0, second=0)
                    meeting_dt = tz.localize(meeting_dt)

                    # Build meeting name
                    if meeting_suffix:
                        meeting_name = (
                            f"Aitkin County Board Meeting - {meeting_suffix}"
                        )
                    else:
                        meeting_name = "Aitkin County Board Meeting"

                    # Determine status
                    if meeting_dt < lookback.replace(
                        hour=0, minute=0, second=0, microsecond=0
                    ):
                        continue  # Skip old meetings beyond lookback

                    if meeting_dt.date() < now.date():
                        status = "Past"
                    elif re.search(
                        r"cancel(?:led|ed)", li_text, re.IGNORECASE
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
                            "Meeting name": meeting_name,
                            "Scheduled time": scheduled_time,
                            "Meeting link": MEETING_LINK,
                            "Agenda link": agenda_link,
                            "Status": status,
                        }
                    )

                except Exception as e:
                    logger.warning(f"Error parsing meeting entry: {e}")
                    continue

        return meetings


if __name__ == "__main__":
    from schedule.schedule_scraper import run_test

    run_test(
        url="https://www.co.aitkin.mn.us/board/2026",
        timezone="America/Chicago",
        schedule_type="unique_aitkincounty",
    )
