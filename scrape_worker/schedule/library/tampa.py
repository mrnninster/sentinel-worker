import os
import sys
import pytz
import re
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Tampa:

    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_tampa(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        tz = pytz.timezone(timezone)
        now = datetime.now(tz)
        year = now.year

        div = soup.find("div", class_="calendar-list")
        if not div:
            return self.meetings
        content = div.find("div", class_="view-content")
        if not content:
            return self.meetings

        # Date headers (h2.title) group items; parse "ThuFebruary5" etc.
        current_date = None
        for child in content.find_all(True, recursive=False):
            if child.name == "h2" and "title" in child.get("class", []):
                raw = child.get_text(strip=True)
                # Handle date ranges like "ThuFebruary5-FriFebruary6"
                raw = raw.split("-")[0]
                m = re.match(r"[A-Za-z]{3}([A-Za-z]+)(\d+)", raw)
                if m:
                    current_date = f"{m.group(1)} {m.group(2)}"
                continue

            classes = " ".join(child.get("class", []))
            if "item" not in classes or "row" not in classes:
                continue

            # Filter for city council meetings by category
            agency = child.find("div", class_="calendar-item-agency")
            category = agency.get_text(strip=True).lower() if agency else ""
            if "city council" not in category:
                continue

            # Get meeting name
            name_div = child.find("div", class_="fw-bold")
            if not name_div:
                continue
            name_link = name_div.find("a")
            meeting_name = (
                name_link.get_text(strip=True)
                if name_link
                else name_div.get_text(strip=True)
            )

            # Get time text (first text node in field-content span)
            raw_time = ""
            time_div = child.find("div", class_="views-field-nothing-2")
            if time_div:
                span = time_div.find("span", class_="field-content")
                if span:
                    for c in span.children:
                        if isinstance(c, str) and c.strip():
                            raw_time = c.strip()
                            break

            # Parse date + time
            if not current_date or not raw_time:
                continue

            # Clean time: "9:00am" -> "9:00 am", handle ranges like "5:01pm"
            clean_time = raw_time.split("-")[0].strip()
            clean_time = re.sub(r"(am|pm)", r" \1", clean_time, flags=re.IGNORECASE)

            try:
                date_time_str = f"{current_date} {year} {clean_time}"
                meeting_dt = datetime.strptime(date_time_str, "%B %d %Y %I:%M %p")
            except ValueError:
                try:
                    meeting_dt = datetime.strptime(date_time_str, "%B %d %Y %I %p")
                except ValueError:
                    continue

            meeting_dt_local = tz.localize(meeting_dt)
            if meeting_dt_local.date() < now.date():
                continue

            meeting_dt_utc = meeting_dt_local.astimezone(pytz.utc)
            meeting_date_time = (
                meeting_dt_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            )

            if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                status = "Cancelled"
            else:
                status = "Upcoming"

            self.meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date_time,
                    "Meeting link": None,
                    "Agenda link": None,
                    "Status": status,
                }
            )
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://www.tampa.gov/calendar",
        schedule_type="unique_tampa",
        timezone="America/New_York",
    )
