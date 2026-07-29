import os
import sys
import re
import pytz
from datetime import date
from datetime import datetime
from datetime import timedelta
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Hialeah:
    def __init__(self):
        self.agenda_url = "https://www.hialeahfl.gov/AgendaCenter/City-Council-9"
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        self.meetings = []
        self.tuesdays = []
        self.meeting_link = None

    def unique_hialeah(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)
        domain = urlparse(url).scheme + "://" + urlparse(url).netloc
        timezone = pytz.timezone(timezone)
        now = datetime.now(timezone)
        current_year = now.year
        current_month = now.month

        for month in range(current_month, 13):
            # Find the first day of the month
            first_day = date(current_year, month, 1)

            # Find the day of the week for the first day (0 = Monday, 1 = Tuesday, ..., 6 = Sunday)
            weekday = first_day.weekday()

            # Calculate the number of days to the second and fourth Tuesdays
            days_to_second_tuesday = (1 - weekday + 7) % 7 + 7
            days_to_fourth_tuesday = (1 - weekday + 7) % 7 + 7 + 14

            # Calculate the dates for the second and fourth Tuesdays
            second_tuesday = first_day + timedelta(days=days_to_second_tuesday)
            fourth_tuesday = first_day + timedelta(days=days_to_fourth_tuesday)

            # Add them to the list
            self.tuesdays.append(second_tuesday)
            self.tuesdays.append(fourth_tuesday)

        for day in self.tuesdays:
            agenda_link = None
            div_element = soup.find("div", class_="moduleContentNew")

            time_tag = div_element.find_all("li")
            time_tag = time_tag[0]
            time_tag = time_tag.get_text(strip=True)
            time_tag = time_tag.replace(".", "")

            # Use strptime to parse the input string
            parsed_time = datetime.strptime(time_tag, "%I %p")

            # Use strftime to format the parsed time in the desired output format
            meeting_time = parsed_time.strftime("%H:%M")
            meeting_date_time_web = f"{day} {meeting_time}"
            meeting_date_time_web = datetime.strptime(
                meeting_date_time_web, "%Y-%m-%d %H:%M"
            )
            meeting_date_time_local = timezone.localize(meeting_date_time_web)
            meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

            # Format it to the desired ISO 8601 format
            meeting_date_time = meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            start_of_today_local = datetime(
                now.year, now.month, now.day, tzinfo=timezone
            )

            if meeting_date_time_local < start_of_today_local:
                continue
            name = div_element.find("h1", class_="headline")
            name = name.get_text(strip=True)

            meet = div_element.find("h2", class_="subhead1")
            meet = meet.get_text(strip=True)
            meeting_name = f"{name} {meet}"

            if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                status = "Canceled"
            else:
                status = "Upcoming"

            soup_new = self.scraper.fetch_with_bs(url=self.agenda_url)
            soup_new = self.scraper.convert_to_soup(string=soup_new)
            agenda_table = soup_new.find("table", id="table9")
            rows = agenda_table.tbody.find_all("tr", class_="catAgendaRow")

            for row in rows:
                item = row.find("td", class_="downloads")
                div = item.find("div", class_="popoutBtm")
                link_div = div.find("a", class_="pdf")
                link = link_div.get("href")

                text_div = row.find("p")
                text = text_div.get_text(strip=True)
                date_pattern = re.compile(r"([a-zA-Z]+\s+\d{1,2},\s+\d{4})")

                # Search for the date in the URL
                match = date_pattern.search(text)

                if match:
                    extracted_date = match.group(1)
                    # Parse the original date string
                    original_date = datetime.strptime(extracted_date, "%B %d, %Y")

                    # Format the date in the desired format
                    extracted_date = pytz.utc.localize(original_date)
                date_text = meeting_date_time_utc.date()
                date_str = date_text.strftime("%Y-%m-%d")
                extracted_date_str = extracted_date.strftime("%Y-%m-%d")

                if extracted_date_str == date_str:
                    agenda_link = domain + link

            self.meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date_time,
                    "Meeting link": self.meeting_link,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
            )
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://www.hialeahfl.gov/435/City-Council",
        schedule_type="unique_hialeah",
        timezone="America/New_York",
        get_full_archive_flag=False,
    )
