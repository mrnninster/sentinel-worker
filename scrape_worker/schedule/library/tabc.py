import os
import sys
import re
from datetime import datetime, timedelta
from dateutil import parser
import pytz
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Tabc:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_tabc(self, url, timezone="America/Chicago"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        div = soup.find("div", id="___gatsby")
        main = div.find("main", id="content")
        row = main.find("div", class_="row")
        container = row.find(
            "div",
            class_="pt-8 pt-xl-0 order-1 col-xl-6 col-lg-8 offset-xl-0 offset-lg-2",
        )

        meeting_divs = container.find_all("div", class_="css-1odvxm")

        for meet in meeting_divs:
            # Find the p tag containing 'Next commission meeting'
            p_tag = meet.find("p").find(
                "strong",
                string=lambda text: text and "Next commission" in text,
            )

            if p_tag:
                meet_div = meet.find("p")
                # Extract text from the p tag
                text = meet_div.get_text(strip=True)

                # Split text at ':'
                parts = text.split(":", 1)

                # Save first part as meeting name, remove 'Next ' and capitalize the first letters
                meeting_name = " ".join(
                    word.capitalize() for word in parts[0].replace("Next ", "").split()
                )

                # Save second part as meeting_date_time, remove the timezone code 'CDT'
                meeting_date_time_web = (
                    parts[1]
                    .replace("Â\xa0", " ")
                    .replace(" CDT", "")
                    .replace(" CT", "")
                    .replace(".", "")
                    .replace("B", "")
                    .replace("View agenda", "")
                    .strip()
                )

                try:
                    # Parse the date using dateutil.parser
                    print(meeting_date_time_web)
                    meeting_date_time_web = parser.parse(
                        meeting_date_time_web, fuzzy=True, ignoretz=True
                    )
                    print(meeting_date_time_web)
                    # Localize to the specified timezone
                    meeting_date_time_local = timezone.localize(meeting_date_time_web)
                    print(meeting_date_time_local)
                except (ValueError, OverflowError) as e:
                    print(f"Error parsing date: {e}")
                    continue

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
        url="https://www.tabc.texas.gov/about-us/agency-meetings/",
        schedule_type="unique_tabc",
        timezone="America/Chicago",
    )
