import os
import re
import sys
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Senate:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_senate(self, url, timezone="America/New_York"):
        # Fetch HTML with JS rendering (rendered list)
        html_content = self.scraper.scrape_html(url=url, render="true")
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        div = soup.find("div", id="secondary_col2")
        if not div:
            return self.meetings

        table = div.find("table")
        if not table:
            return self.meetings

        tbody = table.find("tbody")
        if not tbody:
            return self.meetings

        rows = tbody.find_all("tr")
        for row in rows:
            columns = row.find_all("td")
            for column in columns:
                p_tags = column.find_all("p")
                if p_tags is not None:
                    for tag in p_tags:
                        tag_class = tag.get("class")
                        if tag_class is not None and "contentsubtitle" in tag_class:
                            b_tag = tag.find("b")
                            if not b_tag:
                                continue
                            meeting_date = b_tag.get_text(strip=True)
                            next_p = tag.find_next("p")
                            if not next_p:
                                continue
                            meeting_info = next_p.get_text(strip=True)
                            meeting_info = meeting_info.replace(".", "")
                            # Regular expression pattern to match the time and the meeting name
                            match = re.search(r"(\d+:\d+ [ap]m)", meeting_info)
                            meeting_name = re.sub(
                                r"(\d+:\d+ [ap]m): ", "", meeting_info
                            )

                            if match:
                                # Extract the time and meeting name from the match groups
                                meeting_time = match.group(1)

                                meeting_date_time_web = f"{meeting_date} {meeting_time}"

                                meeting_date_time_web = datetime.strptime(
                                    meeting_date_time_web,
                                    "%A, %b %d, %Y %I:%M %p",
                                )

                                meeting_date_time_local = timezone.localize(
                                    meeting_date_time_web
                                )

                                meeting_date_time_utc = (
                                    meeting_date_time_local.astimezone(pytz.utc)
                                )

                                meeting_date_time = meeting_date_time_utc.strftime(
                                    "%Y-%m-%dT%H:%M:%S.000Z"
                                )

                                if meeting_date_time_local.date() < now.date():
                                    continue

                                if re.search(
                                    r"Cancel(?:led|ed)",
                                    meeting_name,
                                    re.IGNORECASE,
                                ):
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
        url="https://www.senate.gov/legislative/floor_activity_pail.htm",
        schedule_type="unique_senate",
        timezone="America/New_York",
    )
