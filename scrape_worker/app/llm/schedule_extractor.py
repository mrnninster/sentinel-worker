"""
LLM schedule extractor.

Pipeline stage after page fetch + HtmlCleaner:
  cleaned Markdown → OpenAI JSON → validate → retry with feedback → meetings

Meeting dicts use display-name keys:
  Meeting name, Scheduled time (UTC …Z), Status, Agenda link, Meeting link, …
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urljoin

import pytz
from dateutil import parser as date_parser
from openai import AsyncOpenAI

log = logging.getLogger(__name__)

_TRAILING_PAREN = re.compile(r"(?<=\S)\s*\([^)]*\)\s*$")


@dataclass
class ExtractionResult:
    meetings: list[dict[str, Any]]
    model_used: str
    attempts: int
    errors: list[str] = field(default_factory=list)
    raw_response: Optional[str] = None


class ScheduleExtractor:
    """Extract structured meetings from cleaned calendar Markdown via OpenAI."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model: str = "gpt-4o-mini",
        max_attempts: int = 3,
        timeout: int = 120,
        max_chars: int = 120_000,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model
        self.max_attempts = max_attempts
        self.timeout = timeout
        self.max_chars = max_chars
        self._client: Optional[AsyncOpenAI] = None

    @property
    def client(self) -> AsyncOpenAI:
        if not self.api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Configure it in the environment or .env."
            )
        if self._client is None:
            self._client = AsyncOpenAI(api_key=self.api_key, timeout=self.timeout)
        return self._client

    async def extract(
        self,
        page_markdown: str,
        *,
        page_url: str,
        timezone_name: str,
    ) -> ExtractionResult:
        if not page_markdown or not page_markdown.strip():
            return ExtractionResult(
                meetings=[],
                model_used=self.model,
                attempts=0,
                errors=["Empty page content"],
            )

        truncated = page_markdown.strip()
        if len(truncated) > self.max_chars:
            log.warning(
                "Page markdown truncated from %d to %d chars for LLM",
                len(truncated),
                self.max_chars,
            )
            truncated = truncated[: self.max_chars]

        system_prompt, user_prompt = self._build_prompts(
            truncated, page_url=page_url, timezone_name=timezone_name
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        errors: list[str] = []
        last_raw: Optional[str] = None
        model_used = self.model

        for attempt in range(1, self.max_attempts + 1):
            log.info(
                "Schedule extraction attempt %d/%d model=%s",
                attempt,
                self.max_attempts,
                self.model,
            )
            try:
                raw, model_used = await self._invoke(messages)
                last_raw = raw
            except Exception as exc:
                log.exception("LLM call failed on attempt %d", attempt)
                errors.append(f"LLM error: {exc}")
                return ExtractionResult(
                    meetings=[],
                    model_used=model_used,
                    attempts=attempt,
                    errors=errors,
                    raw_response=last_raw,
                )

            parsed = self._parse_json(raw)
            ok, meetings, validation_errors = self._validate_and_normalize(
                parsed,
                page_url=page_url,
                timezone_name=timezone_name,
            )
            if ok:
                meetings = self._clean_titles(meetings)
                log.info("Extracted %d meetings", len(meetings))
                return ExtractionResult(
                    meetings=meetings,
                    model_used=model_used,
                    attempts=attempt,
                    errors=[],
                    raw_response=raw,
                )

            errors = validation_errors
            log.warning(
                "Validation failed attempt %d: %s",
                attempt,
                "; ".join(validation_errors),
            )
            messages.append({"role": "assistant", "content": raw or ""})
            messages.append(
                {
                    "role": "user",
                    "content": self._feedback(validation_errors),
                }
            )

        return ExtractionResult(
            meetings=[],
            model_used=model_used,
            attempts=self.max_attempts,
            errors=errors,
            raw_response=last_raw,
        )

    async def _invoke(self, messages: list[dict[str, str]]) -> tuple[str, str]:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        text = (response.choices[0].message.content or "").strip()
        model_used = response.model or self.model
        return text, model_used

    def _build_prompts(
        self,
        page_markdown: str,
        *,
        page_url: str,
        timezone_name: str,
    ) -> tuple[str, str]:
        today = datetime.now(pytz.timezone(timezone_name)).strftime("%Y-%m-%d")

        system_prompt = (
            "You are an expert at reading government and municipal meeting calendars. "
            "Extract every distinct upcoming (and recently past, if listed) meeting or "
            "hearing from the page content. Ignore navigation chrome, cookie banners, "
            "social widgets, footers, and unrelated news. Prefer structured listings "
            "(tables, event cards, lists) over incidental date mentions."
        )

        schema = (
            "Return a JSON object with keys:\n"
            '  "total_count": number (must equal length of meetings),\n'
            '  "meetings": array of objects, each with:\n'
            '    "meeting_name": string (required, concise title without trailing status),\n'
            '    "local_datetime": string (required) — the meeting date/time as shown or '
            "clearly implied on the page, in a parseable form like "
            '"2026-07-26 10:00 AM" or "July 26, 2026 at 2:00 PM". '
            "If only a date is present, use 12:00 AM local as the time.\n"
            '    "status": string — one of "Upcoming", "In progress", "Cancelled", '
            '"Completed" (default Upcoming),\n'
            '    "agenda_link": string|null — absolute or relative URL to agenda/packet,\n'
            '    "meeting_link": string|null — livestream / join / webcast URL,\n'
            '    "stream_type": string|null,\n'
            '    "phone_number": string|null,\n'
            '    "passcode": string|null,\n'
            '    "access_id": string|null,\n'
            '    "user_live_link": string|null,\n'
            '    "user_archive_link": string|null\n'
            "Do not include markdown fences or commentary — JSON only."
        )

        user_prompt = (
            f"Page URL: {page_url}\n"
            f"Jurisdiction timezone (IANA): {timezone_name}\n"
            f"Today's local date in that timezone: {today}\n\n"
            "Page content (cleaned Markdown):\n"
            "<<<PAGE>>>\n"
            f"{page_markdown}\n"
            "<<<END_PAGE>>>\n\n"
            "Rules:\n"
            "(1) Extract real meetings/hearings/sessions only.\n"
            "(2) Resolve relative links against the page URL when obvious; otherwise "
            "leave relative paths as-is.\n"
            "(3) Do not invent meetings that are not on the page.\n"
            "(4) If the same meeting appears twice, keep one copy.\n"
            "(5) meeting_name must not be empty.\n"
            f"{schema}"
        )
        return system_prompt, user_prompt

    def _parse_json(self, content: str) -> Any:
        cleaned = (content or "").strip()
        if cleaned.startswith("```json"):
            end = cleaned.find("```", 7)
            if end != -1:
                cleaned = cleaned[7:end].strip()
        elif cleaned.startswith("```"):
            end = cleaned.find("```", 3)
            if end != -1:
                cleaned = cleaned[3:end].strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            log.debug("Failed to parse schedule JSON: %s", content[:500])
            return None

    def _validate_and_normalize(
        self,
        parsed: Any,
        *,
        page_url: str,
        timezone_name: str,
    ) -> tuple[bool, list[dict[str, Any]], list[str]]:
        errors: list[str] = []
        if parsed is None:
            return False, [], ["Response was not valid JSON"]
        if not isinstance(parsed, dict):
            return (
                False,
                [],
                ["Top-level JSON must be an object with total_count and meetings"],
            )

        meetings_raw = parsed.get("meetings")
        claimed = parsed.get("total_count")
        if meetings_raw is None:
            return False, [], ["Missing 'meetings' array"]
        if not isinstance(meetings_raw, list):
            return (
                False,
                [],
                [f"'meetings' must be an array, got {type(meetings_raw).__name__}"],
            )
        if claimed is not None and int(claimed) != len(meetings_raw):
            errors.append(
                f"total_count ({claimed}) does not match meetings length "
                f"({len(meetings_raw)})"
            )

        try:
            tz = pytz.timezone(timezone_name)
        except Exception:
            return False, [], [f"Invalid timezone: {timezone_name}"]

        normalized: list[dict[str, Any]] = []
        for index, item in enumerate(meetings_raw):
            if not isinstance(item, dict):
                errors.append(f"Entry {index} is not an object")
                continue

            name = self._as_str(item.get("meeting_name") or item.get("Meeting name"))
            local_dt = self._as_str(
                item.get("local_datetime")
                or item.get("scheduled_time_local")
                or item.get("Scheduled time")
            )
            if not name:
                errors.append(f"Entry {index} missing meeting_name")
                continue
            if not local_dt:
                errors.append(f"Entry {index} ({name!r}) missing local_datetime")
                continue

            try:
                utc_iso = self._to_utc_iso(local_dt, tz)
            except Exception as exc:
                errors.append(
                    f"Entry {index} ({name!r}) has unparseable local_datetime "
                    f"{local_dt!r}: {exc}"
                )
                continue

            status = (
                self._as_str(item.get("status") or item.get("Status")) or "Upcoming"
            )
            meeting: dict[str, Any] = {
                "Meeting name": name,
                "Scheduled time": utc_iso,
                "Status": status,
            }

            optional_map = {
                "agenda_link": "Agenda link",
                "Agenda link": "Agenda link",
                "meeting_link": "Meeting link",
                "Meeting link": "Meeting link",
                "stream_type": "Stream type",
                "Stream type": "Stream type",
                "phone_number": "Phone number",
                "Phone number": "Phone number",
                "passcode": "Passcode",
                "Passcode": "Passcode",
                "access_id": "Access ID",
                "Access ID": "Access ID",
                "user_live_link": "user_live_link",
                "user_archive_link": "user_archive_link",
            }
            for src, dest in optional_map.items():
                if src in item and item[src] is not None and str(item[src]).strip():
                    val = str(item[src]).strip()
                    if dest in (
                        "Agenda link",
                        "Meeting link",
                        "user_live_link",
                        "user_archive_link",
                    ):
                        val = self._absolutize(val, page_url)
                    meeting[dest] = val

            normalized.append(meeting)

        if errors:
            return False, [], errors
        return True, normalized, []

    def _to_utc_iso(self, local_datetime: str, tz: pytz.BaseTzInfo) -> str:
        """Parse a local datetime string into UTC ISO form ending in Z."""
        text = local_datetime.strip()
        if text.endswith("Z") or text.endswith("+00:00"):
            dt = date_parser.isoparse(text.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        dt = date_parser.parse(text, fuzzy=True)
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        else:
            dt = dt.astimezone(tz)
        utc = dt.astimezone(pytz.utc)
        return utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _absolutize(link: str, page_url: str) -> str:
        if link.startswith(("http://", "https://", "mailto:", "tel:")):
            return link
        return urljoin(page_url, link)

    @staticmethod
    def _as_str(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _clean_titles(meetings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for meeting in meetings:
            name = meeting.get("Meeting name")
            if isinstance(name, str):
                meeting["Meeting name"] = _TRAILING_PAREN.sub("", name).strip()
        return meetings

    @staticmethod
    def _feedback(errors: list[str]) -> str:
        joined = "\n".join(f"- {e}" for e in errors)
        return (
            "Your previous JSON failed validation. Fix these issues and return "
            "corrected JSON only:\n"
            f"{joined}"
        )
