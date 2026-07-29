# sandiego.py
import os
import sys
import re
import pytz
from dateutil import tz
from dateutil.parser import parse
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Sandiego:

    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_sandiego(self, url, timezone="America/Los_Angeles"):
        """San Diego Granicus publisher page (view_id=31).

        Custom-themed page with a 2-column table (Event + Next Meeting).
        No meeting link or agenda columns — meeting links only appear when
        a meeting is in progress (replacing the date text with a player link).
        Table uses id="listTable" and class="t-dark-border", unlike the
        standard Granicus classes.
        """
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        self.meetings = []

        table = soup.find("table", {"id": "listTable"})
        if not table or not table.tbody:
            return self.meetings

        base_url = re.match(r"(https?://[^/]+)", url)
        base_url = base_url.group(1) if base_url else ""

        rows = table.tbody.find_all("tr")
        for row in rows:
            columns = row.find_all("td")
            if len(columns) < 2:
                continue

            meeting_name = columns[0].get_text(strip=True)
            if not meeting_name:
                continue

            raw_date = columns[1].get_text(strip=True)
            status = "Upcoming"
            meeting_link = None

            # Check for in-progress indicators in the date column
            phrases_to_check = [
                "inprogress",
                "insession",
                "viewmeetinglive",
                "viewnow",
            ]
            clean_date = re.sub(r"\W+", "", raw_date.lower())
            if any(phrase in clean_date for phrase in phrases_to_check):
                status = "In progress"
                # Extract meeting link from the date column when live
                a_tag = columns[1].find("a", href=True)
                if a_tag:
                    meeting_link = a_tag["href"]
                    if meeting_link.startswith("//"):
                        meeting_link = "https:" + meeting_link
                    elif meeting_link.startswith("/"):
                        meeting_link = base_url + meeting_link

                meeting_date_time = (
                    datetime.now(pytz.UTC)
                    .replace(second=0, microsecond=0)
                    .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                    + "Z"
                )
            else:
                # Strip hidden unix timestamp prefix and parse date
                date_text = re.sub(r"^\d+", "", raw_date).strip()
                # Replace &nbsp; entities that survive get_text
                date_text = date_text.replace("\xa0", " ")
                try:
                    dt = datetime.strptime(date_text, "%A, %B %d, %Y - %I:%M %p")
                except ValueError:
                    try:
                        dt = parse(date_text, fuzzy=True).replace(tzinfo=None)
                    except Exception:
                        continue

                dt = dt.replace(tzinfo=tz.gettz(timezone)).astimezone(tz.gettz("UTC"))
                meeting_date_time = (
                    dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                )

            self.meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date_time,
                    "Meeting link": meeting_link,
                    "Agenda link": None,
                    "Status": status,
                }
            )

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://sandiego.granicus.com/ViewPublisher.php?view_id=31",
        schedule_type="unique_sandiego",
        timezone="America/Los_Angeles",
    )
