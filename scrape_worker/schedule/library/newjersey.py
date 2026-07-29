import os
import sys
import pytz
import json
import logging
import requests
import certifi
from pathlib import Path
from dateutil import parser
from dotenv import load_dotenv
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test


def njleg_ca_bundle_path() -> str:
    """
    Returns path to a combined CA bundle (certifi + DigiCert intermediate).

    This is needed because www.njleg.state.nj.us only sends the leaf certificate
    in the TLS handshake and does not include the intermediate certificate.
    This causes SSL verification to fail even though the certificate is valid.

    The fix:
    1. Prefer REQUESTS_CA_BUNDLE or SSL_CERT_FILE if set (ops override)
    2. Otherwise, combine certifi's CA bundle with the vendored intermediate
    3. Write to /tmp (works on both local dev and Heroku)
    4. Return the path to use as verify= parameter in requests
    """
    # Ops override for both local and Heroku
    override = os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("SSL_CERT_FILE")
    if override:
        log.info(f"Using operator-specified CA bundle: {override}")
        return override

    # Find repo root relative to this file
    # schedule/library/newjersey.py -> go up 2 levels to repo root
    here = Path(__file__).resolve()
    repo_root = here.parents[2]

    intermediate = repo_root / "schedule" / "certs" / "DigiCertGlobalG2TLSRSASHA2562020CA1.pem"
    if not intermediate.exists():
        # Fall back to certifi; request will likely still fail, but we keep verification ON.
        log.warning(
            f"DigiCert intermediate certificate not found at {intermediate}. "
            "Falling back to certifi - SSL verification may still fail."
        )
        return certifi.where()

    # Write combined bundle to /tmp (works on both local and Heroku)
    combined = Path("/tmp/njleg_combined_ca_bundle.pem")

    # Only create if it doesn't exist (avoid recreating on every call)
    if not combined.exists():
        try:
            # Combine intermediate + certifi root bundle
            combined_content = intermediate.read_text() + "\n" + Path(certifi.where()).read_text()
            combined.write_text(combined_content)
            log.info(f"Created combined CA bundle at {combined}")
        except Exception as e:
            log.warning(f"Failed to create combined CA bundle: {e}. Falling back to certifi.")
            return certifi.where()

    return str(combined)


