#!/usr/bin/env python3
"""General-purpose "remove this show/movie from everywhere" tool --
independent of the Castilian project entirely (built 2026-09-12, after a
general question about whether such a thing existed at all: it didn't).

A single page lists every Sonarr series and Radarr movie (searchable),
tap one, confirm, and it:
  1. Moves the ENTIRE show/movie folder to a staging area (never deletes
     outright -- same reversible-by-default philosophy as castilian-
     review's stage_for_deletion(), just for "I don't want this anymore"
     instead of "this isn't Castilian").
  2. Removes it from Sonarr/Radarr (deleteFiles=false -- the move above
     already handled the file, this just stops it being tracked/re-
     grabbed).
  3. Removes the matching item from Jellyfin's library, if it can find
     one -- immediate, not waiting for Jellyfin's own next scheduled
     library scan to notice the file's gone.
  4. Resets its Jellyseerr media record (DELETE /api/v1/media/:id), so it
     shows up as freely requestable again instead of staying marked
     "available" for a title that no longer exists. Never blacklists
     anything -- confirmed live (2026-09-12) that this is exactly what a
     plain DELETE there does: after it, a search for the title comes back
     with mediaInfo: null, not some blocked/blacklisted state.

Undo reverses steps 1-2 exactly (move the folder back, re-add to Sonarr/
Radarr from a captured restore payload) and triggers a Jellyfin library
refresh so it re-discovers the file. Jellyseerr is NOT explicitly
restored -- it re-syncs its own availability from Sonarr/Radarr on its
own periodic schedule once the item is back there, so forcing it here
would just be racing that same sync for no real benefit.

Env:
    SONARR_URL / SONARR_KEY, RADARR_URL / RADARR_KEY
    JELLYFIN_URL / JELLYFIN_KEY
    JELLYSEERR_URL / JELLYSEERR_KEY
    MEDIA_TV_ROOT       default /mnt/vault/tv
    MEDIA_MOVIES_ROOT   default /mnt/vault/movies
    MEDIA_STAGING_DIR   default /mnt/vault/staged-for-deletion
    MEDIA_UNDO_DIR      default /mnt/vault/mega-staging/queue/media-remove-undo
"""
import datetime
import json
import os
import re
import shutil
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, quote

SONARR_URL = os.environ.get("SONARR_URL")
SONARR_KEY = os.environ.get("SONARR_KEY")
RADARR_URL = os.environ.get("RADARR_URL")
RADARR_KEY = os.environ.get("RADARR_KEY")
JELLYFIN_URL = os.environ.get("JELLYFIN_URL")
JELLYFIN_KEY = os.environ.get("JELLYFIN_KEY")
JELLYSEERR_URL = os.environ.get("JELLYSEERR_URL")
JELLYSEERR_KEY = os.environ.get("JELLYSEERR_KEY")

TV_ROOT = os.environ.get("MEDIA_TV_ROOT", "/mnt/vault/tv").rstrip("/")
MOVIES_ROOT = os.environ.get("MEDIA_MOVIES_ROOT", "/mnt/vault/movies").rstrip("/")
STAGING_DIR = os.environ.get("MEDIA_STAGING_DIR", "/mnt/vault/staged-for-deletion")
UNDO_DIR = os.environ.get("MEDIA_UNDO_DIR", "/mnt/vault/mega-staging/queue/media-remove-undo")

ID_PATH_RE = re.compile(r"^/(remove|undo)$")


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


def jellyfin(method, path, params=""):
    sep = "&" if "?" in path else "?"
    url = f"{JELLYFIN_URL.rstrip('/')}{path}{sep}api_key={JELLYFIN_KEY}{params}"
    return _http(method, url)


def jellyseerr(method, path, body=None):
    return _http(method, f"{JELLYSEERR_URL.rstrip('/')}{path}",
                 {"X-Api-Key": JELLYSEERR_KEY, "Content-Type": "application/json"}, body)


def _undo_path(kind, item_id):
    return os.path.join(UNDO_DIR, f"{kind}-{item_id}.json")


