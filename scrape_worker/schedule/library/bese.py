# This is not currently live. Returns no entries. Should probably use youtube.
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


class Bese:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_bese(self, url, timezone="America/Chicago"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        wrapper = soup.find("div", class_="sf_cols main-content-wrap")
        main = wrapper.find("div", class_="sf_colsIn sf_1col_1in_100")

        div = main.find("div", class_="sfContentBlock sf-Long-text")
        content = div.find_all("p", style="text-align: center")

        for item in content:
            a_tag = item.find("a")
            if a_tag is not None:
                meeting_name_div = a_tag.find("strong")
                meeting_time_div = a_tag.find_next("em")

                name_text = meeting_name_div.get_text().strip()
                time_text = meeting_time_div.get_text().strip()

                meeting_date, meeting_name = name_text.split("-")
                meeting_name = meeting_name.strip()

                meeting_time, channel = time_text.replace(".", "").split(",")
                if "tba" in meeting_time.lower():
                    print(f"Skipping Meeting ({meeting_name}): No time data yet...")
                    continue

                meeting_link = a_tag.get("href")

                meeting_date_time_web = meeting_date + meeting_time

                try:
                    meeting_date_time_web = datetime.strptime(
                        meeting_date_time_web, "%B %d, %Y %I:%M %p"
                    )
                except ValueError:
                    meeting_date_time_web = datetime.strptime(
                        meeting_date_time_web, "%B %d, %Y "
                    )

                meeting_date_time_local = timezone.localize(meeting_date_time_web)

                meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

                meeting_date_time = meeting_date_time_utc.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                )

                if meeting_date_time_local.date() < now.date():
                    print("Skipping past meeting...")
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
        url="https://bese.louisiana.gov/meetings/live-streaming-and-video-archive",
        schedule_type="unique_bese",
        timezone="America/Chicago",
        get_full_archive_flag=False,
    )