class Newjersey:
    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        self.base_url = "https://www.njleg.state.nj.us/"
        self.agenda_base_url = f"{self.base_url}live-proceedings/"
        self.base_api_url = f"{self.base_url}api/liveProceedings/mediaLink/"
        self.calendar_base_url = f"{self.base_url}api/calendarEvents/selectedDay/"

    def unique_newjersey(self, url, timezone="America/New_York"):
        # Get meetings for today and the next 2 days
        for i in range(0, 3):
            local_timezone = pytz.timezone(timezone)
            local_datetime = datetime.now(local_timezone) + timedelta(days=i)
            date = local_datetime.date()
            date_str = date.strftime("%Y-%m-%d")
            url = f"{self.calendar_base_url}{date_str}"

            # Use combined CA bundle for SSL verification (includes DigiCert intermediate)
            verify_path = njleg_ca_bundle_path()
            allow_insecure = os.getenv("ALLOW_INSECURE_SSL", "").lower() in ("1", "true", "yes")

            try:
                response = requests.get(url, timeout=10, verify=verify_path)
                response.raise_for_status()
                api_response = response.text
            except requests.exceptions.SSLError as e:
                log.warning(
                    f"SSL verification failed for {url}: {str(e)}. "
                    "Likely cause: server is missing intermediate cert chain; "
                    "vendored intermediate bundle should resolve this."
                )
                if not allow_insecure:
                    log.warning("SSL verification is enforced. Skipping this date.")
                    continue
                log.warning("ALLOW_INSECURE_SSL enabled; retrying insecurely for debug only.")
                try:
                    response = requests.get(url, timeout=10, verify=False)
                    response.raise_for_status()
                    api_response = response.text
                except requests.RequestException as retry_error:
                    log.warning(f"Failed to fetch {url} (insecure retry): {str(retry_error)}")
                    continue
            except requests.RequestException as e:
                log.warning(f"Failed to fetch {url}: {str(e)}")
                continue

            if not api_response or api_response.strip() == "":
                continue
            try:
                json_response = json.loads(api_response)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(json_response, list):
                continue
            if not json_response:
                continue
            for item in json_response:
                if not isinstance(item, dict):
                    log.warning(f"Skipping invalid item (not a dict): {item}")
                    continue

                status = "Upcoming"
                meeting_link = None
                agenda_link = None

                try:
                    item_name = item.get("Code_Description", "Unknown Meeting")
                    item_time = item.get("Agenda_Time")
                    item_time_start = item.get("Agenda_Time_Start")
                    committee = item.get("Committee_House")
                    agenda_date = item.get("Agenda_Date_Parameter")
                    agenda_type = item.get("Agenda_Type")
                    agenda_type_description = item.get("AgendaTypeDescription")

                    # Validate required fields
                    if not all(
                        [
                            item_time,
                            item_time_start,
                            committee,
                            agenda_date,
                            agenda_type,
                        ]
                    ):
                        log.warning(
                            f"Skipping item with missing required fields: {item_name}"
                        )
                        continue

                    item_date = item_time_start.split("T")[0]
                except (AttributeError, KeyError) as e:
                    log.warning(f"Error extracting item fields: {str(e)}")
                    continue

                try:
                    item_date_time = f"{item_date} {item_time}"
                    item_date_time = parser.parse(item_date_time)
                    date_time = datetime.strftime(
                        item_date_time, TimeFormatter.desired_format()
                    )
                    utc_time = TimeFormatter(date_time, timezone).get_utc_time(
                        as_datetime=True
                    )
                    item_date_time = utc_time.isoformat().replace("+00:00", "Z")
                except (ValueError, AttributeError) as e:
                    log.warning(f"Error parsing date/time for {item_name}: {str(e)}")
                    continue

                if str(agenda_type).lower() != "q":
                    if agenda_type_description:
                        agenda_link = f"{self.agenda_base_url}{agenda_date}/{committee}/{agenda_type_description}"
                    # log.info(f"Agenda link => {agenda_link}")

                # Check meeting is live
                message = ""
                is_live = False
                if not agenda_type_description:
                    log.warning(
                        f"Skipping live check for {item_name}: missing AgendaTypeDescription"
                    )
                else:
                    live_proceeding_link = f"{self.base_api_url}{agenda_date}/{committee}/{agenda_type_description}"
                    try:
                        live_proceeding_response = self.scraper.scrape_html(
                            url=live_proceeding_link,
                            verify=njleg_ca_bundle_path()
                        )
                        if not live_proceeding_response:
                            live_proceeding_json = {"message": ""}
                        else:
                            try:
                                live_proceeding_json = json.loads(
                                    live_proceeding_response
                                )
                            except (json.JSONDecodeError, ValueError) as e:
                                log.warning(
                                    f"Failed to parse live proceeding JSON for {item_name}: {str(e)}"
                                )
                                live_proceeding_json = {"message": ""}
                    except (
                        requests.RequestException,
                        AttributeError,
                        KeyError,
                        TypeError,
                    ) as e:
                        log.warning(
                            f"Failed to fetch live proceeding for {item_name}: {str(e)}"
                        )
                        live_proceeding_json = {"message": ""}
                    # log.info(f"Live proceeding json => {live_proceeding_json}")

                    message = live_proceeding_json.get("message", "")
                    is_live = message.lower().startswith("/live") if message else False
                # log.info(f"Is live => {is_live}")
                if message != "" and is_live:
                    status = "In Progress"
                    live_url = f"{self.agenda_base_url}/mediaplayer?committee={committee}&agendaDate={agenda_date}&agendaType={agenda_type}&av=V"
                    try:
                        live_soup = self.scraper.scrape_html(
                            url=live_url,
                            verify=njleg_ca_bundle_path()
                        )
                        if live_soup is None:
                            log.warning(
                                f"Failed to scrape live URL for {item_name}: returned None"
                            )
                            meeting_link = None
                        else:
                            live_soup = self.scraper.convert_to_soup(string=live_soup)
                            if live_soup is None:
                                log.warning(
                                    f"Failed to convert HTML to soup for {item_name}"
                                )
                                meeting_link = None
                            else:
                                iframe = live_soup.find("iframe")
                                meeting_link = (
                                    iframe.get("src").strip()
                                    if iframe and iframe.get("src")
                                    else None
                                )
                                if not meeting_link:
                                    log.warning(
                                        f"No iframe found for {committee} on {agenda_date}"
                                    )
                    except (AttributeError, TypeError, ValueError, KeyError) as e:
                        log.warning(
                            f"Failed to extract stream URL for {item_name}: {str(e)}"
                        )
                        meeting_link = None

                meeting = {
                    "Meeting name": item_name,
                    "Scheduled time": item_date_time,
                    "Agenda link": agenda_link,
                    "Meeting link": meeting_link,
                    "Status": status,
                }
                self.meetings.append(meeting)
        return self.meetings


if __name__ == "__main__":
    run_test(url="https://www.njleg.state.nj.us", schedule_type="unique_newjersey")