def _save_undo(kind, item_id, record):
    os.makedirs(UNDO_DIR, exist_ok=True)
    with open(_undo_path(kind, item_id), "w") as f:
        json.dump(record, f)


def _load_undo(kind, item_id):
    try:
        with open(_undo_path(kind, item_id)) as f:
            return json.load(f)
    except Exception:
        return None


def _clear_undo(kind, item_id):
    try:
        os.remove(_undo_path(kind, item_id))
    except FileNotFoundError:
        pass


def _stage_folder(local_path, staging_subdir):
    # Whole-folder version of castilian-review's own collision-safe move
    # -- moves the ENTIRE show/movie directory (every season, every
    # extra), not a single file, since removing something here means
    # getting rid of all of it.
    if not os.path.isdir(local_path):
        return None
    basename = os.path.basename(local_path.rstrip("/"))
    dest = os.path.join(staging_subdir, basename)
    if os.path.exists(dest):
        dest = f"{dest}.{int(datetime.datetime.now().timestamp())}"
    os.makedirs(staging_subdir, exist_ok=True)
    shutil.move(local_path, dest)
    return dest


def find_jellyfin_item(folder_basename, item_type):
    # Matched by folder name against Jellyfin's own item PATH, not title
    # -- Jellyfin displays localized (Spanish, throughout this library)
    # metadata titles that routinely don't match Sonarr/Radarr's own
    # (English) titles at all (found live testing this: Radarr says
    # "Doctor Strange in the Multiverse of Madness", Jellyfin shows
    # "Doctor Strange en el multiverso de la locura" for the exact same
    # file) -- title matching would have silently failed here. The
    # on-disk folder name is the one thing guaranteed to agree between
    # Sonarr/Radarr and Jellyfin regardless of display-language metadata.
    # AnyProviderIdEquals looked like the "correct" way to do this but
    # didn't actually filter anything when checked live, 2026-09-12.
    r = jellyfin("GET", "/Items", f"&IncludeItemTypes={item_type}&Recursive=true&Limit=5000&Fields=Path")
    if "_error" in r:
        return None
    needle = f"/{folder_basename}/"
    for it in (r.get("_body") or {}).get("Items", []):
        if needle in (it.get("Path") or ""):
            return it.get("Id")
    return None


def find_jellyseerr_media_id(tmdb_id, media_kind):
    # media_kind: "movie" or "tv" -- Jellyseerr's whole data model keys
    # off TMDB ids even for TV (not TVDB), and Sonarr's own series object
    # already exposes tmdbId directly alongside tvdbId, confirmed live --
    # no tvdbId->tmdbId conversion needed.
    r = jellyseerr("GET", f"/api/v1/{media_kind}/{tmdb_id}")
    if "_error" in r:
        return None
    media_info = (r.get("_body") or {}).get("mediaInfo")
    return media_info.get("id") if media_info else None


