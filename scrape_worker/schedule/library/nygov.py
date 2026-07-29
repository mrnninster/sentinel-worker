import os
import re
import sys
import pytz
from datetime import datetime, timedelta, UTC
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Nygov:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_nygov(self, url, timezone="America/Los_Angeles"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        now = datetime.now(UTC)

        timezone = pytz.timezone(timezone)

        div = soup.find("div", class_="layout-content")

        container = div.find(
            "div",
            class_="bg-primary-blue text-white m-hero__meta -landingHero -left",
        )

        wrapper = container.find(
            "div",
            class_="m-landingHero__contentWrapper -left -video -bottomLive",
        )
        if wrapper is not None:
            live_link_div = wrapper.find("div", class_="m-landingHero__buttons")
            meeting_link = (
                live_link_div.find("button").get("data-stream")
                if live_link_div
                else None
            )

            date_div = wrapper.find("div", class_="m-landingHero__date")
            meeting_date = (
                date_div.find("span", class_="a-date a-hero__dateLanding").get_text(
                    strip=True
                )
                if date_div
                else None
            )

            detail_div = wrapper.find("div", class_="m-landingHero__description")
            meeting_text = (
                detail_div.find(
                    "div", class_="a-text__string a-hero__description -text"
                ).get_text(strip=True)
                if detail_div
                else None
            )

            if meeting_text is not None:
                # Extract time using regex
                match = re.search(r"At (\d{1,2}:\d{2} [APM]{2})", meeting_text)
                meeting_time = match.group(1) if match else None

                # Save remaining text as meeting_name
                meeting_name = re.sub(r"At \d{1,2}:\d{2} [APM]{2}, ", "", meeting_text)

            meeting_date_time_web = meeting_date + " " + meeting_time

            meeting_date_time_web = datetime.strptime(
                meeting_date_time_web, "%B %d, %Y %I:%M %p"
            )

            meeting_date_time_local = timezone.localize(meeting_date_time_web)

            meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

            meeting_date_time = meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            local_now = datetime.now(timezone)
            if local_now.date() > meeting_date_time_local.date():
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
        url="https://www.ny.gov/live",
        schedule_type="unique_nygov",
        timezone="America/New_York",
        get_full_archive_flag=False,
    )
