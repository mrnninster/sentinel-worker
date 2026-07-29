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


class Housecommittee:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_housecommittee(self, url, timezone="America/New_York"):
        # Fetch HTML with JS rendering (rendered list)
        html_content = self.scraper.scrape_html(url=url, render="true")
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        div = soup.find("div", id="body")
        if not div:
            return self.meetings

        table = div.find("table", class_="calendar")
        if not table:
            return self.meetings

        tbody = table.find("tbody")
        if not tbody:
            return self.meetings

        rows = tbody.find_all("tr")
        for row in rows:
            columns = row.find_all("td")
            for column in columns:
                calendar_text = column.find("div", class_="calendar-text")
                if not calendar_text:
                    continue
                date_div = calendar_text.find("a")
                if not date_div:
                    continue
                meeting_date = date_div.get("title")
                tags = column.find_all("p")
                if tags is not None:
                    for tag in tags:
                        try:
                            time_tag = tag.find("a").find("b")
                            # Extract the meeting time
                            meeting_time = time_tag.get_text()

                            # Extract the meeting name by removing the <b> tag and trimming whitespace
                            meeting_name = (
                                tag.find("a").text.replace(meeting_time, "").strip()
                            )
                        except AttributeError:
                            continue

                        meeting_date_time_web = f"{meeting_date} {meeting_time}"

                        meeting_date_time_web = datetime.strptime(
                            meeting_date_time_web, "%B %d, %Y %I:%M %p"
                        )

                        # Convert each datetime object to the specified timezone
                        meeting_date_time_local = timezone.localize(
                            meeting_date_time_web
                        )

                        meeting_date_time_utc = meeting_date_time_local.astimezone(
                            pytz.utc
                        )

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
        url="https://docs.house.gov/Committee/Calendar/ByMonth.aspx",
        schedule_type="unique_housecommittee",
        timezone="America/New_York",
    )
