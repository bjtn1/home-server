#!/usr/bin/env python3
"""Browse everything sitting in /mnt/vault/staged-for-deletion and either
restore it (move back + best-effort re-sync Sonarr/Radarr/Jellyfin) or
delete it forever (real, irreversible rm).

Built 2026-09-12 because staged-for-deletion had accumulated content from
FOUR different staging mechanisms over one long session -- castilian-
review (per-track, records an exact undo in its own verdict cache),
media-remove (whole-folder, records an exact undo in its own undo dir),
castilian-scan.py (auto-staging, records NOTHING), and the very original
one-time bulk sweep (also records nothing) -- so a restore can't assume
an exact "undo record" exists at all. Two tiers, cheapest/most-exact
first:

  1. Exact undo: scan castilian-review's VERDICT_CACHE_DIR and media-
     remove's UNDO_DIR for a record whose staged_path matches. If found,
     replay it exactly (its own captured restore_payload), same
     confidence as clicking "undo" in either of those tools directly.
  2. Best-effort fallback (no exact record):
       - TV episode: Sonarr almost always still has the series tracked
         (only a full media-remove series deletion changes that) --
         just move the file back and re-monitor the matching episode by
         filename, exactly like castilian-review's own sync, no stored
         payload needed at all.
       - TV whole show (series not currently tracked): move the folder
         back, then re-add the series via a Sonarr lookup-by-title
         (folder name minus trailing " (YYYY)") and re-monitor everything.
       - Movie: move the folder back, then re-add via a Radarr lookup-
         by-title the same way, using the single most common quality
         profile among already-tracked movies as a reasonable default.
     Every fallback re-add is clearly labeled "best guess" in the
     response, distinct from an exact restore.

Delete-forever is real and permanent: shutil.rmtree/os.remove, no
staging, no undo. Also removes any exact-undo record pointing at the
deleted path so nothing is left dangling.

Env:
    SONARR_URL / SONARR_KEY, RADARR_URL / RADARR_KEY
    JELLYFIN_URL / JELLYFIN_KEY
    MEDIA_STAGING_DIR   default /mnt/vault/staged-for-deletion
    MEDIA_TV_ROOT       default /mnt/vault/tv
    MEDIA_MOVIES_ROOT   default /mnt/vault/movies
    CASTILIAN_VERDICT_CACHE_DIR  default /mnt/vault/mega-staging/queue/castilian-verdict-cache
    MEDIA_REMOVE_UNDO_DIR        default /mnt/vault/mega-staging/queue/media-remove-undo
"""
import json
import os
import re
import shutil
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

SONARR_URL = os.environ.get("SONARR_URL")
SONARR_KEY = os.environ.get("SONARR_KEY")
RADARR_URL = os.environ.get("RADARR_URL")
RADARR_KEY = os.environ.get("RADARR_KEY")
JELLYFIN_URL = os.environ.get("JELLYFIN_URL")
JELLYFIN_KEY = os.environ.get("JELLYFIN_KEY")

STAGING_DIR = os.environ.get("MEDIA_STAGING_DIR", "/mnt/vault/staged-for-deletion")
TV_ROOT = os.environ.get("MEDIA_TV_ROOT", "/mnt/vault/tv").rstrip("/")
MOVIES_ROOT = os.environ.get("MEDIA_MOVIES_ROOT", "/mnt/vault/movies").rstrip("/")
CASTILIAN_CACHE_DIR = os.environ.get(
    "CASTILIAN_VERDICT_CACHE_DIR", "/mnt/vault/mega-staging/queue/castilian-verdict-cache")
MEDIA_REMOVE_UNDO_DIR = os.environ.get(
    "MEDIA_REMOVE_UNDO_DIR", "/mnt/vault/mega-staging/queue/media-remove-undo")

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi"}
SXXEXX_RE = re.compile(r"[Ss](\d{1,3})[Ee](\d{1,3})")
ABSOLUTE_NUM_RE = re.compile(r"(?<!\d)(\d{2,4})(?!\d)")
YEAR_SUFFIX_RE = re.compile(r"\s*\(\d{4}\)\s*$")


