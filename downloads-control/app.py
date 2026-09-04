#!/usr/bin/env python3
"""
Single-page control panel: one button to pause SABnzbd + qBittorrent
together, one button to resume both. Talks to each app's own API --
doesn't touch containers, doesn't lose queue state, just pauses/resumes
in place exactly like clicking each app's own native pause button.
"""
import json
import os
import datetime
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SABNZBD_URL = os.environ["SABNZBD_URL"]  # e.g. http://gluetun:8080
SABNZBD_API_KEY = os.environ["SABNZBD_API_KEY"]
QBIT_URL = os.environ["QBIT_URL"]  # e.g. http://gluetun:8181
QBIT_USER = os.environ["QBIT_USER"]
QBIT_PASS = os.environ["QBIT_PASS"]

# /state is a bind mount (see docker-compose.yml) so "last triggered" and
# the console log both survive container recreation, not just a restart.
LAST_TRIGGERED_FILE = "/state/last-triggered.json"
CONSOLE_LOG = "/state/console.log"

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Downloads</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #111; color: #eee;
         display: flex; flex-direction: column; align-items: center; justify-content: center;
         height: 100vh; margin: 0; gap: 24px; }
  button { font-size: 1.5rem; padding: 24px 48px; border-radius: 16px; border: none;
           cursor: pointer; font-weight: 600; width: 80vw; max-width: 360px; }
  #pause { background: #c0392b; color: white; }
  #resume { background: #27ae60; color: white; }
  #status { font-size: 1rem; color: #999; min-height: 1.5em; text-align: center; }
  #info { font-size: 0.8rem; color: #777; text-align: left; max-width: 360px;
          background: #1a1a1a; padding: 12px 16px; border-radius: 10px; line-height: 1.5; }
  #lastrun { font-size: 0.8rem; color: #888; }
  h3 { color: #999; font-weight: 500; margin: 12px 0 -8px; }
  pre { font-size: 0.85rem; color: #ccc; text-align: left; max-width: 360px; width: 80vw;
        background: #1a1a1a; padding: 16px; border-radius: 12px; overflow-x: auto;
        white-space: pre-wrap; word-break: break-word; max-height: 40vh; overflow-y: auto; }
</style>
</head>
<body>
  <div id="info">Pauses/resumes SABnzbd and qBittorrent together via each
    app's own API -- no containers touched, no queue state lost, same as
    clicking each app's own native pause button.<br>
    Last triggered: <span id="lastrun">loading...</span></div>
  <div id="status">checking status...</div>
  <button id="pause" onclick="act('/pause')">⏸ Pause All Downloads</button>
  <button id="resume" onclick="act('/resume')">▶ Resume All Downloads</button>
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
    document.getElementById('lastrun').textContent =
      j.last_time ? fmt(j.last_time) + ' (' + j.last_action + ')' : 'never';
  } catch (e) {}
}
async function refreshLog() {
  try {
    const l = await fetch('/log').then(r => r.text());
    document.getElementById('log').textContent = l || '(nothing yet)';
  } catch (e) { document.getElementById('log').textContent = 'error: ' + e; }
}
async function act(path) {
  document.getElementById('status').textContent = 'working...';
  const r = await fetch(path, {method: 'POST'});
  const j = await r.json();
  document.getElementById('status').textContent = j.message;
  refreshMeta();
  refreshLog();
}
async function refresh() {
  try {
    const r = await fetch('/status');
    const j = await r.json();
    document.getElementById('status').textContent = j.message;
  } catch (e) {
    document.getElementById('status').textContent = 'status check failed';
  }
}
refresh();
refreshMeta();
refreshLog();
setInterval(refresh, 15000);
setInterval(refreshLog, 5000);
</script>
</body>
</html>"""


def sab_request(mode):
    url = f"{SABNZBD_URL}/api?mode={mode}&output=json&apikey={SABNZBD_API_KEY}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def sab_status():
    d = sab_request("queue")
    return d.get("queue", {}).get("paused", False)


def qbit_login():
    data = urllib.parse.urlencode({"username": QBIT_USER, "password": QBIT_PASS}).encode()
    req = urllib.request.Request(f"{QBIT_URL}/api/v2/auth/login", data=data)
    resp = urllib.request.urlopen(req, timeout=10)
    cookie = resp.headers.get("Set-Cookie", "")
    sid = cookie.split(";")[0] if cookie else ""
    return sid


def qbit_request(path, sid, method="GET", data=None):
    req = urllib.request.Request(f"{QBIT_URL}{path}", data=data, method=method)
    req.add_header("Cookie", sid)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read()


def qbit_pause_all():
    sid = qbit_login()
    # Newer qBittorrent WebAPI (v2.11+) renamed pause/resume to stop/start.
    qbit_request("/api/v2/torrents/stop", sid, method="POST",
                 data=urllib.parse.urlencode({"hashes": "all"}).encode())


def qbit_resume_all():
    sid = qbit_login()
    qbit_request("/api/v2/torrents/start", sid, method="POST",
                 data=urllib.parse.urlencode({"hashes": "all"}).encode())


def qbit_any_active():
    sid = qbit_login()
    body = qbit_request("/api/v2/torrents/info", sid)
    torrents = json.loads(body)
    return any(t.get("state") not in ("pausedDL", "pausedUP", "stoppedDL", "stoppedUP") for t in torrents)


def record_trigger(action):
    try:
        with open(LAST_TRIGGERED_FILE, "w") as f:
            json.dump({"time": datetime.datetime.now(datetime.timezone.utc).isoformat(), "action": action}, f)
    except Exception:
        pass  # best-effort -- never let this break the actual pause/resume


def get_last_triggered():
    try:
        with open(LAST_TRIGGERED_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def append_log(action, message):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(CONSOLE_LOG, "a") as f:
            f.write(f"[{ts}] {action}: {message}\n")
    except Exception:
        pass  # best-effort -- never let this break the actual pause/resume


def tail_log(n=200) -> str:
    try:
        with open(CONSOLE_LOG) as f:
            return "".join(f.readlines()[-n:])
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
        elif self.path == "/status":
            try:
                sab_paused = sab_status()
                qbit_active = qbit_any_active()
                if sab_paused and not qbit_active:
                    msg = "⏸ Everything paused"
                elif not sab_paused and qbit_active:
                    msg = "▶ Downloading"
                else:
                    msg = f"SABnzbd {'paused' if sab_paused else 'running'}, qBittorrent {'active' if qbit_active else 'idle/paused'}"
                self._json({"message": msg})
            except Exception as e:
                self._json({"message": f"status check failed: {e}"}, 500)
        elif self.path == "/meta":
            meta = get_last_triggered()
            self._json({"last_time": meta.get("time"), "last_action": meta.get("action")})
        elif self.path == "/log":
            data = tail_log().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/pause":
            record_trigger("paused")
            errors = []
            try:
                sab_request("pause")
            except Exception as e:
                errors.append(f"SABnzbd: {e}")
            try:
                qbit_pause_all()
            except Exception as e:
                errors.append(f"qBittorrent: {e}")
            msg = "⏸ Paused" if not errors else "Partial failure: " + "; ".join(errors)
            append_log("pause", msg)
            self._json({"message": msg})
        elif self.path == "/resume":
            record_trigger("resumed")
            errors = []
            try:
                sab_request("resume")
            except Exception as e:
                errors.append(f"SABnzbd: {e}")
            try:
                qbit_resume_all()
            except Exception as e:
                errors.append(f"qBittorrent: {e}")
            msg = "▶ Resumed" if not errors else "Partial failure: " + "; ".join(errors)
            append_log("resume", msg)
            self._json({"message": msg})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # keep container logs quiet


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8765), Handler)
    server.serve_forever()
