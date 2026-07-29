import os
import sys
import re
from datetime import datetime
import pytz
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Azpharm:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_azpharm(self, url, timezone):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        div = soup.find("div", class_="dialog-off-canvas-main-canvas")
        page_div = div.find("div", id="page")
        main_div = page_div.find("div", id="main")

        content_div = (
            main_div.find("section", class_="section")
            .find("article")
            .find("div", class_="node__content clearfix")
        )

        meeting_div = content_div(
            "div",
            class_="views-element-container block block-views block-views-blockmeetings-listing-block-1",
        )

        for meeting_div in meeting_div:
            table = meeting_div.find(
                "table",
                class_="table table-hover table-striped views-table views-view-table cols-7",
            ).find("tbody")
            rows = table.find_all("tr")
            for row in rows:
                columns = row.find_all("td")
                meeting_name_div = columns[0].find("a")
                meeting_time_div = columns[1].find("time")
                agenda_div = columns[2].find("a")
                meeting_link_div = columns[3]

                meeting_name = meeting_name_div.get_text(strip=True)
                agenda_link = agenda_div.get("href") if agenda_div else None
                if agenda_link is not None:
                    agenda_link = domain + agenda_link

                meeting_link = (
                    meeting_link_div.get("href") if meeting_link_div else None
                )
                if meeting_link is not None:
                    meeting_link = domain + meeting_link

                # Extract the text inside the <time> tag and parse it
                meeting_date_time_text = meeting_time_div.text.strip()
                meeting_date_time_local = datetime.strptime(
                    meeting_date_time_text, "%B %d, %Y - %I:%M%p"
                )

                # Localize the datetime object to the specified local timezone
                meeting_date_time_local = timezone.localize(meeting_date_time_local)

                # Convert the local time to UTC
                meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

                # Format meeting_date_time as needed
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
        url="https://pharmacy.az.gov/meetings",
        schedule_type="unique_azpharm",
        timezone="America/Phoenix",
        get_full_archive_flag=False,
    )
