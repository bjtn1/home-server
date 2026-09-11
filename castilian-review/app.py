#!/usr/bin/env python3
"""
Castilian Review (renamed from castilian-control, 2026-09-11 strip-down):
one job only -- is this audio track genuinely Castilian? Everything else
that used to live in this app (duration-mismatch review, episode re-match
review, scan/queue triggering) has been removed. Scanning, matching, and
muxing all run automatically via Jenkins (jenkins.bjtn.xyz,
"castilian-pipeline" job, every ~30 min) -- see that job's config for what
it actually does (castilian-drop-scan.sh + castilian-queue.sh run).

Two kinds of entries can show up here, told apart by a "mode" field on
each cache entry (never inferred from content):
  - drop-zone sources (the original case): a track marked CASTILIAN gets
    moved into the drop-zone so Jenkins' next run extracts and muxes it
    into the matching library file; marked NOT_CASTILIAN is a no-op (the
    scan that queued it already quarantined the file elsewhere).
  - library files ("mode": "library_review", added 2026-09-11 for the
    Castilian-library audit): a track marked CASTILIAN just has the
    verdict recorded -- the file already correctly sits in /tv or
    /movies, nothing to move. Marked NOT_CASTILIAN moves the file to
    LIBRARY_STAGING_DIR (mirroring its path relative to whichever library
    root it came from) -- final deletion is a separate, deliberate step
    outside this app.

This app never mutates the library directly on its own initiative --  for
the drop-zone case it only leaves instructions for Jenkins' next
scheduled run to act on; for the library case, moving a file to staging
is the review action itself, not something a later job does.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

EXTRACT_CLIP_SCRIPT = "/scripts/castilian-extract-clip.sh"
# Where a track marked CASTILIAN gets moved to (drop-zone sources only) --
# same directory castilian-drop-scan.sh watches, so it's picked up on
# Jenkins' next scheduled run without this app needing to trigger anything.
DROP_DIR = os.environ.get("CASTILIAN_DROP_DIR", "/mnt/vault/mega-staging/drop-zone")
# 2026-09-11: a library file marked NOT_CASTILIAN also needs Sonarr/Radarr
# told about it -- otherwise they keep the episode/movie monitored,
# notice the file is now missing, and try to re-grab it (found live: this
# is exactly what happened with the first library-audit batch, done as a
# one-off manual script rather than wired into this page -- fixed here so
# every single click does the right thing on its own, not just the first
# big batch). SONARR_KEY/RADARR_KEY missing or a request failing degrades
# gracefully -- the file still gets staged either way, this is just best-
# effort so a Sonarr/Radarr hiccup never blocks the actual review action.
SONARR_URL = os.environ.get("SONARR_URL")
SONARR_KEY = os.environ.get("SONARR_KEY")
RADARR_URL = os.environ.get("RADARR_URL")
RADARR_KEY = os.environ.get("RADARR_KEY")
# Where a track marked NOT_CASTILIAN gets moved to (library sources only).
LIBRARY_STAGING_DIR = os.environ.get("CASTILIAN_STAGING_DIR", "/mnt/vault/staged-for-deletion")
LIBRARY_ROOTS = {
    "/mnt/vault/tv/": os.path.join(LIBRARY_STAGING_DIR, "shows"),
    "/mnt/vault/movies/": os.path.join(LIBRARY_STAGING_DIR, "movies"),
}

# Human-in-the-loop final say on UNRESOLVED verdicts (castilian-whisper-
# check.sh declined to guess). See CASTILIAN_VERDICT_CACHE_DIR in that
# script's own header for the cache format this reads.
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


def write_human_verdict(key: str, verdict: str, undo: dict = None) -> None:
    # verdict is "castilian" or "not_castilian" -- validated by the
    # caller (do_POST) before this is ever called, not re-validated here.
    # undo (library_review only) carries exactly what's needed to fully
    # reverse whatever this verdict changed from a later "clear" -- the
    # file move + arr sync for NOT_CASTILIAN (see stage_for_deletion()),
    # or the metadata tag for CASTILIAN (see tag_castilian_track()). Its
    # own "kind" (implicit via the verdict, checked by do_POST's "clear"
    # branch) picks which of undo_stage_for_deletion()/
    # untag_castilian_track() a later clear replays.
    record = {
        "verdict": verdict,
        "reviewed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if undo:
        record["undo"] = undo
    with open(_human_verdict_path(key), "w") as f:
        json.dump(record, f)


def list_review_entries() -> list:
    # Scans every <key>.json castilian-whisper-check.sh (or the library
    # audit) has ever written. Only UNRESOLVED verdicts (2/3) with NO
    # human_verdict yet are returned -- CONFIRMED tracks (0/1) are never
    # surfaced here (see this module's docstring), and neither is anything
    # a human (or an automated backfill pass) has already resolved.
    #
    # The `data.get("verdict") not in (2, 3)` check alone used to be
    # trusted as equivalent to "still needs review", because the only way
    # an entry ever got resolved was a click on THIS page within the same
    # session -- which called refresh() right after, so a resolved entry
    # never lingered. That assumption broke the day a bulk backfill script
    # resolved ~1,800 entries outside the page's own click flow: their
    # original verdict field never changes (nothing rewrites it), so they
    # kept passing this check and got shipped to the browser anyway --
    # 2,295 entries instead of ~500, hanging the page trying to render
    # that many cards in one innerHTML assignment. Excluding anything with
    # a human_verdict already recorded, however it got resolved, is what
    # this function's own docstring already claimed it did.
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
        human_verdict = read_human_verdict(key)
        if human_verdict is not None:
            continue
        data["key"] = key
        data["human_verdict"] = human_verdict
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
    # Called when a DROP-ZONE track is marked CASTILIAN -- moves the
    # source file back into the drop-zone, where castilian-drop-scan.sh
    # (run by Jenkins' castilian-pipeline job, every ~30 min) will pick it
    # up on its own -- this app doesn't trigger anything itself.
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


def _arr_request(base_url, api_key, method, path, data=None):
    # Shared minimal client for both Sonarr and Radarr -- stdlib only (no
    # requests dependency, matching the rest of this app). Returns the
    # parsed JSON response, or a dict with an "_error" key on any failure
    # (unreachable, bad key, timeout, non-JSON body) -- every caller below
    # treats that the same way: log it in the returned note, never raise,
    # never block the file move that already happened.
    if not base_url or not api_key:
        return {"_error": "URL/API key not configured"}
    url = f"{base_url.rstrip('/')}/api/v3{path}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "X-Api-Key": api_key, "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except Exception as e:
        return {"_error": str(e)}


# Matches "S01E02" style markers anywhere in a filename -- the common case
# for most shows in this library.
SXXEXX_RE = re.compile(r"[Ss](\d{1,3})[Ee](\d{1,3})")
# Fallback for anime-style absolute numbering with no SxxExx marker at all
# (e.g. "Detective Conan 042.mkv", "Detective Conan - 754 [...].mkv") --
# first standalone 2-4 digit number in the filename, matched against
# Sonarr's own absoluteEpisodeNumber field rather than season/episode.
ABSOLUTE_NUM_RE = re.compile(r"(?<!\d)(\d{2,4})(?!\d)")


def sync_sonarr_episode(file_path: str) -> tuple:
    # Returns (note, undo) -- undo is a dict clear_verdict_undo() can
    # replay to reverse this exact call (re-monitor the same episode id),
    # or None if nothing was actually changed (so there's nothing to undo).
    m = re.match(r"^/mnt/vault/tv/([^/]+)/", file_path)
    if not m:
        return "not under /mnt/vault/tv -- skipped Sonarr sync", None
    show_folder = m.group(1)
    series = _arr_request(SONARR_URL, SONARR_KEY, "GET", "/series")
    if isinstance(series, dict) and "_error" in series:
        return f"Sonarr unreachable ({series['_error']}) -- unmonitor manually", None
    sid = next((s["id"] for s in series if s.get("path", "").rstrip("/").endswith("/" + show_folder)), None)
    if sid is None:
        return f"'{show_folder}' not tracked in Sonarr -- nothing to sync", None
    episodes = _arr_request(SONARR_URL, SONARR_KEY, "GET", f"/episode?seriesId={sid}")
    if isinstance(episodes, dict) and "_error" in episodes:
        return f"couldn't fetch Sonarr episodes ({episodes['_error']}) -- unmonitor manually", None
    basename = os.path.basename(file_path)
    target = None
    se = SXXEXX_RE.search(basename)
    if se:
        season, ep = int(se.group(1)), int(se.group(2))
        target = next((e for e in episodes
                        if e.get("seasonNumber") == season and e.get("episodeNumber") == ep), None)
    if target is None:
        # try every standalone number in the filename against absolute
        # numbering, closest-to-start first, rather than guessing which
        # one is "the" episode number from position alone
        for num_m in ABSOLUTE_NUM_RE.finditer(basename):
            num = int(num_m.group(1))
            hit = next((e for e in episodes if e.get("absoluteEpisodeNumber") == num), None)
            if hit:
                target = hit
                break
    if target is None:
        return f"couldn't identify the episode for '{basename}' in Sonarr -- unmonitor manually", None
    if not target.get("monitored"):
        # already unmonitored (e.g. a previous batch run already handled
        # it) -- nothing this click changed, so nothing to undo either.
        return f"already unmonitored in Sonarr ({show_folder})", None
    r = _arr_request(SONARR_URL, SONARR_KEY, "PUT", "/episode/monitor",
                      {"episodeIds": [target["id"]], "monitored": False})
    if isinstance(r, dict) and "_error" in r:
        return f"found the episode but failed to unmonitor it ({r['_error']})", None
    return f"unmonitored in Sonarr ({show_folder})", {"kind": "sonarr", "episode_id": target["id"]}


def sync_radarr_movie(file_path: str) -> tuple:
    # Returns (note, undo) -- undo carries everything needed to re-POST
    # this exact movie back to Radarr (it was fully removed, not just
    # unmonitored, so "undo" here means re-adding it from scratch with
    # the same settings it already had).
    m = re.match(r"^/mnt/vault/movies/([^/]+)/", file_path)
    if not m:
        return "not under /mnt/vault/movies -- skipped Radarr sync", None
    movie_folder = m.group(1)
    movies = _arr_request(RADARR_URL, RADARR_KEY, "GET", "/movie")
    if isinstance(movies, dict) and "_error" in movies:
        return f"Radarr unreachable ({movies['_error']}) -- remove manually", None
    target = next((mv for mv in movies if mv.get("path", "").rstrip("/").endswith("/" + movie_folder)), None)
    if target is None:
        return f"'{movie_folder}' not tracked in Radarr -- nothing to sync", None
    restore_payload = {
        "title": target.get("title"),
        "tmdbId": target.get("tmdbId"),
        "qualityProfileId": target.get("qualityProfileId"),
        "rootFolderPath": os.path.dirname(target.get("path", "")) or "/movies",
        "monitored": target.get("monitored", True),
        "minimumAvailability": target.get("minimumAvailability", "released"),
        "addOptions": {"searchForMovie": False},
    }
    r = _arr_request(RADARR_URL, RADARR_KEY, "DELETE",
                      f"/movie/{target['id']}?deleteFiles=false&addImportExclusion=false")
    if isinstance(r, dict) and "_error" in r:
        return f"found the movie but failed to remove it from Radarr ({r['_error']})", None
    return f"removed from Radarr ({movie_folder})", {"kind": "radarr", "payload": restore_payload}


def tag_castilian_track(file_path: str, audio_idx: int) -> tuple:
    # Marking CASTILIAN in the review page used to mean only a side JSON
    # record (read by castilian-whisper-check.sh's human-override check,
    # see that script's own header) -- correct for skipping a re-run of
    # THIS pipeline, but useless to any other tool (castilian-patterns.sh's
    # metadata-only sweep, Bazarr, a future full-library re-scan, or just
    # opening the file elsewhere) that judges a track by its own tags, not
    # by a cache file living outside it. This writes the exact same
    # language/title convention mux-castilian-audio.sh already uses for a
    # freshly-muxed genuine Castilian track (see that script's
    # find_spanish_track()/process_pair()) directly onto the real track
    # via mkvpropedit, so the file itself now says what a human confirmed
    # -- durable, and correct no matter what reads it next.
    #
    # audio_idx is ffmpeg's 0-based audio-relative index (same convention
    # as castilian-extract-clip.sh takes -- see its header for why this
    # is NOT mkvmerge's global track `id`). mkvmerge -J's own track list
    # is walked the same way (filter to type=="audio", keep container
    # order) to land on the identical track, then edited by that track's
    # UID rather than by position -- sidesteps any doubt about whether
    # ffmpeg's and mkvpropedit's own 1-based `track:aN` numbering agree,
    # the same non-assumption castilian-extract-clip.sh's own header
    # insists on for the ffmpeg/mkvmerge pairing.
    #
    # Returns (note, undo) -- undo carries the track's own original
    # language/language-ietf/name (None for a property that wasn't set at
    # all, vs. a real prior value to put back) so a later "clear" can put
    # it back exactly, the same "undo has to actually work" bar
    # stage_for_deletion()/undo_stage_for_deletion() were held to.
    try:
        probe = subprocess.run(["mkvmerge", "-J", file_path],
                                capture_output=True, text=True, timeout=30)
        info = json.loads(probe.stdout)
    except Exception as e:
        return f"couldn't read track info to tag metadata ({e})", None
    audio_tracks = [t for t in info.get("tracks", []) if t.get("type") == "audio"]
    if audio_idx >= len(audio_tracks):
        return (f"couldn't tag metadata (audio index {audio_idx} out of range, "
                f"only {len(audio_tracks)} audio tracks found)"), None
    props = audio_tracks[audio_idx].get("properties", {})
    uid = props.get("uid")
    if uid is None:
        return "couldn't tag metadata (track has no UID)", None
    r = subprocess.run(
        ["mkvpropedit", file_path, "--edit", f"track:={uid}",
         "--set", "language=spa", "--set", "language-ietf=es-ES", "--set", "name=Castellano"],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return f"couldn't tag metadata (mkvpropedit failed: {r.stderr.strip()[:200]})", None
    undo = {
        "kind": "tag",
        "file_path": file_path,
        "uid": uid,
        "orig_language": props.get("language"),
        "orig_language_ietf": props.get("language_ietf"),
        "orig_name": props.get("track_name"),
    }
    return "tagged audio track as Castellano (es-ES) in file metadata", undo


def untag_castilian_track(undo: dict) -> str:
    # Reverses exactly one tag_castilian_track() call -- puts the track's
    # original language/language-ietf/name back. A property that was
    # never set to begin with (language-ietf and name are optional in
    # Matroska; language is not -- mkvmerge -J always reports a value for
    # it, defaulting to "und") is deleted outright rather than "restored"
    # to some guessed value, so a track that had no title before this
    # doesn't end up with an empty one after undo.
    if not os.path.isfile(undo["file_path"]):
        return f"can't restore metadata -- {undo['file_path']} no longer exists"
    args = ["mkvpropedit", undo["file_path"], "--edit", f"track:={undo['uid']}",
            "--set", f"language={undo['orig_language'] or 'und'}"]
    args += ["--set", f"language-ietf={undo['orig_language_ietf']}"] if undo.get("orig_language_ietf") \
        else ["--delete", "language-ietf"]
    args += ["--set", f"name={undo['orig_name']}"] if undo.get("orig_name") \
        else ["--delete", "name"]
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return f"failed to restore original track metadata ({r.stderr.strip()[:200]})"
    return "restored original track metadata"


def stage_for_deletion(key: str) -> tuple:
    # Called when a LIBRARY track is marked NOT_CASTILIAN -- moves the
    # actual library file out of /tv or /movies into LIBRARY_STAGING_DIR,
    # preserving its path relative to whichever library root it came from
    # (so "shows/<Show>/<file>" stays recognizable for a human reviewing
    # the staging folder later). Also syncs Sonarr/Radarr so they stop
    # wanting the file (see sync_sonarr_episode/sync_radarr_movie).
    #
    # Returns (note, undo) -- undo is everything clear_verdict_undo()
    # needs to fully reverse both the move and the arr sync from a later
    # "clear" click (a misclick undo, not just a cosmetic status reset).
    # None only if the file was already gone before this ran (nothing
    # this call did, so nothing for undo to reverse).
    entry_path = os.path.join(VERDICT_CACHE_DIR, f"{key}.json")
    with open(entry_path) as f:
        data = json.load(f)
    source_path = data["source_path"]
    if not os.path.isfile(source_path):
        return f"source file no longer at recorded path: {source_path}", None
    for root, staging_subdir in LIBRARY_ROOTS.items():
        if source_path.startswith(root):
            rel = source_path[len(root):]
            dest = os.path.join(staging_subdir, rel)
            if os.path.exists(dest):
                st = os.stat(source_path)
                stem, ext = os.path.splitext(dest)
                dest = f"{stem}.{int(st.st_mtime)}-{st.st_size}{ext}"
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(source_path, dest)
            # File is safely moved regardless of what happens next -- the
            # arr sync below is best-effort and never undoes or blocks
            # the move that already succeeded.
            if root == "/mnt/vault/tv/":
                arr_note, arr_undo = sync_sonarr_episode(source_path)
            else:
                arr_note, arr_undo = sync_radarr_movie(source_path)
            undo = {"staged_path": dest, "original_path": source_path, "arr": arr_undo}
            return f"staged for deletion: {dest} -- {arr_note}", undo
    # Not under a known library root -- refuse rather than guess a
    # destination for a file that isn't what this entry claims it is.
    return f"source_path isn't under a known library root, left in place: {source_path}", None


def undo_stage_for_deletion(undo: dict) -> str:
    # Reverses exactly one stage_for_deletion() call: moves the file back
    # to its original library path, then replays whatever arr action
    # needs reversing. Called from a "clear" verdict on an entry whose
    # last recorded verdict was NOT_CASTILIAN -- a plain status reset
    # would otherwise silently leave the file staged and Sonarr/Radarr
    # still unaware of it, which is exactly the trap a misclick-undo is
    # supposed to not have.
    staged_path, original_path = undo["staged_path"], undo["original_path"]
    notes = []
    if os.path.isfile(staged_path):
        if os.path.exists(original_path):
            # Something now occupies the original spot -- e.g. a manual
            # re-import while this was staged. Never silently clobber it;
            # shutil.move onto an existing file just overwrites it, so
            # this has to be checked before calling it.
            notes.append(f"can't move back -- {original_path} already exists (left staged at {staged_path})")
        else:
            os.makedirs(os.path.dirname(original_path), exist_ok=True)
            shutil.move(staged_path, original_path)
            notes.append("file moved back to library")
    else:
        notes.append(f"staged file no longer at {staged_path} -- couldn't move it back")
    arr = undo.get("arr")
    if arr and arr.get("kind") == "sonarr":
        r = _arr_request(SONARR_URL, SONARR_KEY, "PUT", "/episode/monitor",
                          {"episodeIds": [arr["episode_id"]], "monitored": True})
        notes.append("failed to re-monitor in Sonarr" if isinstance(r, dict) and "_error" in r
                     else "re-monitored in Sonarr")
    elif arr and arr.get("kind") == "radarr":
        r = _arr_request(RADARR_URL, RADARR_KEY, "POST", "/movie", arr["payload"])
        notes.append("failed to re-add to Radarr" if isinstance(r, dict) and "_error" in r
                     else "re-added to Radarr")
    return "; ".join(notes)


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

        if path in ("/", "/review"):
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
            entry_path = os.path.join(VERDICT_CACHE_DIR, f"{key}.json")
            if not os.path.exists(entry_path):
                self._text("no such track", code=404)
                return
            try:
                with open(entry_path) as f:
                    entry_data = json.load(f)
            except Exception:
                entry_data = {}
            # Ground truth is where the file actually lives, not the
            # "mode" tag -- 18 pre-migration cache entries (created before
            # the library_review mode existed) point at real files under
            # /mnt/vault/tv or /mnt/vault/movies but have no mode field at
            # all, which used to make them fall through to the drop-zone
            # branch below and silently do nothing (or worse, get queued
            # for muxing) instead of actually staging them.
            source_path = entry_data.get("source_path", "")
            is_library = entry_data.get("mode") == "library_review" or any(
                source_path.startswith(root) for root in LIBRARY_ROOTS
            )
            try:
                if verdict == "clear":
                    # Lets a misclick or a changed mind be undone -- back
                    # to unreviewed, not to some other guessed state. Both
                    # library verdicts change something outside this cache
                    # entry (NOT_CASTILIAN moves the file + syncs arr,
                    # CASTILIAN tags the track's own metadata), so a plain
                    # status reset would silently leave that change in
                    # place -- dispatched by "kind" on the stored undo dict
                    # to whichever of undo_stage_for_deletion()/
                    # untag_castilian_track() actually reverses it.
                    prev = read_human_verdict(key)
                    undo_note = ""
                    if prev and prev.get("undo"):
                        if prev.get("verdict") == "not_castilian":
                            undo_note = " -- " + undo_stage_for_deletion(prev["undo"])
                        elif prev.get("verdict") == "castilian":
                            undo_note = " -- " + untag_castilian_track(prev["undo"])
                    try:
                        os.remove(_human_verdict_path(key))
                    except FileNotFoundError:
                        pass
                    self._text(f"saved{undo_note}")
                elif verdict == "castilian":
                    if is_library:
                        # Library file is already correctly sitting in
                        # /tv or /movies -- recording the verdict is the
                        # whole action; the only side effect is tagging
                        # the track's own metadata (see
                        # tag_castilian_track()), not moving anything.
                        tag_note, tag_undo = tag_castilian_track(source_path, entry_data.get("audio_idx", 0))
                        write_human_verdict(key, verdict, undo=tag_undo)
                        self._text(f"saved -- kept in library -- {tag_note}")
                    else:
                        write_human_verdict(key, verdict)
                        note = queue_for_muxing(key)
                        self._text(f"saved -- {note}")
                else:
                    # not_castilian
                    if is_library:
                        note, undo = stage_for_deletion(key)
                        write_human_verdict(key, verdict, undo=undo)
                        self._text(f"saved -- {note}")
                    else:
                        # Drop-zone source: the file is already correctly
                        # sitting in quarantine (drop-rejected/) --
                        # recording the human call is the whole action,
                        # nothing to move.
                        write_human_verdict(key, verdict)
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
    print("Castilian Review listening on :8768")
    server.serve_forever()
