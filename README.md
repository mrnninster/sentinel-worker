# Sentinel scrape worker

Standalone FastAPI **middleman** that accepts coordinator push commands, dispatches each job to a **Heroku one-off dyno** (or a **local thread**), and relays results. Up to **50** concurrent loads per worker (`HEROKU_ONEOFF_LIMIT`).

Embedded schedule scraper (Playwright + dedicated parsers + optional LLM) runs inside each job process/thread — not on the web dyno under load.

## Local

```bash
cd scrape_worker
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && playwright install chromium
cp .env.example .env   # set WORKER_* / COORDINATOR_* / OPENAI_API_KEY as needed

uvicorn main:app --env-file .env --host 127.0.0.1 --port 8100 --reload
```

From the repo root:

```bash
uvicorn main:app --app-dir scrape_worker --env-file scrape_worker/.env --host 127.0.0.1 --port 8100 --reload
```

OpenAPI: http://127.0.0.1:8100/docs

Local default: `DISPATCH_MODE=auto` with no Heroku API key → each job runs in a **daemon thread** and callbacks to `/v1/internal/jobs/{id}/result`.

### Env

One `.env` in this directory (see `.env.example`). Important keys:

| Key | Notes |
|-----|--------|
| `WORKER_ID` | Unique per process |
| `WORKER_SHARED_TOKEN` | Same value as coordinator (`WORKER_TOKEN` alias OK) |
| `COORDINATOR_URL` | Registry base URL |
| `WORKER_PUBLIC_URL` | This worker’s public base URL (callback target for jobs) |
| `WORKER_REGISTRATION_OPEN` | `1` to self-register on boot |
| `HEROKU_ONEOFF_LIMIT` / `WORKER_CAPACITY` | Max concurrent jobs (**default 50**) |
| `RESERVE_SCRAPE` / `RESERVE_TRANSCRIPT` | Reserved slots (default `1` each) so monitors cannot fill the pool |
| `DISPATCH_MODE` | `auto` (Heroku if configured, else local) \| `local` \| `heroku` |
| `HEROKU_API_KEY` / `HEROKU_APP_NAME` | Required on Heroku to spawn one-offs |
| `HEROKU_DYNO_SIZE` | One-off size (`standard-1x` default) |
| `INTERNAL_CALLBACK_TOKEN` | Job→middleman Bearer (defaults to shared token) |
| `TRANSCRIPTAPI_API_KEY` | TranscriptAPI fallback when YouTube Innertube is blocked |
| `WORKER_HEARTBEAT_SECONDS` | Periodic heartbeat interval (default `30`) |
| `OPENAI_API_KEY` | Needed for LLM scrape mode |
| `SCRAPER_MODE` | `embedded` (default) or `http` |

**Capacity:** every worker advertises and enforces up to **50** loads. Stream-status has priority; **1** scrape and **1** transcript slot stay reserved. On Heroku, each running job is a separate one-off dyno (cost scales with concurrency).

## Coordinator protocol

All worker → coordinator calls use:

```http
Authorization: Bearer {WORKER_SHARED_TOKEN}
X-Worker-Id: {WORKER_ID}
Content-Type: application/json
```

### Heartbeat / load

```http
POST {COORDINATOR_URL}/v1/workers/heartbeat
```

Sent:

- every `WORKER_HEARTBEAT_SECONDS` (`reason: "heartbeat"`)
- when a job finishes (`reason: "idle"` if nothing left, else `"job_finished"`)

Example:

```json
{
  "worker_id": "scrape-worker-1",
  "reason": "heartbeat",
  "load": 14,
  "capacity": 50,
  "oneoff_limit": 50,
  "oneoff_running": 12,
  "load_by_type": {
    "scrape": 1,
    "stream_status": 13,
    "transcript": 0
  },
  "queued_by_type": {
    "scrape": 0,
    "stream_status": 2,
    "transcript": 0
  },
  "running_job_ids": ["…"],
  "running_jobs": [
    { "job_id": "…", "load_type": "stream_status" }
  ]
}
```

