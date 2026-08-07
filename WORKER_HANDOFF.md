# Worker handoff — Command (coordinator) protocol

Give this document to the **scrape / transcript worker** team.  
Command = Sentinel License Registry (this repo). Workers talk **only** to the Command public URL. Never call one-off dynos from Command.

Replace placeholders:

| Placeholder | Example |
|-------------|---------|
| `COORDINATOR_URL` | `https://sentinel-registry.example.com` |
| `WORKER_PUBLIC_URL` | `https://scrape-worker.example.com` |
| `WORKER_ID` | `scrape-worker-1` |
| `WORKER_SHARED_TOKEN` | same secret as Command `WORKER_SHARED_TOKEN` |

---

## 1. Roles (do not change)

```
Command ──POST /v1/commands/*──► Worker web (middleman)
Worker  ──POST /v1/workers/*───► Command

Worker may spawn Heroku one-offs / local threads internally.
Command never addresses those dynos.
Ignore Command → one-off links. Ignore worker-internal /v1/internal/... from Command’s POV.
```

**HTTP semantics**

| Code | Meaning |
|------|---------|
| **202** | Accepted (queued / will run). **Not** “Playwright started”. |
| **200** | OK (sync done or accepted — treat like accepted if job continues via callback). |
| **4xx/5xx** | Reject — Command marks job failed / transcript fail. |

Job is **done** only when worker posts **result** / **fail** (or transcript PDF / transcript fail). Do **not** mark Command-side work complete on 202 alone.

---

## 2. Auth (every worker → Command call)

```http
Authorization: Bearer <WORKER_SHARED_TOKEN>
X-Worker-Id: <WORKER_ID>
Content-Type: application/json   # except transcript PDF upload (multipart)
```

Command → worker command pushes use the same bearer on the worker’s `/v1/commands/*` routes.

---

## 3. Lifecycle

### 3.1 Register (once / on boot)

`POST {COORDINATOR_URL}/v1/workers/register`

```json
{
  "worker_id": "scrape-worker-1",
  "base_url": "https://scrape-worker.example.com",
  "token": "<WORKER_SHARED_TOKEN>",
  "capacity": 50,
  "oneoff_limit": 50,
  "allowed_sources": ["*"]
}
```

- Treat the worker as a **pool**, not a single runner.
- Advertise `capacity` / `oneoff_limit` ≈ max parallel one-offs (e.g. 50).
- `base_url` must be reachable from Command (no trailing slash required; Command strips it).

### 3.2 Heartbeat (~every 60s)

`POST {COORDINATOR_URL}/v1/workers/heartbeat`

Worker default: `WORKER_HEARTBEAT_SECONDS=60`.

```json
{
  "worker_id": "scrape-worker-1",
  "load": 7,
  "capacity": 50,
  "oneoff_limit": 50,
  "oneoff_running": 5,
  "load_by_type": {
    "scrape": 4,
    "stream_status": 3,
    "transcript": 0
  },
  "queued_by_type": {
    "scrape": 2,
    "stream_status": 0,
    "transcript": 0
  },
  "running_job_ids": ["job_abc", "job_def"],
  "running_jobs": [
    { "job_id": "job_abc", "load_type": "scrape" },
    { "job_id": "job_def", "load_type": "stream_status" }
  ],
  "reason": "heartbeat"
}
```

**Required semantics for Command pool fill**

- `oneoff_running` = dynos/threads actually executing.
- `queued_by_type.scrape` = accepted but not yet running (middleman queue).
- Command free slots ≈ `oneoff_limit - oneoff_running - queued_scrape`.
- `reason`: `heartbeat` | `idle` | `job_finished`.

Command may return `dispatch` with more scrape work when slots are free. Process it; do not require `load === 0`.

### 3.3 Cancel

Worker accepts cancel pushes today:

- `POST {WORKER_PUBLIC_URL}/v1/commands/{job_id}/cancel`
- `POST {WORKER_PUBLIC_URL}/v1/commands/cancel` with `{"job_id":"..."}`

Cancels queued work or kills a running one-off when possible. Lease expiry + fail/result still apply if cancel races a completion.

---

## 4. Sample: schedule scrape

### Command → Worker

`POST {WORKER_PUBLIC_URL}/v1/commands/scrape`

```http
Authorization: Bearer <WORKER_SHARED_TOKEN>
Content-Type: application/json
```

