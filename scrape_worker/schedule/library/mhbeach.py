import os
import re
import sys
import pytz
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Mhbeach:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_mhbeach(self, url, timezone="America/Los_Angeles"):
        # Fetch HTML with JS rendering (rendered list)
        html_content = self.scraper.scrape_html(url=url, render="true")
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        div = soup.find("div", id="ColumnUserControl1")
        if not div:
            return self.meetings

        # Get month and year from the calendar title
        month_year = None
        for td in div.find_all("td"):
            text = td.get_text(strip=True)
            # Look for pattern like "January 2026" at start
            match = re.match(
                r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})",
                text,
            )
            if match:
                month_year = f"{match.group(1)} {match.group(2)}"
                break

        if not month_year:
            # Fallback to current month/year
            month_year = now.strftime("%B %Y")

        table = div.find(
            "table", class_="calendar calendar_grid calendar-mini-grid-grid"
        )
        if not table:
            return self.meetings

        tbody = table.find("tbody")
        if not tbody:
            return self.meetings

        rows = tbody.find_all("tr")

        for row in rows:
            columns = row.find_all("td", class_="calendar_day_with_items")

            for column in columns:
                # Get day number from direct text of the TD (not nested in divs)
                day_num = "".join(column.find_all(string=True, recursive=False)).strip()
                if not day_num or not day_num.isdigit():
                    continue

                items = column.find_all("div", class_="calendar_item")

                for item in items:
                    time_span = item.find("span", class_="calendar_eventtime")
                    meeting_time = time_span.get_text(strip=True) if time_span else ""

                    # Skip events without a time (all-day events like surveys)
                    if not meeting_time:
                        continue

                    meeting_info_div = item.find("a", class_="calendar_eventlink")
                    if not meeting_info_div:
                        continue

                    meeting_name = meeting_info_div.get_text(strip=True)

                    # Build date string: "January 21, 2026 4:00 PM"
                    meeting_date_time_str = f"{month_year.split()[0]} {day_num}, {month_year.split()[1]} {meeting_time}"
                    try:
                        meeting_date_time_web = datetime.strptime(
                            meeting_date_time_str, "%B %d, %Y %I:%M %p"
                        )
                    except ValueError as e:
                        print(
                            f"Skipping meeting due to {e}",
                            meeting_date_time_str,
                        )
                        continue

                    meeting_date_time_local = timezone.localize(meeting_date_time_web)

                    meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

                    meeting_date_time = meeting_date_time_utc.strftime(
                        "%Y-%m-%dT%H:%M:%S.000Z"
                    )

                    if meeting_date_time_local.date() < now.date():
                        continue

                    if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                        status = "Cancelled"
                    else:
                        status = "Upcoming"
                    meeting_link = None
                    agenda_link = None

                    dictionary = {
                        "Meeting name": meeting_name,
                        "Scheduled time": meeting_date_time,
                        "Meeting link": meeting_link,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }
                    self.meetings.append(dictionary)
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://www.manhattanbeach.gov/residents/city-calendar-month-view",
        schedule_type="unique_mhbeach",
        timezone="America/Los_Angeles",
    )
