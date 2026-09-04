#!/usr/bin/env python3
"""
Single-page control panel: one button to manually fire ssd-backup.sh (see
that script for the full rationale -- backs up each restic repo on the
2x4TB SSD pair, then replicates the whole finished tree to the replica
drive). Normally runs nightly via bjtn's crontab; this is a supplement
for "I just want tonight's backup done now" without waiting for cron.

Runs directly on the host (not containerized) as bjtn's own systemd
--user service, same reasoning as supernote-control: the script needs
broad access to /mnt/vault, /mnt/games, and both backup SSDs, plus the
host's already-configured restic install -- replicating all of that into
a new container (new restic install, half a dozen new mounts) for no new
capability wasn't worth it over just reusing what's already there.

Double-click safety comes from the script's own flock
(/tmp/ssd-backup.lock) -- a second trigger while one's already in flight
just hits the lock and exits fast.
"""
import datetime
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACKUP_SCRIPT = "/home/bjtn/docker/scripts/ssd-backup.sh"
LOG = "/home/bjtn/logs/ssd-backup.log"
LAST_TRIGGERED_FILE = "/home/bjtn/logs/backup-control-last-triggered.json"

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SSD Backup</title>
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
        white-space: pre-wrap; word-break: break-word; max-height: 40vh; overflow-y: auto; }
  h3 { color: #999; font-weight: 500; margin: 0 0 -8px; }
  #info { font-size: 0.8rem; color: #777; text-align: left; max-width: 700px; width: 90vw;
          background: #1a1a1a; padding: 12px 16px; border-radius: 10px; line-height: 1.5;
          box-sizing: border-box; }
  #info code { color: #9db8d8; }
  #lastrun { font-size: 0.8rem; color: #888; }
</style>
</head>
<body>
  <div id="info">Runs <code>ssd-backup.sh</code> -- backs up every restic
    repo on the 2x4TB SSD pair (each repo's own source path, e.g. Immich,
    Nextcloud, books, ROM saves), runs integrity checks, then replicates
    the whole finished tree to the replica drive. Can take a while for a
    large incremental backup. Also runs nightly via cron regardless of
    this button.<br>
    Last triggered: <span id="lastrun">loading...</span></div>
  <button id="go" onclick="go()">💾 Run SSD Backup</button>
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
async function refresh() {
  try {
    const l = await fetch('/log').then(r => r.text());
    document.getElementById('log').textContent = l || '(nothing yet)';
  } catch (e) { document.getElementById('log').textContent = 'error: ' + e; }
}
async function go() {
  const btn = document.getElementById('go');
  btn.disabled = true;
  try {
    await fetch('/trigger', {method: 'POST'});
  } catch (e) {}
  setTimeout(() => { btn.disabled = false; }, 3000);
  refresh();
  refreshMeta();
}
refresh();
refreshMeta();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


def tail_log(n=100) -> str:
    try:
        with open(LOG) as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except FileNotFoundError:
        return ""
    except Exception as e:
        return f"error reading log: {e}"


def start_run():
    subprocess.Popen([BACKUP_SCRIPT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      stdin=subprocess.DEVNULL, start_new_session=True)
    # ssd-backup.sh writes its own log via `tee`-equivalent `log()` already --
    # nothing more to capture here.


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
        elif self.path == "/log":
            self._text(tail_log())
        elif self.path == "/meta":
            data = json.dumps({"last_triggered": get_last_triggered()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/trigger":
            record_trigger()
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
    server = ThreadingHTTPServer(("0.0.0.0", 8770), Handler)
    server.serve_forever()
