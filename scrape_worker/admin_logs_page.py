"""Admin home page: realtime process logs (SSE) with filters + download."""

from __future__ import annotations

import json
from html import escape


def logs_login_html(*, error: str | None = None) -> str:
    err = (
        f'<p class="err">{escape(error)}</p>'
        if error
        else '<p class="hint">Enter the worker admin token to view recent logs.</p>'
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Worker logs</title>
  <style>
    :root {{
      --bg: #12141a;
      --panel: #1a1d26;
      --text: #e8eaef;
      --muted: #8b93a7;
      --accent: #3d9c7a;
      --err: #d96b6b;
      --border: #2a3040;
      --mono: "IBM Plex Mono", "SFMono-Regular", Consolas, monospace;
      --sans: "IBM Plex Sans", "Segoe UI", system-ui, sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; min-height: 100vh; display: grid; place-items: center;
      background: radial-gradient(1200px 600px at 10% -10%, #1e2433, var(--bg));
      color: var(--text); font-family: var(--sans);
    }}
    form {{
      width: min(420px, 92vw); background: var(--panel); border: 1px solid var(--border);
      padding: 1.5rem; border-radius: 8px;
    }}
    h1 {{ margin: 0 0 .35rem; font-size: 1.25rem; letter-spacing: .02em; }}
    .hint, .err {{ margin: 0 0 1rem; color: var(--muted); font-size: .9rem; }}
    .err {{ color: var(--err); }}
    label {{ display: block; font-size: .8rem; color: var(--muted); margin-bottom: .35rem; }}
    input[type=password] {{
      width: 100%; padding: .7rem .75rem; border-radius: 6px; border: 1px solid var(--border);
      background: #10131a; color: var(--text); font-family: var(--mono); font-size: .95rem;
    }}
    button {{
      margin-top: 1rem; width: 100%; padding: .7rem; border: 0; border-radius: 6px;
      background: var(--accent); color: #04120c; font-weight: 600; cursor: pointer;
    }}
  </style>
</head>
<body>
  <form method="get" action="/">
    <h1>Worker logs</h1>
    {err}
    <label for="token">Admin token</label>
    <input id="token" name="token" type="password" autocomplete="current-password" required autofocus/>
    <button type="submit">Open logs</button>
  </form>
</body>
</html>"""


def logs_viewer_html(*, token: str, worker_id: str) -> str:
    tok = escape(token)
    wid = escape(worker_id)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Worker logs · {wid}</title>
  <style>
    :root {{
      --bg: #0f1116;
      --panel: #171a22;
      --text: #e7eaf1;
      --muted: #8b93a7;
      --border: #2a3040;
      --accent: #3d9c7a;
      --live: #4ecf96;
      --debug: #6b7280;
      --info: #7eb6ff;
      --warning: #e0b35a;
      --error: #e07a7a;
      --mono: "IBM Plex Mono", "SFMono-Regular", Consolas, monospace;
      --sans: "IBM Plex Sans", "Segoe UI", system-ui, sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; height: 100vh; display: flex; flex-direction: column;
      background: var(--bg); color: var(--text); font-family: var(--sans);
    }}
    header {{
      flex: 0 0 auto; padding: .85rem 1rem; border-bottom: 1px solid var(--border);
      background: var(--panel); display: flex; flex-wrap: wrap; gap: .75rem 1rem; align-items: end;
    }}
    header h1 {{
      margin: 0; font-size: 1rem; font-weight: 600; letter-spacing: .03em;
      margin-right: auto; align-self: center;
    }}
    .meta {{ color: var(--muted); font-size: .75rem; align-self: center; max-width: 36rem; }}
    label {{ display: flex; flex-direction: column; gap: .25rem; font-size: .7rem; color: var(--muted); }}
    input, select {{
      min-width: 9rem; padding: .4rem .5rem; border-radius: 5px; border: 1px solid var(--border);
      background: #10131a; color: var(--text); font-family: var(--mono); font-size: .8rem;
    }}
    input#q {{ min-width: 14rem; }}
    button {{
      padding: .45rem .8rem; border: 0; border-radius: 5px; background: var(--accent);
      color: #04120c; font-weight: 600; cursor: pointer; height: 2rem;
    }}
    button.ghost {{
      background: transparent; color: var(--muted); border: 1px solid var(--border);
    }}
    #status {{ font-size: .75rem; color: var(--muted); align-self: center; }}
    #status.live {{ color: var(--live); }}
    main {{
      flex: 1 1 auto; overflow: auto; padding: .5rem 0; font-family: var(--mono);
      font-size: .78rem; line-height: 1.45;
    }}
    .row {{
      display: grid; grid-template-columns: 11.5rem 4.5rem minmax(7rem, 12rem) 1fr;
      gap: .55rem; padding: .2rem 1rem; border-bottom: 1px solid #151922;
      white-space: pre-wrap; word-break: break-word;
    }}
    .row:hover {{ background: #151922; }}
    .ts {{ color: var(--muted); }}
    .lvl {{ font-weight: 700; }}
    .lvl.DEBUG {{ color: var(--debug); }}
    .lvl.INFO {{ color: var(--info); }}
    .lvl.WARNING {{ color: var(--warning); }}
    .lvl.ERROR, .lvl.CRITICAL {{ color: var(--error); }}
    .logger {{ color: #9aa3b5; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .empty {{ padding: 2rem 1rem; color: var(--muted); text-align: center; }}
    .note {{
      flex: 0 0 auto; padding: .4rem 1rem; border-top: 1px solid var(--border);
      color: var(--muted); font-size: .7rem; background: var(--panel);
    }}
    @media (max-width: 800px) {{
      .row {{ grid-template-columns: 1fr; gap: .15rem; }}
      input#q {{ min-width: 10rem; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Logs · {wid}</h1>
    <span class="meta" id="meta" title="Live view keeps a sliding window of the newest 500 matching lines in the browser. Download pulls daily files for the last 7 calendar days.">
      Connecting…
    </span>
    <label>Lookback
      <select id="lookback">
        <option value="1">1 hour</option>
        <option value="6" selected>6 hours</option>
        <option value="24">24 hours</option>
      </select>
    </label>
    <label>Level
      <select id="level">
        <option value="">All</option>
        <option value="DEBUG">DEBUG+</option>
        <option value="INFO" selected>INFO+</option>
        <option value="WARNING">WARNING+</option>
        <option value="ERROR">ERROR+</option>
      </select>
    </label>
    <label>Keyword
      <input type="search" id="q" placeholder="job_id, scrape, error…"/>
    </label>
    <button type="button" id="apply">Apply</button>
    <button type="button" class="ghost" id="download" title="Download on-disk logs for the last 7 days (including today)">Download 7d</button>
    <label style="flex-direction:row;align-items:center;gap:.4rem;align-self:center;">
      <input type="checkbox" id="live" checked/> Live
    </label>
    <span id="status">idle</span>
  </header>
  <main id="log"></main>
  <div class="note">
    Logs are written to disk and survive reloads. The live view tails that file over one SSE
    connection and keeps only the newest <strong>500</strong> matching lines in the browser
    (older rows drop off as new ones arrive). <strong>Download 7d</strong> exports today + 6 prior days.
    Heroku disks are ephemeral across dyno restarts; one-off dyno logs need <code>heroku logs</code>.
  </div>
  <script>
    const TOKEN = {json.dumps(token)};
    const MAX_VISIBLE_LINES = 500;
    const root = document.getElementById("log");
    const statusEl = document.getElementById("status");
    const metaEl = document.getElementById("meta");
    let source = null;
    let stickBottom = true;
    let shownCount = 0;
    let seen = new Set();

    root.addEventListener("scroll", () => {{
      const gap = root.scrollHeight - root.scrollTop - root.clientHeight;
      stickBottom = gap < 48;
    }});

    function esc(s) {{
      return String(s).replace(/[&<>"']/g, (c) => ({{
        "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
      }})[c]);
    }}

    function entryKey(e) {{
      return String(e.seq != null ? e.seq : (e.ts + "|" + e.message));
    }}

    function buildRow(e) {{
      const row = document.createElement("div");
      row.className = "row";
      row.dataset.key = entryKey(e);
      row.innerHTML =
        '<span class="ts">' + esc(e.iso) + '</span>' +
        '<span class="lvl ' + esc(e.level) + '">' + esc(e.level) + '</span>' +
        '<span class="logger" title="' + esc(e.logger) + '">' + esc(e.logger) + '</span>' +
        '<span class="msg">' + esc(e.message) + '</span>';
      return row;
    }}

    function clearLogView(message) {{
      root.replaceChildren();
      seen.clear();
      shownCount = 0;
      if (message) {{
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = message;
        root.appendChild(empty);
      }}
    }}

    function trimToMax() {{
      const rows = root.querySelectorAll(".row");
      const overflow = rows.length - MAX_VISIBLE_LINES;
      if (overflow <= 0) {{
        shownCount = rows.length;
        return;
      }}
      let removedHeight = 0;
      const removeUntil = Math.min(overflow, rows.length);
      for (let i = 0; i < removeUntil; i++) {{
        const row = rows[i];
        removedHeight += row.offsetHeight;
        const key = row.dataset.key;
        if (key) seen.delete(key);
        row.remove();
      }}
      shownCount = root.querySelectorAll(".row").length;
      if (!stickBottom && removedHeight) {{
        root.scrollTop = Math.max(0, root.scrollTop - removedHeight);
      }}
    }}

    function appendEntries(entries) {{
      if (!entries || !entries.length) return;
      const empty = root.querySelector(".empty");
      if (empty) empty.remove();
      const frag = document.createDocumentFragment();
      for (const e of entries) {{
        const key = entryKey(e);
        if (seen.has(key)) continue;
        seen.add(key);
        frag.appendChild(buildRow(e));
      }}
      if (frag.childNodes.length) {{
        root.appendChild(frag);
        trimToMax();
        if (stickBottom) root.scrollTop = root.scrollHeight;
      }}
    }}

    function updateMeta(stats) {{
      const hours = stats?.retention_hours ?? 6;
      const days = stats?.download_days ?? 7;
      const bytes = stats?.disk_bytes;
      const sizeLabel = (bytes == null)
        ? ""
        : (" · log file " + (bytes < 1024 ? bytes + " B" : (bytes/1024).toFixed(1) + " KB"));
      metaEl.textContent =
        shownCount + " / " + MAX_VISIBLE_LINES + " lines on screen (sliding window)" +
        sizeLabel + " · disk kept " + days + " days · lookback up to " + hours + "h";
    }}

    function setStatus(text, live) {{
      statusEl.textContent = text;
      statusEl.classList.toggle("live", !!live);
    }}

    function stopStream() {{
      if (source) {{
        source.close();
        source = null;
      }}
    }}

    function startStream() {{
      stopStream();
      clearLogView();
      const hours = parseFloat(document.getElementById("lookback").value || "6");
      const since = new Date(Date.now() - hours * 3600 * 1000).toISOString();
      const params = new URLSearchParams();
      params.set("token", TOKEN);
      params.set("since", since);
      params.set("limit", String(MAX_VISIBLE_LINES));
      const level = document.getElementById("level").value;
      const q = document.getElementById("q").value.trim();
      if (level) params.set("level", level);
      if (q) params.set("q", q);

      setStatus("Connecting…", false);
      source = new EventSource("/v1/admin/logs/stream?" + params.toString());

      source.addEventListener("snapshot", (ev) => {{
        const data = JSON.parse(ev.data);
        clearLogView();
        if (!(data.entries || []).length) {{
          clearLogView("No log lines match these filters yet — waiting for new lines…");
        }} else {{
          appendEntries(data.entries);
        }}
        updateMeta(data.stats);
        setStatus("Live", true);
      }});

      source.addEventListener("entries", (ev) => {{
        const data = JSON.parse(ev.data);
        appendEntries(data.entries || []);
        updateMeta(data.stats);
        setStatus("Live", true);
      }});

      source.addEventListener("ping", (ev) => {{
        try {{
          const data = JSON.parse(ev.data);
          updateMeta(data.stats);
        }} catch (_) {{}}
        setStatus("Live", true);
      }});

      source.onerror = () => {{
        setStatus("Reconnecting…", false);
      }};
    }}

    async function downloadLogs() {{
      const params = new URLSearchParams();
      params.set("token", TOKEN);
      params.set("days", "7");
      const level = document.getElementById("level").value;
      const q = document.getElementById("q").value.trim();
      if (level) params.set("level", level);
      if (q) params.set("q", q);
      setStatus("Downloading…", false);
      try {{
        const res = await fetch("/v1/admin/logs/download?" + params.toString(), {{
          cache: "no-store"
        }});
        if (!res.ok) {{
          setStatus("Download failed", false);
          return;
        }}
        const blob = await res.blob();
        const cd = res.headers.get("Content-Disposition") || "";
        const match = /filename=\"([^\"]+)\"/.exec(cd);
        const name = match ? match[1] : "worker-logs.log";
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        setStatus(document.getElementById("live").checked ? "Live" : "idle",
                  document.getElementById("live").checked);
      }} catch (err) {{
        setStatus("Download failed", false);
      }}
    }}

    document.getElementById("apply").addEventListener("click", () => {{
      if (document.getElementById("live").checked) startStream();
      else {{
        stopStream();
        setStatus("idle", false);
      }}
    }});
    document.getElementById("download").addEventListener("click", downloadLogs);
    document.getElementById("live").addEventListener("change", () => {{
      if (document.getElementById("live").checked) startStream();
      else {{
        stopStream();
        setStatus("Paused", false);
      }}
    }});
    document.getElementById("q").addEventListener("keydown", (ev) => {{
      if (ev.key === "Enter") document.getElementById("apply").click();
    }});

    if (document.getElementById("live").checked) startStream();
  </script>
</body>
</html>"""
