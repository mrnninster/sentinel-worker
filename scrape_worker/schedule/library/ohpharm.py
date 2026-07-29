import os
import sys
from urllib.parse import urlparse
import re
from datetime import datetime
import pytz
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Ohpharm:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def remove_ordinal_suffix(self, date_str):
        return re.sub(r"(\d)(st|nd|rd|th)", r"\1", date_str)

    def unique_ohpharm(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        year = now.year

        agenda_link = None

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc
        # Set the timezone to "America/New_York"

        article = soup.find("article", id="main_content")
        name_div = article.find("h1")
        meeting_name = name_div.get_text().strip()

        details_div = name_div.find_next("p", id="phBody_pHeader")
        meeting_text = details_div.get_text().strip()

        # Define a regular expression pattern to match dates and times
        pattern = r"\b\w+day, \w+ \d{1,2}(?:st|nd|rd|th)? starting at \d{1,2}:\d{2} (?:AM|PM)\b"

        # Find all matches in the text
        matches = re.findall(pattern, meeting_text)

        # Parse the extracted date and time strings
        date_times = []

        if matches:
            for match in matches:
                match = self.remove_ordinal_suffix(match)

                dt_str = (
                    match.replace("starting at", "").replace(".", "").strip()
                    + f" {str(year)}"
                )

                dt = datetime.strptime(dt_str, "%A, %B %d %I:%M %p %Y")
                date_times.append(dt)

            agenda = []
            docu_div = article.find(
                "ul", id="phBody_DocumentListing1_ulDocumentList_Structured"
            )
            docu_list = docu_div.find_all("li")
            for item in docu_list:
                docu_name = item.find("a").get_text().strip()
                if "agenda" in docu_name.lower():
                    agenda_link = domain + item.find("a").get("href")
                    agenda.append({"name": docu_name, "url": agenda_link})

            for dt in date_times:

                meeting_date_time_local = timezone.localize(dt)

                meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

                meeting_date_time = meeting_date_time_utc.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                )

                if meeting_date_time_local.date() < now.date():
                    print(
                        f"Skipping past meeting from {meeting_date_time_local.date()}"
                    )
                    continue

                if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                    status = "Cancelled"
                else:
                    status = "Upcoming"
                meeting_link = None

                for doc in agenda:
                    docu_name = doc["name"]
                    agenda_link = doc["url"]

                    # Extract month and year from the document name
                    match = re.match(r"(\w+) (\d{4})", docu_name)
                    if match:
                        docu_month = match.group(1)

                        # Convert document month name to month number
                        try:
                            docu_month_num = datetime.strptime(docu_month, "%B").month
                        except ValueError:
                            continue

                        # Check if the document month and year match the dt month and year
                        if dt.month == docu_month_num:
                            agenda_link = agenda_link
                        else:
                            agenda_link = None

                dictionary = {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date_time,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
                self.meetings.append(dictionary)
        else:
            print("No meetings on the calendar for now, Try later...")

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://www.pharmacy.ohio.gov/boardmeeting",
        schedule_type="unique_ohpharm",
        timezone="America/New_York",
        get_full_archive_flag=False,
    )
