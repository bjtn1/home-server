#!/usr/bin/env python3
"""
Single-page control panel: one button to manually fire the same
Missing/Cutoff-Unmet searches that arr-cutoff-search.sh already runs on
its own 6h systemd timer (see ~/docker/scripts/arr-cutoff-search.sh for
the full history/rationale). This is a supplement, not a replacement --
the timer keeps running on its own schedule regardless of whether this
button is ever clicked.

Reuses the exact same overlap-safety idea as that script: check each
app's own command queue first and skip firing if the same search type is
already in flight, so a click here can never stack a duplicate search on
top of one the timer (or a previous click) already queued.
"""
import json
import urllib.request
import os
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RADARR_URL = os.environ["RADARR_URL"]  # e.g. http://radarr:7878
RADARR_KEY = os.environ["RADARR_KEY"]
SONARR_URL = os.environ["SONARR_URL"]  # e.g. http://sonarr:8989
SONARR_KEY = os.environ["SONARR_KEY"]

# /state is a bind mount (see docker-compose.yml) so "last triggered" and
# the console log both survive container recreation, not just a restart.
LAST_TRIGGERED_FILE = "/state/last-triggered.txt"
CONSOLE_LOG = "/state/console.log"
PROGRESS_FILE = "/state/progress.json"

SEARCHES = [
    ("Radarr", RADARR_URL, RADARR_KEY, "MissingMoviesSearch"),
    ("Radarr", RADARR_URL, RADARR_KEY, "CutoffUnmetMoviesSearch"),
    ("Sonarr", SONARR_URL, SONARR_KEY, "MissingEpisodeSearch"),
    ("Sonarr", SONARR_URL, SONARR_KEY, "CutoffUnmetEpisodeSearch"),
]

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Arr Searches</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #111; color: #eee;
         display: flex; flex-direction: column; align-items: center; justify-content: center;
         height: 100vh; margin: 0; gap: 24px; }
  button { font-size: 1.5rem; padding: 24px 48px; border-radius: 16px; border: none;
           cursor: pointer; font-weight: 600; width: 80vw; max-width: 420px;
           background: #2d7de0; color: white; }
  button:disabled { background: #444; color: #999; cursor: default; }
  #status { font-size: 0.9rem; color: #999; min-height: 5em; text-align: left;
            white-space: pre-line; max-width: 420px; }
  #info { font-size: 0.8rem; color: #777; text-align: left; max-width: 420px;
          background: #1a1a1a; padding: 12px 16px; border-radius: 10px; line-height: 1.5; }
  #info code { color: #9db8d8; }
  #lastrun { font-size: 0.8rem; color: #888; }
  h3 { color: #999; font-weight: 500; margin: 12px 0 -8px; align-self: flex-start;
       margin-left: calc(50% - 210px); }
  pre { font-size: 0.85rem; color: #ccc; text-align: left; max-width: 420px; width: 80vw;
        background: #1a1a1a; padding: 16px; border-radius: 12px; overflow-x: auto;
        white-space: pre-wrap; word-break: break-word; max-height: 40vh; overflow-y: auto; }
  #barwrap { width: 80vw; max-width: 420px; height: 10px; background: #1a1a1a;
             border-radius: 6px; overflow: hidden; }
  #barfill { height: 100%; width: 0%; background: #2d7de0; transition: width 0.4s ease; }
  #barlabel { font-size: 0.8rem; color: #999; }
</style>
</head>
<body>
  <div id="info">Fires the same Missing-content + Cutoff-Unmet searches that
    <code>arr-cutoff-search.sh</code> already runs on its own 6h systemd timer
    -- this is a manual supplement, not a replacement; the timer keeps
    running regardless. Safe to click any time, skips any search already
    in flight rather than stacking a duplicate.<br>
    Last triggered: <span id="lastrun">loading...</span></div>
  <button id="go" onclick="go()">🔍 Trigger Missing / Cutoff Searches</button>
  <div id="status">click the button to fire a search</div>
  <div id="barwrap" hidden><div id="barfill"></div></div>
  <div id="barlabel"></div>
  <h3>Console</h3>
  <pre id="log">loading...</pre>