```json
{
  "job_id": "xYz9AbCdEfGh",
  "source_id": "austin-tx",
  "callback_url": "https://sentinel-registry.example.com/v1/workers/jobs/xYz9AbCdEfGh/result",
  "scrape_request": {
    "url": "https://www.youtube.com/@CityOfAustinTX/streams",
    "mode": "dedicated",
    "schedule_type": "youtube_table",
    "timezone": "America/Chicago",
    "filter_by_categories": true,
    "youtube_fallback": {
      "channel_url": "https://www.youtube.com/@CityOfAustinTX/streams",
      "on_primary_failure": "same_day_stub",
      "match": "title_date",
      "require_title_match": true
    }
  }
}
```

**Worker must**

1. Return **202** immediately after queueing (preferred for Playwright / one-offs). Do not await Heroku spawn or Playwright on the accept path.
2. Run scrape on a one-off / thread.
3. POST results to `callback_url` (or `/v1/workers/jobs/{job_id}/fail`).
4. Treat Command **4xx** on result/fail (e.g. `409 job not active`) as permanent — do **not** buffer/retry those forever on heartbeat.

**Mode resolution (worker):** `mode=auto` + `schedule_type` set → dedicated parser only. LLM runs only when `mode=llm`, or `mode=auto` with **no** `schedule_type`. Dedicated failure does **not** fall through to LLM; optional `youtube_fallback` may still run.

Calendar-style scrape body may look like:

```json
{
  "url": "https://example.gov/meetings",
  "mode": "dedicated",
  "schedule_type": "wordpress_table",
  "timezone": "America/New_York",
  "filter_by_categories": true,
  "youtube_fallback": {
    "channel_url": "https://www.youtube.com/@ExampleCity/streams",
    "on_primary_failure": "same_day_stub",
    "match": "title_date",
    "require_title_match": true
  }
}
```

#### `require_title_match` (all YouTube handling)

Command always sets `require_title_match: true` whenever it includes a
`youtube_fallback`, including YouTube-first, YouTube-only, and
calendar-then-YouTube requests. Workers must not fall back to date-only matching
for requests from Command.

On a calendar scrape the calendar owns the meeting list, and the channel is only
used to attach a video to a meeting that already exists. When
`require_title_match` is true the worker must attach a video **only** when the
video title matches the meeting title as well as the date. Matching on date
alone pins the same video onto every meeting held that day, which is wrong.

This applies to **every** calendar `schedule_type` (`civicclerk_table`,
`primegov_table`, `wordpress_table`, and any other dedicated calendar parser) —
not one vendor alone.

Title matching evaluates all of these strategies and uses the strongest safe
match: normalized exact match, phrase containment, keyword intersection,
token Jaccard, and fuzzy token similarity. Non-exact matches normally require
at least two shared meaningful words; high-confidence fuzzy matching also
recovers misspellings. This prevents generic overlaps such as `City Council`
versus `County Council`.

Date matching is also bounded by `max_meeting_age_hours` (default `24`): a
channel video is only date-matched onto meetings newer than that, so a
back-dated VOD cannot be pinned onto a meeting that finished days ago. An exact
`video_id` match still refreshes status at any age. Send `0` to allow backfill
onto older meetings.

Keep title and date separate. The title is used only by the title matcher. The
video date must come from YouTube's structured `ytInitialData` card metadata:
modern `metadataParts` or legacy `publishedTimeText` (`scheduled_time` for
upcoming, `published_time` for live/concluded). Never infer the video date from
numbers or date-looking text in its title.

Expected unmatched calendar meeting (any vendor) — **omit** video fields entirely
(do not send `null` / empty / channel / guessed values):

```json
{
  "Meeting name": "Planning and Zoning Commission",
  "Scheduled time": "2026-08-05T22:00:00Z",
  "Status": "Upcoming",
  "Agenda link": "https://example.gov/meetings/123/agenda",
  "user_live_link": "https://example.gov/meetings/123"
}
```

`user_live_link` remains the calendar/portal event page (CivicClerk media page,
PrimeGov event page, WordPress meeting page, etc.). It is not a playable stream
and must not be copied into `Meeting link`. If a title-and-date match is later
found, the matched result may add the YouTube `Meeting link`, `video_id`, and
`Stream type` while preserving `user_live_link`.

### Unified YouTube architecture

All scrape/fallback/monitor paths go through `scrape_worker/youtube_core/`:

