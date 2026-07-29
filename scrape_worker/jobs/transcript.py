"""Fetch YouTube transcript cues, render a PDF, and upload it to Command."""

from __future__ import annotations

import base64
import logging
import re
from io import BytesIO
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

import httpx

log = logging.getLogger(__name__)

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_JSON_PATTERNS = {
    "visitor_data": re.compile(r'"VISITOR_DATA":"([^"]+)"'),
    "client_version": re.compile(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"'),
}
_PANEL_PARAMS_RE = re.compile(
    r'"contentSourcePanelIdentifier":\{"surface":"ENGAGEMENT_PANEL_SURFACE_WATCH",'
    r'"tag":"PAmodern_transcript_view"\},"globalConfiguration":\{"params":"([^"]+)"'
)


class TranscriptError(RuntimeError):
    """A transcript could not be fetched or uploaded."""

    def __init__(
        self,
        message: str,
        *,
        rate_limited: bool = False,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.rate_limited = rate_limited
        # Structured code for downstream filtering (e.g. "no_captions", "rate_limited").
        self.reason = reason or ("rate_limited" if rate_limited else "error")


def extract_video_id(video_id: str | None, video_url: str | None) -> str:
    candidate = (video_id or "").strip()
    if _VIDEO_ID_RE.fullmatch(candidate):
        return candidate

    raw_url = (video_url or "").strip()
    if raw_url:
        parsed = urlparse(raw_url)
        if parsed.hostname in {"youtu.be", "www.youtu.be"}:
            candidate = parsed.path.strip("/").split("/", 1)[0]
        elif parsed.hostname and parsed.hostname.endswith("youtube.com"):
            candidate = (parse_qs(parsed.query).get("v") or [""])[0]
            if not candidate and parsed.path.startswith("/shorts/"):
                candidate = parsed.path.split("/")[2]
    if not _VIDEO_ID_RE.fullmatch(candidate):
        raise TranscriptError("A valid 11-character YouTube video_id is required")
    return candidate


def _walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _simple_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("simpleText"), str):
        return value["simpleText"]
    runs = value.get("runs")
    if isinstance(runs, list):
        return "".join(str(run.get("text") or "") for run in runs if isinstance(run, dict))
    return ""


def _timestamp_seconds(value: str) -> float:
    parts = value.strip().split(":")
    try:
        total = 0.0
        for part in parts:
            total = total * 60 + float(part)
        return total
    except ValueError:
        return 0.0


def _parse_panel_cues(data: dict[str, Any]) -> list[dict[str, Any]]:
    cues: list[dict[str, Any]] = []
    for node in _walk(data):
        if not isinstance(node, dict):
            continue
        item = node.get("timelineItemViewModel")
        if not isinstance(item, dict):
            continue

        timestamp = _simple_text(item.get("timestamp"))
        text = ""
        content_items = item.get("contentItems")
        if isinstance(content_items, list):
            for content in content_items:
                if not isinstance(content, dict):
                    continue
                segment = content.get("transcriptSegmentViewModel")
                if isinstance(segment, dict):
                    text = _simple_text(segment)
                    if text:
                        break
        if not text:
            continue

        start = _timestamp_seconds(timestamp)
        for child in _walk(node):
            if isinstance(child, dict):
                endpoint = child.get("watchEndpoint")
                if isinstance(endpoint, dict) and endpoint.get("startTimeSeconds") is not None:
                    try:
                        start = float(endpoint["startTimeSeconds"])
                    except (TypeError, ValueError):
                        pass
                    break
        cues.append({"start": start, "text": text.strip()})
    return cues


def _build_panel_params(video_id: str) -> str:
    """
    Serialize the get_panel params proto: field 149 { 1: video_id, 3: 1 }.

    Older watch pages only ship the legacy searchable-transcript endpoint, so the
    modern params cannot always be scraped from the HTML.
    """
    inner = b"\x0a" + bytes([len(video_id)]) + video_id.encode("ascii") + b"\x18\x01"
    raw = b"\xaa\x09" + bytes([len(inner)]) + inner
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def fetch_innertube_panel(video_id: str, *, timezone: str = "America/New_York") -> list[dict[str, Any]]:
    """Use the modern Show transcript get_panel request."""
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    with httpx.Client(timeout=45.0, follow_redirects=True, headers=headers) as client:
        watch = client.get(watch_url)
        if watch.status_code in {403, 429}:
            raise TranscriptError("YouTube rate limited the transcript request", rate_limited=True)
        watch.raise_for_status()

        visitor_match = _JSON_PATTERNS["visitor_data"].search(watch.text)
        version_match = _JSON_PATTERNS["client_version"].search(watch.text)
        visitor_data = visitor_match.group(1) if visitor_match else ""
        client_version = version_match.group(1) if version_match else "2.20260728.01.00"

        context = {
            "client": {
                "hl": "en",
                "gl": "US",
                "clientName": "WEB",
                "clientVersion": client_version,
                "visitorData": visitor_data,
                "userAgent": headers["User-Agent"],
                "platform": "DESKTOP",
                "timeZone": timezone,
            },
            "user": {"lockedSafetyMode": False},
            "request": {"useSsl": True},
        }

        candidates = [_build_panel_params(video_id)]
        panel_match = _PANEL_PARAMS_RE.search(watch.text)
        if panel_match and panel_match.group(1) not in candidates:
            candidates.append(panel_match.group(1))

        for params in candidates:
            response = client.post(
                "https://www.youtube.com/youtubei/v1/get_panel?prettyPrint=false",
                json={
                    "context": context,
                    "panelId": "PAmodern_transcript_view",
                    "params": params,
                },
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Visitor-Id": visitor_data,
                },
            )
            if response.status_code in {403, 429}:
                raise TranscriptError("YouTube rate limited get_panel", rate_limited=True)
            if response.status_code >= 400:
                continue
            cues = _parse_panel_cues(response.json())
            if cues:
                return cues

        raise TranscriptError(
            "YouTube get_panel returned no transcript cues",
            reason="no_captions",
        )


