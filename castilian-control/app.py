#!/usr/bin/env python3
"""
Castilian pipeline review tools (2026-09-08 rewrite): two human-review
queues, nothing else. Everything that used to live here for triggering
scans/queue-runs, watching progress, and previewing/overriding matches by
hand now lives in Jenkins (jenkins.bjtn.xyz, "castilian-pipeline" job,
runs automatically every ~30 min) -- see that job's config for what it
actually does (castilian-drop-scan.sh + castilian-queue.sh run). This app
now only does what a human, not a scheduler, needs to decide:

  /review           -- is this audio track genuinely Castilian? Only
                        UNRESOLVED tracks are shown here -- the classifier's
                        own CONFIRMED calls are trusted and no longer
                        double-checked by a human (2026-09-08 decision:
                        review of already-CONFIRMED tracks never actually
                        gated anything downstream anyway -- a confirmed
                        track flows through to muxing automatically either
                        way -- so it was pure optional extra-credit
                        checking with no enforcement, and not worth the
                        time given how solid the classifier's positive-
                        evidence-only rules have proven).
  /review-duration  -- this track's audio checks out as Castilian and its
                        target episode was found, but the container
                        durations don't match closely enough to auto-mux
                        safely (a different cut/release, e.g. a broadcast
                        bumper the source lacks) -- is it actually safe to
                        mux anyway?
  /review-matches   -- (2026-09-10) content-verified episode re-matches
                        from scripts/castilian-episode-match.py (built the
                        same night episode-number matching turned out
                        unreliable for shows with regional dub-numbering
                        divergence -- see that script's own docstring).
                        Review-only for now: approving/rejecting just
                        records the human call durably: the matcher
                        confirms WHICH segment belongs to WHICH real
                        episode via real audio/transcript content, not
                        just a number -- but actually splitting a bundled
                        source's audio and muxing each segment onto its
                        own target is a separate, not-yet-built piece
                        (needs real silence-boundary detection, not the
                        rough span ESTIMATE this page's audio preview
                        uses to pick a safe-ish listening point).

Both write into mechanisms the real pipeline already reads on its own next
run (the human-verdict cache castilian-whisper-check.sh checks first, and
castilian-queue.sh's per-job override file) -- this app never mutates the
library directly, it only leaves instructions for Jenkins' next scheduled
run to act on.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

QUEUE_SCRIPT = "/scripts/castilian-queue.sh"
EXTRACT_CLIP_SCRIPT = "/scripts/castilian-extract-clip.sh"
MKVMERGE = "mkvmerge"
QUEUE_DIR = os.environ.get("CASTILIAN_QUEUE_DIR", "/mnt/vault/mega-staging/queue")
QUEUE_TSV = os.path.join(QUEUE_DIR, "queue.tsv")
# Where a track marked CASTILIAN on /review gets moved to -- same directory
# castilian-drop-scan.sh watches, so it's picked up on Jenkins' next
# scheduled run without this app needing to trigger anything itself.
DROP_DIR = os.environ.get("CASTILIAN_DROP_DIR", "/mnt/vault/mega-staging/drop-zone")

# "Review Tracks" -- human-in-the-loop final say on UNRESOLVED verdicts
# (castilian-whisper-check.sh declined to guess). See CASTILIAN_VERDICT_CACHE_DIR
# in that script's own header for the cache format this reads.
VERDICT_CACHE_DIR = os.environ.get(
    "CASTILIAN_VERDICT_CACHE_DIR", "/mnt/vault/mega-staging/queue/castilian-verdict-cache")
# Any snippet will do (not the exact resolving moment) -- reuses
# castilian-extract-clip.sh's own default start_pct reasoning (0.30: past
# the intro theme, short of end credits) with a shorter, human-listening
# -appropriate duration instead of the classifier's own longer clips.
REVIEW_SNIPPET_DUR = "25"
REVIEW_SNIPPET_START_PCT = "0.30"
REVIEW_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
REVIEW_AUDIO_PATH_RE = re.compile(r"^/review-audio/([0-9a-f]{64})$")
REVIEW_VERDICT_PATH_RE = re.compile(r"^/review-verdict/([0-9a-f]{64})$")

# "Duration Review" (2026-09-08) -- separate cache/key space from the
# whisper-verdict one above: these keys are sha256(source::target) PAIRS,
# not sha256(source::audio_idx) TRACKS, since the judgment call here is
# about one specific pairing being safe to mux despite a duration
# mismatch, not the track's dialect (already settled by the time
# something shows up here at all).
DURATION_CACHE_DIR = os.environ.get(
    "CASTILIAN_DURATION_CACHE_DIR", "/mnt/vault/mega-staging/queue/duration-verdict-cache")
DURATION_AUDIO_PATH_RE = re.compile(r"^/review-duration-audio/([0-9a-f]{64})$")
DURATION_VERDICT_PATH_RE = re.compile(r"^/review-duration-verdict/([0-9a-f]{64})$")

# "Match Review" (2026-09-10) -- see this module's docstring for the full
# story. MATCH_RESULTS_PATH is read-only reference data (one-shot output
# of a scripts/castilian-episode-match.py run, not regenerated by this
# app); MATCH_CACHE_DIR holds only this page's own human verdicts,
# separate from that source file so a future re-run of the matcher never
# clobbers a human's already-recorded call.
MATCH_RESULTS_PATH = os.environ.get(
    "CASTILIAN_MATCH_RESULTS", "/mnt/vault/mega-staging/queue/castilian-episode-matches.json")
MATCH_CACHE_DIR = os.environ.get(
    "CASTILIAN_MATCH_VERDICT_CACHE_DIR", "/mnt/vault/mega-staging/queue/match-verdict-cache")
MATCH_AUDIO_PATH_RE = re.compile(r"^/review-matches-audio/([0-9a-f]{64})$")
MATCH_VERDICT_PATH_RE = re.compile(r"^/review-matches-verdict/([0-9a-f]{64})$")


PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Castilian Review</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #111; color: #eee;
         display: flex; flex-direction: column; align-items: center; justify-content: center;
         min-height: 100vh; margin: 0; gap: 14px; padding: 28px 16px; box-sizing: border-box; }
  h1 { font-size: 1.2rem; margin: 0 0 6px; }
  .sub { font-size: 0.82rem; color: #888; text-align: center; max-width: 420px; line-height: 1.5; margin-bottom: 10px; }
  .bigbtn { font-size: 1.05rem; padding: 16px 24px; border-radius: 14px; border: none;
            cursor: pointer; font-weight: 600; text-align: center; text-decoration: none;
            display: flex; align-items: center; justify-content: center; gap: 10px;
            width: 80vw; max-width: 380px; background: #2a2a2a; color: #eee; }
  .badge { background: #d9a441; color: #1a1400; font-size: 0.75rem; font-weight: 700;
           padding: 2px 9px; border-radius: 10px; }
  .badge.zero { background: #383838; color: #888; }
  a.jenkins { font-size: 0.8rem; color: #6fa8e8; margin-top: 8px; }
</style>
</head>
<body>
  <h1>&#127911; Castilian Review</h1>
  <div class="sub">Scanning, matching, and muxing all run automatically via Jenkins now
    (every ~30 min). These two are the only things that genuinely need a human.</div>
  <a href="/review" class="bigbtn">Unresolved Tracks <span class="badge zero" id="reviewbadge">...</span></a>
  <a href="/review-duration" class="bigbtn">Duration Mismatches <span class="badge zero" id="durationbadge">...</span></a>
  <a href="/review-matches" class="bigbtn">Episode Re-Matches <span class="badge zero" id="matchesbadge">...</span></a>
  <a href="https://jenkins.bjtn.xyz" class="jenkins">jenkins.bjtn.xyz &rarr;</a>
<script>
fetch('/review-list.json').then(r => r.json()).then(rows => {
  const n = rows.filter(r => !r.human_verdict).length;
  const b = document.getElementById('reviewbadge');
  b.textContent = n; b.className = n > 0 ? 'badge' : 'badge zero';
}).catch(() => {});
fetch('/review-duration-list.json').then(r => r.json()).then(rows => {
  const n = rows.filter(r => !r.human_verdict).length;
  const b = document.getElementById('durationbadge');
  b.textContent = n; b.className = n > 0 ? 'badge' : 'badge zero';
}).catch(() => {});
fetch('/review-matches-list.json').then(r => r.json()).then(rows => {
  const n = rows.filter(r => !r.human_verdict).length;
  const b = document.getElementById('matchesbadge');
  b.textContent = n; b.className = n > 0 ? 'badge' : 'badge zero';
}).catch(() => {});
</script>
</body>
</html>"""


