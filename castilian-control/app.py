#!/usr/bin/env python3
"""
Single-page control panel for castilian-queue.sh (see
~/docker/scripts/castilian-queue.sh for the queue itself): Run, Stop, and
Resume. A run can take hours (large downloads), so /trigger and /resume
start it in the background and return immediately -- the page polls
/status and /log to show progress instead.

Double-click safety on Run comes from the queue script's own flock (see
its `run` case) -- this app doesn't need to track "is it running" itself,
a second trigger while one's already in flight just hits the lock and
exits fast, which shows up as a normal log line.

Stop is more direct: it kills whatever's running (see STOPPABLE) and
marks it STOPPED in the queue so self-heal won't quietly retry it, rather
than waiting for the flock to naturally clear -- someone hitting Stop
wants it to actually stop.
"""
import json
import os
import re
import signal
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

QUEUE_SCRIPT = "/scripts/castilian-queue.sh"
RUN_LOG = "/tmp/castilian-queue-run.log"
RESUME_PATH_RE = re.compile(r"^/resume/(\d+)$")

# Processes we're willing to stop on request. Matched by substring against
# /proc/<pid>/cmdline rather than tracking a specific subprocess.Popen
# handle, so a Stop works regardless of whether the run in flight was
# started via /trigger, a container restart in between, or the CLI
# directly on the host -- anything matching in *this container's* PID
# namespace is fair game.
STOPPABLE = ("castilian-queue.sh", "megadl")

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
  table { width: 90vw; max-width: 700px; border-collapse: collapse; font-size: 0.85rem; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #2a2a2a; }
  th { color: #999; font-weight: 500; }
  .st-DONE { color: #4caf6e; } .st-FAILED { color: #e05a5a; }
  .st-RUNNING { color: #3d8fe0; } .st-STOPPED { color: #d9a441; } .st-PENDING { color: #999; }
  .resume-btn { font-size: 0.8rem; padding: 4px 12px; border-radius: 8px; width: auto;
                background: #3a8a4a; }
  #empty { color: #666; font-size: 0.85rem; }
</style>
</head>
<body>
  <button id="go" onclick="go()">&#127464;&#127466; Run Castilian Queue</button>
  <button id="stop" onclick="stopAll()" style="background:#b03030;">&#9209; Stop Everything</button>
  <button id="resumeall" onclick="resumeAll()" style="background:#3a8a4a;">&#9654; Resume All Stopped</button>
  <h3>Queue status</h3>
  <table id="queue"><thead><tr><th>ID</th><th>Status</th><th>Mode</th><th>Target</th><th>Note</th><th></th></tr></thead>
    <tbody id="queue-body"></tbody></table>
  <div id="empty" hidden>(empty queue)</div>
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
async function stopAll() {
  if (!confirm('Stop everything currently running? Jobs in progress will be marked STOPPED and will not auto-resume.')) return;
  const btn = document.getElementById('stop');
  btn.disabled = true;
  btn.textContent = 'Stopping...';
  try {
    await fetch('/stop', {method: 'POST'});
  } catch (e) {}
  btn.disabled = false;
  btn.textContent = '⏹ Stop Everything';
  refresh();
}
async function resumeOne(id, btn) {
  btn.disabled = true;
  btn.textContent = '...';
  try {
    await fetch('/resume/' + id, {method: 'POST'});
  } catch (e) {}
  refresh();
}
async function resumeAll() {
  if (!confirm('Resume every STOPPED job? This includes ones you may have stopped on purpose and left that way.')) return;
  const btn = document.getElementById('resumeall');
  btn.disabled = true;
  try {
    await fetch('/resume-all', {method: 'POST'});
  } catch (e) {}
  setTimeout(() => { btn.disabled = false; }, 3000);
  refresh();
}
function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
async function refresh() {
  try {
    const rows = await fetch('/status.json').then(r => r.json());
    const body = document.getElementById('queue-body');
    const empty = document.getElementById('empty');
    document.getElementById('queue').hidden = rows.length === 0;
    empty.hidden = rows.length !== 0;
    body.innerHTML = rows.map(row => {
      const resumeCell = row.status === 'STOPPED'
        ? `<button class="resume-btn" onclick="resumeOne('${esc(row.id)}', this)">Resume</button>`
        : '';
      return `<tr><td>${esc(row.id)}</td><td class="st-${esc(row.status)}">${esc(row.status)}</td>` +
             `<td>${esc(row.mode)}</td><td>${esc(row.target)}</td><td>${esc(row.note)}</td>` +
             `<td>${resumeCell}</td></tr>`;
    }).join('');
  } catch (e) {
    document.getElementById('queue-body').innerHTML =
      `<tr><td colspan="6">error: ${esc(e)}</td></tr>`;
  }
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


def run_status_json() -> str:
    try:
        r = subprocess.run([QUEUE_SCRIPT, "status", "--json"], capture_output=True, text=True, timeout=20)
        if r.returncode != 0 or not r.stdout.strip():
            return "[]"
        return r.stdout
    except Exception:
        return "[]"


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


def _find_stoppable_pids():
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                cmdline = f.read().decode(errors="replace")
        except OSError:
            continue
        if any(m in cmdline for m in STOPPABLE):
            pids.append(int(entry))
    return pids


def stop_all() -> str:
    """Kills anything matching STOPPABLE in this container's PID namespace
    (SIGTERM, then SIGKILL after a grace period for stragglers), then marks
    any job the queue still thinks is RUNNING as STOPPED so self-heal won't
    auto-retry it on the next run. Safe to call even if nothing is running --
    the OS-level kill and the queue's own `halt` are both then no-ops."""
    pids = _find_stoppable_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if pids:
        time.sleep(3)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    with open(RUN_LOG, "a") as f:
        f.write(f"\n----- stop requested -- killed pids {pids} -----\n")
    try:
        r = subprocess.run([QUEUE_SCRIPT, "halt"], capture_output=True, text=True, timeout=20)
        return f"stopped {len(pids)} process(es)\n{r.stdout}{r.stderr}"
    except Exception as e:
        return f"stopped {len(pids)} process(es), but halt errored: {e}"


def resume_job(job_id: str) -> str:
    try:
        r = subprocess.run([QUEUE_SCRIPT, "resume", job_id], capture_output=True, text=True, timeout=20)
        note = r.stdout + r.stderr
        if r.returncode != 0:
            return note or f"could not resume job {job_id}"
    except Exception as e:
        return f"error resuming job {job_id}: {e}"
    start_run()
    return note or f"resumed job {job_id}"


def resume_all() -> str:
    # Jobs end up STOPPED for different reasons (one crashed mid-run,
    # another was deliberately told to stop and isn't meant to come back
    # yet) -- this resumes literally all of them, on purpose, when that's
    # actually what's wanted. The page guards this behind a confirm()
    # dialog (same as Stop) since resuming everything at once already
    # restarted a real download unintentionally once.
    try:
        r = subprocess.run([QUEUE_SCRIPT, "resume", "--all"], capture_output=True, text=True, timeout=20)
        note = r.stdout + r.stderr
    except Exception as e:
        return f"error resuming: {e}"
    start_run()
    return note or "resumed"


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
        elif self.path == "/status.json":
            data = run_status_json().encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
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
        elif self.path == "/stop":
            try:
                self._text(stop_all())
            except Exception as e:
                self._text(f"failed to stop: {e}", code=500)
        elif self.path == "/resume-all":
            try:
                self._text(resume_all())
            except Exception as e:
                self._text(f"failed to resume: {e}", code=500)
        elif (m := RESUME_PATH_RE.match(self.path)):
            try:
                self._text(resume_job(m.group(1)))
            except Exception as e:
                self._text(f"failed to resume: {e}", code=500)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8768), Handler)
    server.serve_forever()