def remove_show(series_id):
    s = arr(SONARR_URL, SONARR_KEY, "GET", f"/series/{series_id}")
    if "_error" in s:
        return {"error": f"couldn't fetch series: {s['_error']}"}
    series = s["_body"]
    notes = []
    m = re.match(r"^/tv/(.+)$", series.get("path", ""))
    local_path = os.path.join(TV_ROOT, m.group(1)) if m else None
    staged_to = _stage_folder(local_path, os.path.join(STAGING_DIR, "shows")) if local_path else None
    notes.append(f"staged: {staged_to}" if staged_to else "folder not found on disk (nothing to move)")

    restore_payload = {
        "title": series.get("title"), "tvdbId": series.get("tvdbId"),
        "tmdbId": series.get("tmdbId"), "qualityProfileId": series.get("qualityProfileId"),
        "languageProfileId": series.get("languageProfileId"), "seasonFolder": series.get("seasonFolder"),
        "seriesType": series.get("seriesType"), "rootFolderPath": os.path.dirname(series.get("path", "")) or "/tv",
        "monitored": series.get("monitored", True),
        "addOptions": {"monitor": "all", "searchForMissingEpisodes": False},
    }
    d = arr(SONARR_URL, SONARR_KEY, "DELETE", f"/series/{series_id}?deleteFiles=false&addImportListExclusion=false")
    if "_error" in d:
        notes.append(f"failed to remove from Sonarr: {d['_error']}")
    else:
        notes.append("removed from Sonarr")

    jf_id = find_jellyfin_item(os.path.basename(local_path), "Series") if local_path else None
    if jf_id:
        r = jellyfin("DELETE", f"/Items/{jf_id}")
        notes.append("removed from Jellyfin" if "_error" not in r else f"Jellyfin removal failed: {r['_error']}")
    else:
        notes.append("not found in Jellyfin (nothing to remove there)")

    js_id = find_jellyseerr_media_id(series.get("tmdbId"), "tv") if series.get("tmdbId") else None
    if js_id:
        r = jellyseerr("DELETE", f"/api/v1/media/{js_id}")
        notes.append("reset in Jellyseerr (requestable again)" if "_error" not in r
                     else f"Jellyseerr reset failed: {r['_error']}")
    else:
        notes.append("no Jellyseerr record found")

    _save_undo("show", series_id, {
        "staged_path": staged_to, "original_path": local_path, "restore_payload": restore_payload,
    })
    return {"notes": notes}


def remove_movie(movie_id):
    m = arr(RADARR_URL, RADARR_KEY, "GET", f"/movie/{movie_id}")
    if "_error" in m:
        return {"error": f"couldn't fetch movie: {m['_error']}"}
    movie = m["_body"]
    notes = []
    mm = re.match(r"^/movies/(.+)$", movie.get("path", ""))
    local_path = os.path.join(MOVIES_ROOT, mm.group(1)) if mm else None
    staged_to = _stage_folder(local_path, os.path.join(STAGING_DIR, "movies")) if local_path else None
    notes.append(f"staged: {staged_to}" if staged_to else "folder not found on disk (nothing to move)")

    restore_payload = {
        "title": movie.get("title"), "tmdbId": movie.get("tmdbId"),
        "qualityProfileId": movie.get("qualityProfileId"),
        "rootFolderPath": os.path.dirname(movie.get("path", "")) or "/movies",
        "monitored": movie.get("monitored", True), "minimumAvailability": movie.get("minimumAvailability", "released"),
        "addOptions": {"searchForMovie": False},
    }
    d = arr(RADARR_URL, RADARR_KEY, "DELETE", f"/movie/{movie_id}?deleteFiles=false&addImportExclusion=false")
    if "_error" in d:
        notes.append(f"failed to remove from Radarr: {d['_error']}")
    else:
        notes.append("removed from Radarr")

    jf_id = find_jellyfin_item(os.path.basename(local_path), "Movie") if local_path else None
    if jf_id:
        r = jellyfin("DELETE", f"/Items/{jf_id}")
        notes.append("removed from Jellyfin" if "_error" not in r else f"Jellyfin removal failed: {r['_error']}")
    else:
        notes.append("not found in Jellyfin (nothing to remove there)")

    js_id = find_jellyseerr_media_id(movie.get("tmdbId"), "movie") if movie.get("tmdbId") else None
    if js_id:
        r = jellyseerr("DELETE", f"/api/v1/media/{js_id}")
        notes.append("reset in Jellyseerr (requestable again)" if "_error" not in r
                     else f"Jellyseerr reset failed: {r['_error']}")
    else:
        notes.append("no Jellyseerr record found")

    _save_undo("movie", movie_id, {
        "staged_path": staged_to, "original_path": local_path, "restore_payload": restore_payload,
    })
    return {"notes": notes}


