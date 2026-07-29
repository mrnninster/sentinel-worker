import os
import re
import sys
import pytz
import json
import base64
import logging
import urllib.parse
from dateutil import parser
from fuzzywuzzy import fuzz
from dotenv import load_dotenv
from datetime import datetime, UTC
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.youtube import Youtube
from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test

# Setup the log
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Googlecal:
    def __init__(self):
        self.scraper = HtmlScraper()
        self.meetings = []
        self.url_map = {}
        private_key_b64 = os.getenv("GOOGLE_PRIVATE_KEY")
        private_key = (
            base64.b64decode(private_key_b64).decode("utf-8")
            if private_key_b64
            else None
        )
        self.credential_file = {
            "type": "service_account",
            "project_id": os.getenv("GOOGLE_PROJECT_ID"),
            "private_key_id": os.getenv("GOOGLE_PRIVATE_KEY_ID"),
            "private_key": private_key,
            "client_email": os.getenv("GOOGLE_CLIENT_EMAIL"),
            "client_id": os.getenv("GOOGLE_CLIENT_ID"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": os.getenv("GOOGLE_CLIENT_X509_CERT_URL"),
        }
        self.SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
        self.self_contained_parser = True

    def extract_calendar_id(self, url):
        # Extract calendar_id from the url
        parsed_url = urllib.parse.urlparse(url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        calendar_id = query_params.get("src", [None])[0]
        if not calendar_id:
            raise ValueError("Could not extract calendar_id from url")
        return calendar_id

    def get_meetings(self, calendar_id):
        now = datetime.now(UTC)

        time_min = now.isoformat()

        creds = service_account.Credentials.from_service_account_info(
            self.credential_file, scopes=self.SCOPES
        )

        service = build("calendar", "v3", credentials=creds)

        events = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )

        # Print the events
        for event in events.get("items", []):
            event_summary = event.get("summary", "No summary available")
            event_start = event["start"].get(
                "dateTime", event["start"].get("date", "No start date")
            )
            event_end = event["end"].get(
                "dateTime", event["end"].get("date", "No end date")
            )

            meeting_date_time = datetime.fromisoformat(event_start)
            formatted_date_time = meeting_date_time.astimezone(pytz.utc)
            meeting_date_time = formatted_date_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            meeting_name = event_summary
            if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                status = "Canceled"
            else:
                status = "Upcoming"
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

    def _get_austin_meeting_link(self, meeting_name):
        match = re.search(r"ATXN\s*(\d+)", meeting_name, re.IGNORECASE)
        if match:
            number = match.group(1)
            temp_url = f"https://media.swagit.com/austintx/atxn{number}"

            if temp_url in self.url_map.keys():
                return self.url_map[temp_url]
            else:
                soup_str = self.scraper.scrape_html(url=temp_url)
                soup = self.scraper.convert_to_soup(string=soup_str)
                # log.info(f"soup => {soup}")

                if soup:
                    iframe = soup.find("iframe")
                    # log.info(f"iframe => {iframe}")

                    if iframe:
                        iframe_url = iframe.get("src")
                        # log.info(f"iframe_url => {iframe_url}")

                        if iframe_url:
                            main_page = self.scraper.scrape_html(url=iframe_url)
                            main_soup = self.scraper.convert_to_soup(string=main_page)
                            # log.info(f"main_soup => {main_soup}")

                            if main_soup:
                                # Find the specific script tag containing jwplayer setup
                                scripts = main_soup.find_all("script")
                                # log.info(f"scripts => {scripts}")

                                for script in scripts:
                                    if (
                                        script.string
                                        and "jwplayer(" in script.string
                                        and "playlist:" in script.string
                                    ):
                                        script_content = script.string

                                        # Find the playlist array more precisely
                                        # Look for the pattern: playlist: [{
                                        start_pattern = r"playlist:\s*\["
                                        start_match = re.search(
                                            start_pattern, script_content
                                        )

                                        if start_match:
                                            start_pos = (
                                                start_match.end() - 1
                                            )  # Start from the opening bracket

                                            # Find the matching closing bracket
                                            bracket_count = 0
                                            end_pos = start_pos

                                            for i, char in enumerate(
                                                script_content[start_pos:],
                                                start_pos,
                                            ):
                                                if char == "[":
                                                    bracket_count += 1
                                                elif char == "]":
                                                    bracket_count -= 1
                                                    if bracket_count == 0:
                                                        end_pos = i + 1
                                                        break

                                            playlist_str = script_content[
                                                start_pos:end_pos
                                            ]
                                            js_string = re.sub(
                                                r"(\w+)\s*:(?!//)",
                                                r'"\1":',
                                                playlist_str,
                                            )
                                            js_string = re.sub(
                                                r"'([^']*)'",
                                                r'"\1"',
                                                js_string,
                                            )
                                            js_string = re.sub(
                                                r",(\s*[}\]])",
                                                r"\1",
                                                js_string,
                                            )
                                            js_string = json.loads(js_string)
                                            # print(f"js_string => {js_string}")

                                            playlist_url = js_string[0]["sources"][0][
                                                "file"
                                            ]
                                            # print(f"playlist_url => {playlist_url}")

                                            self.url_map[temp_url] = playlist_url
                                            return playlist_url
        return None
    
    
    def googlecal_table(self, url, timezone="America/New_York"):
        """
        Calls get_meetings and returns the meetings list.
        """
        calendar_id = self.extract_calendar_id(url)
        meetings = self.get_meetings(calendar_id)
        return meetings


    def googlecal_table_austin(self, url, timezone="America/New_York"):
        """
        Calls get_meetings, then updates the 'Meeting link' for Austin TX meetings with ATXN numbers.
        Returns the full meetings list.
        """
        meetings = self.googlecal_table(url, timezone)
        for meeting in meetings:
            meeting_name = meeting.get("Meeting name", "")
            link = self._get_austin_meeting_link(meeting_name)
            cleaned_name = re.split(r"[/-]", meeting_name, maxsplit=1)[0].strip()
            meeting["Meeting name"] = cleaned_name
            if link:
                meeting["Meeting link"] = link
        return meetings

    def googlecal_table_youtube(self, url, timezone="America/New_York"):
        """
        Calls google_table, then updates the 'Meeting link' for YouTube meetings with YouTube links.
        Returns the full meetings list.
        """
        self.meetings = self.googlecal_table(url, timezone)
        self.channel_url = os.getenv("ARG_CHANNEL_URL")

        # Get live youtube meets
        live_youtube_meetings = []
        if self.channel_url:
            # Note: proxy_first=False by default. For Dearborn, we use a permanent stream URL
            # and don't require geo-restricted monitoring, so proxy is not needed here.
            # If geo-restricted monitoring becomes necessary, set proxy_first=True.
            youtube = Youtube(url=self.channel_url, meeting_title="")
            if youtube.is_valid_youtube_streams_url():
                soup_str = self.scraper.scrape_html(url=self.channel_url)
                youtube_soup = self.scraper.convert_to_soup(soup_str)
                live_youtube_meetings = youtube.get_live_videos(youtube_soup)

        # Get current date
        current_date = datetime.now(pytz.utc).date()

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

        # log.info(f"Meetings => {self.meetings}")
        return self.meetings
    
    
    def googlecal_table_dearborn(self, url, timezone="America/New_York"):
        """
        Calls get_meetings and returns the meetings list.
        """
        # Permanent stream url for dearborn
        permanent_stream_url = "https://www.youtube.com/watch?v=_TmvQeW4PxE"
        
        # Get calendar meetings
        meetings = self.googlecal_table_youtube(url, timezone)
        
        # Add permanent stream url to meetings
        for meeting in meetings:
            if meeting["Meeting link"] == "" or meeting["Meeting link"] is None:
                meeting["Meeting link"] = permanent_stream_url
        
        # Current date
        current_date = datetime.now(pytz.utc).date()
        
        # Attach agenda links to meetings
        current_year = current_date.year
        soup_str = self.scraper.scrape_html(url = f"https://www.dearborn.gov/government/city-council/{current_year}-council-meeting-agendas")
        soup = self.scraper.convert_to_soup(string=soup_str)
        agenda_items = soup.find_all("div", class_="swiper-slide p-1 h-full")
        
        agenda_details = []
        for item in agenda_items:
            try:
                agenda_link = None
                agenda_href = item.find("a", href=re.compile(r'\.pdf$', re.IGNORECASE))
                if agenda_href:
                    agenda_link = agenda_href.get("href")
                header = item.find("h4")
                if not header:
                    log.warning("Skipping Dearborn agenda card without h4 header")
                    continue
                header_text = header.get_text(strip=True)
                if "-" not in header_text:
                    log.warning(f"Skipping Dearborn agenda card missing '-' delimiter: {header_text}")
                    continue
                day_str, agenda_title = [segment.strip() for segment in header_text.split("-", 1)]
                day = parser.parse(day_str)
                day = day.date()
                if day >= current_date:
                    agenda_details.append({
                        "day": day,
                        "agenda_title": agenda_title,
                        "agenda_link": agenda_link
                    })
            except Exception as e:
                log.warning(f"Error processing agenda item: {e}")
                continue
        # log.info(f"Agenda details => {agenda_details}")
        
        for meeting in meetings:
            meet_date = parser.parse(meeting["Scheduled time"]).date()
            # Iterate over a shallow copy ([:]) so we can safely remove matched agendas
            # without skipping items during iteration. This allows us to remove each
            # agenda_detail once matched to prevent duplicate matches.
            for agenda_detail in agenda_details[:]:
                if meet_date == agenda_detail["day"] and agenda_detail["agenda_title"].lower() in meeting["Meeting name"].lower():
                    meeting["Agenda link"] = agenda_detail["agenda_link"]
                    agenda_details.remove(agenda_detail)
                    break
                
        # log.info(f"Meetings => {meetings}")
        return meetings


if __name__ == "__main__":
    # Example calendar_id, replace with a real one for actual test
    run_test(url="https://calendar.google.com/calendar/u/0/embed?src=c_58f1cdec3534ef240229955d282a6489ebdf530061b15df9a81fc89d6fdc9a1c@group.calendar.google.com&ctz=America/Detroit", schedule_type="googlecal_table_dearborn", timezone="America/New_York")
    # run_test(url="https://calendar.google.com/calendar/u/0/embed?src=goaa.execadmn@gmail.com&ctz=America/New_York&pli=1", schedule_type="googlecal_table_austin")
