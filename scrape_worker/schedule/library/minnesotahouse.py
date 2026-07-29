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
from urllib.parse import urlparse, urljoin

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.youtube import Youtube
from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Minnesotahouse:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_minnesotahouse(self, url: str, timezone: str) -> list:
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)
        meeting_cards = soup.find_all("div", class_="card pb-3 my-2 ml-2 d-print-block")
        for meeting_card in meeting_cards:
            # defaults
            status = "Upcoming"
            meeting_link = None
            youtube_url = None
            
            row = meeting_card.find("div", class_="row")
            if not row:
                log.warning("Unrecognized meeting card structure, skipping")
                continue
            
            live_tag = row.find("div", class_="col-lg-2")
            if live_tag and "live" in live_tag.get_text(strip=True).lower():
                status = "In Progress"
            
            header_div = meeting_card.find("div", class_="card-header")
            if not header_div:
                log.warning("Missing header div, skipping")
                continue
            
            # Set meeting date and time
            datetime_span = header_div.find("span", class_="text-white").get_text(separator=" ", strip=True)
            datetime_obj = parser.parse(datetime_span, fuzzy=True)
            meeting_date_time_str = datetime.strftime(datetime_obj, TimeFormatter.desired_format())
            utc_time = TimeFormatter(meeting_date_time_str, timezone).get_utc_time(as_datetime=True)
            meet_date_time = utc_time.isoformat().replace("+00:00", "Z")
            
            # Verify meeting is relevant to current date
            current_date = datetime.now(pytz.UTC).date()
            if utc_time.date() < current_date:
                continue
            
            # Get meeting name
            if "bg-house" in header_div.get("class"):
                title_type = "House"
            elif "bg-senate" in header_div.get("class"):
                title_type = "Senate"
            elif "bg-joint" in header_div.get("class"):
                title_type = "Joint"
            else:
                title_type = "Unknown"
                continue
            
            title_h3 = header_div.find("h3").text.strip().replace("\n", "")
            meeting_name = f"{title_type} {title_h3}"
            
            # Set agenda link
            agenda_link = None
            if utc_time.date():
                
                # Check if agenda is provided(house)
                if title_type.lower() == "house":
                    list_elements = meeting_card.find_all("li", class_="list-group-item py-0")
                    for list_element in list_elements:
                        if "agenda" in list_element.get_text(strip=True).lower():
                            agenda_link = list_element.find("a").get("href")
                            break
                    
                # Check if agenda is provided(joint)
                if title_type.lower() == "joint":
                    
                    # Index positions of date and materials in the table
                    date_index = 0
                    stream_index = 3
                    materials_index = 2
                    
                    # Get link elements
                    link_elements = meeting_card.find_all("a")
                    for link_element in link_elements:
                        
                        # Agenda information link
                        if "for more agenda information" in link_element.get_text(strip=True).lower():
                            page_link = link_element.get("href")
                            
                            url_parts = urlparse(page_link)
                            page_link = url_parts.scheme + "://" + url_parts.netloc + url_parts.path
                            
                            # Get page soup
                            page_soup = self.scraper.scrape_html(url=page_link)
                            page_soup = self.scraper.convert_to_soup(string=page_soup)
                            
                            # Get table body
                            tbody = page_soup.find("tbody")
                            body_rows = tbody.find_all("tr")
                            
                            # Iterate through body rows
                            found_link = False
                            for body_row in body_rows:
                                if not found_link:
                                    columns = body_row.find_all("td")
                                
                                    # verify date and set agenda link
                                    date = columns[date_index]
                                    date = date.get_text(" ", strip=True)
                                    verify_date = parser.parse(date, fuzzy=True)
                                    
                                    if verify_date.date() == utc_time.date():
                                        # get stream link
                                        meeting_link = columns[stream_index].find("a").get("href")
                                        
                                        # check if link is relative path
                                        if meeting_link.startswith("/"):
                                            meeting_link = urljoin(page_link, meeting_link)
                                        
                                        # get agenda link
                                        material_contents = columns[materials_index].find_all("li")
                                        for material_content in material_contents:
                                            if "agenda" in material_content.get_text(strip=True).lower():
                                                relative_agenda_link = material_content.find("a").get("href")
                                                agenda_link = urljoin(page_link, relative_agenda_link)
                                                found_link = True
                                                break
                        
                        # Public viewing information
                        elif not meeting_link:
                            paragraphs = meeting_card.find_all("p")
                            for paragraph in paragraphs:
                                if "public viewing information" in paragraph.get_text(strip=True).lower():
                                    # Find youtube channel
                                    links = paragraph.find_all("a")
                                    for link in links:
                                        if "youtube" in link.get_text(strip=True).lower():
                                            youtube_url = link.get("href")

                                            # Create Youtube instance and get the @handle format URL for channel links
                                            youtube = Youtube(url=youtube_url, meeting_title="")
                                            handle_url = youtube.get_channel_handle_url()
                                            log.info(f"Original Youtube url => {youtube_url}")
                                            log.info(f"Handle url => {handle_url}")

                                            # check is direct youtube video link
                                            if "watch?v=" in youtube_url or youtube_url.endswith("/live"):
                                                meeting_link = youtube_url
                                                status = "In progress"

                                            # Handle youtube channel link
                                            else:
                                                live_youtube_meetings = []
                                                if handle_url:
                                                    # Update youtube instance to use handle URL for validation and scraping
                                                    youtube = Youtube(url=handle_url, meeting_title="")
                                                    if youtube.is_valid_youtube_streams_url():
                                                        soup_str = self.scraper.scrape_html(url=handle_url)
                                                        youtube_soup = self.scraper.convert_to_soup(soup_str)
                                                        live_youtube_meetings = youtube.get_live_videos(youtube_soup)

                                                        # Get current time in UTC
                                                        current_date = datetime.now(pytz.UTC).date()

                                                        # Adding Youtube links
                                                        if live_youtube_meetings:
                                                            for youtube_meet in live_youtube_meetings[:]:
                                                                if (
                                                                    utc_time.date() == current_date
                                                                    and fuzz.token_set_ratio(
                                                                        youtube_meet["video_title"], meeting_name
                                                                    )
                                                                    > 85
                                                                ):
                                                                    status = "In progress" # fallback for youtube channel specific logic
                                                                    meeting_link = f"https://www.youtube.com/watch?v={youtube_meet['video_id']}"
                                                                    live_youtube_meetings.remove(youtube_meet)
                                                                    break

                                                            # if there is only 1 live stream and meeting should be in progress
                                                            if status.lower() == "in progress" and len(live_youtube_meetings) == 1:
                                                                video_id = live_youtube_meetings[0]["video_id"]
                                                                meeting_link = f"https://www.youtube.com/watch?v={video_id}"                   

            self.meetings.append({
                "Meeting name": meeting_name,
                "Scheduled time": meet_date_time,
                "Agenda link": agenda_link,
                "Meeting link": meeting_link,
                "Status": status,
            })
        return self.meetings
            

            
if __name__ == "__main__":

    url = "https://www.house.mn.gov/schedules"
    timezone = "America/Chicago"
    schedule_type = "unique_minnesotahouse"
    run_test(
        url=url,
        timezone=timezone,
        schedule_type=schedule_type,
    )