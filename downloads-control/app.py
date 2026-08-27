#!/usr/bin/env python3
"""
Single-page control panel: one button to pause SABnzbd + qBittorrent
together, one button to resume both. Talks to each app's own API --
doesn't touch containers, doesn't lose queue state, just pauses/resumes
in place exactly like clicking each app's own native pause button.
"""
import json
import os
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SABNZBD_URL = os.environ["SABNZBD_URL"]  # e.g. http://gluetun:8080
SABNZBD_API_KEY = os.environ["SABNZBD_API_KEY"]
QBIT_URL = os.environ["QBIT_URL"]  # e.g. http://gluetun:8181
QBIT_USER = os.environ["QBIT_USER"]
QBIT_PASS = os.environ["QBIT_PASS"]

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
</style>
</head>
<body>
  <div id="status">checking status...</div>
  <button id="pause" onclick="act('/pause')">⏸ Pause All Downloads</button>
  <button id="resume" onclick="act('/resume')">▶ Resume All Downloads</button>
<script>
async function act(path) {
  document.getElementById('status').textContent = 'working...';
  const r = await fetch(path, {method: 'POST'});
  const j = await r.json();
  document.getElementById('status').textContent = j.message;
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
setInterval(refresh, 15000);
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
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/pause":
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
            self._json({"message": msg})
        elif self.path == "/resume":
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
            self._json({"message": msg})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # keep container logs quiet


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8765), Handler)
    server.serve_forever()
