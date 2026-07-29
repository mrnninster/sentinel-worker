# nebraskalegislature.py
import os
import sys
import re
import pytz
import requests
from datetime import datetime
from urllib.parse import urljoin
from dateutil import parser
from bs4 import BeautifulSoup

# Add project path for local imports
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from logging_config import get_dedicated_debug_logger

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test


class Nebraskalegislature:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.log = get_dedicated_debug_logger(__name__)

    def nebraskalegislature_table(self, url, timezone="America/Chicago"):
        """
        Parses the "streaming now" and "coming soon" sections from the given URL
        and returns a list of meetings with structured information.

        Args:
            url (str): The URL of the Nebraska Legislature streaming page.
            timezone (str): The timezone for the scheduled times. Defaults to "America/Chicago".

        Returns:
            list: A list of dictionaries containing meeting details.
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
            " Chrome/113.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
        }

        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
        except requests.RequestException as e:
            print(f"Failed to fetch the URL: {e}")
            return self.meetings

        soup = BeautifulSoup(response.text, "html.parser")

        # Define sections to parse
        sections = {
            "streaming_now": "Streaming Now",
            "coming_soon": "Coming Soon",
        }

        for section_key, section_title in sections.items():
            # Find the section header
            header = soup.find("h2", string=re.compile(section_title, re.IGNORECASE))
            if not header:
                print(f"Section '{section_title}' not found.")
                continue

            # The parent container of the meetings
            container = header.find_parent(
                "div", class_="content-teaser-group__container"
            )
            if not container:
                print(f"Container for section '{section_title}' not found.")
                continue

            # Find all meeting items within the container
            attr_class = "content-teaser__container"
            if section_title == "Coming Soon":
                attr_class += "--horizontal"
            meeting_items = container.find_all("a", class_=attr_class)

            for item in meeting_items:
                # Extract meeting name
                title_tag = item.find("div", class_="content-teaser__title")
                meeting_name = (
                    title_tag.get_text(strip=True) if title_tag else "No Title"
                )

                # Extract scheduled time
                attr_taxonomy_class = "content-teaser__taxonomy"
                if section_title == "Coming Soon":
                    attr_taxonomy_class += "--horizontal"
                taxonomy_tag = item.find("div", class_=attr_taxonomy_class)
                taxonomy_text = (
                    taxonomy_tag.get_text(strip=True) if taxonomy_tag else ""
                )
                # Example taxonomy_text: "Government | 1/23, 12:00PM"

                # Extract category and filter for Government only
                category_match = re.search(r"^([^|]+)", taxonomy_text)
                category = category_match.group(1).strip() if category_match else ""
                if category != "Government":
                    self.log.debug(
                        f"Skipping non-Government meeting '{meeting_name}' "
                        f"(category: '{category if category else 'empty/unknown'}')"
                    )
                    continue  # Skip non-Government broadcasts

                # Extract date/time portion after the pipe separator
                # Example: "Government | 1/23, 12:00PM" or "Government | 12:00PM"
                date_time_match = re.search(r"\|\s*(.+)", taxonomy_text)
                if not date_time_match:
                    self.log.debug(
                        f"Could not extract date/time from taxonomy text "
                        f"'{taxonomy_text}' for '{meeting_name}'."
                    )
                    isotime = None
                else:
                    date_time_str = date_time_match.group(1).strip()

                    # Use dateutil.parser with fuzzy=True for flexible parsing
                    local_tz = pytz.timezone(timezone)
                    now = datetime.now(local_tz)

                    # For "Streaming Now", if no date found, prepend today's date
                    if section_title == "Streaming Now":
                        has_date = re.search(r"\d{1,2}/\d{1,2}", date_time_str)
                        if not has_date:
                            # No date found, add today's date
                            month_day = f"{now.month}/{now.day}"
                            date_time_str = f"{month_day}, {date_time_str}"

                    try:
                        # Parse with fuzzy=True to handle various date formats
                        # Use default=now to provide context for missing date components
                        parsed_dt = parser.parse(
                            date_time_str,
                            fuzzy=True,
                            default=now.replace(second=0, microsecond=0, tzinfo=None),
                            ignoretz=True,
                        )

                        # Localize to the specified timezone (parsed datetime is naive)
                        parsed_dt = local_tz.localize(parsed_dt)

                        # Convert to UTC
                        utc_dt = parsed_dt.astimezone(pytz.utc)
                        isotime = utc_dt.isoformat().replace("+00:00", "Z")

                    except (ValueError, TypeError, OverflowError) as e:
                        self.log.warning(
                            f"Error parsing date/time '{date_time_str}' "
                            f"for '{meeting_name}': {e}"
                        )
                        isotime = None

                # Extract meeting link
                href = item.get("href")
                meeting_link = urljoin(url, href) if href else None

                # Determine status
                status = "In progress" if section_key == "streaming_now" else "Upcoming"

                # Extract agenda link if available (assuming it's part of the href or another attribute)
                # This part may need to be adjusted based on the actual HTML structure
                agenda_link = None  # Placeholder as agenda link extraction is not clear from the sample HTML

                # Append the meeting information
                self.meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": isotime,
                        "Meeting link": meeting_link,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }
                )

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://nebraskapublicmedia.org/en/watch/live/",
        schedule_type="nebraskalegislature_table",
        timezone="America/Chicago",
        get_full_archive_flag=False,
    )