`load_type` values: `"scrape"` | `"stream_status"` | `"transcript"` (transcript reserved only).

**Command should push while `oneoff_running < capacity`**, not only when `load === 0`. Treat `202` as accepted/queued (may wait for a free slot).

### Schedule scrape

**Command → worker**

```http
POST {WORKER_PUBLIC_URL}/v1/commands/scrape
```

```json
{
  "job_id": "...",
  "source_id": "...",
  "scrape_request": {
    "url": "https://…",
    "timezone": "America/Chicago",
    "mode": "dedicated",
    "schedule_type": "swagit_table"
  },
  "callback_url": null
}
```

Accepted with `202` (`accepted: true`). Work runs on a one-off/thread; middleman relays the result to the coordinator.

**Worker → coordinator**

```http
POST {COORDINATOR_URL}/v1/workers/jobs/{job_id}/result
```

```json
{
  "worker_id": "scrape-worker-1",
  "ok": true,
  "meetings": [ /* display-key meeting dicts */ ],
  "meta": {}
}
```

Then an idle/load heartbeat is sent.

### Stream-status monitor

Long-running poll of a YouTube channel until the video is `concluded` / `adjourned`, or `max_duration_seconds` (default 8h).

**Command → worker**

```http
POST {WORKER_PUBLIC_URL}/v1/commands/stream-status
```

```json
{
  "job_id": "...",
  "meeting_id": "...",
  "channel_url": "https://www.youtube.com/@Handle/streams",
  "video_id": "FRhtCgsIPRU",
  "video_url": null,
  "timezone": "America/New_York",
  "poll_interval_seconds": 60,
  "max_duration_seconds": 28800
}
```

Include a real `video_id` or `video_url`. Without one, status is `channel_snapshot` and the loop will not exit on concluded.

**Worker → coordinator** (each poll)

```http
POST {COORDINATOR_URL}/v1/workers/jobs/{job_id}/result
```

```json
{
  "worker_id": "scrape-worker-1",
  "ok": true,
  "load_type": "stream_status",
  "job_id": "...",
  "meeting_id": "...",
  "channel_url": "...",
  "timezone": "America/New_York",
  "status": "live",
  "video_id": "...",
  "video_url": null,
  "video_title": "...",
  "meeting_link": "https://www.youtube.com/watch?v=...",
  "scheduled_time": null,
  "started_streaming_on": null,
  "note": null,
  "live_videos": [],
  "upcoming_videos": [],
  "concluded_on_page": [],
  "skipped_videos": []
}
```

Terminal statuses that end the monitor loop: `concluded`, `adjourned`, `skipped` (always-on live older than 24h).

On hard failure: `POST …/jobs/{job_id}/fail` with `meeting_id`, `job_id`, `error`.

Cancel: `POST {WORKER_PUBLIC_URL}/v1/commands/{job_id}/cancel`.

### Transcript PDF

**Command → worker**

```http
POST {WORKER_PUBLIC_URL}/v1/commands/transcript
```

```json
{
  "meeting_id": "austin-tx:e7l6sylixl0",
  "video_id": "E7l6sYLIXl0",
  "video_url": "https://www.youtube.com/watch?v=E7l6sYLIXl0",
  "title": "City Council Regular Meeting",
  "source_id": "austin-tx",
  "callback_url": "https://registry.example.com/v1/workers/transcripts",
  "fail_url": "https://registry.example.com/v1/workers/transcripts/fail"
}
```

The worker returns `202`, then:

1. requests YouTube's modern Innertube `get_panel` transcript;
2. falls back to TranscriptAPI when YouTube is blocked or returns no cues;
3. renders timestamped cues to a PDF;
4. uploads the PDF as multipart form data to `callback_url`.

Failures are posted to `fail_url` with `rate_limited: true` when an upstream
returns HTTP 429. The internal one-off callback only releases the worker pool
slot; Command continues to receive transcript PDF/failure on its dedicated
transcript endpoints.

### YouTube scrape notes

For `schedule_type=youtube_table` (+ optional `youtube_fallback`):

