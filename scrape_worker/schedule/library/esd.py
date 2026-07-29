import pytz
import re
from datetime import datetime
from logging_config import LOG_LEVEL
from logging_config import get_dedicated_debug_logger
from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test

log = get_dedicated_debug_logger(__name__)
log.setLevel(LOG_LEVEL)


class Esd:
    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.base_url = "https://www.highfivemedia.org"
        self.self_contained_parser = True

    def unique_esd(self, url: str, timezone: str = "America/Denver"):
        """
        Scrapes Eagle County School District BOE meeting videos from High Five Media page.

        Args:
            url: The URL of the High Five Media page for Eagle County School District
            timezone: Timezone for the meetings (default: America/Denver)

        Returns:
            List of meeting dictionaries with video links
        """
        local_timezone = pytz.timezone(timezone)
        current_date = datetime.now(local_timezone).date()

        # Scrape the page
        page = self.scraper.scrape_html(url=url, render=True)
        soup = self.scraper.convert_to_soup(page)

        # Find all BOE Meeting video entries from the playlist structure
        boe_meetings = []

        # Find all playlist collection items (each represents a meeting)
        playlist_div = soup.find("div", id="w-slider-mask-0")
        if not playlist_div:
            log.warning(
                f"Could not find playlist div with id 'w-slider-mask-0' on page: {url}"
            )
            return self.meetings

        playlist_items = playlist_div.find_all(
            "div", class_="program-slide-playlist-landscape w-slide"
        )

        for item in playlist_items:
            # Extract date from playlist-episode-text div
            episode_text_div = item.find("div", class_="playlist-episode-text")
            if not episode_text_div:
                continue

            meeting_name = episode_text_div.get_text(strip=True)

            # Extract date from text (format: "BOE Meeting 11/12/2025")
            date_match = None
            date_patterns = [
                r"(\d{1,2}/\d{1,2}/\d{4})",
                r"(\d{1,2}-\d{1,2}-\d{4})",
                r"(\d{1,2}\.\d{1,2}\.\d{4})",
            ]

            for pattern in date_patterns:
                match = re.search(pattern, meeting_name)
                if match:
                    date_match = match.group(1)
                    break

            if not date_match:
                log.warning(f"Could not extract date from '{meeting_name}'")
                continue

            # Find the video link in the playlist-button-wrapper
            button_wrapper = item.find("div", class_="playlist-button-wrapper")
            if not button_wrapper:
                log.warning(f"Could not find button wrapper for '{meeting_name}'")
                continue

            video_link_tag = button_wrapper.find("a", class_="program-playlist-button")
            if not video_link_tag or not video_link_tag.get("href"):
                log.warning(f"Could not find video link for '{meeting_name}'")
                continue

            href = video_link_tag.get("href", "")

            try:
                # Parse the date
                if "/" in date_match:
                    meeting_date = datetime.strptime(date_match, "%m/%d/%Y")
                elif "-" in date_match:
                    meeting_date = datetime.strptime(date_match, "%m-%d-%Y")
                else:
                    meeting_date = datetime.strptime(date_match, "%m.%d.%Y")

                # Skip past meetings
                if meeting_date.date() < current_date:
                    continue

                # Construct video link
                if href.startswith("http"):
                    video_link = href
                elif href.startswith("/"):
                    video_link = f"{self.base_url}{href}"
                else:
                    video_link = f"{self.base_url}/{href}"

                # Default meeting time is 5:30 PM (as mentioned on the page)
                meeting_datetime = meeting_date.replace(hour=17, minute=30)
                meeting_datetime_local = local_timezone.localize(meeting_datetime)
                meeting_datetime_utc = meeting_datetime_local.astimezone(pytz.utc)
                utc_time = meeting_datetime_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

                boe_meetings.append(
                    {
                        "date": meeting_date.date(),
                        "name": meeting_name,
                        "link": video_link,
                        "utc_time": utc_time,
                    }
                )
            except Exception as e:
                log.warning(
                    f"Could not parse date '{date_match}' from '{meeting_name}': {e}"
                )
                continue

        # Convert to meeting format
        for boe_meeting in boe_meetings:
            # Extract the actual meeting link from the video page
            video_link = boe_meeting["link"]
            actual_meeting_link = self.extract_meeting_link_from_video_page(video_link)

            # Use the extracted link if available, otherwise fall back to video_link
            meeting_link = actual_meeting_link if actual_meeting_link else video_link

            meeting = {
                "Meeting name": boe_meeting["name"],
                "Scheduled time": boe_meeting["utc_time"],
                "Meeting link": meeting_link,
                "Agenda link": None,
                "Status": "Upcoming",
            }

            # Avoid duplicates
            if not self._is_duplicate(
                meeting["Meeting name"], meeting["Scheduled time"]
            ):
                self.meetings.append(meeting)

        log.debug(f"Meetings: {self.meetings}")
        return self.meetings

    def extract_meeting_link_from_video_page(self, video_link: str) -> str:
        """
        Scrapes a video page and extracts the actual meeting link from the telvue player iframe.

        Args:
            video_link: The URL of the video page to scrape

        Returns:
            The actual meeting link from the iframe src, or None if not found
        """
        try:
            # Scrape the video page
            page = self.scraper.scrape_html(url=video_link, render=True)
            soup = self.scraper.convert_to_soup(page)

            # Find the telvue player container div
            # The div has multiple classes, so we search for the main class
            player_container = soup.find("div", class_="telvue_player_container")

            if not player_container:
                log.warning(
                    f"Could not find telvue_player_container div on page: {video_link}"
                )
                return None

            # Find the iframe within the container
            iframe = player_container.find("iframe", class_="telvue-player-ratio")

            if not iframe:
                log.warning(
                    f"Could not find iframe in telvue_player_container on page: {video_link}"
                )
                return None

            # Extract the src attribute
            meeting_link = iframe.get("src")

            if not meeting_link:
                log.warning(f"Iframe src attribute not found on page: {video_link}")
                return None

            log.info(
                f"Extracted meeting link: {meeting_link} from video page: {video_link}"
            )
            return meeting_link

        except Exception as e:
            log.warning(
                f"Error extracting meeting link from video page '{video_link}': {e}"
            )
            return None

    def _is_duplicate(self, meeting_name, scheduled_time):
        """Check if a meeting already exists."""
        return any(
            m.get("Scheduled time") == scheduled_time
            and m.get("Meeting name") == meeting_name
            for m in self.meetings
        )


if __name__ == "__main__":
    run_test(
        url="https://www.highfivemedia.org/watch/eagle-county-school-district",
        schedule_type="unique_esd",
        timezone="America/Denver",
    )
