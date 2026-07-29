# Sentinel scrape worker

Standalone FastAPI worker with an embedded schedule scraper (Playwright + dedicated parsers + optional LLM).
Talks to the Sentinel coordinator (license registry) over HTTP: push commands, heartbeats, and job results.

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

### Env

One `.env` in this directory (see `.env.example`). Important keys:

| Key | Notes |
|-----|--------|
| `WORKER_ID` | Unique per process |
| `WORKER_SHARED_TOKEN` | Same value as coordinator (`WORKER_TOKEN` alias OK) |
| `COORDINATOR_URL` | Registry base URL |
| `WORKER_PUBLIC_URL` | This worker’s public base URL |
| `WORKER_REGISTRATION_OPEN` | `1` to self-register on boot |
| `WORKER_CAPACITY` | Reported capacity (keep `1` on small VMs; scrapes are serialized) |
| `WORKER_HEARTBEAT_SECONDS` | Periodic heartbeat interval (default `30`) |
| `OPENAI_API_KEY` | Needed for LLM scrape mode |
| `SCRAPER_MODE` | `embedded` (default) or `http` |
| `SCRAPER_URL` | Remote scraper base URL when `SCRAPER_MODE=http` |

On **512 MB / 0.5 vCPU**, run **one** Chromium scrape at a time (`WORKER_CAPACITY=1`). Scale by adding more worker processes, not by raising capacity.

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
- immediately when a job finishes (`reason: "idle"` if nothing left, else `"job_finished"`)

Example (stream-status monitor running, scrapes free):

```json
{
  "worker_id": "scrape-worker-1",
  "reason": "heartbeat",
  "load": 1,
  "load_by_type": {
    "scrape": 0,
    "stream_status": 1
  },
  "running_job_ids": ["XrHxPmJDazzC5eBj"],
  "running_jobs": [
    { "job_id": "XrHxPmJDazzC5eBj", "load_type": "stream_status" }
  ]
}
```

Fully idle:

```json
{
  "worker_id": "scrape-worker-1",
  "reason": "idle",
  "load": 0,
  "load_by_type": { "scrape": 0, "stream_status": 0 },
  "running_job_ids": [],
  "running_jobs": []
}
```

`load_type` values: `"scrape"` | `"stream_status"`.

Suggested dispatch: treat `load_by_type.scrape === 0` as free for schedule scrapes even if `stream_status > 0` (only if you accept shared Playwright on that host).

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

Scrapes are accepted with `202` and run under a lock (one Playwright scrape at a time).

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
main.py              Worker FastAPI (commands / heartbeats / results)
scraper_bridge.py    embedded | http scrape bridge
app/                 Schedule scrape FastAPI + LLM pipeline
schedule/library/    Dedicated platform parsers
utils/               HTML / Playwright / YouTube helpers
data/                Meeting categories, etc.
.env.example         All env vars
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
  -e PORT=10000 \
  sentinel-scrape-worker
```

Give each replica a unique `WORKER_ID`. Set secrets: `WORKER_SHARED_TOKEN`, `COORDINATOR_URL`, `WORKER_PUBLIC_URL`, optional `OPENAI_API_KEY`.

## Heroku (GitHub deploy, no Docker)

This app uses **classic buildpacks** (Python + Apt for Chromium libs), not the container stack.

| File | Purpose |
|------|---------|
| `Procfile` | `web` process → uvicorn |
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
  COORDINATOR_URL=https://…

# Prefer ≥ standard-1x — Playwright will OOM on tiny dynos
heroku ps:type standard-1x -a sentinel-scrape-worker-1
```

Then in the Heroku dashboard: **Deploy → Connect to GitHub** → enable auto-deploy → **Deploy Branch**.

After deploy, confirm:

```bash
curl -sS https://sentinel-scrape-worker-1.herokuapp.com/health
```

Ensure `WORKER_PUBLIC_URL` matches that `https://…herokuapp.com` URL (the setup script sets it by default).