def fetch_transcript_api(
    video_id: str,
    api_key: str,
    *,
    language: str = "en",
) -> list[dict[str, Any]]:
    if not api_key:
        raise TranscriptError("TRANSCRIPTAPI_API_KEY is not configured")
    with httpx.Client(timeout=90.0, follow_redirects=True) as client:
        response = client.get(
            "https://transcriptapi.com/api/v2/youtube/transcript",
            params={
                "video_url": video_id,
                "language": language,
                "format": "json",
                "include_timestamp": "true",
            },
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if response.status_code == 429:
        raise TranscriptError("TranscriptAPI rate limited the request", rate_limited=True)
    if response.status_code == 404:
        # 404 from TranscriptAPI means the video has no captions at all.
        raise TranscriptError(
            f"TranscriptAPI returned 404: {response.text[:200]}",
            reason="no_captions",
        )
    if response.status_code >= 400:
        raise TranscriptError(
            f"TranscriptAPI returned {response.status_code}: {response.text[:200]}"
        )
    data = response.json()
    rows = data if isinstance(data, list) else data.get("transcript") or data.get("segments") or []
    cues = [
        {"start": float(row.get("start") or 0), "text": str(row.get("text") or "").strip()}
        for row in rows
        if isinstance(row, dict) and str(row.get("text") or "").strip()
    ]
    if not cues:
        raise TranscriptError("TranscriptAPI returned no transcript cues", reason="no_captions")
    return cues


def fetch_transcript(
    video_id: str,
    *,
    api_key: str,
    timezone: str = "America/New_York",
    language: str = "en",
) -> tuple[list[dict[str, Any]], str]:
    try:
        return fetch_innertube_panel(video_id, timezone=timezone), "youtube_innertube"
    except Exception as exc:
        log.warning("Innertube transcript failed video_id=%s: %s", video_id, exc)
        try:
            return (
                fetch_transcript_api(video_id, api_key, language=language),
                "transcriptapi",
            )
        except TranscriptError as fallback_exc:
            rate_limited = bool(getattr(exc, "rate_limited", False)) or fallback_exc.rate_limited
            primary_reason = getattr(exc, "reason", "error")
            fallback_reason = fallback_exc.reason
            combined_reason = (
                "no_captions"
                if primary_reason == "no_captions" and fallback_reason == "no_captions"
                else ("rate_limited" if rate_limited else "error")
            )
            raise TranscriptError(
                f"Innertube failed: {exc}; TranscriptAPI failed: {fallback_exc}",
                rate_limited=rate_limited,
                reason=combined_reason,
            ) from fallback_exc


def _format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def build_pdf(
    cues: list[dict[str, Any]],
    *,
    title: str,
    video_id: str,
) -> tuple[bytes, str]:
    try:
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    except ModuleNotFoundError as exc:
        raise TranscriptError(
            "reportlab is not installed (pip install reportlab)"
        ) from exc

    buffer = BytesIO()
    styles = getSampleStyleSheet()
    document = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        rightMargin=0.65 * inch,
        leftMargin=0.65 * inch,
        topMargin=0.65 * inch,
        bottomMargin=0.65 * inch,
        title=title,
        author="Sentinel",
    )
    story = [
        Paragraph(title or f"YouTube transcript {video_id}", styles["Title"]),
        Paragraph(f"Video ID: {video_id}", styles["Normal"]),
        Spacer(1, 0.2 * inch),
    ]
    plain_text: list[str] = []
    for cue in cues:
        text = str(cue.get("text") or "").strip()
        if not text:
            continue
        timestamp = _format_timestamp(float(cue.get("start") or 0))
        plain_text.append(f"[{timestamp}] {text}")
        escaped = (
            text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        story.append(Paragraph(f"<b>{timestamp}</b> &nbsp; {escaped}", styles["BodyText"]))
        story.append(Spacer(1, 0.06 * inch))
    if not plain_text:
        raise TranscriptError("Cannot build a PDF from an empty transcript")
    document.build(story)
    return buffer.getvalue(), "\n".join(plain_text)


def upload_pdf(
    *,
    url: str,
    token: str,
    worker_id: str,
    pdf: bytes,
    meeting_id: str | None,
    video_id: str,
    language: str,
    text: str,
) -> None:
    if not url:
        raise TranscriptError("Transcript callback_url is missing")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Worker-Id": worker_id,
    }
    data = {
        "meeting_id": meeting_id or "",
        "video_id": video_id,
        "language": language,
        "text": text,
    }
    files = {"file": (f"{video_id}.pdf", pdf, "application/pdf")}
    with httpx.Client(timeout=180.0) as client:
        response = client.post(url, headers=headers, data=data, files=files)
    if response.status_code == 429:
        raise TranscriptError("Command rate limited transcript upload", rate_limited=True)
    if response.status_code >= 400:
        raise TranscriptError(
            f"Command transcript upload returned {response.status_code}: {response.text[:300]}"
        )


