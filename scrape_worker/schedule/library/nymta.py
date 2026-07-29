import os
import re
import sys
import pytz
from urllib.parse import urlparse
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Nymta:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_nymta(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        year = now.year

        # Find the h3 tag for the current year (2024)
        year_h3 = soup.find("h3", string=lambda text: text and str(year) in text)
        # If the h3 tag is found, find the ul and scrape the meetings
        if year_h3:
            ul = year_h3.find_next_sibling("ul")
            if ul:
                for li in ul.find_all("li"):
                    # Check if the li has an 'a' tag with an 'href' attribute
                    a_tag = li.find("a")
                    if a_tag and a_tag.get("href"):
                        # Save the href value
                        href = a_tag.get("href")
                        link = domain + href

                        soup_new = self.scraper.fetch_with_bs(url=link)
                        soup_new = self.scraper.convert_to_soup(string=soup_new)
                        # Find all divs with class name 'field'
                        field_divs = soup_new.find_all("div", class_="field")
                        agenda_p = soup_new.find("div", class_="paragraph")
                        agenda_tag = (
                            agenda_p.find_all("div", class_="field--item")
                            if agenda_p
                            else None
                        )
                        agenda_items_list = []

                        # Iterate through agenda items
                        if agenda_tag is not None:
                            for item in agenda_tag:
                                a_link = item.find("a")[
                                    "href"
                                ]  # Extract href attribute
                                agenda_link = domain + a_link
                                agenda_name = (
                                    item.find(class_="link-title").get_text().strip()
                                )  # Extract agenda name

                                # Create a dictionary with a_link and agenda_name as a pair
                                agenda_item_dict = {
                                    "agenda_name": agenda_name,
                                    "a_link": agenda_link,
                                }

                                # Add the dictionary to the list of agenda items
                                agenda_items_list.append(agenda_item_dict)

                        for div in field_divs:
                            # Find h3 and ul within the div
                            h3 = div.find("h3")
                            ul = div.find("ul")

                            if h3 and ul:
                                # Split the h3 text at ':'
                                saved_date, saved_name = h3.text.split(":")

                                # Process each li within the ul
                                # Process each li within the ul
                                for li in ul.find("li"):

                                    # Use regex to match the time format and the rest of the text
                                    time_pattern = r"\b\d{1,2}:\d{2} [ap]\.m\."
                                    time_match = re.search(time_pattern, li.text)
                                    if time_match:
                                        time_part = time_match.group(0)
                                        # Remove the time part from the original text
                                        name_part = li.text.replace(
                                            time_part, ""
                                        ).strip()
                                        meeting_name = saved_name
                                        # Combine the dates and names
                                        meeting_date_time_web = (
                                            saved_date + " " + time_part.strip()
                                        )
                                        meeting_date_time_web = (
                                            str(year)
                                            + " "
                                            + meeting_date_time_web.replace(
                                                ".", ""
                                            ).strip()
                                        )

                                        # Parse the original time string
                                        meeting_date_time_web = datetime.strptime(
                                            meeting_date_time_web,
                                            "%Y %A, %B %d %I:%M %p",
                                        )
                                        meeting_date_time_local = timezone.localize(
                                            meeting_date_time_web
                                        )

                                        # Convert the original time to UTC
                                        meeting_date_time_utc = (
                                            meeting_date_time_local.astimezone(pytz.utc)
                                        )

                                        # Format the UTC time in the desired format
                                        meeting_date_time = (
                                            meeting_date_time_utc.strftime(
                                                "%Y-%m-%dT%H:%M:%S.000Z"
                                            )
                                        )

                                        # Correctly split the meeting_name at the colon to get the part after the colon
                                        meeting_name_parts = meeting_name.split(":")
                                        if len(meeting_name_parts) > 1:
                                            meeting_name_clean = meeting_name_parts[
                                                1
                                            ].strip()  # Take the part after the colon
                                        else:
                                            meeting_name_clean = (
                                                meeting_name.strip()
                                            )  # Fallback to the whole meeting_name if no colon is found

                                        # Initialize a variable to hold the matching agenda link
                                        agenda_link = None

                                        # Iterate through agenda_items_list to find a match
                                        for agenda_item in agenda_items_list:
                                            agenda_name = agenda_item["agenda_name"]
                                            # Split the agenda_name into words
                                            agenda_name_words = set(
                                                agenda_name.lower().split()
                                            )
                                            # Split the meeting_name into words
                                            meeting_name_words = set(
                                                meeting_name_clean.lower().split()
                                            )
                                            # Check for common words
                                            common_words = (
                                                agenda_name_words.intersection(
                                                    meeting_name_words
                                                )
                                            )
                                            if common_words:
                                                # If there's a common word, assign the agenda_link
                                                agenda_link = agenda_item["a_link"]
                                                break  # Stop the loop once a match is found

                                        # Determine the status based on the conditions
                                        if re.search(
                                            r"Cancel(?:led|ed)",
                                            meeting_name,
                                            re.IGNORECASE,
                                        ):
                                            status = "Cancelled"
                                        else:
                                            status = "Upcoming"
                                        meeting_link = None
                                        if now.date() > meeting_date_time_local.date():
                                            continue
                                        self.meetings.append(
                                            {
                                                "Meeting name": meeting_name,
                                                "Scheduled time": meeting_date_time,
                                                "Meeting link": meeting_link,
                                                "Agenda link": agenda_link,
                                                "Status": status,
                                            }
                                        )
            # Convert each dictionary to a tuple of its values to make it hashable
            unique_meetings = set(tuple(meeting.values()) for meeting in self.meetings)

            # Convert the set back to a list of dictionaries, keeping only one copy of each unique meeting
            self.meetings = [
                dict(zip(self.meetings[0].keys(), meeting))
                for meeting in unique_meetings
            ]
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://new.mta.info/transparency/board-and-committee-meetings",
        schedule_type="unique_nymta",
        timezone="America/New_York",
        get_full_archive_flag=False,
    )
