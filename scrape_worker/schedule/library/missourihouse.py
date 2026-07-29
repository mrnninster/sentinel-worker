import re
import os
import sys
import logging
from typing import List, Dict, Any
from dateutil import parser
from datetime import datetime
from dotenv import load_dotenv
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test
from utils.scrape_html import HtmlScraper, ReturnType

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Missourihouse:

    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        self.media_sources = [
            {
                "name": "house hearing room 1",
                "url": "https://sg001-harmony.sliq.net/00325/Harmony/en/PowerBrowser/PowerBrowserV2/20191211/-1/14058"
            },
            {
                "name": "house hearing room 3",
                "url": "https://sg001-harmony.sliq.net/00325/Harmony/en/PowerBrowser/PowerBrowserV2/20191211/-1/14059"
            },
            {
                "name": "house hearing room 5",
                "url": "https://sg001-harmony.sliq.net/00325/Harmony/en/PowerBrowser/PowerBrowserV2/20191211/-1/14060"
            },
            {
                "name": "house hearing room 6",
                "url": "https://sg001-harmony.sliq.net/00325/Harmony/en/PowerBrowser/PowerBrowserV2/20191211/-1/14061"
            },
            {
                "name": "house hearing room 7",
                "url": "https://sg001-harmony.sliq.net/00325/Harmony/en/PowerBrowser/PowerBrowserV2/20191211/-1/14062"
            },
            {
                "name": "Joint hearing room 117",
                "url": "https://sg001-harmony.sliq.net/00325/Harmony/en/PowerBrowser/PowerBrowserV2/20191211/-1/14063"
            }
        ]

        
    def unique_missourihouse(self, url: str, timezone: str) -> List[Dict[str, Any]]:
        # Fetch HTML (rendered for JavaScript-heavy content)
        html_content = self.scraper.scrape_html(url=url, render="true")
        soup = self.scraper.convert_to_soup(html_content)

        self.meetings = []
        domain = urlparse(url).scheme + "://" + urlparse(url).netloc
        day_containers = soup.find_all("section", class_="dayContainerDIV")
        
        for day in day_containers:
            date_str = None
            date_div = day.select_one('div.titleBar h1')

            if date_div:
                text_content = date_div.get_text()
                date_match = re.search(r'\b(\d{1,2}/\d{1,2}/\d{4})\b', text_content)
                if date_match:
                    date_str = date_match.group(1)
                
            day_meetings = day.find_all("div", class_="hearingStyle")
            for day_meeting in day_meetings:
                status = None
                time_str = None
                room_str = None
                agenda_link = None
                meeting_link = None
                
                # Get meeting title
                h2_tag = day_meeting.find("h2")
                if h2_tag:
                    title_text = h2_tag.get_text(strip=True).lower()
                    # Split on dash and take first part, or use whole text if no dash
                    title_parts = title_text.split("-", 1)
                    title = title_parts[0].strip() if title_parts else title_text
                else:
                    continue  # Skip this meeting if no title found
                
                # Get meeting status
                status_options = ["in progress", "recess"]
                live_status = [bool(stream_status) for stream_status in status_options if stream_status in title_text]
                if any(live_status):
                    status = "In progress"
                else:
                    status = "Upcoming"
                
                # Get meeting time and room
                article = day_meeting.find("article")

                if article:
                    # Find a tag
                    a_tag = article.find("a")
                    if a_tag:
                        a_tag_href = a_tag.get("href")
                        if a_tag_href:
                            notice_id = a_tag_href.lower().split("noticedetails")[1] 
                    
                    # Set agenda_link
                    agenda_link = urljoin(domain,f"AllHearings.aspx?nid={notice_id}")
                    
                    # Find the <br> tag
                    br_tag = article.find('br')

                    if br_tag:
                        # Extract time - text immediately after <br>
                        if br_tag.next_sibling and isinstance(br_tag.next_sibling, str):
                            time_str = br_tag.next_sibling.strip()
                            
                            datetime_str = f"{time_str} {date_str}"
                            meeting_date_time = parser.parse(datetime_str, fuzzy=True)
                            meeting_date_time = datetime.strftime(meeting_date_time, TimeFormatter.desired_format())
                            utc_time = TimeFormatter(meeting_date_time, timezone).get_utc_time(as_datetime=True)
                            meeting_date_time = utc_time.isoformat().replace("+00:00", "Z")

                        # Extract room - find the text node containing "hearing room" before <br>
                        for sibling in br_tag.previous_siblings:
                            if isinstance(sibling, str) and 'hearing room' in sibling.lower():
                                room_str = sibling.strip()
                                break

                        # Match room to media source for meeting link
                        if room_str:
                            for media_source in self.media_sources:
                                if media_source["name"] in room_str.lower():
                                    meeting_link = media_source["url"]
                                    break
                
                self.meetings.append(
                    {
                        "Meeting name": title,
                        "Scheduled time": meeting_date_time,
                        "Meeting link": meeting_link,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }
                )
        return self.meetings
        

if __name__ == "__main__":
    url = "https://house.mo.gov/AllHearings.aspx"
    timezone = "America/Chicago"
    schedule_type = "unique_missourihouse"
    run_test(url=url, timezone=timezone, schedule_type=schedule_type)