def post_failure(
    *,
    url: str,
    token: str,
    worker_id: str,
    meeting_id: str | None,
    video_id: str | None,
    error: str,
    rate_limited: bool,
    reason: str | None = None,
) -> None:
    if not url:
        log.error("Transcript fail_url missing: %s", error)
        return
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Worker-Id": worker_id,
        "Content-Type": "application/json",
    }
    body = {
        "worker_id": worker_id,
        "meeting_id": meeting_id,
        "video_id": video_id,
        "error": error,
        "rate_limited": rate_limited,
        "reason": reason or ("rate_limited" if rate_limited else "error"),
    }
    try:
        with httpx.Client(timeout=90.0) as client:
            response = client.post(url, headers=headers, json=body)
        if response.status_code >= 400:
            log.error("Transcript fail callback returned %s: %s", response.status_code, response.text[:300])
    except Exception:
        log.exception("Transcript fail callback failed")


def run_transcript_job(payload: dict[str, Any], env: dict[str, str], worker_id: str) -> dict[str, Any]:
    video_id = extract_video_id(payload.get("video_id"), payload.get("video_url"))
    api_key = (
        env.get("TRANSCRIPTAPI_API_KEY")
        or env.get("TRANSCRIPTAPI_KEY")
        or ""
    ).strip()
    cues, provider = fetch_transcript(
        video_id,
        api_key=api_key,
        timezone=str(payload.get("timezone") or "America/New_York"),
        language=str(payload.get("language") or "en"),
    )
    pdf, text = build_pdf(
        cues,
        title=str(payload.get("title") or f"YouTube transcript {video_id}"),
        video_id=video_id,
    )
    upload_pdf(
        url=str(payload.get("callback_url") or ""),
        token=env.get("WORKER_SHARED_TOKEN") or env.get("WORKER_TOKEN") or "",
        worker_id=worker_id,
        pdf=pdf,
        meeting_id=payload.get("meeting_id"),
        video_id=video_id,
        language=str(payload.get("language") or "en"),
        text=text,
    )
    return {
        "worker_id": worker_id,
        "ok": True,
        "load_type": "transcript",
        "job_id": payload.get("job_id"),
        "meeting_id": payload.get("meeting_id"),
        "video_id": video_id,
        "provider": provider,
        "cue_count": len(cues),
        "coordinator_relayed": True,
        "terminal": True,
    }
