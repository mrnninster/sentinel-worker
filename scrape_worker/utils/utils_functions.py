# utils_functions.py
import calendar
from datetime import datetime
import pytz

import requests
import logging
from requests.exceptions import RequestException
from typing import Dict, Any, Optional, Union

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def remove_duplicates(input_list: list, exclude_field: str) -> list:
    """
    Removes duplicates from a list of dictionaries based on all fields except
    the specified one. This method is useful when we need to remove all
    duplicate entries from a list of dictionaries while keeping one field,
    such as date or agenda link, unique.

    Args:
        input_list (list): A list of dictionaries to process.
        exclude_field (str): The field to exclude when determining uniqueness.

    Returns:
        list: A list of dictionaries with duplicates removed.
    """
    unique_entries = {}

    for obj in input_list:
        key = frozenset(
            {key: value for key, value in obj.items() if key != exclude_field}.items()
        )
        unique_entries[key] = obj

    return list(unique_entries.values())


def get_api_json_call(base_api_url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Fetches JSON data from an API endpoint with the given parameters.

    Args:
        base_api_url (str): The base URL of the API.
        params (Dict[str, Any]): The query parameters for the API request.

    Returns:
        Dict[str, Any]: The JSON response from the API.

    Raises:
        requests.exceptions.HTTPError: If the HTTP request returns an error.
        requests.exceptions.RequestException: For general request errors.
    """
    try:
        response = requests.get(base_api_url, params=params, timeout=10)
        response.raise_for_status()
        if "application/json" in response.headers.get("Content-Type", ""):
            return response.json()
        else:
            log.warning("Warning: Response does not contain valid JSON.")
            return {}
    except RequestException as e:
        log.warning(f"JSON API call request exception occurred: {e}")
        return {}


def get_day_number_by_name(day_name: str) -> int:
    day_name = day_name.capitalize()
    return list(calendar.day_name).index(day_name)


def to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def extract_custom_id(value: Union[str, dict, None]) -> Optional[str]:
    """
    Extract ID from custom type field in Bubble API responses.

    Handles both object and string formats, with validation for empty strings.

    Args:
        value: Custom type field value - can be:
            - A string ID (e.g., "1766174273443x514943364085863000")
            - A dict with "_id" or "id" key
            - None or empty value

    Returns:
        Extracted ID string if valid, None otherwise

    Examples:
        >>> extract_custom_id("1766174273443x514943364085863000")
        '1766174273443x514943364085863000'
        >>> extract_custom_id({"_id": "1766174273443x514943364085863000"})
        '1766174273443x514943364085863000'
        >>> extract_custom_id("")
        None
        >>> extract_custom_id(None)
        None
    """
    if not value:
        return None
    if isinstance(value, str):
        # Validate non-empty string
        return value if value.strip() else None
    if isinstance(value, dict):
        id_value = value.get("_id") or value.get("id")
        # Validate extracted ID is a non-empty string
        return id_value if isinstance(id_value, str) and id_value.strip() else None
    return None