def _http(method, url, headers=None, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return {"_status": r.status, "_body": json.loads(raw) if raw else None}
    except urllib.error.HTTPError as e:
        return {"_status": e.code, "_error": e.read().decode(errors="replace")}
    except Exception as e:
        return {"_error": str(e)}


def arr(base_url, api_key, method, path, body=None):
    return _http(method, f"{base_url.rstrip('/')}/api/v3{path}", {
        "X-Api-Key": api_key, "Content-Type": "application/json",
    }, body)


def jellyfin_refresh():
    if not JELLYFIN_URL or not JELLYFIN_KEY:
        return
    _http("POST", f"{JELLYFIN_URL.rstrip('/')}/Library/Refresh?api_key={JELLYFIN_KEY}")


# ---- staged content listing ----

def _walk_files(base):
    out = []
    for dirpath, _, filenames in os.walk(base):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, base)
            try:
                st = os.stat(full)
            except OSError:
                continue
            out.append({"relpath": rel, "size": st.st_size, "mtime": st.st_mtime})
    return out


def staged_list():
    items = []
    for kind, subdir in (("show", "shows"), ("movie", "movies")):
        base = os.path.join(STAGING_DIR, subdir)
        if not os.path.isdir(base):
            continue
        for folder in sorted(os.listdir(base)):
            folder_path = os.path.join(base, folder)
            if not os.path.isdir(folder_path):
                continue
            files = _walk_files(folder_path)
            if files:
                items.append({"type": kind, "folder": folder, "files": files})
    return items


# ---- exact-undo-record lookup (matches what castilian-review / media-remove already recorded) ----

def _find_exact_movie_undo(staged_folder_path):
    # castilian-review's per-track undo lives inside a `*.human_verdict.json`
    # sidecar (only for a NOT_CASTILIAN verdict); media-remove's lives in
    # its own dedicated undo dir, one file per removed item. Both store
    # {"staged_path", "original_path", ...} in a broadly compatible shape;
    # matched here by staged_path prefix (castilian-review's staged_path
    # is the exact file, media-remove's is the whole folder).
    if os.path.isdir(MEDIA_REMOVE_UNDO_DIR):
        for fname in os.listdir(MEDIA_REMOVE_UNDO_DIR):
            if not fname.startswith("movie-"):
                continue
            try:
                rec = json.load(open(os.path.join(MEDIA_REMOVE_UNDO_DIR, fname)))
            except Exception:
                continue
            if rec.get("staged_path") == staged_folder_path:
                return {"source": "media-remove", "path": os.path.join(MEDIA_REMOVE_UNDO_DIR, fname), "record": rec}
    if os.path.isdir(CASTILIAN_CACHE_DIR):
        for fname in os.listdir(CASTILIAN_CACHE_DIR):
            if not fname.endswith(".human_verdict.json"):
                continue
            try:
                rec = json.load(open(os.path.join(CASTILIAN_CACHE_DIR, fname)))
            except Exception:
                continue
            undo = rec.get("undo")
            if undo and undo.get("kind") == "radarr" and \
                    os.path.dirname(undo.get("staged_path", "")) == staged_folder_path:
                return {"source": "castilian-review", "path": os.path.join(CASTILIAN_CACHE_DIR, fname), "record": undo}
    return None


def _find_exact_show_undo(staged_folder_path):
    if os.path.isdir(MEDIA_REMOVE_UNDO_DIR):
        for fname in os.listdir(MEDIA_REMOVE_UNDO_DIR):
            if not fname.startswith("show-"):
                continue
            try:
                rec = json.load(open(os.path.join(MEDIA_REMOVE_UNDO_DIR, fname)))
            except Exception:
                continue
            if rec.get("staged_path") == staged_folder_path:
                return {"source": "media-remove", "path": os.path.join(MEDIA_REMOVE_UNDO_DIR, fname), "record": rec}
    return None


# ---- restore ----

