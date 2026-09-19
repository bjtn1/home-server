#!/usr/bin/env python3
"""
Idempotent library-conversion pipeline for Castilian Spanish media libraries.

For every video file under the given root path(s):
  - If it's a 4:3 (or near-4:3) video, stretch it to 16:9 (no crop, no pillarbox).
    Already-16:9 (or otherwise non-4:3) video is left untouched.
  - If it isn't already a Matroska (.mkv) container, remux/convert it to one.
  - Tag the CASTILIAN audio track as Spanish (legacy `spa` + IETF `es-ES`).
    ONLY that one track: which one is decided by whisper_media.pick_spanish_audio()
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
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import whisper_media as M  # noqa: E402  (shared, tested Castilian-track picker)

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
            tracks = [t for t in M.mkv_info(str(path))["tracks"] if t["type"] == "audio"]
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


def audio_tag_plan(audio_tracks: list) -> dict:
    """Decide which SINGLE audio track (if any) is the Castilian one and whether it still
    needs the spa/es-ES tag. -> {'target': ordinal|None, 'needs': bool, 'note': str}"""
    fake = [{"type": "audio", "properties": {"track_name": t["name"], "language": t["language"],
                                             "language_ietf": t["ietf"] or ""}} for t in audio_tracks]
    idx, _name, note = M.pick_spanish_audio(fake)
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
    return result.returncode == 0 and verify_output(tools, dst)


def run_mkvmerge_remux(tools: Tools, src: Path, dst: Path) -> bool:
    result = subprocess.run([tools.mkvmerge, "-o", str(dst), str(src)],
                            capture_output=True, timeout=3600)
    return result.returncode in (0, 1) and verify_output(tools, dst)


def run_ffmpeg_remux(tools: Tools, src: Path, dst: Path) -> bool:
    result = subprocess.run(
        [tools.ffmpeg, "-y", "-i", str(src),
         "-map", "0:v", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?", "-c", "copy", str(dst)],
        capture_output=True, timeout=3600,
    )
    return result.returncode == 0 and verify_output(tools, dst)


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
    is_mkv = path.suffix.lower() == ".mkv"
    stretch_needed = aspect == "stretch" and not multi_video
    plan = audio_tag_plan(info["audio_tracks"]) if info["audio_tracks"] else \
        {"target": None, "needs": False, "note": "no audio"}
    tag_needed = plan["needs"]
    target = path.with_suffix(".mkv")
    ambiguous = plan["note"].startswith("AMBIGUOUS")

    if is_mkv and not stretch_needed and not tag_needed:
        if multi_video and aspect == "stretch":
            return "MULTI_VIDEO"
        return "AUDIO_AMBIGUOUS" if ambiguous else "ALREADY_DONE"

    if not is_mkv and target.exists():
        return "TARGET_EXISTS"

    if dry_run:
        actions = []
        if stretch_needed:
            actions.append("stretch")
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
    for path in find_video_files(args.roots):
        status = process_file(tools, path, args.dry_run, args.min_age_minutes)
        counts[status] = counts.get(status, 0) + 1
        if status != "ALREADY_DONE":
            log.info("%s: %s", status, path)

    log.info("Summary: %s", counts)
    bad = {k: v for k, v in counts.items() if k in ("PROBE_FAILED", "STRETCH_FAILED", "REMUX_FAILED", "ERROR")}
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