REVIEW_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Castilian Review</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #111; color: #eee;
         display: flex; flex-direction: column; align-items: center; justify-content: flex-start;
         min-height: 100vh; margin: 0; gap: 16px; padding: 24px 16px 60px; box-sizing: border-box; }
  a { color: #6fa8e8; }
  h2 { margin: 0; width: 90vw; max-width: 700px; }
  h2 .count { color: #999; font-weight: 400; font-size: 0.9rem; }
  #info { font-size: 0.8rem; color: #777; text-align: left; max-width: 700px; width: 90vw;
          background: #1a1a1a; padding: 12px 16px; border-radius: 10px; line-height: 1.5;
          box-sizing: border-box; }
  .card { width: 90vw; max-width: 700px; background: #1a1a1a; border-radius: 12px;
          padding: 14px 16px; display: flex; flex-direction: column; gap: 8px; }
  .card.done { opacity: 0.55; }
  .fname { font-size: 0.9rem; word-break: break-word; }
  .badge { display: inline-block; font-size: 0.7rem; padding: 2px 8px; border-radius: 8px;
           margin-left: 6px; vertical-align: middle; }
  .b-2, .b-3 { background: #4a3d1c; color: #d9c07f; }
  audio { width: 100%; height: 32px; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .actions button { font-size: 0.85rem; padding: 8px 14px; border-radius: 8px; border: none;
                     cursor: pointer; font-weight: 600; }
  .yes { background: #2f7d4a; color: white; }
  .no { background: #a03636; color: white; }
  .reviewed-note { font-size: 0.78rem; color: #8fd98f; }
  .reviewed-note.no { color: #e08a8a; }
  #empty { color: #666; font-size: 0.85rem; }
</style>
</head>
<body>
  <a href="/" style="align-self:flex-start; margin-left: 5vw; font-size: 0.85rem;">&larr; back</a>
  <div id="info">Tracks the mechanical classifier declined to guess on. Play the snippet,
    then give your final call -- it's saved separately from the mechanical verdict and is
    never overwritten by a later automated re-check.</div>

  <h2>Unresolved <span class="count" id="count"></span></h2>
  <div id="list"></div>
  <div id="empty" class="card" hidden><span id="empty-label">(none)</span></div>

<script>
function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
const VERDICT_LABEL = {2: 'INCONCLUSIVE', 3: 'CHECK FAILED'};

function cardHTML(e) {
  const badge = `<span class="badge b-${e.verdict}">${VERDICT_LABEL[e.verdict] ?? e.verdict}</span>`;
  const done = !!e.human_verdict;
  let note = '';
  if (done) {
    const label = e.human_verdict.verdict === 'castilian' ? 'You said: CASTILIAN'
                : e.human_verdict.verdict === 'not_castilian' ? 'You said: NOT CASTILIAN' : '';
    const cls = e.human_verdict.verdict === 'castilian' ? '' : 'no';
    note = `<span class="reviewed-note ${cls}">${label} &mdash; <a href="#" onclick="setVerdict('${e.key}','clear');return false;">undo</a></span>`;
  }
  return `<div class="card ${done ? 'done' : ''}" id="card-${e.key}">
    <div class="fname">${esc(e.basename)}${badge}</div>
    <audio controls preload="none" src="/review-audio/${e.key}"></audio>
    <div class="actions">
      <button class="yes" onclick="setVerdict('${e.key}','castilian')">&#9989; Castilian</button>
      <button class="no" onclick="setVerdict('${e.key}','not_castilian')">&#10060; Not Castilian</button>
      ${note}
    </div>
  </div>`;
}

async function setVerdict(key, verdict) {
  try {
    await fetch('/review-verdict/' + key, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({verdict}),
    });
  } catch (e) {}
  refresh();
}

async function refresh() {
  let rows;
  try {
    rows = await fetch('/review-list.json').then(r => r.json());
  } catch (e) { return; }
  document.getElementById('count').textContent = `(${rows.length})`;
  document.getElementById('list').innerHTML = rows.map(cardHTML).join('');
  document.getElementById('empty').hidden = rows.length !== 0;
}
refresh();
</script>
</body>
</html>"""


REVIEW_DURATION_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Duration Review</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #111; color: #eee;
         display: flex; flex-direction: column; align-items: center; justify-content: flex-start;
         min-height: 100vh; margin: 0; gap: 16px; padding: 24px 16px 60px; box-sizing: border-box; }
  a { color: #6fa8e8; }
  h2 { margin: 0; width: 90vw; max-width: 700px; }
  h2 .count { color: #999; font-weight: 400; font-size: 0.9rem; }
  #info { font-size: 0.8rem; color: #777; text-align: left; max-width: 700px; width: 90vw;
          background: #1a1a1a; padding: 12px 16px; border-radius: 10px; line-height: 1.5;
          box-sizing: border-box; }
  .card { width: 90vw; max-width: 700px; background: #1a1a1a; border-radius: 12px;
          padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; }
  .card.done { opacity: 0.55; }
  .fname { font-size: 0.9rem; word-break: break-word; }
  .reason { font-size: 0.78rem; color: #d9c07f; }
  .side label { font-size: 0.75rem; color: #999; display: block; margin-bottom: 4px; }
  audio { width: 100%; height: 32px; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .actions button { font-size: 0.85rem; padding: 8px 14px; border-radius: 8px; border: none;
                     cursor: pointer; font-weight: 600; }
  .yes { background: #2f7d4a; color: white; }
  .no { background: #a03636; color: white; }
  .reviewed-note { font-size: 0.78rem; color: #8fd98f; }
  .reviewed-note.no { color: #e08a8a; }
  #empty { color: #666; font-size: 0.85rem; }
  .spinner { width: 16px; height: 16px; border-radius: 50%; display: inline-block;
             border: 2px solid #333; border-top-color: #2d6fb0;
             animation: spin 0.8s linear infinite; vertical-align: middle; margin-right: 8px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .loadingline { color: #999; font-size: 0.85rem; display: flex; align-items: center; padding: 10px 2px; }
</style>
</head>
<body>
  <a href="/" style="align-self:flex-start; margin-left: 5vw; font-size: 0.85rem;">&larr; back</a>
  <div id="info">Tracks that check out as Castilian, but whose target file's duration
    doesn't match closely enough to auto-mux safely (likely a different cut/release -- e.g.
    a broadcast bumper the source lacks). Listen to both sides, then decide: "Mux Anyway"
    queues it for the next Jenkins run with just the duration check skipped for this one
    pairing -- every other safety check (dialect confirmation, existing-track check) still
    applies. "Skip" leaves it alone.</div>

  <h2>Mismatches <span class="count" id="count"></span></h2>
  <div id="list"><div class="loadingline"><span class="spinner"></span>checking every pairing's
    audio tracks and duration -- can take up to a minute across multiple standing jobs...</div></div>
  <div id="empty" class="card" hidden><span>(none)</span></div>

<script>
function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function cardHTML(e) {
  const done = !!e.human_verdict;
  let note = '';
  if (done) {
    const label = e.human_verdict.verdict === 'mux' ? 'You said: MUX ANYWAY' : 'You said: SKIP';
    const cls = e.human_verdict.verdict === 'mux' ? '' : 'no';
    note = `<span class="reviewed-note ${cls}">${label} &mdash; <a href="#" onclick="setVerdict('${e.key}','clear');return false;">undo</a></span>`;
  }
  return `<div class="card ${done ? 'done' : ''}" id="card-${e.key}">
    <div class="fname">${esc(e.basename_source)}</div>
    <div class="reason">${esc(e.reason)}</div>
    <div class="side"><label>Source (Castilian audio)</label>
      <audio controls preload="none" src="/review-duration-audio/${e.key}?side=source"></audio></div>
    <div class="side"><label>Target: ${esc(e.basename_target)}</label>
      <audio controls preload="none" src="/review-duration-audio/${e.key}?side=target"></audio></div>
    <div class="actions">
      <button class="yes" onclick="setVerdict('${e.key}','mux')">&#9989; Mux Anyway</button>
      <button class="no" onclick="setVerdict('${e.key}','skip')">&#10060; Skip</button>
      ${note}
    </div>
  </div>`;
}

async function setVerdict(key, verdict) {
  try {
    await fetch('/review-duration-verdict/' + key, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({verdict}),
    });
  } catch (e) {}
  refresh();
}

async function refresh() {
  let rows;
  try {
    rows = await fetch('/review-duration-list.json').then(r => r.json());
  } catch (e) { return; }
  document.getElementById('count').textContent = `(${rows.length})`;
  document.getElementById('list').innerHTML = rows.map(cardHTML).join('');
  document.getElementById('empty').hidden = rows.length !== 0;
}
refresh();
</script>
</body>
</html>"""


REVIEW_MATCHES_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Episode Match Review</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #111; color: #eee;
         display: flex; flex-direction: column; align-items: center; justify-content: flex-start;
         min-height: 100vh; margin: 0; gap: 16px; padding: 24px 16px 60px; box-sizing: border-box; }
  a { color: #6fa8e8; }
  h2 { margin: 0; width: 90vw; max-width: 700px; }
  h2 .count { color: #999; font-weight: 400; font-size: 0.9rem; }
  #info { font-size: 0.8rem; color: #777; text-align: left; max-width: 700px; width: 90vw;
          background: #1a1a1a; padding: 12px 16px; border-radius: 10px; line-height: 1.5;
          box-sizing: border-box; }
  .card { width: 90vw; max-width: 700px; background: #1a1a1a; border-radius: 12px;
          padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; }
  .card.done { opacity: 0.55; }
  .fname { font-size: 0.9rem; word-break: break-word; }
  .segbadge { font-size: 0.7rem; color: #999; }
  .episode { font-size: 0.85rem; color: #d9c07f; }
  .via { font-size: 0.75rem; color: #7fb37f; }
  .side label { font-size: 0.75rem; color: #999; display: block; margin-bottom: 4px; }
  audio { width: 100%; height: 32px; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .actions button { font-size: 0.85rem; padding: 8px 14px; border-radius: 8px; border: none;
                     cursor: pointer; font-weight: 600; }
  .yes { background: #2f7d4a; color: white; }
  .no { background: #a03636; color: white; }
  .reviewed-note { font-size: 0.78rem; color: #8fd98f; }
  .reviewed-note.no { color: #e08a8a; }
  #empty { color: #666; font-size: 0.85rem; }
</style>
</head>
<body>
  <a href="/" style="align-self:flex-start; margin-left: 5vw; font-size: 0.85rem;">&larr; back</a>
  <div id="info">Content-verified episode matches -- real regional dub-numbering divergence
    made this show's episode NUMBERS unreliable, so each of these was actually confirmed by
    matching real audio/dialogue content, not just a filename. Both clips below are the
    ENGLISH reference audio embedded in each file (not the Spanish dub) -- the exact same
    pair the matcher itself compared to confirm the match, so you can directly judge "does
    this sound like the same dialogue" in one language. Listen to both, then
    approve or reject. Review-only for now: approving just records your call -- actually
    splitting a bundled source's audio and muxing it in is a separate step, not automated
    here yet.</div>

  <h2>Re-Matches <span class="count" id="count"></span></h2>
  <div id="list"></div>
  <div id="empty" class="card" hidden><span>(none)</span></div>

<script>
function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function cardHTML(e) {
  const done = !!e.human_verdict;
  let note = '';
  if (done) {
    const label = e.human_verdict.verdict === 'approved' ? 'You said: APPROVED' : 'You said: REJECTED';
    const cls = e.human_verdict.verdict === 'approved' ? '' : 'no';
    note = `<span class="reviewed-note ${cls}">${label} &mdash; <a href="#" onclick="setVerdict('${e.key}','clear');return false;">undo</a></span>`;
  }
  const segLabel = e.segment_count > 1 ? ` <span class="segbadge">(segment ${e.segment_index + 1}/${e.segment_count}: "${esc(e.segment_title)}")</span>` : '';
  return `<div class="card ${done ? 'done' : ''}" id="card-${e.key}">
    <div class="fname">${esc(e.basename_source)}${segLabel}</div>
    <div class="episode">&rarr; ${esc(e.episode)}: ${esc(e.title)}</div>
    <div class="via">confirmed via ${esc(e.confirmed_via)}${e.score != null ? ' (score ' + e.score.toFixed(3) + ')' : ''}</div>
    <div class="side"><label>Source's English reference track</label>
      <audio controls preload="none" src="/review-matches-audio/${e.key}?side=source"></audio></div>
    <div class="side"><label>Target: ${esc(e.basename_target)}</label>
      <audio controls preload="none" src="/review-matches-audio/${e.key}?side=target"></audio></div>
    <div class="actions">
      <button class="yes" onclick="setVerdict('${e.key}','approved')">&#9989; Approve</button>
      <button class="no" onclick="setVerdict('${e.key}','rejected')">&#10060; Reject</button>
      ${note}
    </div>
  </div>`;
}

async function setVerdict(key, verdict) {
  try {
    await fetch('/review-matches-verdict/' + key, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({verdict}),
    });
  } catch (e) {}
  refresh();
}

async function refresh() {
  let rows;
  try {
    rows = await fetch('/review-matches-list.json').then(r => r.json());
  } catch (e) { return; }
  document.getElementById('count').textContent = `(${rows.length})`;
  document.getElementById('list').innerHTML = rows.map(cardHTML).join('');
  document.getElementById('empty').hidden = rows.length !== 0;
}
refresh();
</script>
</body>
</html>"""


def _human_verdict_path(key: str) -> str:
    return os.path.join(VERDICT_CACHE_DIR, f"{key}.human_verdict.json")


def _snippet_path(key: str) -> str:
    return os.path.join(VERDICT_CACHE_DIR, f"{key}.snippet.wav")


def read_human_verdict(key: str):
    try:
        with open(_human_verdict_path(key)) as f:
            return json.load(f)
    except Exception:
        return None


def write_human_verdict(key: str, verdict: str) -> None:
    # verdict is "castilian" or "not_castilian" -- validated by the
    # caller (do_POST) before this is ever called, not re-validated here.
    with open(_human_verdict_path(key), "w") as f:
        json.dump({
            "verdict": verdict,
            "reviewed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }, f)


def list_review_entries() -> list:
    # Scans every <key>.json castilian-whisper-check.sh has ever written
    # and pairs each with its human_verdict, if any. Only UNRESOLVED
    # verdicts (2/3) are returned -- see this module's own docstring for
    # why CONFIRMED tracks (0/1) are no longer surfaced here at all.
    entries = []
    try:
        names = os.listdir(VERDICT_CACHE_DIR)
    except FileNotFoundError:
        return entries
    for fname in names:
        if not fname.endswith(".json") or fname.endswith(".human_verdict.json"):
            continue
        key = fname[: -len(".json")]
        if not REVIEW_KEY_RE.match(key):
            continue  # not one of ours (defensive -- ignore anything unexpected in this dir)
        try:
            with open(os.path.join(VERDICT_CACHE_DIR, fname)) as f:
                data = json.load(f)
        except Exception:
            continue
        if data.get("verdict") not in (2, 3):
            continue
        data["key"] = key
        data["human_verdict"] = read_human_verdict(key)
        entries.append(data)
    entries.sort(key=lambda e: e.get("checked_at", ""), reverse=True)
    return entries


def ensure_review_snippet(key: str) -> "str | None":
    # Extracts once, cached forever after (a track's audio doesn't
    # change). Prefers the offset of whichever real transcription attempt
    # had the MOST words (proof of substantial spoken dialogue, not noise
    # or silence) when castilian-whisper-check.sh recorded one; falls back
    # to a fixed default otherwise.
    entry_path = os.path.join(VERDICT_CACHE_DIR, f"{key}.json")
    try:
        with open(entry_path) as f:
            data = json.load(f)
    except Exception:
        return None
    snippet_path = _snippet_path(key)
    if os.path.exists(snippet_path) and os.path.getsize(snippet_path) > 0:
        return snippet_path
    start_pct = str(data["good_snippet_offset"]) if "good_snippet_offset" in data else REVIEW_SNIPPET_START_PCT
    try:
        r = subprocess.run(
            [EXTRACT_CLIP_SCRIPT, data["source_path"], str(data["audio_idx"]),
             snippet_path, REVIEW_SNIPPET_DUR, start_pct],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0 and os.path.exists(snippet_path) and os.path.getsize(snippet_path) > 0:
            return snippet_path
    except Exception:
        pass
    return None


def _collision_safe_move(src: str, dest_dir: str) -> str:
    # Same convention as castilian-drop-scan.sh's own move_aside(): if a
    # same-named file already exists at the destination, suffix with the
    # source's own mtime+size rather than silently overwrite it.
    os.makedirs(dest_dir, exist_ok=True)
    basename = os.path.basename(src)
    dest = os.path.join(dest_dir, basename)
    if os.path.exists(dest):
        st = os.stat(src)
        stem, ext = os.path.splitext(basename)
        suffix = f"{stem}.{int(st.st_mtime)}-{st.st_size}{ext}" if ext else f"{basename}.{int(st.st_mtime)}-{st.st_size}"
        dest = os.path.join(dest_dir, suffix)
    shutil.move(src, dest)
    return dest


def queue_for_muxing(key: str) -> str:
    # Called when a track is marked CASTILIAN in the review page -- moves
    # the source file back into the drop-zone, where castilian-drop-scan.sh
    # (run by Jenkins' castilian-pipeline job, every ~30 min) will pick it
    # up on its own -- this app doesn't trigger anything itself anymore.
    entry_path = os.path.join(VERDICT_CACHE_DIR, f"{key}.json")
    with open(entry_path) as f:
        data = json.load(f)
    source_path = data["source_path"]
    audio_idx = data["audio_idx"]
    if not os.path.isfile(source_path):
        return f"source file no longer at recorded path: {source_path}"
    new_path = _collision_safe_move(source_path, DROP_DIR)
    # A file move changes its resolved path, and castilian-whisper-
    # check.sh's cache key is a hash of that path -- so the override needs
    # to be written under the key it'll compute for the file's NEW
    # location, or a future check would never find it. Also written under
    # the OLD key (already done by the caller before this function runs)
    # so the review-page listing, keyed to whatever was actually clicked,
    # still shows "reviewed" immediately.
    new_resolved = os.path.realpath(new_path)
    new_key = hashlib.sha256(f"{new_resolved}::{audio_idx}".encode()).hexdigest()
    write_human_verdict(new_key, "castilian")
    return f"moved to drop-zone: {new_path} -- Jenkins' castilian-pipeline job will pick it up on its next scheduled run"


def list_job_rows() -> list:
    rows = []
    try:
        with open(QUEUE_TSV) as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 6:
                    rows.append({"id": parts[0], "source": parts[1], "target": parts[2],
                                 "status": parts[3], "note": parts[4], "mode": parts[5]})
    except FileNotFoundError:
        pass
    return rows


def _override_path(job_id: str) -> str:
    return os.path.join(QUEUE_DIR, f"job-{job_id}.overrides.tsv")


def read_overrides(job_id: str) -> dict:
    """source -> {"target": str, "force_duration": bool}"""
    out = {}
    try:
        with open(_override_path(job_id)) as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 2:
                    flags = parts[2] if len(parts) >= 3 else ""
                    out[parts[0]] = {"target": parts[1], "force_duration": "force_duration" in flags.split(",")}
    except FileNotFoundError:
        pass
    return out


def write_overrides(job_id: str, overrides: dict) -> None:
    path = _override_path(job_id)
    if not overrides:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    with open(path, "w") as f:
        for src, info in overrides.items():
            flags = "force_duration" if info.get("force_duration") else ""
            f.write(f"{src}\t{info['target']}\t{flags}\n")


def requeue_job(job_id: str) -> str:
    try:
        r = subprocess.run([QUEUE_SCRIPT, "requeue", job_id], capture_output=True, text=True, timeout=20)
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return f"error requeueing job {job_id}: {e}"


def run_preview(job_id: str):
    """Runs `castilian-queue.sh preview <id>` (dry-run under the hood --
    see that command's own header) and returns the parsed array, or None
    if the job doesn't exist / the script errored."""
    try:
        r = subprocess.run([QUEUE_SCRIPT, "preview", job_id], capture_output=True, text=True, timeout=120)
    except Exception:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def _duration_verdict_path(key: str) -> str:
    return os.path.join(DURATION_CACHE_DIR, f"{key}.json")


def read_duration_verdict(key: str):
    try:
        with open(_duration_verdict_path(key)) as f:
            return json.load(f)
    except Exception:
        return None


def write_duration_verdict(key: str, source: str, target: str, job_id: str, verdict: str) -> None:
    os.makedirs(DURATION_CACHE_DIR, exist_ok=True)
    with open(_duration_verdict_path(key), "w") as f:
        json.dump({
            "source": source, "target": target, "job_id": job_id, "verdict": verdict,
            "reviewed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }, f)


def clear_duration_verdict(key: str) -> None:
    try:
        os.remove(_duration_verdict_path(key))
    except FileNotFoundError:
        pass


def _duration_pair_path(key: str) -> str:
    return os.path.join(DURATION_CACHE_DIR, f"{key}.pair.json")


def _write_duration_pair_index(key: str, source: str, target: str, job_id: str) -> None:
    # Small index written alongside the verdict cache, separate from it,
    # so audio playback and the verdict-write handler can cheaply resolve
    # key -> (source, target, job_id) WITHOUT re-running the expensive
    # full dry-run scan (list_duration_mismatches() below) on every single
    # request -- that scan is only meant to run once per page load.
    os.makedirs(DURATION_CACHE_DIR, exist_ok=True)
    with open(_duration_pair_path(key), "w") as f:
        json.dump({"source": source, "target": target, "job_id": job_id}, f)


def read_duration_pair(key: str):
    try:
        with open(_duration_pair_path(key)) as f:
            return json.load(f)
    except Exception:
        return None


def list_duration_mismatches() -> list:
    """Scans every distinct (source,target,mode) job pairing for rows
    whose ONLY blocker is a duration mismatch -- everything else about the
    pairing (dialect confirmation, existing-track check) already passed.
    Reuses castilian-queue.sh preview (the exact dry-run check a real
    build would do), not a separate reimplementation. Slow (a real
    per-file check across however many standing jobs exist) -- the page
    shows a spinner with an honest time estimate rather than pretending
    this is instant."""
    seen = {}
    for row in list_job_rows():
        pair_key = (row["source"], row["target"], row["mode"])
        if pair_key not in seen:
            seen[pair_key] = row["id"]

    out = []
    for (source_dir, target_dir, mode), job_id in seen.items():
        preview = run_preview(job_id)
        if not preview:
            continue
        for r in preview:
            if r.get("action") != "skip" or "duration mismatch" not in (r.get("reason") or ""):
                continue
            source = r["source"]
            target = r.get("target")
            if not target:
                continue
            key = hashlib.sha256(f"{source}::{target}".encode()).hexdigest()
            _write_duration_pair_index(key, source, target, job_id)
            out.append({
                "key": key, "job_id": job_id, "source": source, "target": target,
                "basename_source": os.path.basename(source), "basename_target": os.path.basename(target),
                "reason": r["reason"], "human_verdict": read_duration_verdict(key),
            })
    out.sort(key=lambda e: e["basename_source"])
    return out


def _duration_snippet_path(key: str, side: str) -> str:
    return os.path.join(DURATION_CACHE_DIR, f"{key}.{side}.snippet.wav")


def ensure_duration_snippet(key: str, side: str) -> "str | None":
    pair = read_duration_pair(key)
    if pair is None:
        return None
    path = pair["source"] if side == "source" else pair["target"]
    snippet_path = _duration_snippet_path(key, side)
    if os.path.exists(snippet_path) and os.path.getsize(snippet_path) > 0:
        return snippet_path
    os.makedirs(DURATION_CACHE_DIR, exist_ok=True)
    try:
        r = subprocess.run(
            [EXTRACT_CLIP_SCRIPT, path, "0", snippet_path, REVIEW_SNIPPET_DUR, REVIEW_SNIPPET_START_PCT],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0 and os.path.exists(snippet_path) and os.path.getsize(snippet_path) > 0:
            return snippet_path
    except Exception:
        pass
    return None


def _mkvmerge_audio_tracks(path: str) -> list:
    try:
        r = subprocess.run([MKVMERGE, "-J", path], capture_output=True, text=True, timeout=30)
        tracks = json.loads(r.stdout).get("tracks", [])
    except Exception:
        return []
    return [t for t in tracks if t.get("type") == "audio"]


def _is_english_track(props: dict) -> bool:
    # Same two-signal check castilian-audio-align.py's is_english_props()
    # uses (language tag, with a title-text fallback for real files found
    # tonight that only say "[Eng]" in the track name, tagged 'und').
    lang = (props.get("language") or props.get("language_ietf") or "").lower()
    if lang in ("eng", "en"):
        return True
    text = f"{props.get('track_name') or ''}".lower()
    return bool(re.search(r"\[eng(lish)?\]|\benglish\b", text))


def find_english_audio_idx(path: str) -> "int | None":
    """0-based index among audio-only tracks (ffmpeg -map 0:a:N
    convention) of the first track that looks like English -- this is
    the SAME reference track castilian-episode-match.py actually
    compared against the target when confirming a match (see that
    script's is_english_props usage), found live 2026-09-10: the review
    page was instead always previewing audio index 0, which for most of
    these Spanish-source files is the CASTILIAN track -- asking a human
    to judge a Spanish-vs-English pairing they can't actually compare by
    ear, when the real verification was English-vs-English the whole
    time. Returns None if no track looks English (caller falls back to
    index 0 rather than failing outright)."""
    for i, t in enumerate(_mkvmerge_audio_tracks(path)):
        if _is_english_track(t.get("properties", {})):
            return i
    return None


def _match_verdict_path(key: str) -> str:
    return os.path.join(MATCH_CACHE_DIR, f"{key}.json")


def read_match_verdict(key: str):
    try:
        with open(_match_verdict_path(key)) as f:
            return json.load(f)
    except Exception:
        return None


def write_match_verdict(key: str, verdict: str) -> None:
    os.makedirs(MATCH_CACHE_DIR, exist_ok=True)
    with open(_match_verdict_path(key), "w") as f:
        json.dump({
            "verdict": verdict,
            "reviewed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }, f)


def clear_match_verdict(key: str) -> None:
    try:
        os.remove(_match_verdict_path(key))
    except FileNotFoundError:
        pass


def _load_match_rows() -> dict:
    """key -> row, from the one-shot MATCH_RESULTS_PATH. Re-read on every
    call (not cached in memory) -- this file is small (one night's worth
    of matches) and a future matcher re-run should be picked up on the
    next page load without needing this app restarted."""
    try:
        with open(MATCH_RESULTS_PATH) as f:
            rows = json.load(f)
    except FileNotFoundError:
        return {}
    return {r["key"]: r for r in rows}


def list_match_entries() -> list:
    out = []
    for key, row in _load_match_rows().items():
        entry = dict(row)
        entry["human_verdict"] = read_match_verdict(key)
        out.append(entry)
    out.sort(key=lambda e: (e["basename_source"], e["segment_index"]))
    return out


def _match_snippet_path(key: str, side: str) -> str:
    return os.path.join(MATCH_CACHE_DIR, f"{key}.{side}.snippet.wav")


def ensure_match_snippet(key: str, side: str) -> "str | None":
    rows = _load_match_rows()
    row = rows.get(key)
    if row is None:
        return None
    snippet_path = _match_snippet_path(key, side)
    if os.path.exists(snippet_path) and os.path.getsize(snippet_path) > 0:
        return snippet_path
    if side == "source":
        path = row["source"]
        # Segment-span-aware, unlike the other review pages' fixed 0.30 --
        # a bundled multi-segment source (most of these) needs a start
        # point actually inside ITS segment, not the file's first one.
        span_start, span_end = row["segment_span"]
        start_pct = str(round(span_start + (span_end - span_start) * 0.3, 3))
    else:
        path = row["target"]
        start_pct = REVIEW_SNIPPET_START_PCT
    # Both sides preview the ENGLISH reference track specifically, not
    # just "whatever's first" -- see find_english_audio_idx()'s comment.
    # This is the exact pair the matcher itself verified content against,
    # so a human listening to both can directly judge "does this sound
    # like the same dialogue," in one language, rather than being asked
    # to compare Spanish against English by ear.
    audio_idx = find_english_audio_idx(path)
    if audio_idx is None:
        audio_idx = 0  # no detected English track -- fall back rather than fail outright
    os.makedirs(MATCH_CACHE_DIR, exist_ok=True)
    try:
        r = subprocess.run(
            [EXTRACT_CLIP_SCRIPT, path, str(audio_idx), snippet_path, REVIEW_SNIPPET_DUR, start_pct],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0 and os.path.exists(snippet_path) and os.path.getsize(snippet_path) > 0:
            return snippet_path
    except Exception:
        pass
    return None


class Handler(BaseHTTPRequestHandler):
    def _text(self, body: str, code=200):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, body: str):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        query = parse_qs(urlsplit(self.path).query)

        if path == "/":
            self._html(PAGE)
        elif path == "/review":
            self._html(REVIEW_PAGE)
        elif path == "/review-list.json":
            self._json(list_review_entries())
        elif (m := REVIEW_AUDIO_PATH_RE.match(path)):
            snippet = ensure_review_snippet(m.group(1))
            if not snippet:
                self.send_response(404)
                self.end_headers()
                return
            try:
                with open(snippet, "rb") as f:
                    data = f.read()
            except Exception:
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")  # snippet is cached on disk already; no second cache layer needed
            self.end_headers()
            self.wfile.write(data)
        elif path == "/review-duration":
            self._html(REVIEW_DURATION_PAGE)
        elif path == "/review-duration-list.json":
            self._json(list_duration_mismatches())
        elif (m := DURATION_AUDIO_PATH_RE.match(path)):
            side = (query.get("side") or [""])[0]
            if side not in ("source", "target"):
                self.send_response(400)
                self.end_headers()
                return
            snippet = ensure_duration_snippet(m.group(1), side)
            if not snippet:
                self.send_response(404)
                self.end_headers()
                return
            try:
                with open(snippet, "rb") as f:
                    data = f.read()
            except Exception:
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/review-matches":
            self._html(REVIEW_MATCHES_PAGE)
        elif path == "/review-matches-list.json":
            self._json(list_match_entries())
        elif (m := MATCH_AUDIO_PATH_RE.match(path)):
            side = (query.get("side") or [""])[0]
            if side not in ("source", "target"):
                self.send_response(400)
                self.end_headers()
                return
            snippet = ensure_match_snippet(m.group(1), side)
            if not snippet:
                self.send_response(404)
                self.end_headers()
                return
            try:
                with open(snippet, "rb") as f:
                    data = f.read()
            except Exception:
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlsplit(self.path).path

        if (m := REVIEW_VERDICT_PATH_RE.match(path)):
            key = m.group(1)
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._text("bad request body", code=400)
                return
            verdict = body.get("verdict")
            if verdict not in ("castilian", "not_castilian", "clear"):
                self._text("verdict must be 'castilian', 'not_castilian', or 'clear'", code=400)
                return
            if not os.path.exists(os.path.join(VERDICT_CACHE_DIR, f"{key}.json")):
                self._text("no such track", code=404)
                return
            try:
                if verdict == "clear":
                    # Lets a misclick or a changed mind be undone -- back
                    # to unreviewed, not to some other guessed state.
                    try:
                        os.remove(_human_verdict_path(key))
                    except FileNotFoundError:
                        pass
                    self._text("saved")
                elif verdict == "castilian":
                    write_human_verdict(key, verdict)
                    note = queue_for_muxing(key)
                    self._text(f"saved -- {note}")
                else:
                    # not_castilian: the file is already correctly sitting
                    # in quarantine (drop-rejected/) -- recording the
                    # human call is the whole action, nothing to move.
                    write_human_verdict(key, verdict)
                    self._text("saved")
            except Exception as e:
                self._text(f"failed to save: {e}", code=500)
        elif (m := DURATION_VERDICT_PATH_RE.match(path)):
            key = m.group(1)
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._text("bad request body", code=400)
                return
            verdict = body.get("verdict")
            if verdict not in ("mux", "skip", "clear"):
                self._text("verdict must be 'mux', 'skip', or 'clear'", code=400)
                return
            pair = read_duration_pair(key)
            if pair is None:
                self._text("no such pairing -- reload the list first", code=404)
                return
            try:
                if verdict == "clear":
                    clear_duration_verdict(key)
                    # Also clear any force_duration override a previous
                    # "mux" call wrote -- a changed mind should actually
                    # un-force it, not just hide the note.
                    overrides = read_overrides(pair["job_id"])
                    overrides.pop(pair["source"], None)
                    write_overrides(pair["job_id"], overrides)
                    self._text("saved")
                elif verdict == "mux":
                    write_duration_verdict(key, pair["source"], pair["target"], pair["job_id"], "mux")
                    overrides = read_overrides(pair["job_id"])
                    overrides[pair["source"]] = {"target": pair["target"], "force_duration": True}
                    write_overrides(pair["job_id"], overrides)
                    note = requeue_job(pair["job_id"])
                    self._text(f"saved -- {note}")
                else:
                    write_duration_verdict(key, pair["source"], pair["target"], pair["job_id"], "skip")
                    self._text("saved")
            except Exception as e:
                self._text(f"failed to save: {e}", code=500)
        elif (m := MATCH_VERDICT_PATH_RE.match(path)):
            key = m.group(1)
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._text("bad request body", code=400)
                return
            verdict = body.get("verdict")
            if verdict not in ("approved", "rejected", "clear"):
                self._text("verdict must be 'approved', 'rejected', or 'clear'", code=400)
                return
            if key not in _load_match_rows():
                self._text("no such match -- reload the list first", code=404)
                return
            try:
                if verdict == "clear":
                    clear_match_verdict(key)
                else:
                    # Review-only for now (see this module's docstring) --
                    # no mux/override side effect here, just the record.
                    write_match_verdict(key, verdict)
                self._text("saved")
            except Exception as e:
                self._text(f"failed to save: {e}", code=500)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8768), Handler)
    server.serve_forever()
