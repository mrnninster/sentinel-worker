# washingtonlegislature.py

import os
import sys
import logging
import requests

from datetime import datetime, timedelta
from dateutil import parser

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from schedule.schedule_scraper import run_test
from utils.format_time import TimeFormatter


MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"
API_CALL_CLIENT_ID = "9375922947"
API_CALL_AUTHORIZATION_WSC_API_KEY = "7WhiEBzijpritypp8bqcU7pfU9uicDR"
API_CALL_AUTHORIZATION_TYPE = "embedder"
API_CALL_DEFAULT_SORT = "ASC"
API_CALL_MAX_RESULT_PARAM = "100"
API_CALL_PAYLOAD_DATE_FORMAT = "%Y-%m-%d"
API_VIDEO_URL = "https://www.tvw.org/video/watch"


class Washingtonlegislature:
    def __init__(self):
        self.self_contained_parser = True
        self.meetings = []
        self.timezone = None

    def _post_api_json_call(
        self, api_url: str, body: dict, headers: dict, timeout: int = 10
    ) -> dict:
        """
        Makes a POST request to an API endpoint and returns JSON response.
        Provides standardized error handling and timeout management.

        Args:
            api_url (str): The API endpoint URL
            body (dict): The JSON body for the POST request
            headers (dict): The HTTP headers for the request
            timeout (int): Request timeout in seconds (default: 10)

        Returns:
            dict: The JSON response from the API, or empty dict on error
        """
        try:
            response = requests.post(
                api_url, json=body, headers=headers, timeout=timeout
            )
            response.raise_for_status()

            response_data = response.json()

            # Validate response structure
            if not isinstance(response_data, dict):
                log.warning(f"Unexpected API response format: {type(response_data)}")
                return {}

            # Check for API errors
            errors = response_data.get("errors", {})
            if isinstance(errors, dict) and errors.get("hasError"):
                log.warning(f"API returned error: {errors.get('message')}")
                return {}

            return response_data

        except requests.Timeout as e:
            log.warning(f"API request timed out: {str(e)}")
            return {}
        except requests.RequestException as e:
            log.warning(f"Failed to fetch from API: {str(e)}")
            return {}
        except (KeyError, ValueError, TypeError) as e:
            log.warning(f"Error parsing API response: {str(e)}")
            return {}
        except Exception as e:
            log.warning(f"Unexpected error in API call: {str(e)}")
            return {}

    def _append_meetings(self, meetings: list):
        if not meetings:
            log.warning("No meetings provided to append")
            return

        for meeting in meetings:
            if not isinstance(meeting, dict):
                log.warning(f"Skipping invalid meeting (not a dict): {meeting}")
                continue

            try:
                meeting_name = meeting.get("title")
                meeting_date = meeting.get("startDateTime")
                video_id = meeting.get("id")
                client_id = meeting.get("clientID")
                api_status = meeting.get("status", "").lower()

                # Validate required fields
                if not all([meeting_name, meeting_date, video_id, client_id]):
                    log.warning(
                        f"Skipping meeting with missing required fields: {meeting_name or 'Unknown'}"
                    )
                    continue

                meeting_date_time_web = parser.parse(
                    meeting_date, fuzzy=True, ignoretz=True
                )

                formatted_naive_datetime = meeting_date_time_web.strftime(
                    TimeFormatter.desired_format()
                )
                time_formatter = TimeFormatter(formatted_naive_datetime, self.timezone)
                formatted_date_time = time_formatter.get_utc_time(as_datetime=True)
                meeting_date = (
                    formatted_date_time.strftime(
                        MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT
                    )[:-3]
                    + "Z"
                )

                meeting_link = (
                    f"{API_VIDEO_URL}?clientID={client_id}&eventID={video_id}"
                )

                # Determine status based on API status field
                if api_status == "live":
                    status = "In Progress"
                else:
                    status = "Upcoming"

                self.meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": meeting_date,
                        "Meeting link": meeting_link,
                        "Agenda link": None,
                        "Status": status,
                    }
                )
            except (ValueError, AttributeError, KeyError) as e:
                log.warning(
                    f"Error processing meeting {meeting.get('title', 'Unknown')}: {str(e)}"
                )
                continue
            except Exception as e:
                log.warning(f"Unexpected error processing meeting: {str(e)}")
                continue

    def unique_washingtonlegislature(self, url: str, timezone: str) -> list:
        self.timezone = timezone
        api_url = "https://api.v3.invintus.com/v2/Search/general"

        today = datetime.today()
        # Use rolling 365-day window instead of end_of_year to ensure visibility across year boundaries
        end_date = today + timedelta(days=365)

        today_str = today.strftime(API_CALL_PAYLOAD_DATE_FORMAT)
        end_date_str = end_date.strftime(API_CALL_PAYLOAD_DATE_FORMAT)

        body = {
            "clientID": API_CALL_CLIENT_ID,
            "sortBy": API_CALL_DEFAULT_SORT,
            "startDateTime": today_str,
            "stopDateTime": end_date_str,
            "resultMax": API_CALL_MAX_RESULT_PARAM,
            # Omit status parameter to get all meetings
        }
        payload = {
            "Wsc-api-key": API_CALL_AUTHORIZATION_WSC_API_KEY,
            "authorization": API_CALL_AUTHORIZATION_TYPE,
        }

        # Make API call using helper method
        response_data = self._post_api_json_call(api_url, body, payload)

        if not response_data:
            # API failure - already logged in helper method
            # Return empty list but errors are logged, so failures are distinguishable from no meetings
            log.warning("API call failed - check logs above for details")
            return self.meetings

        # Extract meetings data
        all_meetings = response_data.get("data", [])
        if not isinstance(all_meetings, list):
            log.warning(f"Expected list of meetings but got {type(all_meetings)}")
            return self.meetings

        meeting_count = response_data.get("meta", {}).get("count", 0)
        log.info(f"Retrieved {meeting_count} meetings from API")

        if not all_meetings:
            # No meetings scheduled - this is normal, not an error
            log.info("No meetings scheduled in the date range")
            return self.meetings

        self._append_meetings(all_meetings)
        log.info(f"Successfully processed {len(self.meetings)} meetings")

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://app.leg.wa.gov/committeeschedules",
        schedule_type="unique_washingtonlegislature",
        timezone="America/Los_Angeles",
    )
