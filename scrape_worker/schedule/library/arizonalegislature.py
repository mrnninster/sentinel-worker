# arizonalegislature.py
import os
import re
import sys
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

import pytz
import requests
from bs4 import BeautifulSoup
from dateutil import parser
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv

# Add project path for imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from utils.format_time import TimeFormatter  # noqa: E402

if __name__ == "__main__":
    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"

DEFAULT_LIVEPROCEEDINGS_URL = "https://www.azleg.gov/liveproceedings/"
DEFAULT_CLIENT_ID = "6361162879"
DEFAULT_LIVE_PREFERENCE_ID = "364"
DEFAULT_UPCOMING_PREFERENCE_ID = "365"
DEFAULT_RESULT_MAX = "100"
DEFAULT_TIMEZONE = "America/Phoenix"

INVINTUS_API_BASE_URL = "https://api.v3.invintus.com/v2"
# API key and JavaScript URL are extracted dynamically from the HTML page.
# The API key is found in the Invintus JavaScript bundle (WSC:"KEY" pattern).
# This approach is self-healing: if Invintus changes their API key or JavaScript URL,
# the scraper will automatically extract the new values without code changes.
# No hardcoded credentials - all extracted at runtime from azleg.gov.

USER_PLAYER_URL = (
    "https://www.azleg.gov/videoplayer/?clientID={client_id}&eventID={event_id}"
)
INVINTUS_HLS_URL = (
    "https://api.v3.invintus.com/StreamURI/hls/{client_id}/{event_id}/media.m3u8"
)

DEFAULT_DATE_WINDOWS = {
    "live": {"start": (-1, "day"), "stop": (1, "day")},
    "upcoming": {"start": (0, "day"), "stop": (1, "week")},
}


