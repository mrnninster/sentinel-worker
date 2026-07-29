import os
import sys
import re
from urllib.parse import urlparse
from datetime import datetime
import pytz
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Nypsc:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_nypsc(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        div = soup.find("div", class_="view-content")

        rows = div.find_all("div", class_="views-row")

        for item in rows:
            name_tag = item.find("div", class_="webny-teaser-title")
            meeting_name = name_tag.get_text(strip=True)

            link = name_tag.find("a").get("href")
            link = domain + link
            soup_new = self.scraper.fetch_with_bs(url=link)
            soup_new = self.scraper.convert_to_soup(string=soup_new)
            section = soup_new.find("div", class_="wysiwyg--field-webny-wysiwyg-body")
            link_tag = section.find("p").find("a")
            meeting_link = link_tag.get("href") if link_tag else None

            # Extract date from day-month-wrapper
            date_tag = item.find("div", class_="month-day-year")
            meeting_date = date_tag.get_text(strip=True) if date_tag else None

            # Extract time from time-wrapper (new structure)
            time_wrapper = item.find("div", class_="time-wrapper")
            if time_wrapper:
                start_time_div = time_wrapper.find("div", class_="start_date")
                meridiem_div = time_wrapper.find("div", class_="meridiem")
                start_time = start_time_div.get_text(strip=True) if start_time_div else ""
                meridiem = meridiem_div.get_text(strip=True).replace(" ET", "") if meridiem_div else ""
                meeting_time = f"{start_time} {meridiem}"
            else:
                meeting_time = "12:00 PM"  # Default if not found

            if not meeting_date:
                continue

            meeting_start_time_web = f"{meeting_date} {meeting_time}"

            meeting_start_time_web = datetime.strptime(
                meeting_start_time_web, "%b %d, %Y %I:%M %p"
            )

            meeting_start_time_local = timezone.localize(meeting_start_time_web)

            # Convert the original time to UTC
            meeting_start_time_utc = meeting_start_time_local.astimezone(pytz.utc)

            # Format the UTC time in the desired format
            meeting_start_time = meeting_start_time_utc.strftime(
                "%Y-%m-%dT%H:%M:%S.000Z"
            )

            if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                status = "Cancelled"
            else:
                status = "Upcoming"
            agenda_link = None
            if now.date() > meeting_start_time_local.date():
                continue
            self.meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_start_time,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
            )

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://dps.ny.gov/public-service-commission-sessions",
        schedule_type="unique_nypsc",
        timezone="America/New_York",
        get_full_archive_flag=False,
    )