<script>
function fmt(iso) {
  if (!iso) return 'never';
  return new Date(iso).toLocaleString(undefined, {hour12: false});
}
async function refreshMeta() {
  try {
    const j = await fetch('/meta').then(r => r.json());
    document.getElementById('lastrun').textContent = fmt(j.last_triggered);
  } catch (e) {}
}
async function refreshLog() {
  try {
    const l = await fetch('/log').then(r => r.text());
    document.getElementById('log').textContent = l || '(nothing yet)';
  } catch (e) { document.getElementById('log').textContent = 'error: ' + e; }
}
async function refreshProgress() {
  const wrap = document.getElementById('barwrap');
  const label = document.getElementById('barlabel');
  try {
    const j = await fetch('/progress').then(r => r.json());
    if (!j.running && j.done === j.total) { wrap.hidden = true; label.textContent = ''; return; }
    wrap.hidden = false;
    const pct = j.total ? Math.round(100 * j.done / j.total) : 0;
    document.getElementById('barfill').style.width = pct + '%';
    label.textContent = `${j.done} of ${j.total} searches done`;
  } catch (e) {}
}
async function go() {
  const btn = document.getElementById('go');
  const status = document.getElementById('status');
  btn.disabled = true;
  status.textContent = 'working...';
  try {
    const r = await fetch('/trigger', {method: 'POST'});
    const j = await r.json();
    status.textContent = j.lines.join('\\n');
  } catch (e) {
    status.textContent = 'request failed: ' + e;
  }
  btn.disabled = false;
  refreshMeta();
  refreshLog();
  refreshProgress();
}
refreshMeta();
refreshLog();
refreshProgress();
setInterval(refreshLog, 5000);
setInterval(refreshProgress, 5000);
</script>
</body>
</html>"""


def api(method, base, key, path, body=None):
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Api-Key", key)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def already_running(base, key, command):
    try:
        commands = api("GET", base, key, "/api/v3/command")
    except Exception:
        return False  # if we can't tell, don't block the trigger -- worst case a harmless duplicate queue entry
    return any(c.get("name") == command and c.get("status") in ("started", "queued") for c in commands)


def trigger_one(name, base, key, command):
    # Returns (label, tracked) -- tracked is {base, key, id} for /progress to
    # poll if a new command was actually queued, or None if there's nothing
    # new to wait on (already running, or the fire itself failed).
    if already_running(base, key, command):
        return f"{name} ({command}): skipped, already running", None
    try:
        result = api("POST", base, key, "/api/v3/command", {"name": command})
        cid = result.get("id")
        return f"{name} ({command}): {result.get('status', '?')}", (
            {"base": base, "key": key, "id": cid} if cid is not None else None
        )
    except Exception as e:
        return f"{name} ({command}): FAILED - {e}", None


def record_progress(tracked):
    try:
        with open(PROGRESS_FILE, "w") as f:
            json.dump({"total": len(SEARCHES), "tracked": tracked}, f)
    except Exception:
        pass


def get_progress() -> dict:
    try:
        with open(PROGRESS_FILE) as f:
            state = json.load(f)
    except Exception:
        return {"done": 0, "total": len(SEARCHES), "running": False}
    total = state.get("total", len(SEARCHES))
    tracked = state.get("tracked", [])
    done = total - len(tracked)  # skipped/failed-to-fire entries already count as resolved
    still_running = False
    for t in tracked:
        try:
            c = api("GET", t["base"], t["key"], f"/api/v3/command/{t['id']}")
            if c.get("status") in ("completed", "failed"):
                done += 1
            else:
                still_running = True
        except Exception:
            done += 1  # can't tell anymore -- don't hang the bar on it forever
    return {"done": min(done, total), "total": total, "running": still_running}


def record_trigger():
    try:
        with open(LAST_TRIGGERED_FILE, "w") as f:
            f.write(datetime.datetime.now(datetime.timezone.utc).isoformat())
    except Exception:
        pass  # best-effort -- never let this break the actual trigger


def get_last_triggered():
    try:
        with open(LAST_TRIGGERED_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def append_log(lines):
    try:
        with open(CONSOLE_LOG, "a") as f:
            for line in lines:
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"[{ts}] {line}\n")
    except Exception:
        pass  # best-effort -- never let this break the actual trigger


def tail_log(n=200) -> str:
    try:
        with open(CONSOLE_LOG) as f:
            return "".join(reversed(f.readlines()[-n:]))  # newest first
    except FileNotFoundError:
        return ""
    except Exception as e:
        return f"error reading log: {e}"


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/meta":
            self._json({"last_triggered": get_last_triggered()})
        elif self.path == "/log":
            data = tail_log().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/progress":
            self._json(get_progress())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/trigger":
            record_trigger()
            results = [trigger_one(*s) for s in SEARCHES]
            lines = [r[0] for r in results]
            tracked = [r[1] for r in results if r[1] is not None]
            append_log(lines)
            record_progress(tracked)
            self._json({"lines": lines})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8767), Handler)
    server.serve_forever()
