import os
import sys
import re
from urllib.parse import urlparse
from datetime import datetime, timedelta, UTC
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Hillsborough:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_hillsborough(self, url, timezone="America/New_York"):
        page_number = 1

        while page_number <= 5:
            modified_url = url.replace("%num%", str(page_number))

            print(f"Updated url => {modified_url}")
            api_key = os.getenv("SCRAPERAPICOM_API_KEY")
            payload = {
                "api_key": api_key,
                "url": modified_url,
                "render": "true",
            }

            soup = self.scraper.fetch_with_scraperapi(payload=payload)

            soup = self.scraper.convert_to_soup(string=soup)
            meetings = self.meeting_scraper(soup, url)
            self.meetings.extend(meetings)
            if page_number < 5:
                print("Next page...")
            else:
                print("Last page scraped!")
            page_number = page_number + 1
        return self.meetings

    def meeting_scraper(self, soup, url, timezone="America/New_York"):

        meetings = []
        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        div = soup.find("div", class_="ais-Configure")
        container = div.find("div", class_="ais-Hits")
        meeting_div = container.find("div", class_="v-list")
        list = meeting_div.find_all("a", class_="v-list-item") if meeting_div else None
        if list is not None:
            for item in list:
                meeting_name_div = item.find("div", class_="font-weight-bold")
                meeting_name = meeting_name_div.get_text(strip=True)  #

                meeting_time_div = item.find(
                    "div", class_="v-list-item-subtitle"
                ).get_text(strip=True)
                try:
                    original_datetime = datetime.strptime(
                        meeting_time_div, "%m/%d/%Y, %I:%M %p %Z"
                    )
                except ValueError:
                    original_datetime = meeting_time_div.strip("- ")
                    original_datetime = datetime.strptime(
                        meeting_time_div, "%m/%d/%Y, %I:%M %p %Z"
                    )
                meeting_date_time = original_datetime.strftime("%Y-%m-%dT%H:%M:%S.000Z")

                # Get the current UTC time
                now = datetime.now(UTC)
                href = item.get("href")
                link = domain + href
                api_key = os.getenv("SCRAPERAPICOM_API_KEY")
                payload = {"api_key": api_key, "url": link, "render": "true"}

                soup_new = self.scraper.fetch_with_scraperapi(payload=payload)

                soup_new = self.scraper.convert_to_soup(string=soup_new)
                agenda_container = soup_new.find(
                    "div", class_="v-container v-locale--is-ltr"
                )
                agenda_column = (
                    agenda_container.find(
                        "div", class_="v-col-sm-5 v-col-md-4 v-col-12"
                    )
                    if agenda_container
                    else None
                )

                agenda_div = (
                    agenda_column.find("a", class_="v-btn") if agenda_column else None
                )
                if agenda_div is not None:
                    agenda_link = agenda_div.get("href")
                else:
                    agenda_link = None

                # Determine the status based on the conditions
                if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                    status = "Cancelled"
                else:
                    status = "Upcoming"
                meeting_link = None
                if now.date() > original_datetime.date():
                    continue
                meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": meeting_date_time,
                        "Meeting link": meeting_link,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }
                )

        return meetings


if __name__ == "__main__":
    run_test(
        url="https://hillsboroughcounty.portal.civicclerk.com/event/search?selectedCategories=2&page=%num%",
        schedule_type="unique_hillsborough",
        timezone="America/New_York",
    )
