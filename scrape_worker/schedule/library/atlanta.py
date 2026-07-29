import re
import os
import sys
import pytz
import logging
from fuzzywuzzy import fuzz
from dateutil import parser
from datetime import datetime
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from utils.youtube import Youtube
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test

MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


class Atlanta:
    """
    This is a self contained scraper for atlanta
    it uses both the youtube streamer and the schedule scraper.

    This is a sample request:
    {
        "geodicts": [
            {
                "geoID": "1725901206632x585898701176532600",
                "schedule_type": "unique_atlanta",
                "url": "https://citycouncil.atlantaga.gov/other/events/public-meetings/-curm-%m%/-cury-%y%",
                "agenda_url": "",
                "timezone": "America/New_York",
                "glitch_meetings": [],
                "debug": false,
                "channel_url": "https://www.youtube.com/@Devour808/streams"
            }
        ],
        "version": "test"
    }
    """

    def __init__(self):
        self.url = None
        self.meetings = []
        self.timezone = None
        self.base_url = None
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        self.channel_url = os.getenv("ARG_CHANNEL_URL")

    def _get_page_soup(self, url: str) -> BeautifulSoup:
        page_soup_str = self.scraper.scrape_html(url=url)
        page_soup = self.scraper.convert_to_soup(page_soup_str)
        return page_soup

    def unique_atlanta(self, url: str, local_timezone: str) -> list:
        self.url = url
        self.timezone = local_timezone
        self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc

        # Parse the channel url
        live_youtube_meetings = []
        if self.channel_url:
            youtube = Youtube(url=self.channel_url, meeting_title="")
            if youtube.is_valid_youtube_streams_url():
                soup_str = self.scraper.scrape_html(url=self.channel_url)
                youtube_soup = self.scraper.convert_to_soup(soup_str)
                live_youtube_meetings = youtube.get_live_videos(youtube_soup)

        now = datetime.now(pytz.timezone(self.timezone))
        year = now.year
        month = now.month
        page_soups = []

        current_datetime_utc = datetime.now(tz=pytz.UTC)  # UTC time
        current_date = current_datetime_utc.date()

        # Loop through the current month and remaining months of the year
        while month <= 12:
            scrape_url = self.url.replace("%y%", str(year)).replace("%m%", str(month))
            page_soup = self._get_page_soup(scrape_url)
            page_soups.append(self._get_page_soup(scrape_url))
            month += 1

        for i, soup in enumerate(page_soups):
            div = soup.find("div", class_="content_area calendar_widget clearfix")
            if div is None:
                retry_url = self.url.replace("%y%", str(year)).replace(
                    "%m%", str(i + now.month)
                )
                retry_soup_page = self._get_page_soup(retry_url)
                div = retry_soup_page.find(
                    "div", class_="content_area calendar_widget clearfix"
                )
                if div is None:
                    log.warning(f"Missing a div for {i} month")
                    continue
            saved_month_row = div.find("h2", class_="current_month_title mobile_hide")
            saved_month_date = None
            if saved_month_row:
                saved_month_date = saved_month_row.get_text(strip=True)

            table = div.find(
                "table",
                class_="calendar calendar_grid calendar-mini-grid-grid",
            )

            body = table.find("tbody")

            rows = body.find_all("tr")
            for row in rows:
                columns = row.find_all("td")

                for column in columns:
                    content = column.find("div", class_="calendar_items")
                    saved_day_date = column.get_text(strip=True, separator=" ").split(
                        " "
                    )[0]
                    saved_date = f"{saved_day_date} {saved_month_date}"

                    if content is not None:
                        items = content.find_all("div", class_="calendar_item")
                        for item in items:
                            item_url = item.find("a").get("href")
                            page_link = f"{self.base_url}{item_url}"

                            meeting_name = item.find("a").get_text(strip=True)

                            if "recess" in meeting_name.lower():
                                continue

                            meeting_time = item.find("span").get_text(strip=True)
                            meeting_date_time_web = saved_date + " " + meeting_time
                            meeting_date_time_web = parser.parse(
                                meeting_date_time_web,
                                fuzzy=True,
                                ignoretz=True,
                            )

                            # Convert each datetime object to the specified timezone
                            meeting_date_time_local = pytz.timezone(
                                self.timezone
                            ).localize(meeting_date_time_web)

                            meeting_date_time_utc = meeting_date_time_local.astimezone(
                                pytz.utc
                            )

                            meeting_date_time = meeting_date_time_utc.strftime(
                                "%Y-%m-%dT%H:%M:%S.000Z"
                            )

                            start_of_today_local = datetime(
                                now.year,
                                now.month,
                                now.day,
                                tzinfo=pytz.timezone(self.timezone),
                            )

                            if meeting_date_time_local < start_of_today_local:
                                continue

                            status = "Upcoming"
                            if re.search(
                                r"Cancel(?:led|ed)",
                                meeting_name,
                                re.IGNORECASE,
                            ) or re.search(r"RECESS", meeting_name, re.IGNORECASE):
                                status = "Cancelled"

                            meeting_link = None
                            agenda_link = None

                            self.meetings.append(
                                {
                                    "Meeting name": meeting_name,
                                    "Scheduled time": meeting_date_time,
                                    "Meeting link": meeting_link,
                                    "Agenda link": agenda_link,
                                    "Status": status,
                                }
                            )

                            # # Check for content image and get the image url
                            # log.info(f"Page link: {page_link}")
                            # page_link_content = self.scraper.scrape_html(url=page_link)
                            # page_link_content_soup = self.scraper.convert_to_soup(page_link_content)
                            # # log.info(f"Page link content soup: {page_link_content_soup}")

                            # # find image for ocr
                            # image_area = page_link_content_soup.find("div", class_="detail-content")
                            # log.info(f"Image area: {image_area}")

                            # if image_area:
                            #     image_tag = image_area.find("img")

                            # if image_tag:
                            #     image_url = image_tag.get("src")
                            #     image_url = f"{self.base_url}{image_url}"
                            # else:
                            #     image_url = None
                            # log.info(f"Image url: {image_url}")

                            # # download image for ocr
                            # if image_url:
                            #     # get image from url
                            #     payload = {"url": image_url}
                            #     get_image = self.scraper.fetch_with_scraperapi(payload=payload, raw=True)

                            #     # # save image to local directory
                            #     # with open("image.jpg", "wb") as f:
                            #     #     f.write(get_image.content)

                            #     # run ocr on image
                            #     image_path = os.path.join(os.path.abspath(os.curdir), "image.jpg")
                            #     tesseract_runner = TessaractRunner()
                            #     ocr_result = tesseract_runner.run_tessaract(image_path)
                            #     log.info(f"OCR result: {ocr_result}")

        # Adding Youtube links
        if live_youtube_meetings:
            for youtube_meet in live_youtube_meetings[:]:
                for meeting in self.meetings:
                    meet_title = meeting["Meeting name"]
                    meet_date = parser.parse(meeting["Scheduled time"])
                    if (
                        meet_date.date() == current_date
                        and fuzz.token_set_ratio(
                            youtube_meet["video_title"], meet_title
                        )
                        > 85
                    ):
                        meeting["Status"] = "In Progress"
                        meeting["Meeting link"] = (
                            f"https://www.youtube.com/watch?v={youtube_meet['video_id']}"
                        )
                        live_youtube_meetings.remove(youtube_meet)
                        break

            # if there is only 1 live stream and 1 expected meet today
            in_progress_meetings = [
                meeting
                for meeting in self.meetings
                if meeting["Status"] == "In Progress"
            ]
            if not in_progress_meetings:
                today_meetings = [
                    meeting
                    for meeting in self.meetings
                    if parser.parse(meeting["Scheduled time"]).date() == current_date
                ]
                if len(today_meetings) == 1 and len(live_youtube_meetings) == 1:
                    video_id = live_youtube_meetings[0]["video_id"]
                    meeting_index = self.meetings.index(today_meetings[0])
                    self.meetings[meeting_index]["Status"] = "In Progress"
                    self.meetings[meeting_index][
                        "Meeting link"
                    ] = f"https://www.youtube.com/watch?v={video_id}"

        log.info(f"Meetings: {self.meetings}")
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://citycouncil.atlantaga.gov/other/events/public-meetings/-curm-%m%/-cury-%y%",
        schedule_type="unique_atlanta",
    )
