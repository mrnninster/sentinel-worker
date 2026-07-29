import os
import re
import sys
import pytz
import requests
from datetime import datetime, UTC
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.pdf_text import extract_pdf_text_from_bytes
from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Ilpharm:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_ilpharm(self, url, timezone="America/Chicago"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        now = datetime.now(UTC)

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        timezone = pytz.timezone(timezone)

        div = soup.find(
            "h3",
            class_="cmp-title__text",
            string=lambda text: text and "Meeting Agendas" in text,
        )
        content = div.find_next("div", class_="cmp-text").find("p")

        rows = content.find_all("a")
        for row in rows:

            meeting_name = "Board Meeting"
            meeting_date_text = row.get_text(strip=True)
            meeting_date = meeting_date_text.split("on")[1].strip()

            agenda_link = domain + row.get("href")

            response = requests.get(agenda_link)

            # Extract text from the PDF
            text = extract_pdf_text_from_bytes(response.content)
            text = text.strip()

            time_pattern = r"\b\d{1,2}:\d{2}\s(?:am|pm)\son\s(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s(?:January|February|March|April|May|June|July|August|September|October|November|December)\s\d{1,2},\s\d{4}\b"

            # Find all matches of the time pattern in the text
            match = re.search(time_pattern, text)

            if match:
                time_data = match[0]
                meeting_time = time_data.split("on")[0].strip()
            else:
                print(f"Skipping Meeting ({meeting_name}): No time data yet...")
                continue

            meeting_date_time_web = meeting_date + " " + meeting_time
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

            meeting_date_time = meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            if meeting_date_time_local.date() < now.date():
                continue

            if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                status = "Cancelled"
            else:
                status = "Upcoming"
            meeting_link = None

            phone_number_pattern = r"by dialing (\d{1}-\d{3}-\d{3}-\d{4})"
            access_code_pattern = r"access code\)[: ]\s?(\d{4} \d{3} \d{4})"

            # Find matches
            phone_number = re.search(phone_number_pattern, text)
            access_code = re.search(access_code_pattern, text)

            # Extract matched groups if found
            phone_number = (
                phone_number.group(1).replace("-", "") if phone_number else None
            )
            access_code = access_code.group(1).replace(" ", "") if access_code else None

            if phone_number and access_code:
                dictionary = {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date_time,
                    "Meeting link": meeting_link,
                    "Phone number": phone_number,
                    "Access ID": access_code,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
            else:
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
        url="https://idfpr.illinois.gov/profs/boards/pharmacy.html",
        schedule_type="unique_ilpharm",
        timezone="America/Chicago",
        get_full_archive_flag=False,
    )
