import logging
import re
import pytz
from datetime import datetime

from schedule.schedule_scraper import run_test
from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter

log = logging.getLogger(__name__)

BASE_URL = "https://legislature.vermont.gov"  # Example base domain


class Vermont:
    self_contained_parser = True

    def __init__(self):
        self._scraper = HtmlScraper()

    def unique_vermont(self, url: str, timezone: str) -> list:
        meetings = []
        tz_info = pytz.timezone(timezone)

        response = self._scraper.scrape_html(url=url, render="true")
        soup = self._scraper.convert_to_soup(string=response)

        # Extract all <tr> rows — grouped by date headers
        date_rows = soup.select("table#all-meeting-table tr.group")
        today = datetime.now(tz=tz_info).date()

        for date_row in date_rows:
            try:
                # Extract the date (e.g., "Tuesday, October 21, 2025")
                date_text = date_row.get_text(strip=True)
                date_match = re.search(
                    r"[A-Za-z]+,\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}", date_text
                )
                if not date_match:
                    continue
                date_str = date_match.group(0)

                # Parse date object
                meeting_date_obj = datetime.strptime(date_str, "%A, %B %d, %Y").date()

                # Find all meeting rows until the next date group
                next_rows = date_row.find_all_next("tr", recursive=False)
                for row in next_rows:
                    # Stop if next date header encountered
                    if "group" in row.get("class", []):
                        break

                    time_cell = row.select_one("td:nth-of-type(1)")
                    committee_cell = row.select_one("td:nth-of-type(2)")
                    agenda_cell = row.select_one("td:nth-of-type(3)")

                    if not (time_cell and committee_cell):
                        continue

                    # Extract time and convert to datetime
                    meeting_time_raw = time_cell.get_text(strip=True)
                    try:
                        meeting_time_obj = datetime.strptime(
                            meeting_time_raw.upper().replace(" ", ""), "%I:%M%p"
                        ).time()
                    except ValueError:
                        log.warning(f"Unrecognized time format: {meeting_time_raw}")
                        continue

                    # Combine date + time with timezone
                    meeting_start_raw = datetime.combine(
                        meeting_date_obj, meeting_time_obj, tzinfo=tz_info
                    )
                    utc_time = TimeFormatter(
                        meeting_start_raw.strftime(TimeFormatter.desired_format()),
                        timezone,
                    ).get_utc_time(as_datetime=True)
                    meeting_start = utc_time.isoformat().replace("+00:00", "Z")

                    # Extract meeting name and location
                    committee_link = committee_cell.select_one("a")
                    meeting_name = (
                        committee_link.get_text(strip=True)
                        if committee_link
                        else committee_cell.get_text(strip=True)
                    )

                    # Extract agenda link if available
                    agenda_link = None
                    if agenda_cell:
                        agenda_a = agenda_cell.select_one("a[href]")
                        if agenda_a:
                            agenda_link = BASE_URL + agenda_a["href"]

                    # Determine status
                    if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                        status = "Cancelled"
                    else:
                        status = "Upcoming"
                    meeting_link = None
                    link_url = (
                        BASE_URL + committee_link["href"] if committee_link else None
                    )
                    if link_url and meeting_date_obj == today:
                        response = self._scraper.scrape_html(
                            url=link_url, render="true"
                        )
                        page_soup = self._scraper.convert_to_soup(string=response)
                        details_div = page_soup.find("dl", class_="summary-table")

                        a_link = (
                            details_div.find(
                                "a",
                                string=lambda text: text
                                and "livestream" in text.lower(),
                            )
                            if details_div
                            else None
                        )

                        meeting_link = a_link["href"] if a_link else None

                    meetings.append(
                        {
                            "Meeting name": meeting_name,
                            "Scheduled time": meeting_start,
                            "Meeting link": meeting_link,
                            "Agenda link": agenda_link,
                            "Status": status,
                        }
                    )
            except Exception as e:
                log.warning(f"Error parsing meeting group: {e}")

        return meetings


if __name__ == "__main__":
    run_test(
        url="https://legislature.vermont.gov/committee/meetings/2026",
        schedule_type="unique_vermont",
    )
