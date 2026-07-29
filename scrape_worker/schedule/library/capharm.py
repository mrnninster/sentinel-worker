import os
import sys
import re
from datetime import datetime, UTC
import pytz
from urllib.parse import urlparse
import requests
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.pdf_text import extract_pdf_text_from_bytes
from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Capharm:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def process_date_text(self, date_text):
        date_format_1 = "%B %d, %Y"  # Example 1 format
        date_format_2 = "%B %d-%d, %Y"  # Example 2 format

        if "-" in date_text:
            # Example 1 or Example 2 format
            date_parts = date_text.split("-")
            if len(date_parts) == 2:
                try:
                    # Example 1 format
                    date1 = datetime.strptime(
                        date_parts[0].strip(), date_format_1
                    ).strftime("%Y-%m-%d")
                    date2 = datetime.strptime(
                        date_parts[1].strip(), date_format_1
                    ).strftime("%Y-%m-%d")
                except ValueError:
                    # Example 2 format
                    text_range = date_text.split()
                    date_range = date_parts[0].split()

                    date1 = f"{date_range[0]} {date_range[1]}, {text_range[2]}"
                    date2 = f"{date_range[0]} {date_parts[1].strip()}"

                    # Parse the input date text
                    date1 = datetime.strptime(date1, date_format_1)
                    date2 = datetime.strptime(date2, date_format_1)

                    # Format the date in the desired format
                    date1 = date1.strftime("%Y-%m-%d")
                    date2 = date2.strftime("%Y-%m-%d")
                return date1, date2
        else:
            date_text = datetime.strptime(date_text, date_format_1)

            # Format the date in the desired format
            date_text = date_text.strftime("%Y-%m-%d")
            return date_text

    def create_meeting(
        self,
        meeting_date_time_web,
        meeting_name,
        agenda_link,
        access_code=None,
        passcode=None,
        meeting_link=None,
        timezone="America/Los_Angeles",
    ):
        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        meeting_date_time_local = timezone.localize(meeting_date_time_web)

        meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

        meeting_date_time = meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        start_of_today_local = datetime(now.year, now.month, now.day, tzinfo=timezone)

        if meeting_date_time_local < start_of_today_local:
            return None

        if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
            status = "Cancelled"
        else:
            status = "Upcoming"

        if access_code and passcode:
            dictionary = {
                "Meeting name": meeting_name,
                "Scheduled time": meeting_date_time,
                "Meeting link": meeting_link,
                "Access ID": access_code,
                "Passcode": passcode,
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
        return dictionary

    def unique_capharm(self, url, timezone="America/Los_Angeles"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        now = datetime.now(UTC)

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        table = soup.find("table", class_="table table-striped").tbody
        rows = table.find_all("tr")
        for row in rows:
            columns = row.find_all("td")
            meeting_name = "Board Meeting"
            meeting_date = columns[0].get_text(strip=True)

            agenda_div = columns[2].find(
                "a", string=lambda text: text and "Agenda" in text
            )
            agenda_link = agenda_div.get("href") if agenda_div else None
            if agenda_link:
                parts = meeting_date.split()

                year = now.year.__str__()
                meet_year = parts[2]

                if meet_year != year:
                    continue

                agenda_link = domain + agenda_link.replace("..", "")

                response = requests.get(agenda_link)

                # Extract text from the PDF
                text = extract_pdf_text_from_bytes(response.content)
                text = text.strip()
                text_cleaned = text.replace("\n", "")

                pattern = r"(\w+ \d{1,2}, \d{4}), (\d{1,2}:\d{2} [ap]\.m\.)"

                # Find all matches
                matches = re.findall(pattern, text)
                # Define the regular expression pattern to match WebEx URLs
                webex_pattern = r"https?://[\w./-]+\??\w*=\w*"

                # Find all matches of the WebEx URLs pattern in the text
                webex_urls = re.findall(webex_pattern, text_cleaned)

                access_code_pattern = r"Access code: (\d{4} \d{3} \d{4})"
                passcode_pattern = r"Passcode: (\d{6,7})"

                # Find all matches
                access_codes = re.findall(access_code_pattern, text_cleaned)
                passcodes = re.findall(passcode_pattern, text_cleaned)

                for match, webex_url, access_code, passcode in zip(
                    matches, webex_urls, access_codes, passcodes
                ):

                    meeting_date_time_web = (match[0] + " " + match[1]).replace(".", "")

                    meeting_date_time_web = datetime.strptime(
                        meeting_date_time_web, "%B %d, %Y %I:%M %p"
                    )

                    dictionary = self.create_meeting(
                        meeting_date_time_web=meeting_date_time_web,
                        meeting_link=webex_url,
                        agenda_link=agenda_link,
                        meeting_name=meeting_name,
                        access_code=access_code,
                        passcode=passcode,
                    )

                    if dictionary is not None:
                        self.meetings.append(dictionary)
            if agenda_link is None:
                parts = meeting_date.split()

                year = now.year.__str__()
                meet_year = parts[2]

                if meet_year != year:
                    continue

                meeting_date = self.process_date_text(meeting_date)
                if isinstance(meeting_date, tuple):
                    for meet_date in meeting_date:
                        meeting_date_time_web = meet_date

                        meeting_date_time_web = datetime.strptime(
                            meeting_date_time_web, "%Y-%m-%d"
                        )

                        dictionary = self.create_meeting(
                            meeting_date_time_web=meeting_date_time_web,
                            agenda_link=agenda_link,
                            meeting_name=meeting_name,
                        )

                        if dictionary is not None:
                            self.meetings.append(dictionary)
                else:

                    meeting_date_time_web = meeting_date

                    meeting_date_time_web = datetime.strptime(
                        meeting_date_time_web, "%Y-%m-%d"
                    )

                    dictionary = self.create_meeting(
                        meeting_date_time_web=meeting_date_time_web,
                        agenda_link=agenda_link,
                        meeting_name=meeting_name,
                    )

                    if dictionary is not None:
                        self.meetings.append(dictionary)
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://www.pharmacy.ca.gov/about/meetings_full.shtml",
        schedule_type="unique_capharm",
        timezone="America/Los_Angeles",
        get_full_archive_flag=False,
    )
