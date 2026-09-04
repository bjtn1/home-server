#!/usr/bin/env python3
"""
Single-page control panel: one button to fire castilian-queue.sh run
(see ~/docker/scripts/castilian-queue.sh for the queue itself). A run can
take hours (large downloads), so /trigger starts it in the background and
returns immediately rather than blocking the request -- the page polls
/status to show progress instead.

Double-click safety comes from the queue script's own flock (see its `run`
case) -- this app doesn't need to track "is it running" itself, a second
trigger while one's already in flight just hits the lock and exits fast,
which shows up as a normal log line.
"""
import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

QUEUE_SCRIPT = "/scripts/castilian-queue.sh"
RUN_LOG = "/tmp/castilian-queue-run.log"

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Castilian Queue</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #111; color: #eee;
         display: flex; flex-direction: column; align-items: center; justify-content: flex-start;
         min-height: 100vh; margin: 0; gap: 20px; padding: 32px 16px; box-sizing: border-box; }
  button { font-size: 1.5rem; padding: 24px 48px; border-radius: 16px; border: none;
           cursor: pointer; font-weight: 600; width: 80vw; max-width: 420px;
           background: #2d7de0; color: white; }
  button:disabled { background: #444; color: #999; cursor: default; }
  pre { font-size: 0.85rem; color: #ccc; text-align: left; max-width: 700px; width: 90vw;
        background: #1a1a1a; padding: 16px; border-radius: 12px; overflow-x: auto;
        white-space: pre-wrap; word-break: break-word; }
  h3 { color: #999; font-weight: 500; margin: 0 0 -8px; }
</style>
</head>
<body>
  <button id="go" onclick="go()">&#127464;&#127466; Run Castilian Queue</button>
  <h3>Queue status</h3>
  <pre id="status">loading...</pre>
  <h3>Recent log</h3>
  <pre id="log">loading...</pre>
<script>
async function go() {
  const btn = document.getElementById('go');
  btn.disabled = true;
  try {
    await fetch('/trigger', {method: 'POST'});
  } catch (e) {}
  setTimeout(() => { btn.disabled = false; }, 3000);
  refresh();
}
async function refresh() {
  try {
    const s = await fetch('/status').then(r => r.text());
    document.getElementById('status').textContent = s || '(empty queue)';
  } catch (e) { document.getElementById('status').textContent = 'error: ' + e; }
  try {
    const l = await fetch('/log').then(r => r.text());
    document.getElementById('log').textContent = l || '(nothing yet)';
  } catch (e) { document.getElementById('log').textContent = 'error: ' + e; }
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


def run_status() -> str:
    try:
        r = subprocess.run([QUEUE_SCRIPT, "status"], capture_output=True, text=True, timeout=20)
        return r.stdout + r.stderr
    except Exception as e:
        return f"error running status: {e}"


def tail_log(n=60) -> str:
    try:
        with open(RUN_LOG) as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except FileNotFoundError:
        return ""
    except Exception as e:
        return f"error reading log: {e}"


def start_run():
    with open(RUN_LOG, "a") as f:
        f.write("\n----- triggered -----\n")
        f.flush()
        subprocess.Popen([QUEUE_SCRIPT, "run"], stdout=f, stderr=subprocess.STDOUT,
                          stdin=subprocess.DEVNULL, start_new_session=True)


class Handler(BaseHTTPRequestHandler):
    def _text(self, body: str, code=200):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/status":
            self._text(run_status())
        elif self.path == "/log":
            self._text(tail_log())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/trigger":
            try:
                start_run()
                self._text("started")
            except Exception as e:
                self._text(f"failed to start: {e}", code=500)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8768), Handler)
    server.serve_forever()