class Arizonalegislature:
    """
    Self-contained scraper for the Arizona Legislature Live & Upcoming Events.

    Primary data source: Invintus listing API used on
    https://www.azleg.gov/liveproceedings/.

    Standard Config:
        schedule_type: unique_arizonalegislature
        schedule_url: https://www.azleg.gov/liveproceedings/
        stream_type: streamlink
        detect_start_method: calendar_detect
        detect_end_method: stream_detect
    """

    def __init__(self, force_api_key_extraction: bool = False):
        self.self_contained_parser = True
        self.meetings = []
        self.session = requests.Session()
        self.timezone = DEFAULT_TIMEZONE
        self._cached_api_key = None  # Cache extracted API key
        self._html_cache = None  # Cache HTML to avoid re-fetching
        self._force_api_key_extraction = force_api_key_extraction  # For testing

    def unique_arizonalegislature(
        self, url: str = DEFAULT_LIVEPROCEEDINGS_URL, timezone: str = DEFAULT_TIMEZONE
    ) -> List[Dict]:
        if not url:
            url = DEFAULT_LIVEPROCEEDINGS_URL

        self.timezone = timezone or DEFAULT_TIMEZONE

        html = self._fetch_html(url)
        # Extract JavaScript URL from HTML and fetch it to get the API key
        # This makes the scraper self-healing if Invintus changes the URL or key
        js_url = self._extract_js_url_from_html(html)
        if not js_url:
            error_msg = (
                "Could not find Invintus JavaScript URL in HTML. "
                "The page structure may have changed."
            )
            if self._force_api_key_extraction:
                raise ValueError(error_msg)
            log.warning(error_msg)
            js_content = ""
        else:
            js_content = self._fetch_html(js_url)
        # Append JS content to HTML cache for key extraction
        self._html_cache = html + "\n" + js_content

        # Try to extract API key from the JavaScript file
        extracted_key = self._extract_api_key_from_html(self._html_cache)
        if extracted_key:
            self._cached_api_key = extracted_key
            log.info("Extracted API key from Invintus JavaScript")
        else:
            # No fallback - extraction must succeed
            error_msg = (
                "Could not extract API key from Invintus JavaScript. "
                "The JavaScript file may be unavailable or the key format changed."
            )
            if self._force_api_key_extraction:
                raise ValueError(error_msg)
            log.warning(error_msg)

        listing_configs = self._extract_listing_configs(html)

        events = []
        for listing in listing_configs:
            preference = self._fetch_listing_preference(
                listing["client_id"], listing["preference_id"]
            )
            search_payload = self._build_search_payload(listing, preference)
            events.extend(self._search_events(listing["client_id"], search_payload))

        if not events:
            log.warning("No events returned from Invintus API.")
            self.meetings = []
            return self.meetings

        self.meetings = self._build_meetings(events)
        log.info("Found %s Arizona Legislature meetings", len(self.meetings))
        return self.meetings

    def _fetch_html(self, url: str) -> str:
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            log.warning("Failed to fetch %s: %s", url, exc)
            return ""

    def _extract_listing_configs(self, html: str) -> List[Dict[str, str]]:
        configs = []
        client_id_by_pref = self._extract_client_ids(html)
        listing_blocks = self._extract_listing_blocks(html)

        for block in listing_blocks:
            preference_id = block.get("preference_id")
            if not preference_id:
                continue
            listing_type = block.get("listing_type") or "upcoming"
            configs.append(
                {
                    "listing_type": listing_type,
                    "preference_id": preference_id,
                    "client_id": client_id_by_pref.get(
                        preference_id, DEFAULT_CLIENT_ID
                    ),
                }
            )

        if not configs:
            configs = [
                {
                    "listing_type": "live",
                    "preference_id": DEFAULT_LIVE_PREFERENCE_ID,
                    "client_id": DEFAULT_CLIENT_ID,
                },
                {
                    "listing_type": "upcoming",
                    "preference_id": DEFAULT_UPCOMING_PREFERENCE_ID,
                    "client_id": DEFAULT_CLIENT_ID,
                },
            ]

        return configs

    def _extract_listing_blocks(self, html: str) -> List[Dict[str, Optional[str]]]:
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        blocks = []
        for listing_div in soup.select("div.invintus-event-listing"):
            preference_id = None
            for class_name in listing_div.get("class", []):
                if class_name.startswith("invintus-event-listing-preference-"):
                    preference_id = class_name.rsplit("-", 1)[-1]
                    break

            heading_text = ""
            heading = listing_div.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
            if heading:
                heading_text = heading.get_text(strip=True).lower()

            listing_type = None
            if "live" in heading_text:
                listing_type = "live"
            elif "upcoming" in heading_text:
                listing_type = "upcoming"

            blocks.append(
                {
                    "preference_id": preference_id,
                    "listing_type": listing_type,
                }
            )

        return blocks

    def _extract_client_ids(self, html: str) -> Dict[str, str]:
        client_id_by_pref = {}
        if not html:
            return client_id_by_pref

        for match in re.finditer(
            r"InvintusEventListing\.launch\((\{.*?\})\)",
            html,
            re.DOTALL,
        ):
            payload = match.group(1)
            client_match = re.search(r"clientID['\"]?\s*:\s*['\"]?(\d+)", payload)
            pref_match = re.search(r"id['\"]?\s*:\s*['\"]?(\d+)", payload)
            if client_match and pref_match:
                client_id_by_pref[pref_match.group(1)] = client_match.group(1)

        return client_id_by_pref

    def _extract_js_url_from_html(self, html: str) -> Optional[str]:
        """Extract Invintus JavaScript URL from HTML.

        The URL is typically found in script tags that load the Invintus event listing.
        Pattern: 'https://eventlisting.invintus.com/app.js'
        """
        if not html:
            return None

        # Look for the Invintus app.js URL in script tags or JavaScript code
        # Pattern matches: 'https://eventlisting.invintus.com/app.js'
        patterns = [
            r"['\"](https?://eventlisting\.invintus\.com/[^'\"]*app\.js[^'\"]*)['\"]",
            r"(https?://eventlisting\.invintus\.com/[^\s<>\"']*app\.js)",
        ]

        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                js_url = match.group(1)
                log.debug("Extracted Invintus JavaScript URL: %s", js_url)
                return js_url

        log.warning("Could not extract Invintus JavaScript URL from HTML")
        return None

    def _extract_api_key_from_html(self, html: str) -> Optional[str]:
        """Extract Invintus API key from HTML/JavaScript.

        The API key is found in the Invintus JavaScript bundle as WSC:"KEY".
        Also checks for wsc-api-key patterns in headers or config objects.
        """
        if not html:
            return None

        # Pattern 1: Look for WSC:"KEY" pattern (found in Invintus app.js bundle)
        # This is the primary pattern used by the Invintus event listing widget
        # Format: WSC:"KEY" or "WSC":"KEY" or 'WSC':'KEY'
        wsc_patterns = [
            r'WSC\s*:\s*["\']([A-Za-z0-9]{20,})["\']',  # WSC:"KEY"
            r'["\']WSC["\']\s*:\s*["\']([A-Za-z0-9]{20,})["\']',  # "WSC":"KEY"
        ]
        for wsc_pattern in wsc_patterns:
            match = re.search(wsc_pattern, html)
            if match:
                key = match.group(1)
                if len(key) >= 20 and key.isalnum():
                    log.info("Extracted Invintus API key from WSC pattern")
                    return key

        # Pattern 2: Look for wsc-api-key in headers or config objects
        patterns = [
            r"wsc[-_]api[-_]key['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]",
            r"wscApiKey['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]",
        ]

        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                key = match.group(1)
                # Validate it looks like an API key (alphanumeric, reasonable length)
                if len(key) >= 20 and key.isalnum():
                    log.info("Extracted Invintus API key from HTML")
                    return key

        # Pattern 3: Look in fetch/axios headers
        header_pattern = (
            r"headers\s*[:=]\s*\{[^}]*['\"]wsc[-_]api[-_]key['\"]\s*:\s*"
            r"['\"]([^'\"]+)['\"]"
        )
        match = re.search(header_pattern, html, re.IGNORECASE | re.DOTALL)
        if match:
            key = match.group(1)
            if len(key) >= 20 and key.isalnum():
                log.info("Extracted Invintus API key from headers")
                return key

        log.warning("Could not extract API key from HTML")
        return None

    def _get_api_headers(self) -> Dict[str, str]:
        """Get API headers, using cached extracted key."""
        headers = {"authorization": "embedder"}

        api_key = self._cached_api_key
        if not api_key:
            raise ValueError(
                "API key could not be extracted from Invintus JavaScript. "
                "Ensure the JavaScript file is accessible and contains the WSC key."
            )

        headers["wsc-api-key"] = api_key
        return headers

    def _post_api_json_call(self, endpoint: str, body: Dict, timeout: int = 20) -> Dict:
        url = f"{INVINTUS_API_BASE_URL}/{endpoint.lstrip('/')}"
        headers = self._get_api_headers()

        try:
            response = self.session.post(
                url, json=body, headers=headers, timeout=timeout
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                log.warning("Unexpected Invintus response format from %s", url)
                return {}
            if data.get("errors", {}).get("hasError"):
                log.warning("Invintus API error: %s", data["errors"])
                return {}
            return data
        except requests.HTTPError as exc:
            # Check for authentication errors (401/403)
            if exc.response and exc.response.status_code in (401, 403):
                log.warning(
                    "Invintus API authentication failed (status %d). "
                    "Attempting to extract API key from HTML...",
                    exc.response.status_code,
                )
                # Try to extract API key from cached HTML
                if self._html_cache:
                    extracted_key = self._extract_api_key_from_html(self._html_cache)
                    if extracted_key:
                        self._cached_api_key = extracted_key
                        log.info(
                            "Extracted new API key from HTML cache, retrying API call"
                        )
                        # Retry once with extracted key
                        headers = self._get_api_headers()
                        try:
                            retry_response = self.session.post(
                                url, json=body, headers=headers, timeout=timeout
                            )
                            retry_response.raise_for_status()
                            retry_data = retry_response.json()
                            if isinstance(retry_data, dict) and not retry_data.get(
                                "errors", {}
                            ).get("hasError"):
                                log.info("API call succeeded with extracted key")
                                return retry_data
                        except requests.RequestException:
                            pass  # Fall through to original error

                log.warning(
                    "Invintus API authentication failed and could not extract key. "
                    "Original error: %s",
                    exc,
                )
            else:
                log.warning("Invintus API request failed: %s", exc)
            return {}
        except requests.RequestException as exc:
            log.warning("Invintus API request failed: %s", exc)
            return {}

    def _fetch_listing_preference(self, client_id: str, preference_id: str) -> Dict:
        if not client_id or not preference_id:
            return {}
        payload = {"preferenceID": preference_id, "clientID": client_id}
        response = self._post_api_json_call("Embeded/getListingPreferenceByID", payload)
        return response.get("data", {}) if response else {}

    def _build_search_payload(self, listing: Dict, preference: Dict) -> Dict:
        listing_type = listing.get("listing_type") or "upcoming"
        preference_meta = preference.get("preference", {}) if preference else {}
        query_preferences = preference_meta.get("query", {}).get("preferences", {})

        payload = {
            "sortBy": "ASC",
            "resultMax": DEFAULT_RESULT_MAX,
        }

        start_value = query_preferences.get("startDateTime", {}).get("value")
        stop_value = query_preferences.get("stopDateTime", {}).get("value")
        start_date = self._resolve_preference_date(start_value, listing_type, "start")
        stop_date = self._resolve_preference_date(stop_value, listing_type, "stop")

        if start_date:
            payload["startDateTime"] = start_date
        if stop_date:
            payload["stopDateTime"] = stop_date

        categories = query_preferences.get("categories", {}).get("value")
        if categories:
            payload["categories"] = categories

        title = query_preferences.get("title", {}).get("value")
        if title:
            payload["title"] = title

        description = query_preferences.get("description", {}).get("value")
        if description:
            payload["description"] = description

        status_filter = self._resolve_status_filter(listing_type, preference_meta)
        if status_filter:
            payload["status"] = status_filter

        return payload

    def _resolve_preference_date(
        self, value: object, listing_type: str, bound: str
    ) -> str:
        tz = pytz.timezone(self.timezone)
        now = datetime.now(tz)

        if isinstance(value, dict):
            count = self._safe_int(value.get("count", 0))
            units = value.get("units")
            return self._apply_relative_offset(now, count, units)

        if isinstance(value, str) and value.strip():
            try:
                parsed = parser.parse(value, fuzzy=True, ignoretz=True)
                return parsed.date().strftime("%Y-%m-%d")
            except (ValueError, parser.ParserError):
                pass

        fallback = DEFAULT_DATE_WINDOWS.get(listing_type, {}).get(bound)
        if fallback:
            count, units = fallback
            return self._apply_relative_offset(now, count, units)

        # No date preference found and no fallback available for this listing_type
        if listing_type not in DEFAULT_DATE_WINDOWS:
            log.warning(
                "No date window fallback for listing_type '%s', bound '%s'. "
                "Date filtering may be incomplete.",
                listing_type,
                bound,
            )

        return None

    def _apply_relative_offset(
        self, base_dt: datetime, count: int, units: str
    ) -> Optional[str]:
        if not units:
            return None

        units = units.lower()
        if units == "day":
            target = base_dt + timedelta(days=count)
        elif units == "week":
            target = base_dt + timedelta(weeks=count)
        elif units == "month":
            target = base_dt + relativedelta(months=count)
        elif units == "year":
            target = base_dt + relativedelta(years=count)
        else:
            return None

        return target.date().strftime("%Y-%m-%d")

    def _resolve_status_filter(
        self, listing_type: str, preference_meta: Dict
    ) -> Optional[str]:
        if listing_type == "live":
            return "live"
        if listing_type == "upcoming":
            return "not live"

        preference_name = (preference_meta.get("name") or "").lower()
        if "live" in preference_name:
            return "live"
        if "upcoming" in preference_name:
            return "not live"

        return None

    def _search_events(self, client_id: str, payload: Dict) -> List[Dict]:
        if not client_id:
            return []
        query = dict(payload)
        query["clientID"] = client_id

        response = self._post_api_json_call("Search/general", query)
        data = response.get("data", []) if response else []
        if not isinstance(data, list):
            log.warning("Unexpected data format from Invintus Search/general")
            return []
        return data

    def _build_meetings(self, events: List[Dict]) -> List[Dict]:
        meetings_by_id = {}

        for event in events:
            event_id = event.get("eventID") or event.get("id")
            if not event_id:
                continue

            meeting = self._build_meeting_from_event(event)
            if not meeting:
                continue

            existing = meetings_by_id.get(event_id)
            if not existing:
                meetings_by_id[event_id] = meeting
                continue

            # Deduplication strategy for events with the same eventID:
            # 1. Always prefer "In progress" status
            #    (live events take priority over upcoming)
            # 2. If both are upcoming (or neither is "In progress"),
            #    use the newer event data
            #    This ensures we get the most recent information when an event
            #    appears in multiple listing preferences
            #    (e.g., both "live" and "upcoming" queries)
            if meeting["Status"] == "In progress":
                meetings_by_id[event_id] = meeting
            elif existing["Status"] != "In progress":
                # Both are upcoming - use newer data (current meeting)
                meetings_by_id[event_id] = meeting

        return list(meetings_by_id.values())

    def _build_meeting_from_event(self, event: Dict) -> Optional[Dict]:
        event_id = event.get("eventID") or event.get("id")
        client_id = event.get("clientID") or DEFAULT_CLIENT_ID
        raw_title = (event.get("title") or "").strip()

        if not raw_title:
            return None

        meeting_name = self._clean_meeting_title(raw_title)
        scheduled_time = self._format_event_time(event.get("startDateTime"))
        status = self._normalize_status(event.get("status"), meeting_name)

        player_link = (
            USER_PLAYER_URL.format(client_id=client_id, event_id=event_id)
            if event_id
            else None
        )

        meeting_link = (
            INVINTUS_HLS_URL.format(client_id=client_id, event_id=event_id)
            if event_id
            else None
        )

        return {
            "Meeting name": meeting_name,
            "Scheduled time": scheduled_time,
            "Meeting link": meeting_link,  # HLS stream URL for direct playback
            # Agenda link not available from Invintus API search results.
            # Would require additional API calls or HTML scraping to extract.
            # This is intentionally omitted - agenda links are not provided by the API.
            "Agenda link": None,
            "Status": status,
            # User-facing video player link (azleg.gov videoplayer page).
            # This is the public URL users would visit to watch the meeting
            # in a browser. Different from "Meeting link" which is the
            # direct HLS stream URL.
            "user_live_link": player_link,
        }

    def _clean_meeting_title(self, title: str) -> str:
        cleaned = re.sub(r"^\d{1,2}/\d{1,2}/\d{2,4}\s*[-–—]\s*", "", title).strip()
        return cleaned or title.strip()

    def _format_event_time(self, start_time: str) -> Optional[str]:
        if not start_time:
            return None

        try:
            meeting_date_time_web = parser.parse(start_time, fuzzy=True, ignoretz=True)
            formatted_naive = meeting_date_time_web.strftime(
                TimeFormatter.desired_format()
            )
            time_formatter = TimeFormatter(formatted_naive, self.timezone)
            formatted_date_time = time_formatter.get_utc_time(as_datetime=True)
            return (
                formatted_date_time.strftime(
                    MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT
                )[:-3]
                + "Z"
            )
        except Exception as exc:
            log.warning("Failed to parse meeting time '%s': %s", start_time, exc)
            return None

    def _normalize_status(self, status: str, title: str) -> str:
        if title and re.search(r"Cancel(?:led|ed)", title, re.IGNORECASE):
            return "Cancelled"

        status_text = (status or "").strip().lower()
        if status_text in {"live", "on break", "break"}:
            return "In progress"

        return "Upcoming"

    @staticmethod
    def _safe_int(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default


if __name__ == "__main__":
    # Create scraper - it will automatically extract the API key
    # from the JavaScript file
    scraper = Arizonalegislature()
    meetings = scraper.unique_arizonalegislature(
        url=DEFAULT_LIVEPROCEEDINGS_URL, timezone=DEFAULT_TIMEZONE
    )

    # Print results
    for meeting in meetings:
        print(meeting)
