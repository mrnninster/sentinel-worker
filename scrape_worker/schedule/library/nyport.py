import os
import re
import sys
import pytz
from urllib.parse import urlparse
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Nyport:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_nyport(self, url, timezone="America/New_York"):
        # Fetch HTML with JS rendering (rendered list)
        html_content = self.scraper.scrape_html(url=url, render="true")
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        year = now.year

        div = soup.find("div", class_="AppPage")
        if not div:
            return self.meetings

        table = div.find("table")
        if table is not None:
            # Find all table bodies
            table_bodies = soup.find_all("tbody")

            # Check if any table bodies were found
            if table_bodies:
                # Iterate over each table body
                for table_body in table_bodies:
                    # Extract each row within the table body
                    rows = table_body.find_all("tr")
                    if len(rows) > 2:
                        # Iterate over each row
                        for row in rows:
                            # Extract the data within each cell
                            cells = row.find_all("td")
                            if len(cells) < 3:
                                continue
                            # Assuming each row has 3 cells: DATE, LOCATION, DOWNLOADS
                            meeting_name = "PORT AUTHORITY BOARD MEETINGS"
                            date = cells[0].text.strip()
                            location = cells[1].text.strip()
                            agenda_tag = cells[2].find("a")
                            agenda_link = agenda_tag.get("href") if agenda_tag else None
                            if agenda_link is not None:
                                agenda_link = domain + agenda_link

                            meeting_time = "12:45 pm"
                            meeting_date_time_web = (
                                date + " " + meeting_time + " " + str(year)
                            )
                            try:
                                # Parse the original time string
                                meeting_date_time_web = datetime.strptime(
                                    meeting_date_time_web, "%A, %B %d %I:%M %p %Y"
                                )
                            except ValueError:
                                continue
                            meeting_date_time_local = timezone.localize(
                                meeting_date_time_web
                            )

                            # Convert the original time to UTC
                            meeting_date_time_utc = meeting_date_time_local.astimezone(
                                pytz.utc
                            )

                            # Format the UTC time in the desired format
                            meeting_date_time = meeting_date_time_utc.strftime(
                                "%Y-%m-%dT%H:%M:%S.000Z"
                            )

                            # Determine the status based on the conditions
                            if re.search(
                                r"Cancel(?:led|ed)",
                                meeting_name,
                                re.IGNORECASE,
                            ):
                                status = "Cancelled"
                            else:
                                status = "Upcoming"
                            meeting_link = None
                            if now.date() > meeting_date_time_local.date():
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
        url="https://www.panynj.gov/corporate/en/board-meeting-info/current-agenda-and-meeting.html",
        schedule_type="unique_nyport",
        timezone="America/New_York",
    )