def restore_movie_folder(folder):
    staged_path = os.path.join(STAGING_DIR, "movies", folder)
    original_path = os.path.join(MOVIES_ROOT, folder)
    if not os.path.isdir(staged_path):
        return {"error": f"{staged_path} not found"}
    notes = []
    exact = _find_exact_movie_undo(staged_path)
    if os.path.isdir(original_path) and not os.listdir(original_path):
        # A single-file move (the original one-time bulk sweep, and
        # castilian-review before it staged whole folders) leaves an
        # empty directory behind at the original spot -- safe to clear
        # before moving the real folder back into its place. Anything
        # actually inside it is a genuine conflict, left alone below.
        os.rmdir(original_path)
        notes.append("cleared an empty leftover folder at the original location")
    elif os.path.exists(original_path):
        return {"error": f"can't restore -- {original_path} already exists and isn't empty"}
    os.makedirs(os.path.dirname(original_path), exist_ok=True)
    shutil.move(staged_path, original_path)
    notes.append("folder moved back")

    if exact:
        r = arr(RADARR_URL, RADARR_KEY, "POST", "/movie", exact["record"]["restore_payload"])
        notes.append("re-added to Radarr (exact settings)" if "_error" not in r
                     else f"failed to re-add to Radarr: {r['_error']}")
        if exact["source"] == "media-remove":
            try:
                os.remove(exact["path"])
            except FileNotFoundError:
                pass
    else:
        # already tracked? (e.g. was only unmonitored, not removed)
        movies = arr(RADARR_URL, RADARR_KEY, "GET", "/movie")
        existing = next((m for m in (movies.get("_body") or [])
                          if m.get("path", "").rstrip("/").endswith("/" + folder)), None) \
            if "_error" not in movies else None
        if existing:
            notes.append("already tracked in Radarr, nothing to re-add")
        else:
            title_guess = YEAR_SUFFIX_RE.sub("", folder).strip()
            lookup = arr(RADARR_URL, RADARR_KEY, "GET", f"/movie/lookup?term={urllib.parse.quote(title_guess)}")
            hit = (lookup.get("_body") or [None])[0] if "_error" not in lookup else None
            if not hit:
                notes.append(f"no exact undo record and no Radarr match found for '{title_guess}' "
                              f"-- file is back on disk but NOT re-tracked in Radarr, add it manually")
            else:
                profiles = arr(RADARR_URL, RADARR_KEY, "GET", "/movie")
                from collections import Counter
                common_profile = Counter(
                    m["qualityProfileId"] for m in (profiles.get("_body") or [])
                ).most_common(1)
                profile_id = common_profile[0][0] if common_profile else 1
                payload = {
                    "title": hit.get("title"), "tmdbId": hit.get("tmdbId"),
                    "qualityProfileId": profile_id, "rootFolderPath": "/movies",
                    "monitored": True, "minimumAvailability": "released",
                    "addOptions": {"searchForMovie": False},
                }
                r = arr(RADARR_URL, RADARR_KEY, "POST", "/movie", payload)
                notes.append(f"BEST GUESS re-add to Radarr as '{hit.get('title')}' ({hit.get('year')}) "
                             "-- please double check this is right" if "_error" not in r
                             else f"found a Radarr match but failed to add it: {r['_error']}")
    jellyfin_refresh()
    notes.append("Jellyfin refresh triggered")
    return {"notes": notes}


