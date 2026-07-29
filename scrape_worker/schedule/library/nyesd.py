import re
from urllib.parse import urlparse
from datetime import datetime, timedelta
import pytz
import os
from bs4 import BeautifulSoup
from utils.scrape_html import HtmlScraper, HTMLTags, HTMLAttributes


class Nyesd:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_nyesd(self, url, timezone="America/New_York"):

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        html_string = self.scraper.fetch_with_bs(url)
        soup = self.scraper.convert_to_soup(string=html_string)

        if soup.find("div", class_="view-content").find_next(
            "div", class_="view-content"
        ):
            div = soup.find("div", class_="view-content").find_next(
                "div", class_="view-content"
            )
        else:
            div = soup.find("div", class_="view-content")
        rows = div.find_all("div", class_="views-row")

        for item in rows:
            name_tag = item.find("h1", class_="listing-view__card-headline")
            meeting_name = name_tag.get_text(strip=True)

            link = name_tag.find("a").get("href")
            link = domain + link if link else None
            if link is not None:
                soup_new = self.scraper.fetch_with_bs(url=link)
                soup_new = self.scraper.convert_to_soup(string=soup_new)
                section = soup_new.find("section", class_="news-article__files")
                agenda_link = None

                # Find all div elements with class "field__items"
                div_items = (
                    section.find_all("div", class_="field__item") if section else None
                )

                if div_items is not None:
                    for div in div_items:
                        # Check if the text contains "agenda" or "materials"
                        if (
                            "agenda" in div.text.lower()
                            or "materials" in div.text.lower()
                        ):
                            # Find the link within the div
                            a_link = div.find("a")
                            agenda_link = a_link.get("href") if a_link else None
                            agenda_link = domain + agenda_link if agenda_link else None

            else:
                agenda_link = None

            date_tag = item.find("div", class_="listing-view__card-details")
            date_time = date_tag.find("time")
            meeting_date_time_web = date_time.get("datetime")
            # Parse the original time string
            meeting_date_time_web = datetime.fromisoformat(meeting_date_time_web)

            # Convert the original time to UTC
            meeting_date_time_utc = meeting_date_time_web.astimezone(pytz.utc)

            # Format the UTC time in the desired format
            meeting_date_time = meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                status = "Cancelled"
            else:
                status = "Upcoming"
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