| Layer | Role |
|-------|------|
| `client.py` | Playwright/HTML `ytInitialData` fetch, consent, `/streams`+`/videos`, operation-scoped cache |
| `parser.py` | `lockupViewModel` + legacy `videoRenderer`; dates only from structured metadata |
| `matching.py` | Ranking: video_id → title strategies → structured-date proximity |
| `service.py` | Channel snapshot, stale-live skip, calendar overlay, stream-status |
| `schedule/library/youtube.py` | Thin adapters: `youtube_table`, `youtube_table_la` (SAP filter), `youtube_table_md` (next per day) |
| `utils/youtube.py` | Compatibility facade for dedicated parsers (download/API helpers retained) |

Command job IDs + leases replace WallFly’s Bubble claim/verify. Ranking and
Maryland/LA policies are retained; the legacy-only WallFly scraper is not.

A meeting with no matching video must be returned with **no** `Meeting link`,
`video_id`, or `Stream type` — an unmatched meeting is a valid result, not a
failure. The coordinator drops any video that arrives on more than one meeting
in the same payload, so forced matches lose the video for the real meeting too.

`on_primary_failure: "same_day_stub"` is unaffected: it applies only when the
calendar scrape itself fails and the channel becomes the source of meetings.

### Worker → Command (success)

`POST {COORDINATOR_URL}/v1/workers/jobs/xYz9AbCdEfGh/result`

```json
{
  "worker_id": "scrape-worker-1",
  "ok": true,
  "load_type": "scrape",
  "job_id": "xYz9AbCdEfGh",
  "meetings": [
    {
      "Meeting name": "City Council Regular Meeting",
      "Scheduled time": "2026-07-29T18:00:00Z",
      "Status": "Live",
      "Meeting link": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
      "video_id": "dQw4w9WgXcQ",
      "user_live_link": "https://www.austintexas.gov/department/city-council/meetings",
      "Stream type": "ts_youtube"
    },
    {
      "Meeting name": "Zoning Commission",
      "Scheduled time": "2026-07-28T17:00:00Z",
      "Status": "Adjourned",
      "Meeting link": "https://www.youtube.com/watch?v=E7l6sYLIXl0",
      "video_id": "E7l6sYLIXl0",
      "Stream type": "ts_youtube"
    }
  ],
  "meta": {
    "source_id": "austin-tx",
    "scrape_ms": 45210
  }
}
```

**Status strings Command understands on upsert:** `Live` / `Upcoming` / `Adjourned` (and similar). Adjourned + `video_id` → transcript queue.

### Worker → Command (failure)

`POST {COORDINATOR_URL}/v1/workers/jobs/xYz9AbCdEfGh/fail`

```json
{
  "worker_id": "scrape-worker-1",
  "job_id": "xYz9AbCdEfGh",
  "ok": false,
  "load_type": "scrape",
  "error": "Playwright timeout after 180s"
}
```

---

## 5. Sample: monitoring (stream-status)

### Command → Worker

`POST {WORKER_PUBLIC_URL}/v1/commands/stream-status`

```json
{
  "job_id": "mon_7KpQ2nR4sT",
  "meeting_id": "austin-tx:dqw4w9wgxcq",
  "channel_url": "https://www.youtube.com/@CityOfAustinTX/streams",
  "video_id": "dQw4w9WgXcQ",
  "video_url": null,
  "timezone": "America/Chicago",
  "callback_url": "https://sentinel-registry.example.com/v1/workers/jobs/mon_7KpQ2nR4sT/result",
  "poll_interval_seconds": 60,
  "max_duration_seconds": 28800
}
```

**Worker must**

1. Return **202** (accepted).
2. Poll YouTube / channel page every `poll_interval_seconds`.
3. On **each** poll, POST a result (partial). Command renews the lease.
4. When terminal, POST a final result with terminal `status` — Command completes the job.
5. One active monitor per `meeting_id` (Command enforces exclusivity).

### Worker → Command (poll / still live)

`POST {COORDINATOR_URL}/v1/workers/jobs/mon_7KpQ2nR4sT/result`

```json
{
  "worker_id": "scrape-worker-1",
  "ok": true,
  "load_type": "stream_status",
  "job_id": "mon_7KpQ2nR4sT",
  "meeting_id": "austin-tx:dqw4w9wgxcq",
  "channel_url": "https://www.youtube.com/@CityOfAustinTX/streams",
  "timezone": "America/Chicago",
  "status": "live",
  "video_id": "dQw4w9WgXcQ",
  "video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "video_title": "City Council Regular Meeting",
  "meeting_link": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "scheduled_time": "2026-07-29T18:00:00Z",
  "note": null,
  "live_videos": [{ "id": "dQw4w9WgXcQ", "title": "City Council Regular Meeting" }],
  "upcoming_videos": [],
  "concluded_on_page": []
}
```