def restore_show_file(folder, relpath, target_folder=None):
    # `folder` is where the staged file currently lives (on disk, under
    # STAGING_DIR); `target_folder` is what it should be restored AS --
    # these can differ when the staged folder got a collision suffix at
    # staging time (e.g. "Loki.1789190697", because a "Loki" folder
    # already existed in staging from earlier), which is NOT the show's
    # real library folder name. Defaults to `folder` for the common case
    # (single-episode restore, called directly with no collision
    # involved); restore_show_folder() passes the real name explicitly
    # once it knows it. Getting this wrong doesn't just mislabel a
    # folder -- it silently creates a wrong-named folder in the live
    # library that Sonarr's own path never matches, confirmed live.
    target_folder = target_folder or folder
    staged_path = os.path.join(STAGING_DIR, "shows", folder, relpath)
    original_path = os.path.join(TV_ROOT, target_folder, relpath)
    if not os.path.isfile(staged_path):
        return {"error": f"{staged_path} not found"}
    notes = []
    if os.path.exists(original_path):
        return {"error": f"can't restore -- {original_path} already exists"}
    os.makedirs(os.path.dirname(original_path), exist_ok=True)
    shutil.move(staged_path, original_path)
    notes.append("file moved back")
    # bring back any sibling file sharing the same stem (subtitles etc.)
    stem = os.path.splitext(staged_path)[0]
    staged_dir = os.path.dirname(staged_path)
    if os.path.isdir(staged_dir):
        for fn in os.listdir(staged_dir):
            sib = os.path.join(staged_dir, fn)
            if sib != staged_path and os.path.splitext(sib)[0] == stem and os.path.isfile(sib):
                sib_original = os.path.join(os.path.dirname(original_path), fn)
                if not os.path.exists(sib_original):
                    shutil.move(sib, sib_original)
                    notes.append(f"sibling file moved back too: {fn}")

    series = arr(SONARR_URL, SONARR_KEY, "GET", "/series")
    this_series = next((s for s in (series.get("_body") or [])
                         if s.get("path", "").rstrip("/").endswith("/" + target_folder)), None) \
        if "_error" not in series else None
    if this_series:
        episodes = arr(SONARR_URL, SONARR_KEY, "GET", f"/episode?seriesId={this_series['id']}")
        eps = episodes.get("_body") or []
        basename = os.path.basename(relpath)
        target = None
        se = SXXEXX_RE.search(basename)
        if se:
            season, ep = int(se.group(1)), int(se.group(2))
            target = next((e for e in eps if e.get("seasonNumber") == season and e.get("episodeNumber") == ep), None)
        if target is None:
            for num_m in ABSOLUTE_NUM_RE.finditer(basename):
                hit = next((e for e in eps if e.get("absoluteEpisodeNumber") == int(num_m.group(1))), None)
                if hit:
                    target = hit
                    break
        if target:
            r = arr(SONARR_URL, SONARR_KEY, "PUT", "/episode/monitor",
                    {"episodeIds": [target["id"]], "monitored": True})
            notes.append("re-monitored in Sonarr" if "_error" not in r else f"failed to re-monitor: {r['_error']}")
        else:
            notes.append(f"series is tracked but couldn't identify the episode for '{basename}' -- re-monitor manually")
    else:
        # whole series isn't tracked -- likely removed entirely (media-remove).
        show_folder_path = os.path.join(STAGING_DIR, "shows", folder)
        exact = _find_exact_show_undo(show_folder_path)
        if exact:
            notes.append(f"series not tracked in Sonarr, and this show still has other files staged together "
                         f"-- use \"restore whole show\" to re-add the series from its exact saved settings")
        else:
            title_guess = YEAR_SUFFIX_RE.sub("", target_folder).strip()
            lookup = arr(SONARR_URL, SONARR_KEY, "GET", f"/series/lookup?term={urllib.parse.quote(title_guess)}")
            hit = (lookup.get("_body") or [None])[0] if "_error" not in lookup else None
            if hit:
                payload = {
                    "title": hit.get("title"), "tvdbId": hit.get("tvdbId"), "tmdbId": hit.get("tmdbId"),
                    "qualityProfileId": 4, "rootFolderPath": "/tv", "seasonFolder": True,
                    "seriesType": hit.get("seriesType", "standard"), "monitored": True,
                    "addOptions": {"monitor": "all", "searchForMissingEpisodes": False},
                }
                r = arr(SONARR_URL, SONARR_KEY, "POST", "/series", payload)
                notes.append(f"BEST GUESS re-add to Sonarr as '{hit.get('title')}' -- please double check this is right"
                             if "_error" not in r else f"found a Sonarr match but failed to add it: {r['_error']}")
            else:
                notes.append(f"series not tracked and no Sonarr match found for '{title_guess}' "
                              f"-- file is back on disk but NOT re-tracked, add it manually")
    jellyfin_refresh()
    notes.append("Jellyfin refresh triggered")
    return {"notes": notes}


