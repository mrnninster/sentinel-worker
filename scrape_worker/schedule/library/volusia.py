import os
import sys
import re
from datetime import datetime
import pytz
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Volusia:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_volusia(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        tz = pytz.timezone(timezone)
        now = datetime.now(tz)

        articles = soup.find_all("article")

        for article in articles:
            h2 = article.find("h2")
            if not h2:
                continue
            meeting_name = h2.get_text(strip=True)

            # Date/time is a text node after the h2, e.g. "February 17, 2026 4:00pm"
            container = h2.parent
            if not container:
                continue
            full_text = container.get_text(" ", strip=True)

            date_match = re.search(
                r"([A-Za-z]+ \d{1,2}, \d{4})\s+(\d{1,2}:\d{2}\s*(?:am|pm))",
                full_text,
                re.IGNORECASE,
            )
            if not date_match:
                continue

            date_str = date_match.group(1)
            time_str = date_match.group(2)

            try:
                meeting_dt = datetime.strptime(
                    f"{date_str} {time_str}", "%B %d, %Y %I:%M%p"
                )
            except ValueError:
                continue

            meeting_dt_local = tz.localize(meeting_dt)
            if meeting_dt_local.date() < now.date():
                continue

            meeting_dt_utc = meeting_dt_local.astimezone(pytz.utc)
            meeting_date_time = meeting_dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

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
        url="https://www.volusia.org/government/county-council/advisory-boards/meeting-dates.stml?datatable_category_id=24",
        schedule_type="unique_volusia",
        timezone="America/New_York",
    )
