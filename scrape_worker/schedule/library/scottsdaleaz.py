# scottsdaleaz.p
import logging
import os
from datetime import datetime
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from dateutil import parser

from utils.format_time import TimeFormatter
from utils.scrape_html import HtmlScraper, HTMLTags, HTMLAttributes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

MEETING_DATE_TIME_FORMAT = "%b %d, %Y, %I %p"
MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


class Scottsdaleaz:
    def __init__(self):
        self.timezone = None
        self.base_url = None
        self.url = None
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        self.scrapper_api_key = os.getenv("SCRAPERAPICOM_API_KEY")

    def scottsdaleaz_table(self, url: str, local_timezone: str) -> list:
        self.timezone = local_timezone
        self.url = url
        self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc

        detail_page_soup = self.__get_page_soup_using_scraperapi(url)
        if detail_page_soup is None:
            log.warning(f"Page by {self.url} not found")
            raise Exception("Error: cannot find page")

        meeting_tags = [
            *detail_page_soup.find_all(
                HTMLTags.DIV_TAG,
                class_="col-12 col-md-6 col-lg-3 d-flex align-items-stretch mt-3 event-col",
            ),
            *detail_page_soup.find_all(
                HTMLTags.DIV_TAG,
                class_="col-12 col-md-6 col-lg-3 d-flex align-items-stretch mt-3 event-col visually-hidden",
            ),
        ]

        for meeting_tag in meeting_tags:
            agenda_present = meeting_tag.find(
                HTMLTags.SPAN_TAG, class_="small text-uppercase"
            )
            if agenda_present:
                meeting_name = (
                    meeting_tag.find(HTMLTags.H5_TAG, class_="card-title").text.strip()
                    or ""
                )
                meeting_date_row = (
                    meeting_tag.find(HTMLTags.SPAN_TAG, class_="date")
                    .text.strip()
                    .replace(" starting at", "")
                    .replace(" a.m.", " AM")
                    .replace(" p.m.", " PM")
                )
                meeting_date_time_web = parser.parse(
                    meeting_date_row, fuzzy=True, ignoretz=True
                )

                formatted_naive_datetime = meeting_date_time_web.strftime(
                    TimeFormatter.desired_format()
                )
                time_formatter = TimeFormatter(formatted_naive_datetime, self.timezone)
                formatted_date_time = time_formatter.get_utc_time(as_datetime=True)

                agenda_link = meeting_tag.find(HTMLTags.LINK_TAG)[
                    HTMLAttributes.LINK_ATTRIBUTE
                ]
                meeting_date = (
                    formatted_date_time.strftime(
                        MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT
                    )[:-3]
                    + "Z"
                )

                self.meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": meeting_date,
                        "Meeting link": None,
                        "Agenda link": agenda_link,
                        "Status": "Upcoming",
                    }
                )

        return self.meetings

    def __get_page_soup_using_scraperapi(self, url: str) -> BeautifulSoup:
        payload = {
            "api_key": self.scrapper_api_key,
            "url": url,
            "render": "true",
        }
        page_with_needed_data = self.scraper.fetch_with_scraperapi(payload=payload)

        return self.scraper.convert_to_soup(string=page_with_needed_data)