def restore_show_folder(folder):
    # Whole-show restore, for when Sonarr no longer tracks the series at
    # all (media-remove fully removed it) -- re-adds the series first
    # (from an exact media-remove undo record if one exists, else a
    # best-guess lookup), THEN moves every staged file back.
    staged_folder = os.path.join(STAGING_DIR, "shows", folder)
    if not os.path.isdir(staged_folder):
        return {"error": f"{staged_folder} not found"}
    notes = []
    # `folder` (the staged directory name) is NOT necessarily the show's
    # real library folder name -- it gets a collision suffix at staging
    # time if a folder by that name already existed in staging (e.g.
    # "Loki.1789190697"). target_folder is what files actually get
    # restored AS; only known for certain from an exact undo record's own
    # original_path. Getting this wrong doesn't just mislabel a folder --
    # it silently creates a wrong-named folder in the live library that
    # Sonarr's own path never matches, confirmed live, 2026-09-12.
    exact = _find_exact_show_undo(staged_folder)
    target_folder = folder
    if exact:
        target_folder = os.path.basename(exact["record"]["original_path"].rstrip("/"))
        r = arr(SONARR_URL, SONARR_KEY, "POST", "/series", exact["record"]["restore_payload"])
        notes.append("re-added series to Sonarr (exact settings)" if "_error" not in r
                     else f"failed to re-add series: {r['_error']}")
        if "_error" not in r:
            try:
                os.remove(exact["path"])
            except FileNotFoundError:
                pass
    else:
        title_guess = YEAR_SUFFIX_RE.sub("", folder).strip()
        lookup = arr(SONARR_URL, SONARR_KEY, "GET", f"/series/lookup?term={urllib.parse.quote(title_guess)}")
        hit = (lookup.get("_body") or [None])[0] if "_error" not in lookup else None
        if hit:
            payload = {
                "title": hit.get("title"), "tvdbId": hit.get("tvdbId"), "tmdbId": hit.get("tmdbId"),
                "qualityProfileId": 4, "rootFolderPath": "/tv", "seasonFolder": True,
                "seriesType": hit.get("seriesType", "standard"), "monitored": True,
                "addOptions": {"monitor": "all", "searchForMissingEpisodes": False},
            }
            r = arr(SONARR_URL, SONARR_KEY, "POST", "/series", payload)
            notes.append(f"BEST GUESS re-add to Sonarr as '{hit.get('title')}' -- please double check this is right"
                         if "_error" not in r else f"found a Sonarr match but failed to add it: {r['_error']}")
        else:
            notes.append(f"no Sonarr match found for '{title_guess}' -- files will be moved back but NOT tracked")
    for f in _walk_files(staged_folder):
        r = restore_show_file(folder, f["relpath"], target_folder=target_folder)
        notes.extend(r.get("notes", [r.get("error", "")]))
    return {"notes": notes}


def delete_forever(kind, folder, relpath=None):
    if kind == "movie":
        target = os.path.join(STAGING_DIR, "movies", folder)
    else:
        target = os.path.join(STAGING_DIR, "shows", folder, relpath) if relpath else os.path.join(STAGING_DIR, "shows", folder)
    if not os.path.exists(target):
        return {"error": f"{target} not found"}
    if os.path.isdir(target):
        shutil.rmtree(target)
    else:
        os.remove(target)
    # Clean up any exact-undo record that now points at nothing.
    for undo_dir, prefix in ((MEDIA_REMOVE_UNDO_DIR, kind + "-"),):
        if os.path.isdir(undo_dir):
            for fname in os.listdir(undo_dir):
                if not fname.startswith(prefix):
                    continue
                p = os.path.join(undo_dir, fname)
                try:
                    rec = json.load(open(p))
                except Exception:
                    continue
                if rec.get("staged_path") == target or (rec.get("staged_path") or "").startswith(target + "/"):
                    os.remove(p)
    return {"notes": [f"permanently deleted: {target}"]}


PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Staged for Deletion</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #111; color: #eee;
         display: flex; flex-direction: column; align-items: center;
         min-height: 100vh; margin: 0; gap: 12px; padding: 24px 16px 60px; box-sizing: border-box; }
  h2 { margin: 0; width: 90vw; max-width: 700px; }
  input#search { width: 90vw; max-width: 700px; box-sizing: border-box; padding: 12px 14px;
                 font-size: 1rem; border-radius: 10px; border: none; background: #1a1a1a; color: #eee; }
  #list { width: 90vw; max-width: 700px; display: flex; flex-direction: column; gap: 8px; }
  .group { background: #1a1a1a; border-radius: 10px; padding: 10px 14px; }
  .group summary { cursor: pointer; display: flex; justify-content: space-between; align-items: center; }
  .group .meta { font-size: 0.75rem; color: #999; }
  .grpactions { display: flex; gap: 8px; margin: 10px 0; }
  .file-row { display: flex; justify-content: space-between; align-items: center;
              padding: 8px 0; border-top: 1px solid #2a2a2a; font-size: 0.85rem; }
  .file-row .name { word-break: break-word; margin-right: 8px; }
  .file-row .actions { display: flex; gap: 6px; flex-shrink: 0; }
  button { font-size: 0.8rem; padding: 8px 12px; border-radius: 8px; border: none;
           cursor: pointer; font-weight: 600; }
  .restore { background: #2c4a6e; color: white; }
  .danger { background: #a03636; color: white; }
  #toast { position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%);
           background: #222; color: #eee; padding: 12px 18px; border-radius: 10px;
           max-width: 90vw; font-size: 0.85rem; white-space: pre-line; display: none; }
</style>
</head>
<body>
  <h2>Staged for Deletion <span id="count" style="color:#999;font-weight:400;font-size:0.9rem"></span></h2>
  <input id="search" placeholder="Search...">
  <div id="list"></div>
  <div id="toast"></div>

<script>
function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
function fmtSize(bytes) {
  const gb = bytes / (1024**3);
  return gb >= 1 ? gb.toFixed(1) + ' GB' : (bytes / (1024**2)).toFixed(0) + ' MB';
}
let items = [];

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.style.display = 'none'; }, 6000);
}

function groupHTML(g) {
  const totalSize = g.files.reduce((a, f) => a + f.size, 0);
  const rows = g.files.map(f => `
    <div class="file-row">
      <span class="name">${esc(f.relpath)} <span class="meta">(${fmtSize(f.size)})</span></span>
      <span class="actions">
        <button class="restore" onclick="restoreFile('${g.type}','${esc(g.folder)}','${esc(f.relpath)}')">Restore</button>
        <button class="danger" onclick="deleteForever('${g.type}','${esc(g.folder)}','${esc(f.relpath)}')">Delete forever</button>
      </span>
    </div>`).join('');
  const groupActions = g.type === 'movie'
    ? `<button class="restore" onclick="restoreFolder('movie','${esc(g.folder)}')">Restore movie</button>
       <button class="danger" onclick="deleteForever('movie','${esc(g.folder)}',null)">Delete forever (whole folder)</button>`
    : `<button class="restore" onclick="restoreShowFolder('${esc(g.folder)}')">Restore whole show (if not tracked)</button>
       <button class="danger" onclick="deleteForever('show','${esc(g.folder)}',null)">Delete forever (whole folder)</button>`;
  return `<details class="group" data-folder="${esc(g.folder)}">
    <summary><span>${esc(g.folder)}</span><span class="meta">${g.files.length} file(s), ${fmtSize(totalSize)}</span></summary>
    <div class="grpactions">${groupActions}</div>
    ${rows}
  </details>`;
}

function render(filter) {
  const f = (filter || '').toLowerCase();
  const shown = items.filter(g => !f || g.folder.toLowerCase().includes(f));
  document.getElementById('list').innerHTML = shown.map(groupHTML).join('');
  document.getElementById('count').textContent = `(${items.length} items)`;
}

document.getElementById('search').addEventListener('input', (e) => render(e.target.value));

async function post(url, body) {
  return fetch(url, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  }).then(r => r.json());
}

async function restoreFile(type, folder, relpath) {
  const result = await post('/restore-file', {type, folder, relpath});
  toast((result.notes || [result.error]).join('\\n'));
  await init();
}

async function restoreFolder(type, folder) {
  const result = await post('/restore-folder', {type, folder});
  toast((result.notes || [result.error]).join('\\n'));
  await init();
}

async function restoreShowFolder(folder) {
  const result = await post('/restore-show-folder', {folder});
  toast((result.notes || [result.error]).join('\\n'));
  await init();
}

async function deleteForever(type, folder, relpath) {
  if (!confirm(`Permanently delete ${relpath || ('the whole ' + folder + ' folder')}? This cannot be undone.`)) return;
  const result = await post('/delete-forever', {type, folder, relpath});
  toast((result.notes || [result.error]).join('\\n'));
  await init();
}

async function init() {
  items = await fetch('/staged-list.json').then(r => r.json());
  render(document.getElementById('search').value);
}
init();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, body):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self._html(PAGE)
        elif path == "/staged-list.json":
            self._json(staged_list())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlsplit(self.path).path
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json({"error": "bad request body"}, code=400)
            return
        try:
            if path == "/restore-file":
                self._json(restore_show_file(body["folder"], body["relpath"]))
            elif path == "/restore-folder":
                if body.get("type") == "movie":
                    self._json(restore_movie_folder(body["folder"]))
                else:
                    self._json(restore_show_folder(body["folder"]))
            elif path == "/restore-show-folder":
                self._json(restore_show_folder(body["folder"]))
            elif path == "/delete-forever":
                self._json(delete_forever(body["type"], body["folder"], body.get("relpath")))
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            self._json({"error": str(e)}, code=500)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8770), Handler)
    print("Stage Control listening on :8770")
    server.serve_forever()