- Each of `/streams` and `/videos` is fetched **at most once** per scrape job; paths filter a shared classification.
- Stream-status polls **clear** that cache each check so status can change to `concluded`.
- Live cards with **Started streaming on …** (or “Started streaming N days ago”) older than **24 hours** are treated as always-on streams and **skipped** (not tracked as meeting lives). Override with `YOUTUBE_MAX_LIVE_AGE_HOURS`. Monitor jobs exit with `status: "skipped"`.

Prefer dedicated parsers (`schedule_type` set) over LLM when possible. LLM mode always uses Playwright.

## Standalone scraper API

Without the worker/coordinator layer:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
```

```bash
curl -s http://127.0.0.1:8080/scrape \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://fortbendcountytx.new.swagit.com/views/502/",
    "timezone": "America/Chicago",
    "mode": "dedicated",
    "schedule_type": "swagit_table"
  }'
```

## Layout

```text
main.py                 Middleman FastAPI (commands / queue / relay)
dispatch/               Pool, Heroku one-off + local thread spawners
jobs/runner.py          One-off/thread entrypoint (scrape | stream_status | transcript)
jobs/transcript.py      Innertube/TranscriptAPI retrieval + PDF upload
scraper_bridge.py       embedded | http scrape bridge
app/                    Schedule scrape FastAPI + LLM pipeline
schedule/library/       Dedicated platform parsers
utils/                  HTML / Playwright / YouTube helpers
.env.example            All env vars
```

## Docker / Render (optional)

Build context is this directory. See `Dockerfile` and `render.yaml` if you prefer containers.

```bash
docker build -t sentinel-scrape-worker .
docker run --rm -p 8100:10000 \
  -e WORKER_ID=scrape-worker-1 \
  -e WORKER_SHARED_TOKEN=dev \
  -e COORDINATOR_URL=http://host.docker.internal:8099 \
  -e WORKER_PUBLIC_URL=http://127.0.0.1:8100 \
  -e HEROKU_ONEOFF_LIMIT=50 \
  -e PORT=10000 \
  sentinel-scrape-worker
```

Give each replica a unique `WORKER_ID`. Set secrets: `WORKER_SHARED_TOKEN`, `COORDINATOR_URL`, `WORKER_PUBLIC_URL`, optional `OPENAI_API_KEY`.

## Heroku (GitHub deploy, no Docker)

This app uses **classic buildpacks** (Python + Apt for Chromium libs), not the container stack.

The **web** dyno is the middleman only. Jobs run as **one-off** dynos (`python -m jobs.runner`) spawned via the Platform API — set `HEROKU_API_KEY` and `HEROKU_APP_NAME`.

| File | Purpose |
|------|---------|
| `Procfile` | `web` process → uvicorn middleman |
| `runtime.txt` | Python 3.12 |
| `Aptfile` | OS libs for Playwright Chromium |
| `bin/post_compile` | `playwright install chromium` on each build |
| `app.json` | App metadata / suggested config for GitHub |
| `scripts/setup-heroku.sh` | Create app, set stack + buildpacks + defaults |

**One-time setup**

```bash
cd scrape_worker   # this directory must be the GitHub repo root
./scripts/setup-heroku.sh sentinel-scrape-worker-1

heroku config:set -a sentinel-scrape-worker-1 \
  WORKER_SHARED_TOKEN=… \
  COORDINATOR_URL=https://… \
  HEROKU_API_KEY=… \
  HEROKU_APP_NAME=sentinel-scrape-worker-1 \
  HEROKU_ONEOFF_LIMIT=50

# Prefer ≥ standard-1x — Playwright will OOM on tiny dynos
heroku ps:type standard-1x -a sentinel-scrape-worker-1
```

Then in the Heroku dashboard: **Deploy → Connect to GitHub** → enable auto-deploy → **Deploy Branch**.

After deploy, confirm:

```bash
curl -sS https://sentinel-scrape-worker-1.herokuapp.com/health
```

Ensure `WORKER_PUBLIC_URL` matches that `https://…herokuapp.com` URL (the setup script sets it by default). Health should show `"capacity": 50` and `"dispatch_mode": "heroku"`.
