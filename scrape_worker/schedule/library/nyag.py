import os
import sys
import re
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Nyag:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def remove_ordinal_suffix(self, date_str):
        return re.sub(r"(\d)(st|nd|rd|th)", r"\1", date_str)

    def unique_nyag(self, url, timezone="America/Los_Angeles"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        div = soup.find("main", class_="tw-grow")

        container = div.find("div", class_="node-content")

        section = container.find(
            "div",
            class_="paragraph paragraph--type--section paragraph--view-mode--default tw-section",
        )

        if section is not None:
            live_link_div = container.find(
                "div",
                class_="paragraph paragraph--type--wysiwyg paragraph--view-mode--default",
            )

            detail_div = live_link_div.find_next(
                "div",
                class_="paragraph paragraph--type--wysiwyg paragraph--view-mode--default",
            )

            if detail_div is not None:
                meeting_name = (
                    detail_div.find("h2").get_text(strip=True) if detail_div else None
                )

                meeting_link = (
                    live_link_div.find("iframe").get("src") if live_link_div else None
                )

                date_text_tag = detail_div.find("p") if detail_div else None
                meeting_date, time_text = [
                    str(entity).strip() for entity in date_text_tag.strings
                ]

                if time_text is not None:
                    time_text = time_text.replace(".", "")

                time_match = re.search(
                    r"\b(\d{1,2}:\d{2} (am|pm))\b", time_text, re.IGNORECASE
                )
                meeting_time = time_match.group(1) if time_match else None

                meeting_date_time_web = meeting_date + " " + meeting_time

                meeting_date_time_web = self.remove_ordinal_suffix(
                    meeting_date_time_web
                )

                meeting_date_time_web = datetime.strptime(
                    meeting_date_time_web, "%A, %B %d, %Y %I:%M %p"
                )

                meeting_date_time_local = timezone.localize(meeting_date_time_web)

                meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

                meeting_date_time = meeting_date_time_utc.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                )

                if meeting_date_time_local.date() < now.date():
                    return []

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
            else:
                print("No meeting on the page now, Check later...")
                return []


if __name__ == "__main__":
    run_test(
        url="https://ag.ny.gov/livestream",
        schedule_type="unique_nyag",
        timezone="America/New_York",
    )