def undo_show(series_id):
    undo = _load_undo("show", series_id)
    if not undo:
        return {"error": "nothing to undo for this id"}
    notes = []
    staged, original = undo.get("staged_path"), undo.get("original_path")
    if staged and original:
        if os.path.exists(original):
            notes.append(f"can't move back -- {original} already exists")
        elif os.path.isdir(staged):
            os.makedirs(os.path.dirname(original), exist_ok=True)
            shutil.move(staged, original)
            notes.append("folder moved back")
        else:
            notes.append("staged folder no longer there")
    r = arr(SONARR_URL, SONARR_KEY, "POST", "/series", undo["restore_payload"])
    notes.append("re-added to Sonarr" if "_error" not in r else f"failed to re-add to Sonarr: {r['_error']}")
    jellyfin("POST", "/Library/Refresh")
    notes.append("Jellyfin library refresh triggered")
    _clear_undo("show", series_id)
    return {"notes": notes}


def undo_movie(movie_id):
    undo = _load_undo("movie", movie_id)
    if not undo:
        return {"error": "nothing to undo for this id"}
    notes = []
    staged, original = undo.get("staged_path"), undo.get("original_path")
    if staged and original:
        if os.path.exists(original):
            notes.append(f"can't move back -- {original} already exists")
        elif os.path.isdir(staged):
            os.makedirs(os.path.dirname(original), exist_ok=True)
            shutil.move(staged, original)
            notes.append("folder moved back")
        else:
            notes.append("staged folder no longer there")
    r = arr(RADARR_URL, RADARR_KEY, "POST", "/movie", undo["restore_payload"])
    notes.append("re-added to Radarr" if "_error" not in r else f"failed to re-add to Radarr: {r['_error']}")
    jellyfin("POST", "/Library/Refresh")
    notes.append("Jellyfin library refresh triggered")
    _clear_undo("movie", movie_id)
    return {"notes": notes}


def library_list():
    items = []
    s = arr(SONARR_URL, SONARR_KEY, "GET", "/series")
    for series in (s.get("_body") or []) if "_error" not in s else []:
        items.append({"type": "show", "id": series["id"], "title": series.get("title"),
                       "year": series.get("year"), "path": series.get("path")})
    m = arr(RADARR_URL, RADARR_KEY, "GET", "/movie")
    for movie in (m.get("_body") or []) if "_error" not in m else []:
        items.append({"type": "movie", "id": movie["id"], "title": movie.get("title"),
                       "year": movie.get("year"), "path": movie.get("path")})
    items.sort(key=lambda x: (x["title"] or "").lower())
    return items


PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Remove Media</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #111; color: #eee;
         display: flex; flex-direction: column; align-items: center;
         min-height: 100vh; margin: 0; gap: 12px; padding: 24px 16px 60px; box-sizing: border-box; }
  h2 { margin: 0; width: 90vw; max-width: 600px; }
  input#search { width: 90vw; max-width: 600px; box-sizing: border-box; padding: 12px 14px;
                 font-size: 1rem; border-radius: 10px; border: none; background: #1a1a1a; color: #eee; }
  #list { width: 90vw; max-width: 600px; display: flex; flex-direction: column; gap: 6px; }
  .row { display: flex; justify-content: space-between; align-items: center; background: #1a1a1a;
         padding: 10px 14px; border-radius: 8px; cursor: pointer; }
  .row:active { background: #2a2a2a; }
  .row .type { font-size: 0.7rem; color: #999; margin-left: 8px; }
  .row.done { opacity: 0.5; }
  #modal { position: fixed; inset: 0; background: rgba(0,0,0,0.7); display: none;
           align-items: center; justify-content: center; padding: 20px; box-sizing: border-box; }
  #modal.open { display: flex; }
  #modalbox { background: #1a1a1a; border-radius: 14px; padding: 20px; max-width: 420px; width: 100%; }
  #modalbox h3 { margin-top: 0; }
  #modalbox .notes { font-size: 0.85rem; color: #bbb; white-space: pre-line; margin: 12px 0; }
  #modalbox button { font-size: 1rem; padding: 12px 20px; border-radius: 8px; border: none;
                      cursor: pointer; font-weight: 600; margin-right: 8px; }
  .danger { background: #a03636; color: white; }
  .cancel { background: #333; color: #ccc; }
  .undo { background: #2c4a6e; color: white; }
</style>
</head>
<body>
  <h2>Remove Media</h2>
  <input id="search" placeholder="Search your library...">
  <div id="list"></div>

  <div id="modal">
    <div id="modalbox">
      <h3 id="modal-title"></h3>
      <div id="modal-body"></div>
    </div>
  </div>

<script>
function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
let items = [];

function render(filter) {
  const list = document.getElementById('list');
  const f = (filter || '').toLowerCase();
  const shown = items.filter(i => !f || (i.title || '').toLowerCase().includes(f));
  list.innerHTML = shown.slice(0, 200).map(i => `
    <div class="row" data-type="${i.type}" data-id="${i.id}" onclick="openConfirm('${i.type}', ${i.id})">
      <span>${esc(i.title)}${i.year ? ' (' + i.year + ')' : ''}</span>
      <span class="type">${i.type}</span>
    </div>`).join('');
}

document.getElementById('search').addEventListener('input', (e) => render(e.target.value));

function openConfirm(type, id) {
  const item = items.find(i => i.type === type && i.id === id);
  if (!item) return;
  document.getElementById('modal-title').textContent = item.title;
  document.getElementById('modal-body').innerHTML = `
    <div class="notes">Move this ${type}'s files to staging and remove it from
    Sonarr/Radarr, Jellyfin, and Jellyseerr? This is reversible.</div>
    <button class="danger" onclick="doRemove('${type}', ${id})">Remove it</button>
    <button class="cancel" onclick="closeModal()">Cancel</button>`;
  document.getElementById('modal').classList.add('open');
}

function closeModal() {
  document.getElementById('modal').classList.remove('open');
}

async function doRemove(type, id) {
  document.getElementById('modal-body').innerHTML = '<div class="notes">Working...</div>';
  let result;
  try {
    result = await fetch('/remove', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({type, id}),
    }).then(r => r.json());
  } catch (e) {
    document.getElementById('modal-body').innerHTML = '<div class="notes">Request failed.</div>';
    return;
  }
  const notes = (result.notes || [result.error]).join('\\n');
  document.getElementById('modal-body').innerHTML = `
    <div class="notes">${esc(notes)}</div>
    <button class="undo" onclick="doUndo('${type}', ${id})">Undo</button>
    <button class="cancel" onclick="closeModal()">Close</button>`;
  const row = document.querySelector(`.row[data-type="${type}"][data-id="${id}"]`);
  if (row) row.classList.add('done');
}

async function doUndo(type, id) {
  document.getElementById('modal-body').innerHTML = '<div class="notes">Undoing...</div>';
  let result;
  try {
    result = await fetch('/undo', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({type, id}),
    }).then(r => r.json());
  } catch (e) {
    document.getElementById('modal-body').innerHTML = '<div class="notes">Request failed.</div>';
    return;
  }
  const notes = (result.notes || [result.error]).join('\\n');
  document.getElementById('modal-body').innerHTML = `<div class="notes">${esc(notes)}</div>
    <button class="cancel" onclick="closeModal()">Close</button>`;
  const row = document.querySelector(`.row[data-type="${type}"][data-id="${id}"]`);
  if (row) row.classList.remove('done');
}

document.getElementById('modal').addEventListener('click', (e) => {
  if (e.target.id === 'modal') closeModal();
});

async function init() {
  items = await fetch('/library-list.json').then(r => r.json());
  render('');
}
init();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _text(self, body, code=200):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
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

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self._html(PAGE)
        elif path == "/library-list.json":
            self._json(library_list())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in ("/remove", "/undo"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._text("bad request body", code=400)
            return
        kind, item_id = body.get("type"), body.get("id")
        if kind not in ("show", "movie") or not isinstance(item_id, int):
            self._json({"error": "type must be 'show' or 'movie', id must be an int"}, code=400)
            return
        if path == "/remove":
            fn = remove_show if kind == "show" else remove_movie
        else:
            fn = undo_show if kind == "show" else undo_movie
        try:
            self._json(fn(item_id))
        except Exception as e:
            self._json({"error": str(e)}, code=500)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8769), Handler)
    print("Media Remove listening on :8769")
    server.serve_forever()
