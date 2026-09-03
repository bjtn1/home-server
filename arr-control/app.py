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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RADARR_URL = os.environ["RADARR_URL"]  # e.g. http://radarr:7878
RADARR_KEY = os.environ["RADARR_KEY"]
SONARR_URL = os.environ["SONARR_URL"]  # e.g. http://sonarr:8989
SONARR_KEY = os.environ["SONARR_KEY"]

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
</style>
</head>
<body>
  <button id="go" onclick="go()">🔍 Trigger Missing / Cutoff Searches</button>
  <div id="status">Fires both Sonarr and Radarr missing-content + upgrade searches. Safe to click any time -- skips any search that's already running.</div>
<script>
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
}
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
    if already_running(base, key, command):
        return f"{name} ({command}): skipped, already running"
    try:
        result = api("POST", base, key, "/api/v3/command", {"name": command})
        return f"{name} ({command}): {result.get('status', '?')}"
    except Exception as e:
        return f"{name} ({command}): FAILED - {e}"


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
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/trigger":
            lines = [trigger_one(*s) for s in SEARCHES]
            self._json({"lines": lines})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8767), Handler)
    server.serve_forever()
