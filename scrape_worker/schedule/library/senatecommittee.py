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


class Senatecommittee:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_senatecommittee(self, url, timezone="America/New_York"):
        # Fetch HTML with JS rendering (rendered list)
        html_content = self.scraper.scrape_html(url=url, render="true")
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        div = soup.find("div", id="secondary_col2")
        if not div:
            return self.meetings

        wrapper = div.find("div", class_="dataTables_wrapper no-footer")
        if not wrapper:
            return self.meetings

        table = wrapper.find("table", id="listOfCommittees")
        if not table:
            return self.meetings

        tbody = table.find("tbody")
        if not tbody:
            return self.meetings

        rows = tbody.find_all("tr")
        for row in rows:
            columns = row.find_all("td")
            if len(columns) > 1:
                time_data_div = columns[0]
                spans = time_data_div.find_all("span")
                if len(spans) > 1:
                    meeting_name = columns[1].get_text(strip=True)
                    date_div = columns[0].find("span")
                    time_div = date_div.find_next("span")
                    meeting_date = columns[0].get_text()

                    pattern = r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},\s+\d{4}\b"
                    # Extract the date
                    match = re.search(pattern, meeting_date)

                    if match:
                        meeting_date = match.group()

                    meeting_time = time_div.get_text(strip=True)
                    meeting_time = meeting_time.split("–")[0]

                    meeting_date_time_web = f"{meeting_date} {meeting_time}"

                    try:
                        meeting_date_time_web = datetime.strptime(
                            meeting_date_time_web, "%b %d, %Y %I:%M %p "
                        )
                    except ValueError:
                        continue

                    # Convert each datetime object to the specified timezone
                    meeting_date_time_local = timezone.localize(meeting_date_time_web)

                    meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

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
        url="https://www.senate.gov/committees/hearings_meetings.htm",
        schedule_type="unique_senatecommittee",
        timezone="America/New_York",
    )