### Terminal statuses (job ends — no retry loop)

| `status` | Command maps to | Job |
|----------|-----------------|-----|
| `live` | live | continues |
| `upcoming` / `scheduled` | upcoming | continues |
| `fetch_failed` / `unknown` | *(ignore / keep lease)* | **continues** — page could not be read; do **not** treat as adjourned |
| `concluded` / `adjourned` / `ended` / `timeout` | adjourned | **done** |
| **`skipped`** | **no status change** | **done** (e.g. 24/7 stream — do **not** keep polling) |

#### `skipped` does not adjourn the meeting

`skipped` ends the poll job, but Command leaves the meeting status untouched and
does **not** queue a transcript. Declining to watch a stream is not evidence the
meeting ended. The next scrape of that source resolves real status from the
channel page.

`fetch_failed` is returned when ytInitialData is missing/blocked after a poll.
Command should renew the lease and wait for the next poll — never mark the
meeting adjourned from a failed fetch alone. Successful absence of the owned
`video_id` from the Live tab is still `concluded` (DetectEnd parity).

Poll results may include `started_streaming_on`, `published_time`,
`skipped_videos`, and `match_diagnostics` (video-id continuity / candidate
counts). Title and date remain independent fields.

#### Worker requirement — accurate live start time for the skip guard

The “live stream older than 24h” guard (`YOUTUBE_MAX_LIVE_AGE_HOURS`, default 24)
must use the watch page’s real start timestamp:

`ytInitialPlayerResponse.microformat.playerMicroformatRenderer.liveBroadcastDetails.startTimestamp`

Do **not** use `publishDate` (broadcast created date) or unrelated
“Started streaming on …” strings scraped from channel/`ytInitialData` card text —
those misfire on discrete meetings (observed 2026-07-30: same-day lives reported
as Dec 2023 / Jun–Jul 2026 from wrong elements). Only skip when
`startTimestamp` is genuinely more than 24h old.

Example final (skipped):

```json
{
  "worker_id": "scrape-worker-1",
  "ok": true,
  "load_type": "stream_status",
  "job_id": "mon_7KpQ2nR4sT",
  "meeting_id": "austin-tx:dqw4w9wgxcq",
  "status": "skipped",
  "video_id": "dQw4w9WgXcQ",
  "note": "24/7 channel stream — not a discrete meeting"
}
```

Example final (concluded):

```json
{
  "worker_id": "scrape-worker-1",
  "ok": true,
  "load_type": "stream_status",
  "job_id": "mon_7KpQ2nR4sT",
  "meeting_id": "austin-tx:dqw4w9wgxcq",
  "status": "concluded",
  "video_id": "dQw4w9WgXcQ",
  "video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "video_title": "City Council Regular Meeting",
  "meeting_link": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
}
```

After adjourned/concluded, Command marks the meeting **transcript-eligible** and may dispatch a transcript job. A `skipped` result does not.

---

## 6. Sample: transcripts

Command does **not** generate captions. Worker generates a **PDF** and uploads it.

### 6.1 Command → Worker

`POST {WORKER_PUBLIC_URL}/v1/commands/transcript`

```json
{
  "meeting_id": "austin-tx:e7l6sylixl0",
  "video_id": "E7l6sYLIXl0",
  "video_url": "https://www.youtube.com/watch?v=E7l6sYLIXl0",
  "title": "City Council Regular Meeting",
  "source_id": "austin-tx",
  "callback_url": "https://sentinel-registry.example.com/v1/workers/transcripts",
  "fail_url": "https://sentinel-registry.example.com/v1/workers/transcripts/fail"
}
```

Return **202** after queueing a one-off. Cold dyno + PDF build can be slow — do not block the accept response.

### 6.2 How to obtain the transcript (worker implementation)

**Preferred chain**

