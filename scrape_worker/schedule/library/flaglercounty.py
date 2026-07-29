# flaglercounty.py
import logging
import os
import re
from datetime import datetime
from io import BytesIO
from urllib.parse import urlparse

import pytz
from bs4 import BeautifulSoup

from utils.pdf_scanner import PDFScanner, RequestParams
from utils.scrape_html import HtmlScraper, HTMLTags, HTMLAttributes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

DATE_FORMAT = "%Y %m %d %I:%M %p"
DATE_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.000Z"
DATE_PATTERN = r"\b\d{4} \d{2} \d{2}\b"
PDF_DATE_PATTERN = r"\b\d{1,2}:\d{2} (?:[aApP]\.?[mM].?)"
MEETING_NAME_PATTERN = r"^\d{4} \d{2} \d{2} .*"
MEETING_SUB_STRING_PATTERN_FOR_PDF_SCANNER = "Public Notice"
DEFAULT_ADDITIONAL_MEETING_TIME = "12:00 AM"


class Flaglercounty:
    def __init__(self):
        self.timezone = None
        self.base_url = None
        self.url = None
        self.meetings = []
        self.scraper = HtmlScraper()
        self.pdf_scanner = PDFScanner()
        self.self_contained_parser = True
        self.scrapper_api_key = os.getenv("SCRAPERAPICOM_API_KEY")

    def flaglercounty_table(self, url: str, local_timezone: str) -> list:
        uniq_meeting_agendas = set()
        self.timezone = local_timezone
        self.url = url
        self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc

        detail_page_soup = self._get_page_soup_using_scraperapi(url)

        pages_selected_tag = detail_page_soup.find(
            HTMLTags.LINK_TAG, class_="pg-selected"
        )
        meeting_list = []
        pages_count = 1
        if pages_selected_tag is not None:
            pages_selected_tag[HTMLAttributes.LINK_ATTRIBUTE] = url.replace(
                self.base_url, ""
            )
            pages_tag = [
                pages_selected_tag,
                *detail_page_soup.find_all(HTMLTags.LINK_TAG, class_="pg-normal"),
            ]
            for page_tag in pages_tag:
                if pages_tag and len(page_tag.text.strip()) > 1:
                    continue
                pages_count += 1

                page_link = (
                    f"{self.base_url}{page_tag.get(HTMLAttributes.LINK_ATTRIBUTE)}"
                )
                detail_page_soup = self._get_page_soup_using_scraperapi(page_link)

                meeting_list = [
                    *meeting_list,
                    *detail_page_soup.find_all(
                        HTMLTags.LINK_TAG, class_="content_link"
                    ),
                ]
        if len(meeting_list) == 0:
            meeting_list = detail_page_soup.find_all(
                HTMLTags.LINK_TAG, class_="content_link"
            )

        count = 0
        total_meetings = len(meeting_list)
        meeting_list = meeting_list[::-1]
        public_notice_additional_date = ""
        public_notice_meeting_name = "Default Public Notice Name"
        for meeting in meeting_list:
            agenda_link = meeting.get(HTMLAttributes.LINK_ATTRIBUTE)
            count += 1
            if agenda_link in uniq_meeting_agendas:
                continue
            uniq_meeting_agendas.add(agenda_link)

            meeting_name = meeting.text.strip()
            if not re.match(MEETING_NAME_PATTERN, meeting_name):
                continue
            meeting_additional_time = DEFAULT_ADDITIONAL_MEETING_TIME

            if (
                agenda_link
                and MEETING_SUB_STRING_PATTERN_FOR_PDF_SCANNER in meeting_name
            ):
                pdf_params: RequestParams = RequestParams(
                    link=f"{self.base_url}{agenda_link}"
                )
                log.info(
                    f"Using pdf scanner for <{MEETING_SUB_STRING_PATTERN_FOR_PDF_SCANNER}> meeting"
                )
                pdf_text = self.pdf_scanner.scan_pdf_by_link(pdf_params)
                meeting_time_match = re.search(PDF_DATE_PATTERN, pdf_text)
                meeting_name = meeting_name.replace(
                    MEETING_SUB_STRING_PATTERN_FOR_PDF_SCANNER + " - ", ""
                )
                if meeting_time_match:
                    meeting_additional_time = (
                        meeting_time_match.group()
                        .strip()
                        .replace("a.m.", "AM")
                        .replace("p.m.", "PM")
                        .replace("A.M.", "AM")
                        .replace("P.M.", "PM")
                    )
                else:
                    log.warning(
                        f"For meeting name '{meeting_name}', the datetime in the PDF was not found. This could be an edge case with an unexpected date format. Agenda link: {agenda_link}"
                    )
                public_notice_additional_date = meeting_additional_time
                public_notice_meeting_name = meeting_name

            if (
                not MEETING_SUB_STRING_PATTERN_FOR_PDF_SCANNER in meeting_name
                and public_notice_meeting_name in meeting_name
            ):
                meeting_additional_time = public_notice_additional_date

            meeting_date_match = re.search(DATE_PATTERN, meeting_name)
            meeting_date_loc = None
            if meeting_date_match:
                adjusted_meeting_time = (
                    f"{meeting_date_match.group().strip()} {meeting_additional_time}"
                    if meeting_additional_time
                    else meeting_date_match.group().strip()
                )
                meeting_date_time = datetime.strptime(
                    adjusted_meeting_time, DATE_FORMAT
                )
                meeting_date_loc = self._convert_to_utc(
                    meeting_date_time, self.timezone
                )

            if meeting_name and re.search(
                r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE
            ):
                status = "Cancelled"
            elif meeting_date_loc and not self._is_past_meeting(meeting_date_loc):
                status = "Upcoming"
            else:
                status = "Past"

            meeting_date = (
                meeting_date_loc.strftime(DATE_UTC_FORMAT) if meeting_date_loc else None
            )

            if meeting_date is None:
                log.warning(
                    f"Skipping meeting <{meeting_name}> due to meeting date is None"
                )
                continue

            log.info(
                f"Processed {((count / total_meetings)*100).__round__(1)}% of meetings on page {pages_count}"
            )
            self.meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date,
                    "Meeting link": None,
                    "Agenda link": (
                        f"{self.base_url}{agenda_link}" if agenda_link else None
                    ),
                    "Status": status,
                }
            )
        return self.meetings

    def _get_page_soup_using_scraperapi(self, url: str) -> BeautifulSoup:
        payload = {"api_key": self.scrapper_api_key, "url": url}
        page_with_needed_data = self.scraper.fetch_with_scraperapi(payload=payload)

        return self.scraper.convert_to_soup(string=page_with_needed_data)

    def _convert_to_utc(self, date_time: datetime, local_timezone: str) -> datetime:
        local_tz = pytz.timezone(local_timezone)
        local_dt = local_tz.localize(date_time)
        return local_dt.astimezone(pytz.UTC)

    def _is_past_meeting(self, meeting_date: datetime) -> bool:
        today_start = self._convert_to_utc(datetime.now(), self.timezone).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return today_start > meeting_date
