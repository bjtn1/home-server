#!/usr/bin/env python3
"""
Single-page control panel: one button to manually fire
supernote-pdf-sync.sh (see that script for the full rationale -- it
converts Supernote .note files synced into Nextcloud to PDF in place).
Normally runs nightly via bjtn's crontab; this is a supplement for
"I just wrote a note and want the PDF now" without waiting for the cron.

Runs directly on the host (not containerized) as bjtn's own systemd --user
service -- unlike arr-control/castilian-control, this script does
`docker exec ... nextcloud php occ files:scan`, which needs real Docker
daemon access. Containerizing it would mean mounting /var/run/docker.sock
into a new container just for this one button -- effectively
root-on-host access for a small convenience feature, not worth it.
Running as the same host user the cron job already runs as needs no new
privilege at all.

Double-click safety comes from the script's own flock (added alongside
this) -- a second trigger while one's already in flight just hits the
lock and exits fast, same pattern as castilian-queue.sh's `run`.
"""
import datetime
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SYNC_SCRIPT = "/home/bjtn/docker/scripts/supernote-pdf-sync.sh"
LOG = "/home/bjtn/logs/supernote-pdf-sync.log"
LAST_TRIGGERED_FILE = "/home/bjtn/logs/supernote-control-last-triggered.json"
RUN_OFFSET_FILE = "/home/bjtn/logs/supernote-control-run-offset.txt"
NOTES_DIR = "/mnt/vault/nextcloud/bjtn/files"

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Supernote Sync</title>
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
  #info { font-size: 0.8rem; color: #777; text-align: left; max-width: 700px; width: 90vw;
          background: #1a1a1a; padding: 12px 16px; border-radius: 10px; line-height: 1.5;
          box-sizing: border-box; }
  #info code { color: #9db8d8; }
  #lastrun { font-size: 0.8rem; color: #888; }
  #barwrap { width: 80vw; max-width: 420px; height: 10px; background: #1a1a1a;
             border-radius: 6px; overflow: hidden; }
  #barfill { height: 100%; width: 0%; background: #2d7de0; transition: width 0.4s ease; }
  #barlabel { font-size: 0.8rem; color: #999; }
</style>
</head>
<body>
  <div id="info">Runs <code>supernote-pdf-sync.sh</code> -- converts any
    Supernote <code>.note</code> file anywhere under bjtn's Nextcloud
    files (scanned recursively, not just one folder) to PDF in place
    (skips ones already up to date), then rescans Nextcloud so new PDFs
    show up immediately. Also runs nightly via cron regardless of this
    button.<br>
    Last triggered: <span id="lastrun">loading...</span></div>
  <button id="go" onclick="go()">📝 Convert Notes to PDF</button>
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
async function refreshProgress() {
  const wrap = document.getElementById('barwrap');
  const label = document.getElementById('barlabel');
  try {
    const j = await fetch('/progress').then(r => r.json());
    if (!j.running && j.done === 0) { wrap.hidden = true; label.textContent = ''; return; }
    wrap.hidden = false;
    const pct = j.total ? Math.round(100 * j.done / j.total) : 0;
    document.getElementById('barfill').style.width = pct + '%';
    label.textContent = j.running
      ? `${j.done} of ${j.total} notes processed`
      : `${j.done} of ${j.total} notes processed (finished)`;
  } catch (e) {}
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
  refreshProgress();
}
async function refresh() {
  try {
    const l = await fetch('/log').then(r => r.text());
    document.getElementById('log').textContent = l || '(nothing yet)';
  } catch (e) { document.getElementById('log').textContent = 'error: ' + e; }
}
refresh();
refreshMeta();
refreshProgress();
setInterval(refresh, 5000);
setInterval(refreshProgress, 5000);
</script>
</body>
</html>"""


def tail_log(n=60) -> str:
    try:
        with open(LOG) as f:
            lines = f.readlines()
        return "".join(reversed(lines[-n:]))  # newest first
    except FileNotFoundError:
        return ""
    except Exception as e:
        return f"error reading log: {e}"


def count_total_notes() -> int:
    try:
        r = subprocess.run(["find", NOTES_DIR, "-type", "f", "-iname", "*.note"],
                            capture_output=True, text=True, timeout=30)
        return len([l for l in r.stdout.splitlines() if l.strip()])
    except Exception:
        return 0


def start_run():
    # Snapshot the log's current size and the total .note count now, so
    # /progress can count only this run's per-file lines against the right
    # total, not get confused by a previous run's lines still in the log.
    try:
        offset = os.path.getsize(LOG)
    except FileNotFoundError:
        offset = 0
    try:
        with open(RUN_OFFSET_FILE, "w") as f:
            json.dump({"offset": offset, "total": count_total_notes()}, f)
    except Exception:
        pass
    subprocess.Popen([SYNC_SCRIPT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      stdin=subprocess.DEVNULL, start_new_session=True)
    # supernote-pdf-sync.sh writes its own log via `tee -a "$LOG"` already --
    # nothing more to capture here.


def is_running() -> bool:
    try:
        r = subprocess.run(["pgrep", "-f", f"bash {SYNC_SCRIPT}"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def get_progress() -> dict:
    try:
        with open(RUN_OFFSET_FILE) as f:
            state = json.load(f)
        offset, total = state["offset"], state["total"]
    except Exception:
        offset, total = 0, 0
    done = 0
    try:
        with open(LOG, "rb") as f:
            f.seek(offset)
            for line in f:
                if b"processing: " in line:
                    done += 1
    except FileNotFoundError:
        pass
    return {"done": min(done, total) if total else done, "total": total, "running": is_running()}


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
        elif self.path == "/progress":
            data = json.dumps(get_progress()).encode()
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
    server = ThreadingHTTPServer(("0.0.0.0", 8769), Handler)
    server.serve_forever()
