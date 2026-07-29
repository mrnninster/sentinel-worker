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


class Swagit:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def swagit_table(self, url, timezone="America/Los_Angeles"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        table = soup.find("table", class_="table")
        if not table:
            return self.meetings
        tbody = table.find("tbody")
        if not tbody:
            return self.meetings

        rows = tbody.find_all("tr")

        for row in rows:
            columns = row.find_all("td")

            meeting_name = columns[0].get_text(strip=True)
            meeting_link = columns[0].find("a").get("href")
            meeting_link = domain + meeting_link

            meeting_date_time_web = columns[1].get_text(strip=True)

            meeting_date_time_web = datetime.strptime(
                meeting_date_time_web, "%b %d, %Y %I:%M %p"
            )

            meeting_date_time_local = timezone.localize(meeting_date_time_web)

            meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

            meeting_date_time = meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            if meeting_date_time_local.date() < now.date():
                continue

            if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                status = "Cancelled"
            else:
                status = "Upcoming"
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
        url="https://longbeachca.swagit.com/live",
        schedule_type="swagit_table",
        timezone="America/Los_Angeles",
    )
