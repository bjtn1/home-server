#!/usr/bin/env python3
"""
Idempotent library-conversion pipeline for Castilian Spanish media libraries.

For every video file under the given root path(s):
  - If it's a 4:3 (or near-4:3) video, stretch it to 16:9 (no crop, no pillarbox).
    Already-16:9 (or otherwise non-4:3) video is left untouched.
  - BAKED-IN SIDE BARS ("fake 16:9"): a file that DECLARES 16:9 but whose picture is a 4:3 image with black bars
    burned into the left/right of the frame (Sargento Keroro: 480x360 picture inside 640x360) is detected with
    ffmpeg cropdetect at 5 points; the bars are cropped off and the picture stretched to fill 16:9 (same policy
    as declared-4:3 files). The crop is MEASURED per file, the output is re-measured (bars must be gone) before
    the original is replaced, and results are cached per file so re-runs are cheap. Top/bottom bars
    (cinematic letterbox, e.g. Loki) are NOT touched.
  - If it isn't already a Matroska (.mkv) container, remux/convert it to one.
  - Tag the CASTILIAN audio track as Spanish (legacy `spa` + IETF `es-ES`).
    ONLY that one track: which one is decided by pick_spanish_audio()
    (track NAME beats the es-ES tag beats other Spanish tags; English/Japanese/
    Latino/commentary names are excluded). Other audio tracks are never touched.
    If no track qualifies (e.g. an English-only file) or several rank equally, NOTHING
    is tagged and the file is reported (AUDIO_AMBIGUOUS) for a human -- a wrong tag is
    worse than a missing one.

Safe to re-run on the same tree repeatedly (e.g. from Jenkins): each file's CURRENT
on-disk state is inspected every run, so finished files are skipped.

Safety rules (this tool edits in place and keeps no backup of originals):
  - The original is deleted ONLY after the replacement is fully written, verified,
    tagged and moved into place; an original is never the thing a failure cleanup
    removes. Same-path replacement uses an atomic os.replace().
  - Never overwrites an existing file of a different name (foo.avi + foo.mkv ->
    TARGET_EXISTS, both untouched).
  - Skips files modified in the last --min-age-minutes (default 30): something may
    still be copying them (a half-copied file must never be remuxed).
  - Files with more than one real video stream are left alone (MULTI_VIDEO).
  - Stretch keeps attachments (embedded fonts for ASS subtitles), chapters and
    dispositions; cover-art "video" streams are not pushed through the filter.
  - Single-run lock so two runs can't overlap.

No renaming, no episode-title lookup.
Requires ffmpeg, ffprobe, mkvmerge, and mkvpropedit on PATH.

Usage:
    python3 castilianize.py /mnt/vault/tv /mnt/vault/movies
    python3 castilianize.py --dry-run /mnt/vault/tv
"""

