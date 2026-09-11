#!/usr/bin/env python3
"""Full metadata-only sweep of the whole library (TV + movies), classifying
every audio track as confirmed Castilian, confirmed NOT Castilian, or
ambiguous -- no Whisper/content verification at all (2026-09-11 decision:
metadata alone is enough; anything metadata can't decide goes straight to
a human via castilian-review instead of an expensive transcription pass).

Per file, once every one of its Spanish-candidate tracks (see
is_spanish_candidate() -- deliberately not gated on language=="spa" alone,
see its own docstring) is classified:

  - Any track confirmed Castilian -> file is fine as-is, nothing done.
    The metadata match IS the confirmation (same CASTILIAN_PATTERN/
    LATAM_PATTERN castilian-patterns.sh already uses everywhere else in
    this project), no further action needed.
  - No confirmed-Castilian track, but at least one ambiguous one -> EVERY
    ambiguous track (not just the first found -- a real file can carry
    more than one Spanish-candidate track with no clear dialect signal)
    gets its own entry in castilian-review's queue
    (VERDICT_CACHE_DIR/<hash>.json, verdict=2) for a human to judge.
    Never auto-decided.
  - No confirmed-Castilian and no ambiguous tracks (zero Spanish-candidate
    tracks at all, or every one of them confirmed LatAm/other) ->
    confirmed NOT Castilian. The file is staged for deletion (moved into
    CASTILIAN_STAGING_DIR, mirroring its library-relative path -- same
    layout castilian-review's stage_for_deletion() uses) and Sonarr/
    Radarr are synced (unmonitor episode / remove movie) the same way
    that function does. Duplicated here rather than imported from
    castilian-review's app.py -- this runs as a plain host script (the
    Jenkins "host" agent is a native process, not a container on the
    docker network app.py lives on -- see castilian.env's own comment for
    why it hits the public https://*.bjtn.xyz hostnames instead of
    internal container names), and matches this project's existing
    practice of duplicating a small, already-tested chunk of detection/
    action logic across scripts rather than coupling them (see
    castilian-coverage-report.sh's header for the same reasoning against
    sharing with archive-castilian-audio.sh).

Multi-track files are fully supported -- EVERY audio track in a file is
independently evaluated, not just the first Spanish-looking one. Verified
directly against a real multi-track file in this library (The Legend of
Vox Machina S01E01, 13 audio tracks including both an es-419 LatAm track
and a genuine es-ES Castilian one) as part of building this script.

Idempotent / safe to re-run on a schedule: a file that already has ANY
review-cache entry (a prior scan's ambiguous flag, resolved or not) is
left alone entirely -- never re-classified, never re-staged, no duplicate
review entries, and a human's past verdict is never revisited. A file
already staged is physically gone from the library roots, so it's simply
never seen again by the file walk.

Usage:
    castilian-scan.py [--dry-run]

    --dry-run   classify and print what WOULD happen, but never move a
                file, write a review entry, or call Sonarr/Radarr.

Env:
    CASTILIAN_TV_ROOT            default /mnt/vault/tv
    CASTILIAN_MOVIES_ROOT        default /mnt/vault/movies
    CASTILIAN_STAGING_DIR        default /mnt/vault/staged-for-deletion
    CASTILIAN_VERDICT_CACHE_DIR  default /mnt/vault/mega-staging/queue/
                                 castilian-verdict-cache (same cache
                                 castilian-review's review page reads)
    SONARR_URL / SONARR_KEY, RADARR_URL / RADARR_KEY
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TV_ROOT = os.environ.get("CASTILIAN_TV_ROOT", "/mnt/vault/tv")
MOVIES_ROOT = os.environ.get("CASTILIAN_MOVIES_ROOT", "/mnt/vault/movies")
STAGING_DIR = os.environ.get("CASTILIAN_STAGING_DIR", "/mnt/vault/staged-for-deletion")
VERDICT_CACHE_DIR = os.environ.get(
    "CASTILIAN_VERDICT_CACHE_DIR", "/mnt/vault/mega-staging/queue/castilian-verdict-cache")
LIBRARY_ROOTS = {
    TV_ROOT.rstrip("/") + "/": ("show", os.path.join(STAGING_DIR, "shows")),
    MOVIES_ROOT.rstrip("/") + "/": ("movie", os.path.join(STAGING_DIR, "movies")),
}
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi"}

SONARR_URL = os.environ.get("SONARR_URL")
SONARR_KEY = os.environ.get("SONARR_KEY")
RADARR_URL = os.environ.get("RADARR_URL")
RADARR_KEY = os.environ.get("RADARR_KEY")

SXXEXX_RE = re.compile(r"[Ss](\d{1,3})[Ee](\d{1,3})")
ABSOLUTE_NUM_RE = re.compile(r"(?<!\d)(\d{2,4})(?!\d)")

DRY_RUN = "--dry-run" in sys.argv


def log(msg):
    print(f"[{datetime.datetime.now().strftime('%F %T')}] castilian-scan: {msg}", flush=True)


# -- shared pattern constants, sourced from castilian-patterns.sh at
# runtime (not duplicated as string literals here) so this can never drift
# from the bash copies every other script in this project uses -- see that
# file's own header for why it's kept to just these two strings.
def load_patterns():
    r = subprocess.run(
        ["bash", "-c", f"source {SCRIPT_DIR}/castilian-patterns.sh && "
                        f'printf "%s\\n%s" "$CASTILIAN_PATTERN" "$LATAM_PATTERN"'],
        capture_output=True, text=True, check=True)
    cas, lat = r.stdout.split("\n", 1)
    return cas, lat


CASTILIAN_PATTERN, LATAM_PATTERN = load_patterns()


# Spanish-specific title signals safe to use even with NO language tag at
# all -- everything in CASTILIAN_PATTERN/LATAM_PATTERN EXCEPT the purely
# regional/variant words (european, peninsular, iberian, neutro, neutral)
# that other languages' own tracks legitimately use too, to qualify THEIR
# variant (a French or Portuguese track titled "European", distinguishing
# it from a Canadian/Brazilian variant of that same language, is common in
# real multi-language releases -- seen directly: Vox Machina S01E01 has a
# French track titled "European" and a Portuguese one titled "Brazilian"/
# "European", right alongside the actual es-ES Spanish "European" track).
# An earlier version of this function ran CASTILIAN_PATTERN's full text
# search against every track regardless of language and wrongly classified
# that French track as confirmed Castilian; a later, over-corrected
# version excluded the patterns entirely and would have missed a track
# titled bare "Castellano" or "Latino" with no language tag at all. This
# keeps the Spanish-unambiguous terms (a French/Portuguese/German track
# would never plausibly be titled "Castellano" or "Hispanoamericano") and
# drops only the region-only words that actually collided in practice.
SPANISH_TITLE_RE = re.compile(
    r"spanish|espa[nñ]ol|\bspa\b|castellano|castilian|espa.a|\bspain\b|"
    r"latino|lat[.-]?am|latin[ .-]?america|es[-]?419|mexic|hispanoamerican",
    re.I)


def is_spanish_candidate(lang, ietf, title):
    # Deliberately broader than language=="spa" alone -- the first version
    # of this sweep (a one-off scratch script, 2026-09-11) only looked at
    # tracks tagged language=="spa", and missed a real, genuinely-Spanish
    # track tagged "und" with no language code at all, titled only
    # "1.[Spa][192kbps,...]" (Courage the Cowardly Dog S01E05E06 -- found
    # by hand afterward and patched with a one-off special case at the
    # time). Checking the title for an explicit Spanish marker too, not
    # just the language tag, catches that the same way from the start --
    # but ONLY an explicit Spanish marker (see SPANISH_TITLE_RE above),
    # not the broader dialect-disambiguation patterns classify_track()
    # uses once a track is already known to be Spanish.
    if (lang or "").lower() == "spa":
        return True
    if ietf and ietf.lower().startswith("es"):
        return True
    if title and SPANISH_TITLE_RE.search(title):
        return True
    return False


def classify_track(lang, ietf, title):
    # "castilian" | "latam" | "ambiguous". ietf wins outright when it's a
    # real es-XX BCP47 tag (es-ES is Castilian, any other es-XX is not --
    # matches mux-castilian-audio.sh/archive-castilian-audio.sh's own
    # es-ES-is-authoritative convention), falling back to title/lang text
    # against the two shared patterns otherwise. Both patterns matching
    # (contradictory) or neither matching (no real signal) both mean
    # "ambiguous", not a guess either way.
    if ietf:
        m = re.match(r"^es-([A-Za-z]{2}|\d{3})$", ietf, re.I)
        if m:
            return "castilian" if m.group(1).lower() == "es" else "latam"
    text = f"{title or ''} {lang or ''}"
    cas = bool(re.search(CASTILIAN_PATTERN, text, re.I))
    lat = bool(re.search(LATAM_PATTERN, text, re.I))
    if cas and not lat:
        return "castilian"
    if lat and not cas:
        return "latam"
    return "ambiguous"


def get_audio_tracks(file_path):
    try:
        r = subprocess.run(["mkvmerge", "-J", file_path], capture_output=True, text=True, timeout=60)
        info = json.loads(r.stdout)
    except Exception as e:
        log(f"couldn't probe {file_path}: {e}")
        return None
    return [t for t in info.get("tracks", []) if t.get("type") == "audio"]


def _arr_request(base_url, api_key, method, path, data=None):
    if not base_url or not api_key:
        return {"_error": "URL/API key not configured"}
    url = f"{base_url.rstrip('/')}/api/v3{path}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "X-Api-Key": api_key, "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except Exception as e:
        return {"_error": str(e)}


def sync_sonarr_episode(file_path):
    # Same matching logic as castilian-review's app.py sync_sonarr_episode
    # (SxxExx first, absolute-numbering fallback) -- live-tested there
    # against both real SxxExx files and absolute-numbered ones
    # (Detective Conan) before this copy was written.
    m = re.match(re.escape(TV_ROOT.rstrip("/")) + r"/([^/]+)/", file_path)
    if not m:
        return "not under TV root -- skipped Sonarr sync"
    show_folder = m.group(1)
    series = _arr_request(SONARR_URL, SONARR_KEY, "GET", "/series")
    if isinstance(series, dict) and "_error" in series:
        return f"Sonarr unreachable ({series['_error']}) -- unmonitor manually"
    sid = next((s["id"] for s in series if s.get("path", "").rstrip("/").endswith("/" + show_folder)), None)
    if sid is None:
        return f"'{show_folder}' not tracked in Sonarr -- nothing to sync"
    episodes = _arr_request(SONARR_URL, SONARR_KEY, "GET", f"/episode?seriesId={sid}")
    if isinstance(episodes, dict) and "_error" in episodes:
        return f"couldn't fetch Sonarr episodes ({episodes['_error']}) -- unmonitor manually"
    basename = os.path.basename(file_path)
    target = None
    se = SXXEXX_RE.search(basename)
    if se:
        season, ep = int(se.group(1)), int(se.group(2))
        target = next((e for e in episodes
                        if e.get("seasonNumber") == season and e.get("episodeNumber") == ep), None)
    if target is None:
        for num_m in ABSOLUTE_NUM_RE.finditer(basename):
            hit = next((e for e in episodes if e.get("absoluteEpisodeNumber") == int(num_m.group(1))), None)
            if hit:
                target = hit
                break
    if target is None:
        return f"couldn't identify the episode for '{basename}' in Sonarr -- unmonitor manually"
    if not target.get("monitored"):
        return f"already unmonitored in Sonarr ({show_folder})"
    r = _arr_request(SONARR_URL, SONARR_KEY, "PUT", "/episode/monitor",
                      {"episodeIds": [target["id"]], "monitored": False})
    if isinstance(r, dict) and "_error" in r:
        return f"found the episode but failed to unmonitor it ({r['_error']})"
    return f"unmonitored in Sonarr ({show_folder})"


def sync_radarr_movie(file_path):
    m = re.match(re.escape(MOVIES_ROOT.rstrip("/")) + r"/([^/]+)/", file_path)
    if not m:
        return "not under movies root -- skipped Radarr sync"
    movie_folder = m.group(1)
    movies = _arr_request(RADARR_URL, RADARR_KEY, "GET", "/movie")
    if isinstance(movies, dict) and "_error" in movies:
        return f"Radarr unreachable ({movies['_error']}) -- remove manually"
    target = next((mv for mv in movies if mv.get("path", "").rstrip("/").endswith("/" + movie_folder)), None)
    if target is None:
        return f"'{movie_folder}' not tracked in Radarr -- nothing to sync"
    r = _arr_request(RADARR_URL, RADARR_KEY, "DELETE",
                      f"/movie/{target['id']}?deleteFiles=false&addImportExclusion=false")
    if isinstance(r, dict) and "_error" in r:
        return f"found the movie but failed to remove it from Radarr ({r['_error']})"
    return f"removed from Radarr ({movie_folder})"


def stage_file(file_path, root, staging_subdir):
    rel = file_path[len(root):]
    dest = os.path.join(staging_subdir, rel)
    if os.path.exists(dest):
        st = os.stat(file_path)
        stem, ext = os.path.splitext(dest)
        dest = f"{stem}.{int(st.st_mtime)}-{st.st_size}{ext}"
    if DRY_RUN:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(file_path, dest)
    return dest


def write_review_entry(file_path, audio_idx, show_or_movie):
    resolved = os.path.realpath(file_path)
    key = hashlib.sha256(f"{resolved}::{audio_idx}".encode()).hexdigest()
    entry_path = os.path.join(VERDICT_CACHE_DIR, f"{key}.json")
    if os.path.exists(entry_path):
        return  # already queued (idempotency guard doubles up here too)
    entry = {
        "source_path": file_path,
        "audio_idx": audio_idx,
        "verdict": 2,
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "basename": os.path.basename(file_path),
        "mode": "library_review",
        "show_or_movie": show_or_movie,
    }
    if DRY_RUN:
        return
    os.makedirs(VERDICT_CACHE_DIR, exist_ok=True)
    with open(entry_path, "w") as f:
        json.dump(entry, f)


def load_already_seen_paths():
    # Every resolved path with ANY existing cache entry (ambiguous,
    # human-reviewed, whatever) -- a whole file gets skipped entirely if
    # it's in here, so a past scan or human decision is never revisited
    # and nothing gets double-queued or double-staged.
    seen = set()
    if not os.path.isdir(VERDICT_CACHE_DIR):
        return seen
    for name in os.listdir(VERDICT_CACHE_DIR):
        if not name.endswith(".json") or name.endswith(".human_verdict.json"):
            continue
        try:
            with open(os.path.join(VERDICT_CACHE_DIR, name)) as f:
                data = json.load(f)
            sp = data.get("source_path")
            if sp:
                seen.add(os.path.realpath(sp))
        except Exception:
            continue
    return seen


def scan_root(root, kind):
    stats = {"castilian": 0, "staged": 0, "queued_ambiguous": 0, "skipped_seen": 0, "probe_failed": 0}
    already_seen = load_already_seen_paths()
    root = root.rstrip("/") + "/"
    _, staging_subdir = LIBRARY_ROOTS[root]
    if not os.path.isdir(root):
        log(f"root does not exist, skipping: {root}")
        return stats
    for name in sorted(os.listdir(root)):
        item_dir = os.path.join(root, name)
        if not os.path.isdir(item_dir):
            continue
        for dirpath, _, filenames in os.walk(item_dir):
            for fname in sorted(filenames):
                if os.path.splitext(fname)[1].lower() not in VIDEO_EXTS:
                    continue
                file_path = os.path.join(dirpath, fname)
                if os.path.realpath(file_path) in already_seen:
                    stats["skipped_seen"] += 1
                    continue
                audio_tracks = get_audio_tracks(file_path)
                if audio_tracks is None:
                    stats["probe_failed"] += 1
                    continue
                verdicts = []
                for idx, t in enumerate(audio_tracks):
                    p = t.get("properties", {})
                    lang, ietf, title = p.get("language"), p.get("language_ietf"), p.get("track_name")
                    if not is_spanish_candidate(lang, ietf, title):
                        continue
                    verdicts.append((idx, classify_track(lang, ietf, title)))
                castilian_idxs = [i for i, v in verdicts if v == "castilian"]
                ambiguous_idxs = [i for i, v in verdicts if v == "ambiguous"]
                if castilian_idxs:
                    stats["castilian"] += 1
                    continue
                if ambiguous_idxs:
                    for idx in ambiguous_idxs:
                        write_review_entry(file_path, idx, name)
                        log(f"{'[dry-run] would queue' if DRY_RUN else 'queued'} for review: "
                            f"{file_path} (track {idx})")
                    stats["queued_ambiguous"] += len(ambiguous_idxs)
                    continue
                # No Castilian, no ambiguous -- confirmed not Castilian.
                dest = stage_file(file_path, root, staging_subdir)
                action = "would stage" if DRY_RUN else "staged"
                log(f"[dry-run] {action}: {file_path} -> {dest}" if DRY_RUN else f"{action}: {file_path} -> {dest}")
                if not DRY_RUN:
                    sync_note = sync_sonarr_episode(file_path) if kind == "show" else sync_radarr_movie(file_path)
                    log(f"  arr sync: {sync_note}")
                stats["staged"] += 1
    return stats


def main():
    log(f"starting{' (DRY RUN)' if DRY_RUN else ''} -- TV: {TV_ROOT}, movies: {MOVIES_ROOT}")
    total = {}
    for root, (kind, _) in LIBRARY_ROOTS.items():
        stats = scan_root(root, kind)
        log(f"{kind} root {root}: {stats}")
        for k, v in stats.items():
            total[k] = total.get(k, 0) + v
    log(f"done. totals: {total}")


if __name__ == "__main__":
    main()
