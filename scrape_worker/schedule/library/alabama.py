# alabama.py
import logging
import os
import requests
from typing import Any
from urllib.parse import urlparse, urlencode
from bs4 import BeautifulSoup
from datetime import datetime
import pytz
import re
from difflib import SequenceMatcher
from collections import defaultdict

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"

HOUSE_API_CALL_PAYLOAD_QUERY = "query meetings($body: OrganizationBody, $managedInLinx: Boolean, $autoScroll: Boolean!) { meetings(where: {body: {eq: $body}, startDate: {gteToday: true}, managedInLinx: {eq: $managedInLinx}}) {data {id startDate startTime location title description body hasPublicHearing hasLiveStream committee agendaUrl agendaItems @skip(if: $autoScroll) {id sessionType sessionYear instrumentNbr shortTitle matter recommendation hasPublicHearing sponsor __typename} __typename} count __typename}}"


class Alabama:
    def __init__(self):
        self.timezone = None
        self.base_url = None
        self.url = None
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = None  # Will be initialized when needed

    def _parse_date_time(self, date_str: str, time_str: str, timezone: str) -> str:
        """Parse date and time strings into ISO format datetime string"""
        try:
            # Parse date like "Monday, January 5, 2026"
            # Remove day of week if present
            date_clean = re.sub(r"^[A-Za-z]+,\s*", "", date_str.strip())
            # Parse time like "10:30 AM" or "1:30 PM"
            time_clean = time_str.strip()

            # Combine date and time
            datetime_str = f"{date_clean} {time_clean}"

            # Parse with format like "January 5, 2026 10:30 AM"
            dt = datetime.strptime(datetime_str, "%B %d, %Y %I:%M %p")

            # Convert to timezone-aware datetime
            tz = pytz.timezone(timezone)
            dt = tz.localize(dt)

            # Convert to UTC and return as ISO string
            dt_utc = dt.astimezone(pytz.UTC)
            return dt_utc.isoformat()
        except Exception as e:
            log.warning(f"Error parsing date/time '{date_str}' '{time_str}': {e}")
            return None

    def _make_absolute_url(self, href: str) -> str:
        """Convert relative URL to absolute URL"""
        if not href:
            return None
        if href.startswith("/"):
            return f"{self.base_url}{href}"
        elif href.startswith("http"):
            return href
        else:
            return f"{self.base_url}/{href}"

    def _scrape_html_table(self, url: str, timezone: str) -> list:
        """Scrape meetings from HTML table"""
        html_meetings = []
        try:
            # Initialize scraper if not already done
            if self.scraper is None:
                from utils.scrape_html import HtmlScraper

                self.scraper = HtmlScraper()

            # Try with rendered HTML first (page is JavaScript-rendered)
            # Wait for table to load and increase wait time
            html_content = self.scraper.scrape_html(
                url=url,
                render="true",
                wait_for_selector="table tbody",
                wait_for_seconds=10,
            )
            soup = BeautifulSoup(html_content, "html.parser")

            # Find the table - try multiple selectors
            table = soup.find("table", class_=re.compile(r"w-full"))
            if not table:
                # Try finding by tbody
                tbody = soup.find("tbody")
                if tbody:
                    table = tbody.find_parent("table")

            # Try finding any table with tbody
            if not table:
                all_tables = soup.find_all("table")
                for t in all_tables:
                    if t.find("tbody"):
                        table = t
                        break

            if not table:
                log.warning(f"No table found on {url}")
                # Log a sample of the HTML for debugging
                log.debug(f"HTML sample (first 1000 chars): {html_content[:1000]}")
                return html_meetings

            tbody = table.find("tbody")
            if not tbody:
                log.warning(f"No tbody found in table on {url}")
                return html_meetings

            rows = tbody.find_all("tr")
            log.debug(f"Found {len(rows)} rows in HTML table on {url}")

            for row in rows:
                try:
                    cells = row.find_all("td")
                    if len(cells) < 5:  # Need at least date, time, location, title
                        continue

                    # Extract data from cells (skip first cell which is expand button)
                    date_cell = cells[1] if len(cells) > 1 else None
                    time_cell = cells[2] if len(cells) > 2 else None
                    location_cell = cells[3] if len(cells) > 3 else None
                    title_cell = cells[4] if len(cells) > 4 else None
                    description_cell = cells[5] if len(cells) > 5 else None
                    agenda_cell = cells[8] if len(cells) > 8 else None

                    if (
                        not date_cell
                        or not time_cell
                        or not location_cell
                        or not title_cell
                    ):
                        continue

                    # Extract text content
                    date_text = date_cell.get_text(strip=True)
                    time_text = time_cell.get_text(strip=True)
                    title_text = title_cell.get_text(strip=True)
                    description_text = (
                        description_cell.get_text(strip=True)
                        if description_cell
                        else ""
                    )

                    # Extract location link
                    location_link = location_cell.find("a")
                    location_text = (
                        location_link.get_text(strip=True) if location_link else ""
                    )
                    location_href = (
                        location_link.get("href", "") if location_link else ""
                    )

                    # Build meeting link from href (make absolute if relative)
                    meeting_link = (
                        self._make_absolute_url(location_href)
                        if location_href
                        else None
                    )

                    # Extract agenda link
                    agenda_link = None
                    if agenda_cell:
                        agenda_a = agenda_cell.find("a")
                        if agenda_a:
                            agenda_href = agenda_a.get("href", "")
                            agenda_link = (
                                self._make_absolute_url(agenda_href)
                                if agenda_href
                                else None
                            )

                    # Parse date and time
                    scheduled_time = self._parse_date_time(
                        date_text, time_text, timezone
                    )
                    if not scheduled_time:
                        continue

                    html_meetings.append(
                        {
                            "Meeting name": title_text,
                            "Scheduled time": scheduled_time,
                            "Meeting link": meeting_link,
                            "Agenda link": agenda_link,
                            "Status": "Upcoming",
                            "_source": "html",
                        }
                    )
                except Exception as e:
                    log.warning(f"Error parsing row: {e}")
                    continue

            log.debug(f"Scraped {len(html_meetings)} meetings from HTML on {url}")
        except Exception as e:
            log.warning(f"Error scraping HTML from {url}: {e}")

        return html_meetings

    def _deduplicate_meetings(self, meetings: list) -> list:
        """Remove duplicate meetings from list using fuzzy name matching (90% similarity) and scheduled time"""
        # Group meetings by scheduled time first (O(n))
        time_groups = defaultdict(list)
        meetings_without_time = []

        for meeting in meetings:
            time = meeting.get("Scheduled time", "")
            if time:
                time_groups[time].append(meeting)
            else:
                # Meetings without time - add them directly (let downstream handle)
                meetings_without_time.append(meeting)

        # Apply fuzzy matching within each time group (much smaller groups)
        unique_meetings = []

        for meetings_at_time in time_groups.values():
            # Deduplicate within this time group
            unique_in_group = []
            for meeting in meetings_at_time:
                is_duplicate = False
                name1 = meeting.get("Meeting name", "")

                # Must have name to compare
                if not name1:
                    # If missing name, still add it
                    meeting_clean = {
                        k: v for k, v in meeting.items() if not k.startswith("_")
                    }
                    unique_in_group.append(meeting_clean)
                    continue

                # Only compare with meetings already added in this time group
                for existing_meeting in unique_in_group:
                    name2 = existing_meeting.get("Meeting name", "")
                    if not name2:
                        continue

                    # Check if names match with 90% similarity
                    similarity = SequenceMatcher(
                        None, name1.lower(), name2.lower()
                    ).ratio()
                    if similarity >= 0.90:
                        is_duplicate = True
                        log.debug(
                            f"Skipping duplicate meeting: {meeting.get('Meeting name')}"
                        )
                        break

                if not is_duplicate:
                    # Remove internal tracking fields before returning
                    meeting_clean = {
                        k: v for k, v in meeting.items() if not k.startswith("_")
                    }
                    unique_in_group.append(meeting_clean)

            # Add unique meetings from this time group to the final list
            unique_meetings.extend(unique_in_group)

        # Add meetings without time at the end
        for meeting in meetings_without_time:
            meeting_clean = {k: v for k, v in meeting.items() if not k.startswith("_")}
            unique_meetings.append(meeting_clean)

        return unique_meetings

    def alabama_legis_table(self, url: str, local_timezone: str) -> list:
        self.timezone = local_timezone
        self.url = url
        self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc

        # Try API first
        api_meetings = []
        api_url = f"{self.base_url}/graphql"
        payload = {
            "operationName": "meetings",
            "query": HOUSE_API_CALL_PAYLOAD_QUERY,
            "variables": {
                "autoScroll": False,
                "managedInLinx": True,
            },
        }

        body_calls = ["House", "Senate"]
        managed_in_linx_values = [True, False]

        for body_call in body_calls:
            for managed_in_linx in managed_in_linx_values:
                payload["variables"]["body"] = body_call
                payload["variables"]["managedInLinx"] = managed_in_linx
                try:
                    response = self._call_post_request(api_url, payload)
                    response_data = response.get("data")
                    if response_data:
                        response_meeting = response_data.get("meetings").get("data")
                        for meeting in response_meeting:
                            meeting_name = meeting.get("title")
                            meeting_date = meeting.get("startDate")
                            location = meeting.get("location")
                            meeting_id = meeting.get("id")

                            # Only create link if we have both location and meeting_id
                            if location and meeting_id:
                                params = urlencode(
                                    {"location": location, "meeting": meeting_id}
                                )
                                meeting_link = f"{self.base_url}/live-stream?{params}"
                            else:
                                meeting_link = None

                            api_meetings.append(
                                {
                                    "Meeting name": meeting_name,
                                    "Scheduled time": meeting_date,
                                    "Meeting link": meeting_link,
                                    "Agenda link": meeting.get("agendaUrl"),
                                    "Status": "Upcoming",
                                    "_source": "api",
                                    "_meeting_id": (
                                        str(meeting_id) if meeting_id else None
                                    ),
                                }
                            )
                except Exception as e:
                    log.warning(
                        f"Error querying API for {body_call} with managedInLinx={managed_in_linx}: {e}"
                    )
                    continue

        log.debug(f"Found {len(api_meetings)} meetings from API")

        # If API returned no meetings, try HTML scraping as fallback
        html_meetings = []
        if len(api_meetings) == 0:
            log.info("No meetings from API, trying HTML scraping as fallback...")
            # Try tabs 0, 1, 2
            base_url_clean = url.split("?")[0] if "?" in url else url
            for tab in [0, 1, 2]:
                tab_url = f"{base_url_clean}?tab={tab}"
                log.debug(f"Scraping HTML from tab {tab}: {tab_url}")
                tab_meetings = self._scrape_html_table(tab_url, local_timezone)
                html_meetings.extend(tab_meetings)

        # Combine API and HTML meetings
        all_meetings = api_meetings + html_meetings

        # Deduplicate
        unique_meetings = self._deduplicate_meetings(all_meetings)

        log.info(
            f"Total unique meetings: {len(unique_meetings)} (API: {len(api_meetings)}, HTML: {len(html_meetings)})"
        )

        self.meetings = unique_meetings
        return self.meetings

    def _call_post_request(self, url: str, data: Any) -> requests.Response:
        response = requests.post(url, json=data, timeout=10)
        response.raise_for_status()

        return response.json()


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test

    run_test(
        url="https://alison.legislature.state.al.us/todays-schedule?tab=2",
        schedule_type="alabama_legis_table",
        get_full_archive_flag=True,
    )
