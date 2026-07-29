import os
import sys
import pytz
import re
from urllib.parse import urlparse
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Civicengage:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def civicengage_table(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        timezone = pytz.timezone(timezone)

        div_element = soup.find("div", class_="calendars")

        listed = div_element.find_all("li")
        for list in listed:
            meeting_name = list.find("h3").get_text(strip=True)
            meeting_name = meeting_name.split("-")[0].strip()
            div = list.find("div", class_="date")
            meeting_date_time_web = div.get_text(strip=True)
            meeting_date_time_web = meeting_date_time_web.split("-")[0].strip()
            if "all day" in meeting_date_time_web.lower():
                print(f"Skipping Meeting ({meeting_name}): Time is set to all day...")
                continue

            meeting_date_time_web = datetime.strptime(
                meeting_date_time_web, "%B %d, %Y, %I:%M %p"
            )
            meeting_date_time_local = timezone.localize(meeting_date_time_web)

            # Change time to UTC
            meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)
            # Format it to the desired ISO 8601 format
            meeting_date_time = meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                status = "Canceled"
            else:
                status = "Upcoming"

            agenda_tag = list.find("h3")
            href = agenda_tag.find("a").get("href")
            if href is not None:
                new_url = domain + href
                soup_new = self.scraper.fetch_with_bs(url=new_url)
                soup_new = self.scraper.convert_to_soup(string=soup_new)
                agenda_tag = soup_new.find("a", class_="agendaDownload")
                link = agenda_tag.get("href") if agenda_tag else None
                if link is not None:
                    if link.startswith("https:/"):
                        agenda_link = link
                    else:
                        agenda_link = domain + link
                else:
                    agenda_link = None
            else:
                agenda_link = None
            meeting_link = None
            self.meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date_time,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
            )
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://www.davie-fl.gov/calendar.aspx?CID=14&Keywords=&startDate=&enddate=&view=list",
        schedule_type="civicengage_table",
        timezone="America/New_York",
        get_full_archive_flag=False,
    )