import argparse
import fcntl
import json
import logging
import os
import shutil
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Castilian-track picker. Language TAGS are unreliable (an Inazuma Eleven file has "Castellano" and
# "Japonés" both tagged spa/es-ES), so pick_spanish_audio() reads the track NAME first.
SPANISH_CODES = {"es", "spa", "esp", "spanish", "español", "espanol", "castellano",
                 "es-es", "es-419", "es-mx", "sp"}
# words in a track name that mean "this is NOT the Castilian dub"
NOT_CASTILIAN_NAME = re.compile(
    r"jap|jpn|nippon|orig|\bv\.?o\.?\b|ingl|engl|\beng\b|\[eng\]|franc|french|alem|german|ital|catal|"
    r"portug|coreano|korean|chin|latino|latam|latinoam|mexic|argent|\bmx\b|comentari|commentar",
    re.I)
CASTILIAN_NAME = re.compile(r"castell|castilian|espa[nñ]a|peninsular|\bes-es\b|spain", re.I)
TRUE_CASTILIAN_TAG = ("es-es",)
LATAM_TAG_PREFIX = ("es-419", "es-mx", "es-ar", "es-co", "es-cl")


def mkv_info(path):
    r = subprocess.run(["mkvmerge", "-J", path], capture_output=True, text=True)
    if r.returncode not in (0, 1):
        raise RuntimeError(f"mkvmerge failed on {path}: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)


def _is_spanish_tag(props):
    lang = (props.get("language") or "").lower()
    ietf = (props.get("language_ietf") or "").lower()
    return lang in SPANISH_CODES or ietf.split("-")[0] in ("es", "spa")


def pick_spanish_audio(tracks):
    """-> (audio_relative_index, track_name, note) or (None, None, reason).
    audio_relative_index is the N for ffmpeg's `-map 0:a:N`."""
    audio = [t for t in tracks if t["type"] == "audio"]
    cands = []  # (audio_relative_index, name, tier) -- lower tier = stronger evidence
    for n, t in enumerate(audio):
        p = t["properties"]
        name = p.get("track_name") or ""
        ietf = (p.get("language_ietf") or "").lower()
        lang = (p.get("language") or "").lower()
        if NOT_CASTILIAN_NAME.search(name):
            continue
        if ietf.startswith(LATAM_TAG_PREFIX):
            continue
        # Tier 1: the track NAME says Castilian. Tier 2: only the es-ES tag says so
        # (tags are unreliable: English/Japanese tracks are often tagged spa es-ES).
        # Tier 3: other Spanish-tagged or untagged-unnamed tracks.
        if CASTILIAN_NAME.search(name):
            cands.append((n, name, 1))
        elif ietf in TRUE_CASTILIAN_TAG:
            cands.append((n, name, 2))
        elif _is_spanish_tag(p) or (lang in ("und", "") and not name):
            cands.append((n, name, 3))
    if not cands:
        return None, None, "no Spanish/Castilian audio track (after excluding by name)"
    best = min(c[2] for c in cands)
    pool = [c for c in cands if c[2] == best]
    if len(pool) > 1:
        return pool[0][0], pool[0][1], f"AMBIGUOUS: {len(pool)} equally-ranked candidate tracks, took first"
    return pool[0][0], pool[0][1], "ok"


VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v",
    ".mpg", ".mpeg", ".webm", ".ts",
}

TARGET_LANG_LEGACY = "spa"
TARGET_LANG_IETF = "es-ES"
LOCK_PATH = os.environ.get("CASTILIANIZE_LOCK", "/tmp/castilianize.lock")

log = logging.getLogger("castilianize")


def find_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        sys.exit(f"Required tool '{name}' not found on PATH. Install it and retry.")
    return path


class Tools:
    def __init__(self):
        self.ffmpeg = find_tool("ffmpeg")
        self.ffprobe = find_tool("ffprobe")
        self.mkvmerge = find_tool("mkvmerge")
        self.mkvpropedit = find_tool("mkvpropedit")


def find_video_files(roots):
    for root in roots:
        root = Path(root)
        if not root.exists():
            log.warning("Root does not exist, skipping: %s", root)
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
                yield path


def probe(tools: Tools, path: Path) -> Optional[dict]:
    """-> {'width','height','dar','video_streams','audio_tracks':[{'ordinal','language','ietf','name'}]}
    or None on failure. ordinal is 1-based among audio tracks (mkvpropedit track:aN)."""
    try:
        out = subprocess.run(
            [tools.ffprobe, "-v", "error", "-print_format", "json", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        log.error("ffprobe failed on %s: %s", path, e)
        return None

    streams = data.get("streams", [])
    real_video = [s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")]
    if not real_video:
        return None
    video = real_video[0]

    if path.suffix.lower() == ".mkv":
        # mkvmerge exposes the real track name and IETF tag (ffprobe does not)
        try:
            tracks = [t for t in mkv_info(str(path))["tracks"] if t["type"] == "audio"]
        except Exception as e:  # noqa: BLE001
            log.error("mkvmerge -J failed on %s: %s", path, e)
            return None
        audio = [{"ordinal": i, "language": (t["properties"].get("language") or "und"),
                  "ietf": t["properties"].get("language_ietf"),
                  "name": t["properties"].get("track_name") or ""}
                 for i, t in enumerate(tracks, start=1)]
    else:
        audio = [{"ordinal": i, "language": s.get("tags", {}).get("language", "und"),
                  "ietf": None, "name": s.get("tags", {}).get("title", "")}
                 for i, s in enumerate((s for s in streams if s.get("codec_type") == "audio"), start=1)]
    return {"width": video.get("width"), "height": video.get("height"),
            "dar": video.get("display_aspect_ratio"), "video_streams": len(real_video),
            "audio_tracks": audio}


def classify_aspect(width, height, dar_str) -> str:
    """'stretch' (needs 4:3->16:9), 'leave' (already ~16:9), or 'unusual' (neither, don't touch AR)."""
    ratio = None
    if dar_str and dar_str != "N/A" and ":" in dar_str:
        try:
            num, den = dar_str.split(":")
            ratio = float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            ratio = None
    if ratio is None and width and height:
        ratio = width / height
    if ratio is None:
        return "unusual"

    target_43 = 4.0 / 3.0
    target_169 = 16.0 / 9.0
    d43 = abs(ratio - target_43) / target_43
    d169 = abs(ratio - target_169) / target_169
    if d43 <= 0.10 and d43 <= d169:
        return "stretch"
    if d169 <= 0.10:
        return "leave"
    return "unusual"


BAR_CACHE = os.environ.get("CASTILIANIZE_BAR_CACHE", os.path.expanduser("~/.cache/castilianize-bars.json"))
BAR_SAMPLES = (0.12, 0.3, 0.5, 0.7, 0.88)
_bar_cache = None


def is_pillarbox(w, h, cw, ch, x) -> bool:
    """Pure decision: is a measured content box (cw x ch at x) a 4:3 picture with symmetric side bars in a wide frame?
    cropdetect boxes only ever SHRINK on dark scenes, so callers pass the WIDEST box seen across samples."""
    if not (w and h and cw and ch) or w / h < 1.6:            # frame must be wide
        return False
    left, right = x, w - (x + cw)
    return (ch >= 0.96 * h                                      # full height
            and cw <= 0.86 * w                                  # a real bar on the sides
            and abs(cw / ch - 4 / 3) < 0.06                     # the picture itself is ~4:3
            and left >= 0.05 * w and right >= 0.05 * w         # bars on BOTH sides...
            and abs(left - right) <= 0.05 * w)                  # ...and roughly equal (centred picture)


def measure_content_box(tools: Tools, path: Path):
    """Widest content box across BAR_SAMPLES points -> (cw, ch, x) or None if it cannot be measured."""
    try:
        d = subprocess.run([tools.ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=60).stdout.strip()
        dur = float(d)
    except (ValueError, subprocess.TimeoutExpired, OSError):
        return None
    boxes = []
    for frac in BAR_SAMPLES:
        t = max(1.0, dur * frac)
        try:
            r = subprocess.run([tools.ffmpeg, "-v", "info", "-ss", f"{t:.1f}", "-i", str(path), "-t", "2",
                                "-vf", "cropdetect=limit=24:round=2:reset=0", "-an", "-f", "null", "-"],
                               capture_output=True, text=True, timeout=180)
        except (subprocess.TimeoutExpired, OSError):
            continue
        m = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", r.stderr)
        if m:
            boxes.append(tuple(int(v) for v in m[-1]))
    if len(boxes) < 3:
        return None
    cw = max(b[0] for b in boxes)
    ch = max(b[1] for b in boxes)
    xs = sorted(b[2] for b in boxes if b[0] == cw)
    return cw, ch, xs[len(xs) // 2]


def _load_bar_cache():
    global _bar_cache
    if _bar_cache is None:
        try:
            with open(BAR_CACHE, encoding="utf-8") as f:
                _bar_cache = json.load(f)
        except (OSError, ValueError):
            _bar_cache = {}
    return _bar_cache


def _save_bar_cache():
    try:
        os.makedirs(os.path.dirname(BAR_CACHE), exist_ok=True)
        tmp = BAR_CACHE + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_bar_cache, f)
        os.replace(tmp, BAR_CACHE)
    except OSError as e:
        log.warning("could not save bar cache: %s", e)


def bars_crop(tools: Tools, path: Path, width, height):
    """-> (cw, ch, x, y) crop that removes baked-in side bars, or None (no bars / cannot tell). Cached per file."""
    st = path.stat()
    key = f"{path}|{st.st_size}|{int(st.st_mtime)}"
    cache = _load_bar_cache()
    if key not in cache:
        t0 = time.time()
        box = measure_content_box(tools, path)
        if box is None:
            log.debug("  bars: could not measure (fewer than 3 usable samples) -- left alone, not cached")
            return None                                          # transient failure: do not cache, do not touch
        cw, ch, x = box
        log.debug("  bars: measured widest picture %dx%d at x=%d inside %sx%s (%.1fs) -> %s", cw, ch, x, width, height,
                  time.time() - t0, "PILLARBOX (side bars)" if is_pillarbox(width, height, cw, ch, x) else "no side bars")
        if is_pillarbox(width, height, cw, ch, x):
            # shrink 2 px on each side (cropdetect can include a dark fringe) and keep everything even
            cache[key] = [(cw - 4) // 2 * 2, ch // 2 * 2, (x + 2) // 2 * 2, 0]
        else:
            cache[key] = []
        _save_bar_cache()
    else:
        log.debug("  bars: cached result -> %s", f"crop {cache[key]}" if cache[key] else "no side bars")
    v = cache[key]
    return tuple(v) if v else None


def bars_gone(tools: Tools, path: Path) -> bool:
    """After a fix: the output must measure as bar-free (a stale cache entry must not vouch for it)."""
    box = measure_content_box(tools, path)
    if box is None:
        return False
    try:
        info = subprocess.run([tools.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                               "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=60).stdout.strip().split(",")
        w, h = int(info[0]), int(info[1])
    except (ValueError, IndexError, subprocess.TimeoutExpired, OSError):
        return False
    return not is_pillarbox(w, h, *box)


def audio_tag_plan(audio_tracks: list) -> dict:
    """Decide which SINGLE audio track (if any) is the Castilian one and whether it still
    needs the spa/es-ES tag. -> {'target': ordinal|None, 'needs': bool, 'note': str}"""
    fake = [{"type": "audio", "properties": {"track_name": t["name"], "language": t["language"],
                                             "language_ietf": t["ietf"] or ""}} for t in audio_tracks]
    idx, _name, note = pick_spanish_audio(fake)
    if idx is None:
        return {"target": None, "needs": False, "note": note}
    if note.startswith("AMBIGUOUS"):
        return {"target": None, "needs": False, "note": note}
    t = audio_tracks[idx]
    done = (t["language"].lower() == TARGET_LANG_LEGACY
            and (t["ietf"] or "").lower() == TARGET_LANG_IETF.lower())
    return {"target": t["ordinal"], "needs": not done, "note": "ok"}


def tag_audio(tools: Tools, path: Path, ordinal: int):
    r = subprocess.run(
        [tools.mkvpropedit, str(path), "--edit", f"track:a{ordinal}",
         "--set", f"language={TARGET_LANG_LEGACY}",
         "--set", f"language-ietf={TARGET_LANG_IETF}"],
        capture_output=True, timeout=120,
    )
    if r.returncode not in (0, 1):  # mkvpropedit: 0 ok, 1 warnings, 2 error
        raise RuntimeError(f"mkvpropedit failed on {path}: {r.stderr.decode(errors='replace')[:200]}")


def verify_output(tools: Tools, path: Path, min_size=1000) -> bool:
    if not path.exists() or path.stat().st_size < min_size:
        return False
    try:
        out = subprocess.run(
            [tools.ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return bool(out.stdout.strip())
    except (subprocess.TimeoutExpired, OSError):
        return False


def _report(tool: str, result) -> bool:
    """Log why an external tool failed (last lines of its stderr); returns True if it succeeded."""
    ok = result.returncode == 0 or (tool == "mkvmerge" and result.returncode == 1)
    if not ok:
        err = result.stderr.decode(errors="replace") if isinstance(result.stderr, bytes) else (result.stderr or "")
        log.warning("  %s failed (exit %d): %s", tool, result.returncode, " | ".join(err.strip().splitlines()[-5:]) or "no stderr")
    return ok


def stretch_filter() -> str:
    # Preserve pixel height, widen to hit exactly 16:9, force square pixels.
    return "scale=trunc(ih*16/9/2)*2:ih,setsar=1"


def run_ffmpeg_stretch(tools: Tools, src: Path, dst: Path) -> bool:
    result = subprocess.run(
        [tools.ffmpeg, "-y", "-i", str(src),
         # 0:V = video streams EXCLUDING attached pictures (cover art)
         "-map", "0:V", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-vf", stretch_filter(), "-c:a", "copy", "-c:s", "copy", "-c:t", "copy", str(dst)],
        capture_output=True, timeout=6 * 3600,
    )
    return _report("ffmpeg", result) and verify_output(tools, dst)


def run_ffmpeg_crop_stretch(tools: Tools, src: Path, dst: Path, crop) -> bool:
    """Crop the baked-in side bars, then widen the remaining ~4:3 picture to exactly 16:9 at the same height."""
    cw, ch, x, y = crop
    vf = f"crop={cw}:{ch}:{x}:{y},scale=trunc(ih*16/9/2)*2:ih:flags=lanczos,setsar=1"
    result = subprocess.run(
        [tools.ffmpeg, "-y", "-i", str(src),
         "-map", "0:V", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-vf", vf, "-c:a", "copy", "-c:s", "copy", "-c:t", "copy", str(dst)],
        capture_output=True, timeout=6 * 3600,
    )
    return _report("ffmpeg", result) and verify_output(tools, dst) and bars_gone(tools, dst)


def run_mkvmerge_remux(tools: Tools, src: Path, dst: Path) -> bool:
    result = subprocess.run([tools.mkvmerge, "-o", str(dst), str(src)],
                            capture_output=True, timeout=3600)
    return _report("mkvmerge", result) and verify_output(tools, dst)


def run_ffmpeg_remux(tools: Tools, src: Path, dst: Path) -> bool:
    result = subprocess.run(
        [tools.ffmpeg, "-y", "-i", str(src),
         "-map", "0:v", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?", "-c", "copy", str(dst)],
        capture_output=True, timeout=3600,
    )
    return _report("ffmpeg", result) and verify_output(tools, dst)


def process_file(tools: Tools, path: Path, dry_run: bool, min_age_minutes: float = 30) -> str:
    """Returns a short status string for logging."""
    if ".converting." in path.name:
        return "SKIPPED_LEFTOVER_TEMP"
    if min_age_minutes and (time.time() - path.stat().st_mtime) < min_age_minutes * 60:
        return "SKIPPED_RECENT"

    info = probe(tools, path)
    if info is None:
        return "PROBE_FAILED"

    aspect = classify_aspect(info["width"], info["height"], info["dar"])
    multi_video = info["video_streams"] > 1
    log.debug("%s", path)
    log.debug("  video: %sx%s, display aspect %s -> %s%s; container %s", info["width"], info["height"], info["dar"] or "n/a",
              {"stretch": "4:3, needs stretch", "leave": "~16:9, keep", "unusual": "unusual shape, keep"}[aspect],
              f"; {info['video_streams']} video streams" if multi_video else "", path.suffix.lower())
    for t in info["audio_tracks"]:
        log.debug("  audio a%d: %s/%s %r", t["ordinal"], t["language"], t["ietf"] or "-", t["name"])
    is_mkv = path.suffix.lower() == ".mkv"
    stretch_needed = aspect == "stretch" and not multi_video
    crop = None
    if aspect == "leave" and not multi_video:        # declared 16:9: is the picture really 4:3 with baked-in bars?
        crop = bars_crop(tools, path, info["width"], info["height"])
    crop_needed = crop is not None
    plan = audio_tag_plan(info["audio_tracks"]) if info["audio_tracks"] else \
        {"target": None, "needs": False, "note": "no audio"}
    tag_needed = plan["needs"]
    log.debug("  audio decision: %s", plan["note"] if plan["target"] is None else
              f"Castilian = a{plan['target']} ({'needs spa/es-ES tag' if tag_needed else 'already tagged spa/es-ES'})")
    target = path.with_suffix(".mkv")
    ambiguous = plan["note"].startswith("AMBIGUOUS")

    if is_mkv and not stretch_needed and not crop_needed and not tag_needed:
        if multi_video and aspect == "stretch":
            return "MULTI_VIDEO"
        return "AUDIO_AMBIGUOUS" if ambiguous else "ALREADY_DONE"

    if not is_mkv and target.exists():
        return "TARGET_EXISTS"

    if dry_run:
        actions = []
        if stretch_needed:
            actions.append("stretch")
        if crop_needed:
            actions.append("crop-bars+stretch")
        if not is_mkv:
            actions.append("remux-to-mkv")
        if tag_needed:
            actions.append("tag-audio")
        if multi_video and aspect == "stretch":
            actions.append("(skip stretch: multiple video streams)")
        return f"WOULD_DO: {','.join(actions)}"

    tmp = path.with_suffix(".converting.mkv")
    try:
        if stretch_needed:
            if not run_ffmpeg_stretch(tools, path, tmp):
                return "STRETCH_FAILED"
            status = "STRETCHED" if is_mkv else "STRETCHED_AND_CONVERTED"
        elif crop_needed:
            if not run_ffmpeg_crop_stretch(tools, path, tmp, crop):
                return "BARS_FIX_FAILED"
            status = "BARS_FIXED" if is_mkv else "BARS_FIXED_AND_CONVERTED"
        elif not is_mkv:
            if not run_mkvmerge_remux(tools, path, tmp) and not run_ffmpeg_remux(tools, path, tmp):
                return "REMUX_FAILED"
            status = "REMUXED"
        else:
            # already mkv, no stretch needed, just needs tagging (in place, metadata only)
            tag_audio(tools, path, plan["target"])
            return "TAGGED"

        # Tag the NEW file (decision re-made on what was actually written).
        new_info = probe(tools, tmp)
        if new_info and new_info["audio_tracks"]:
            new_plan = audio_tag_plan(new_info["audio_tracks"])
            if new_plan["target"] and new_plan["needs"]:
                tag_audio(tools, tmp, new_plan["target"])
        if not verify_output(tools, tmp):
            raise RuntimeError("converted file failed verification")

        # Move into place. The original is removed LAST, only once the new file is
        # safely at its final path.
        if is_mkv:
            os.replace(tmp, path)          # atomic same-path replacement
        else:
            os.replace(tmp, target)        # target verified absent above
            path.unlink()
        return status
    except Exception as e:  # noqa: BLE001
        log.exception("Unexpected error processing %s: %s", path, e)
        return "ERROR"
    finally:
        # tmp is only ever a partial/duplicate artifact; the original still exists here
        # unless it was deliberately removed after a successful move (tmp is gone then).
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("roots", nargs="+", help="Root directories to scan (e.g. /mnt/vault/tv /mnt/vault/movies)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would happen without changing anything")
    parser.add_argument("--min-age-minutes", type=float, default=30,
                        help="skip files modified more recently than this (default 30; 0 disables)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("another castilianize run is in progress")

    tools = Tools()
    counts = {}
    files = list(find_video_files(args.roots))
    log.debug("mode: %s | roots: %s | skip files modified in the last %s min | bar cache: %s (%d entries)",
              "DRY RUN" if args.dry_run else "APPLY", ", ".join(args.roots), args.min_age_minutes,
              BAR_CACHE, len(_load_bar_cache()))
    log.debug("tools: %s", ", ".join(f"{k}={v}" for k, v in vars(tools).items()))
    log.debug("%d video files to check", len(files))
    run_start = time.time()
    for n, path in enumerate(files, 1):
        t0 = time.time()
        status = process_file(tools, path, args.dry_run, args.min_age_minutes)
        counts[status] = counts.get(status, 0) + 1
        if status != "ALREADY_DONE":
            log.info("%s: %s", status, path)
        log.debug("  -> %s in %.1fs  [%d/%d, %.0f min elapsed]", status, time.time() - t0, n, len(files),
                  (time.time() - run_start) / 60)

    log.info("Summary: %s", counts)
    bad = {k: v for k, v in counts.items() if k in ("PROBE_FAILED", "STRETCH_FAILED", "BARS_FIX_FAILED", "REMUX_FAILED", "ERROR")}
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