1. **YouTube Innertube `get_panel`** (same path as the page “Show transcript” button).  
2. On empty / block / parse failure → **[TranscriptAPI](https://transcriptapi.com/docs/api/)**.  
3. Build a simple timestamped PDF.  
4. Upload to Command.

#### Path A — Innertube `get_panel`

```http
POST https://www.youtube.com/youtubei/v1/get_panel?prettyPrint=false
Content-Type: application/json
```

Body shape (trim/refresh `context` from a real browser session; do not hardcode forever):

```json
{
  "context": {
    "client": {
      "hl": "en",
      "gl": "US",
      "clientName": "WEB",
      "clientVersion": "2.20260728.01.00",
      "visitorData": "<from browser>",
      "userAgent": "Mozilla/5.0 ...",
      "platform": "DESKTOP",
      "timeZone": "America/New_York"
    },
    "user": { "lockedSafetyMode": false },
    "request": { "useSsl": true }
  },
  "panelId": "PAmodern_transcript_view",
  "params": "<protobuf/base64 embedding video_id — rebuild per video>"
}
```

Example: for `video_id=E7l6sYLIXl0`, a working `params` decoded to bytes containing that id (see coordinator samples `payload.json` / `response.json` in the registry repo).

**Parse cues from the response** (modern UI):

```
content
  .engagementPanelSectionListRenderer
  .content.sectionListRenderer
  .contents[0].itemSectionRenderer
  .contents[]
    .macroMarkersPanelItemViewModel
    .item.timelineItemViewModel
      .timestamp                          // "0:00"
      .contentItems[0].transcriptSegmentViewModel.simpleText
    + sibling watchEndpoint.startTimeSeconds   // 0, 9, 16, ...
```

Normalize to:

```json
[
  { "start": 0, "text": "Alpha says they're going to pay you..." },
  { "start": 9, "text": "And I'll show you why in this video..." }
]
```

**Caveats:** needs fresh `visitorData` / client version; cloud IPs often blocked; schema can drift. Treat failures as “try Path B”.

#### Path B — TranscriptAPI fallback

Docs: https://transcriptapi.com/docs/api/

```bash
curl -G "https://transcriptapi.com/api/v2/youtube/transcript" \
  --data-urlencode "video_url=E7l6sYLIXl0" \
  -H "Authorization: Bearer <TRANSCRIPTAPI_KEY>"
```

Optional free probe: `GET /api/v2/youtube/info?video_url=...`

Response cues: `{ "text", "start", "duration" }[]`.

Env on worker: `TRANSCRIPTAPI_API_KEY` (or `TRANSCRIPTAPI_KEY`).

### 6.3 Worker → Command (PDF success)

`POST {COORDINATOR_URL}/v1/workers/transcripts`  
**multipart/form-data** (not JSON):

```bash
curl -X POST "$COORDINATOR_URL/v1/workers/transcripts" \
  -H "Authorization: Bearer $WORKER_SHARED_TOKEN" \
  -H "X-Worker-Id: $WORKER_ID" \
  -F "file=@/tmp/E7l6sYLIXl0.pdf;type=application/pdf" \
  -F "meeting_id=austin-tx:e7l6sylixl0" \
  -F "video_id=E7l6sYLIXl0" \
  -F "language=en" \
  -F "text=optional plain text dump of cues" \
  -F 'cues=[{"start":0,"text":"Opening remarks…","duration":3.2},{"start":9,"text":"Next item…"}]'
```

Rules:

- Provide **`meeting_id` and/or `video_id`** (Command resolves either).
- `file` must be a real PDF (`%PDF` magic or `.pdf` / `application/pdf`).
- Optional `text` = concatenated cue text for search/debug.
- Optional `cues` = JSON array of timed segments for public-feed clips:
  `[{"start": 12.5, "text": "…", "duration": 3.2}, …]`
  (`duration` optional; also accepted if `text` lines look like `0:09 spoken words`).
- On success Command sets `has_transcript`, stores PDF, updates feed `transcript_url`,
  and persists cues when provided.

There is **no** separate “clip” command. Clips use the same transcript upload path
via the `cues` field (receive → process → return is covered by §6.1–6.4).

### 6.4 Worker → Command (failure)

`POST {COORDINATOR_URL}/v1/workers/transcripts/fail`

```json
{
  "worker_id": "scrape-worker-1",
  "meeting_id": "austin-tx:e7l6sylixl0",
  "video_id": "E7l6sYLIXl0",
  "error": "Innertube blocked; TranscriptAPI 404 no captions",
  "rate_limited": false,
  "reason": "no_captions"
}
```

| `reason` | Meaning |
|----------|---------|
| `no_captions` | Both Innertube and TranscriptAPI agree the video has no captions — final, no retry needed |
| `rate_limited` | Upstream throttling — Command cools the transcript queue (~30 minutes) |
| `error` | Unexpected failure |

Set `"rate_limited": true` when YouTube / upstream is IP-throttling — Command cools the transcript queue (~30 minutes).

---

## 7. Worker endpoints Command expects

| Method | Path | Role |
|--------|------|------|
| `POST` | `/v1/commands/scrape` | Accept scrape job → **202** |
| `POST` | `/v1/commands/stream-status` | Accept monitor job → **202** |
| `POST` | `/v1/commands/transcript` | Accept transcript job → **202** |
| `GET` | `/health` | Liveness |

Internal one-off URLs are **not** registered with Command.

---

## 8. Timeouts / leases (Command side)

| Setting | Typical | Meaning |
|---------|---------|---------|
| Scrape accept timeout | 180s | HTTP to middleman only |
| Monitor accept timeout | 120s | HTTP to middleman only |
| Scrape job lease | 300s | Renew via heartbeat / running_jobs |
| Monitor poll lease | ~3× poll interval | Renew on each poll result |
| Transcript wait after dispatch | ~1800s | Re-dispatch if no PDF/fail |

Loosen worker cold-start: accept fast (202), do heavy work async.

---

## 9. Checklist before go-live

- [ ] Register with `capacity` / `oneoff_limit` ≈ 50 (not hard-coded 1).
- [ ] Heartbeat ~60s reports `oneoff_running`, `queued_by_type`, `load_by_type`.
- [ ] All three commands return **202** when queued (accept path does not await spawn).
- [ ] Scrape result posts meetings; adjourned + video_id feeds transcript queue.
- [ ] Every `youtube_fallback` uses `require_title_match: true` (title+date only).
- [ ] Unmatched calendar meetings omit `Meeting link` / `video_id` / `Stream type`; portal URL stays in `user_live_link`.
- [ ] Monitor posts polls; **`skipped`** ends the job (no infinite loop) and does **not** adjourn / queue transcript.
- [ ] Skip guard uses watch-page `liveBroadcastDetails.startTimestamp` (not publishDate / wrong card text).
- [ ] Monitor treats **`fetch_failed` / `unknown`** as non-terminal (keep lease; do not adjourn).
- [ ] Dates never come from video titles — only structured YouTube card metadata.
- [ ] Transcript: Innertube → TranscriptAPI → PDF → multipart upload including optional `cues` JSON.
- [ ] Transcript fail posts to `/v1/workers/transcripts/fail` (set `rate_limited` / `reason` when appropriate).
- [ ] Same `WORKER_SHARED_TOKEN` on Command and worker; `COORDINATOR_URL` / `WORKER_PUBLIC_URL` correct.
- [ ] No Command calls to one-off dyno URLs.

---

## 10. Quick curl cheat sheet

```bash
# Register
curl -sS -X POST "$COORDINATOR_URL/v1/workers/register" \
  -H "Content-Type: application/json" \
  -d "{\"worker_id\":\"$WORKER_ID\",\"base_url\":\"$WORKER_PUBLIC_URL\",\"token\":\"$WORKER_SHARED_TOKEN\",\"capacity\":50,\"oneoff_limit\":50,\"allowed_sources\":[\"*\"]}"

# Heartbeat
curl -sS -X POST "$COORDINATOR_URL/v1/workers/heartbeat" \
  -H "Authorization: Bearer $WORKER_SHARED_TOKEN" \
  -H "X-Worker-Id: $WORKER_ID" \
  -H "Content-Type: application/json" \
  -d "{\"worker_id\":\"$WORKER_ID\",\"load\":0,\"oneoff_running\":0,\"oneoff_limit\":50,\"capacity\":50,\"load_by_type\":{\"scrape\":0,\"stream_status\":0,\"transcript\":0},\"queued_by_type\":{\"scrape\":0,\"stream_status\":0,\"transcript\":0},\"running_jobs\":[],\"reason\":\"idle\"}"

# Simulate Command → worker scrape accept (on worker host)
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$WORKER_PUBLIC_URL/v1/commands/scrape" \
  -H "Authorization: Bearer $WORKER_SHARED_TOKEN" \
  -H "Content-Type: application/json" \
  -d @scrape_command.json
```

---

*Aligned with sentinel-license coordinator + scrape-worker as of 2026-08-06: pool capacity, 202=accepted, skipped=terminal (no adjourn), require_title_match on all youtube_fallback, transcript fail `reason`, transcript multipart `cues` for feed clips (no separate clip job).*
