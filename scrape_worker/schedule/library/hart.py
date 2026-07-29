import pytz
import re
from dateutil import parser
from datetime import datetime
from logging_config import LOG_LEVEL
from logging_config import get_dedicated_debug_logger
from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test

log = get_dedicated_debug_logger(__name__)
log.setLevel(LOG_LEVEL)


class Hart:
    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.base_url = "https://www.gohart.org"
        self.meeting_link = "https://www.youtube.com/@harttransit/streams"
        self.self_contained_parser = True

    def unique_hart(self, url, timezone="America/New_York"):
        """
        Scrapes HART public meetings page to extract meeting information.

        Args:
            url: The URL of the HART public meetings page
            timezone: Timezone for the meetings (default: America/New_York for Tampa, FL)

        Returns:
            List of meeting dictionaries
        """

        # Scrape the main page
        page = self.scraper.scrape_html(url=url, render=True)
        soup = self.scraper.convert_to_soup(page)

        # Extract agenda links first (before creating meetings)
        agendas = self._extract_agenda_links(soup)

        # Primary extraction: Look for HART meeting boxes structure
        container = soup.find("div", id="hart_meeting-boxes")
        if container:
            local_timezone = pytz.timezone(timezone)
            current_date = datetime.now(local_timezone).date()

            # Find all meeting boxes
            meeting_boxes = container.find_all("div", class_="hart_meeting-box")

            for box in meeting_boxes:
                try:
                    # Extract date from info-title-text
                    date_span = box.find("span", class_="info-title-text")
                    if not date_span:
                        continue

                    date_text = date_span.get_text(strip=True)
                    # Parse date like "MONDAY, NOVEMBER 17, 2025"
                    try:
                        meeting_datetime = parser.parse(date_text, fuzzy=True)
                        meeting_date = meeting_datetime.date()
                        if meeting_date < current_date:
                            continue
                    except Exception as e:
                        log.warning(f"Could not parse date: {date_text} - {e}")
                        continue

                    # Extract meeting title
                    title_div = box.find("div", class_="hart_mb-title")
                    if not title_div:
                        log.warning(
                            f"Could not find meeting title for date: {date_text}"
                        )
                        continue
                    meeting_name = title_div.get_text(strip=True)

                    # Extract time
                    time_div = box.find("div", class_="hart_mb-time")
                    if not time_div:
                        log.warning(
                            f"Could not find meeting time for '{meeting_name}' on '{date_text}'"
                        )
                        continue
                    time_text = time_div.get_text(strip=True)

                    # Check if cancelled (consolidated check)
                    if self._is_meeting_cancelled(meeting_name, time_div, time_text):
                        continue

                    # Parse time - handle various formats
                    meeting_time_str = None
                    if time_text:
                        # Clean up time text (remove "or immediately following..." etc.)
                        time_text = re.sub(
                            r"\s+or\s+.*$", "", time_text, flags=re.IGNORECASE
                        )
                        time_text = time_text.strip()
                        # Try to extract time pattern like "9:30 am" or "1:30pm - 3:00pm"
                        time_match = re.search(
                            r"(\d{1,2}:\d{2}\s*(?:am|pm|AM|PM))",
                            time_text,
                            re.IGNORECASE,
                        )
                        if time_match:
                            meeting_time_str = time_match.group(1)
                        else:
                            # Try format without space
                            time_match = re.search(
                                r"(\d{1,2}:\d{2}(?:am|pm|AM|PM))",
                                time_text,
                                re.IGNORECASE,
                            )
                            if time_match:
                                meeting_time_str = time_match.group(1)

                    # Construct datetime with time
                    if meeting_time_str:
                        try:
                            meeting_datetime_str = f"{date_text} {meeting_time_str}"
                            meeting_datetime_with_time = parser.parse(
                                meeting_datetime_str, fuzzy=True, ignoretz=True
                            )
                        except Exception as e:
                            log.warning(
                                f"Failed to parse meeting time for '{meeting_name}' on '{date_text}': {e}"
                            )
                            continue
                    else:
                        log.warning(
                            f"No meeting time found for '{meeting_name}' on '{date_text}'"
                        )
                        continue

                    # Localize to timezone, then convert to UTC
                    meeting_datetime_local = local_timezone.localize(
                        meeting_datetime_with_time
                    )
                    meeting_datetime_utc = meeting_datetime_local.astimezone(pytz.utc)
                    utc_time = meeting_datetime_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

                    # Match agenda link before creating meeting dictionary
                    meeting_name_normalized = self._normalize_meeting_name(meeting_name)
                    agenda_link = self._find_matching_agenda(
                        agendas, meeting_date, meeting_name_normalized
                    )

                    status = "Upcoming"

                    meeting = {
                        "Meeting name": meeting_name,
                        "Scheduled time": utc_time,
                        "Meeting link": self.meeting_link,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }

                    # Avoid duplicates
                    if not self._is_duplicate(meeting_name, utc_time):
                        self.meetings.append(meeting)

                except Exception as e:
                    log.warning(f"Error extracting meeting from box: {e}")
                    continue

        log.info(f"Found {len(self.meetings)} meetings")
        return self.meetings

    def _extract_agenda_links(self, soup):
        """
        Extract agenda links from hart_board-docs section.
        Returns a list of agenda dictionaries with date, name, and link.
        """
        agendas = []

        # Find the board docs container
        board_docs = soup.find("div", id="hart_board-docs")
        if not board_docs:
            log.debug("hart_board-docs container not found")
            return agendas

        # Find all list items containing agenda information
        agenda_items = board_docs.find_all("li")
        if not agenda_items:
            log.debug("No agenda items found in hart_board-docs")
            return agendas

        for item in agenda_items:
            try:
                # Extract agenda date from link text
                date_link = item.find("span", class_="hart_bp-link")
                if not date_link:
                    continue

                date_a_tag = date_link.find("a")
                if not date_a_tag:
                    continue

                agenda_date_text = date_a_tag.get_text(strip=True)

                # Extract agenda link
                agenda_href = date_a_tag.get("href", "")
                if not agenda_href:
                    continue

                # Construct full agenda URL
                if agenda_href.startswith("http"):
                    agenda_link = agenda_href
                else:
                    agenda_link = (
                        f"{self.base_url}{agenda_href}"
                        if agenda_href.startswith("/")
                        else f"{self.base_url}/{agenda_href}"
                    )

                # Extract agenda name
                name_span = item.find("span", class_="hart_bp-title")
                agenda_name = name_span.get_text(strip=True) if name_span else ""

                # Parse agenda date
                try:
                    # Parse date like "November 3, 2025" or "October 20, 2025"
                    agenda_date = parser.parse(agenda_date_text, fuzzy=True)
                    agendas.append(
                        {
                            "date": agenda_date.date(),
                            "name": agenda_name,
                            "name_normalized": self._normalize_meeting_name(
                                agenda_name
                            ),
                            "link": agenda_link,
                        }
                    )
                except Exception as e:
                    log.warning(
                        f"Could not parse agenda date '{agenda_date_text}': {e}"
                    )
                    continue

            except Exception as e:
                log.warning(f"Error extracting agenda item: {e}")
                continue
        return agendas

    def _find_matching_agenda(self, agendas, meeting_date, meeting_name_normalized):
        """
        Find matching agenda for a meeting by date and name similarity.
        Returns the agenda link if a good match is found, None otherwise.
        """
        if not agendas or not meeting_date or not meeting_name_normalized:
            return None

        best_match = None
        best_score = 0

        for agenda in agendas:
            # Check date match (exact match required)
            if agenda["date"] != meeting_date:
                continue

            # Calculate name similarity score
            agenda_name_normalized = agenda["name_normalized"]
            score = self._calculate_name_similarity(
                meeting_name_normalized, agenda_name_normalized
            )

            if score > best_score:
                best_score = score
                best_match = agenda

        # If we found a good match (score > 0.5), return the agenda link
        if best_match and best_score > 0.5:
            log.info(
                f"Matched agenda for meeting on {meeting_date}: {best_match['link']}"
            )
            return best_match["link"]

        return None

    def _is_duplicate(self, meeting_name, scheduled_time):
        """
        Check if a meeting with the same name and scheduled time already exists.
        Returns True if duplicate, False otherwise.
        """
        return any(
            m.get("Scheduled time") == scheduled_time
            and m.get("Meeting name") == meeting_name
            for m in self.meetings
        )

    def _is_meeting_cancelled(self, meeting_name, time_div, time_text):
        """
        Check if a meeting is cancelled by examining meeting name, time text, and styling.
        Returns True if cancelled, False otherwise.
        """
        # Check meeting name for cancellation keywords
        if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
            return True

        # Check time text and styling
        if time_div:
            style = time_div.get("style", "")
            if "CANCELLED" in time_text.upper() or "color: red" in style.lower():
                return True

        return False

    def _normalize_meeting_name(self, name):
        """
        Normalize meeting name for comparison by removing common words
        and converting to lowercase.
        """
        if not name:
            return ""

        # Convert to lowercase
        normalized = name.lower()

        # Remove common words that might differ
        common_words = [
            "regular",
            "meeting",
            "committee",
            "board",
            "directors",
            "of",
            "the",
            "and",
            "&",
            "ad",
            "hoc",
        ]

        words = normalized.split()
        filtered_words = [w for w in words if w not in common_words]

        return " ".join(filtered_words)

    def _calculate_name_similarity(self, name1, name2):
        """
        Calculate similarity score between two normalized meeting names.
        Returns a score between 0 and 1.
        """
        if not name1 or not name2:
            return 0.0

        # Exact match
        if name1 == name2:
            return 1.0

        # Check if one contains the other
        if name1 in name2 or name2 in name1:
            return 0.8

        # Check word overlap
        words1 = set(name1.split())
        words2 = set(name2.split())

        if not words1 or not words2:
            return 0.0

        # Calculate Jaccard similarity (intersection over union)
        intersection = words1.intersection(words2)
        union = words1.union(words2)

        if not union:
            return 0.0

        jaccard_score = len(intersection) / len(union)

        # Boost score if significant words match
        if len(intersection) >= 2:
            jaccard_score = min(1.0, jaccard_score * 1.2)

        return jaccard_score


if __name__ == "__main__":
    run_test(
        url="https://www.gohart.org/Pages/AboutUS-PublicMeetings.aspx",
        schedule_type="unique_hart",
        timezone="America/New_York",
    )
