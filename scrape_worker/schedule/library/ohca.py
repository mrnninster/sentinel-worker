import os
import sys
import re
from datetime import datetime
from urllib.parse import urlparse
import pytz
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Ohca:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_ohca(self, url, timezone="America/Chicago"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)
        tz = pytz.timezone(timezone)

        now = datetime.now(tz)

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        table = soup.find("table").find("tbody")
        rows = table.find_all("tr")

        for row in rows:
            columns = row.find_all("td")
            for column in columns:
                meeting_name = "Board Meeting"

                # Extract date text from the column
                date_text = column.get_text().strip()

                # Extract date using regex (handles nested <b> tags)
                date_pattern = r"(\w+)\s+(\d{1,2}),?\s*(\d{4})"
                date_match = re.search(date_pattern, date_text)
                if not date_match:
                    continue

                month = date_match.group(1)
                day = date_match.group(2)
                year = date_match.group(3)
                meeting_date = f"{month} {day}, {year}"

                # Check for agenda link
                agenda_link_div = column.find(
                    "a",
                    string=lambda text: text and "agenda" in text.lower(),
                )
                agenda_link = agenda_link_div["href"] if agenda_link_div else None
                if agenda_link and not agenda_link.startswith("http"):
                    agenda_link = domain + agenda_link

                # Get meeting link (registration link)
                meeting_link_div = column.find(
                    "a",
                    string=lambda text: text and "register" in text.lower(),
                )
                meeting_link = meeting_link_div["href"] if meeting_link_div else None

                # No time provided on website - default to 9:00 AM
                meeting_time = "9:00 AM"

                try:
                    meeting_date_time_web = f"{meeting_date} {meeting_time}"
                    meeting_date_time_web = datetime.strptime(
                        meeting_date_time_web, "%B %d, %Y %I:%M %p"
                    )
                except ValueError:
                    continue

                meeting_date_time_local = tz.localize(meeting_date_time_web)

                if meeting_date_time_local.date() < now.date():
                    continue

                meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)
                meeting_date_time = meeting_date_time_utc.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                )

                if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                    status = "Cancelled"
                else:
                    status = "Upcoming"

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
        url="https://oklahoma.gov/ohca/about/boards-and-committees/ohca-board/ocha-board-meetings.html",
        schedule_type="unique_ohca",
        timezone="America/Chicago",
        get_full_archive_flag=False,
    )
