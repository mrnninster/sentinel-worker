import os
import re
import sys
import pytz
from datetime import datetime, timedelta, UTC
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Hylandcloud:

    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def hylandcloud_table(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        timezone = pytz.timezone(timezone)
        now = datetime.now(UTC)

        tables = soup.find_all("table", class_="table")

        for table in tables:
            rows = table.tbody.find_all("tr")

            for i, row in enumerate(rows):
                columns = row.find_all("td")
                if len(columns) > 4:
                    meeting_name = columns[0].get_text(strip=True) if columns else None
                    meeting_name = re.sub(
                        r"\b\d{1,2}/\d{1,2}/\d{4}\b|\b\w+\s+\d{1,2},?\s+\d{4}\b|\s*-\s*",
                        "",
                        meeting_name,
                    ).strip()
                    meeting_date_time_web = columns[2].get_text(strip=True)
                    # Parse the input string into a datetime object
                    meeting_date_time_web = datetime.strptime(
                        meeting_date_time_web, "%m/%d/%Y %I:%M:%S %p"
                    )
                    meeting_date_time_local = timezone.localize(meeting_date_time_web)
                    # Convert the time to UTC
                    meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)
                    # Format the UTC datetime object into the desired output string
                    meeting_date_time = meeting_date_time_utc.strftime(
                        "%Y-%m-%dT%H:%M:%S.000Z"
                    )

                    a_tag = columns[5].find("a", target="_blank")
                    if a_tag is not None:
                        agenda_link = a_tag.get("href")
                        agenda_link = domain + agenda_link
                    else:
                        agenda_link = None
                    link_tag = columns[5].find(
                        "a", string=lambda text: text and "Live Media" in text
                    )

                    if link_tag is not None:
                        meeting_link = link_tag.get("href")
                        meeting_link = domain + meeting_link
                        status = "In progress"
                    else:
                        meeting_link = None

                    if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                        status = "Cancelled"
                    else:
                        status = "Upcoming"
                    if meeting_date_time_local.date() < now.date():
                        continue
                    self.meetings.append(
                        {
                            "Meeting name": meeting_name,
                            "Scheduled time": meeting_date_time,
                            "Meeting link": meeting_link,
                            "Agenda link": agenda_link,
                            "Status": status,
                        }
                    )
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://agendaonline.mymanatee.org/OnBaseAgendaOnline/Meetings/Search",
        schedule_type="hylandcloud_table",
        timezone="America/New_York",
        get_full_archive_flag=False,
    )
