# legis.py
import json
import logging
import os
from datetime import datetime

import pytz
import requests
from dateutil import parser
from bs4 import BeautifulSoup
from requests.exceptions import RequestException
from typing import Dict, Any
from urllib.parse import urlparse

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test
from utils.format_time import TimeFormatter
from utils.scrape_html import HtmlScraper, HTMLTags, HTMLAttributes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


class Legis:
    def __init__(self):
        self.timezone = None
        self.base_url = None
        self.url = None
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        self.scrapper_api_key = os.getenv("SCRAPERAPICOM_API_KEY")

    def legis_georgia_table(self, url: str, local_timezone: str) -> list:
        self.timezone = local_timezone
        self.url = url
        self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc

        legis_api_url = self.base_url + "/api"

        json_auth_response = self.get_api_json_call(
            "https://www.legis.ga.gov/api/authentication/token"
        )
        if json_auth_response:
            tz = pytz.timezone(local_timezone)
            params = {"startDate": datetime.now(tz).strftime("%a %b %d %Y")}
            headers = {
                "Authorization": "Bearer {}".format(json_auth_response),
            }
            json_response = self.get_api_json_call(
                legis_api_url + "/meetings", params, headers
            )

            for meeting in json_response:
                agenda_link = meeting.get("agendaUri")
                meeting_date = meeting.get("start")
                status = "Upcoming"
                meeting_name = meeting.get("subject")
                meeting_link = meeting.get("livestreamUrl")

                self.meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": meeting_date,
                        "Meeting link": meeting_link,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }
                )

            return self.meetings

        log.warning(
            "Authorization token request is failed. Looking for HTML scrape method"
        )
        self.html_legis_georgia_table()

        return self.meetings

    def html_legis_georgia_table(self):
        log.info("Running HTML scraper")

        detail_page_soup = self.__get_page_soup_using_scraperapi(
            self.url, include_headers=True
        )

        pages_row = detail_page_soup.find(HTMLTags.LIST_TAG, class_="pagination")
        pages = pages_row.find_all(HTMLTags.LIST_ITEM_TAG)
        meetings_rows = self.__scrape_all_pages(pages)

        for meeting in meetings_rows:
            meeting_data = meeting.find_all(HTMLTags.COLUMNS_TAG)
            if len(meeting_data) < 6:
                continue
            meeting_name = meeting_data[2].text.strip()

            meeting_date = (
                meeting_data[0].text.strip() + " " + meeting_data[1].text.strip()
            )
            meeting_time_parsed = parser.parse(meeting_date, fuzzy=True, ignoretz=True)
            formatted_naive_datetime = meeting_time_parsed.strftime(
                TimeFormatter.desired_format()
            )
            time_formatter = TimeFormatter(formatted_naive_datetime, self.timezone)
            formatted_date_time = time_formatter.get_utc_time(as_datetime=True)
            meeting_date = (
                formatted_date_time.strftime(
                    MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT
                )[:-3]
                + "Z"
            )

            meeting_link = (
                meeting_data[4]
                .find(HTMLTags.LINK_TAG)
                .get(HTMLAttributes.LINK_ATTRIBUTE)
            )
            agenda_row = meeting_data[5].find(HTMLTags.COLUMNS_TAG)
            agenda_link = (
                agenda_row.get(HTMLAttributes.LINK_ATTRIBUTE) if agenda_row else None
            )
            status = "Upcoming"

            self.meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
            )

    def __scrape_all_pages(self, pages):
        meeting_rows = []
        for page in pages:
            page_link = page.find(
                HTMLTags.LINK_TAG,
                class_="page-link",
                attrs={"aria-label": None},
            )
            if page_link:
                detail_page_soup = self.__get_page_soup_using_scraperapi(
                    self.url + "?page=" + page_link.text, include_headers=True
                )
                meetings_table = detail_page_soup.find(
                    HTMLTags.TABLE_TAG,
                    class_="table table-striped table-sm table-reset-lg",
                )
                local_meeting_rows = meetings_table.find_all(HTMLTags.ROWS_TAG)

                meeting_rows = [*meeting_rows, *local_meeting_rows]

        return meeting_rows

    def __get_page_soup_using_scraperapi(self, url: str, **kwargs) -> BeautifulSoup:
        payload = {
            "api_key": self.scrapper_api_key,
            "url": url,
            "render": "true",
        }
        headers = None
        if kwargs.get("include_headers"):
            headers = {
                "x-sapi-instruction_set": json.dumps(
                    [
                        {
                            "type": "wait_for_selector",
                            "selector": {
                                "type": "css",
                                "value": ".pagination",
                            },
                            "timeout": 30,
                        }
                    ]
                )
            }

        page_with_needed_data = self.scraper.fetch_with_scraperapi(
            payload=payload, headers=headers
        )

        return self.scraper.convert_to_soup(string=page_with_needed_data)

    def get_api_json_call(
        self,
        base_api_url: str,
        params: Dict[str, Any] = None,
        headers: Dict[str, str] = None,
    ) -> Dict[str, Any]:
        """Fetches JSON data from an API endpoint with the given parameters.
        Args:
            base_api_url (str): The base URL of the API.
            params (Dict[str, Any]): The query parameters for the API request.
            headers (Dict[str, str]): The HTTP headers for the API request.
        Returns:
            Dict[str, Any]: The JSON response from the API.
        Raises:
            requests.exceptions.HTTPError: If the HTTP request returns an error.
            requests.exceptions.RequestException: For general request errors.
        """
        try:
            response = requests.get(
                base_api_url, params=params, headers=headers, timeout=40
            )
            response.raise_for_status()
            if "application/json" in response.headers.get("Content-Type", ""):
                return response.json()
            else:
                log.warning("Warning: Response does not contain valid JSON.")
                return {}
        except RequestException as e:
            log.warning(f"JSON API call request exception occurred: {e}")
            return {}


if __name__ == "__main__":
    run_test(
        url="https://www.legis.ga.gov/schedule/all",
        schedule_type="legis_georgia_table",
        get_full_archive_flag=True,
    )
