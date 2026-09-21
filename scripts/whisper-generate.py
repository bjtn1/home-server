#!/usr/bin/env python3
"""Generate Castilian Spanish subtitles with the whisper.cpp server for
Castilian-library videos that lack real subtitle coverage (see
scripts/whisper-subtitle-plan.md; detection rules live in whisper_media.py).

Per video whose probe verdict is NEEDS:
  1. Extract the picked Castilian audio track (ffmpeg -> mono 16 kHz WAV, the
     WHOLE file, never a clip).
  2. POST to whisper /inference (language=es, temperature=0, beam_size=5 by
     default -- beam search measurably reduced loop hallucinations on the
     reference episode; it moves some loops rather than removing them, hence
     step 3). Timeout = duration + 5 min so a hung server fails loudly.
  3. whisper_srt.clean(): collapse stuck/duplicate loops, truncate intra-cue
     loops, flag suspicious repeats/phrases.
  4. Atomically write <video stem>.es.Castilian.srt next to the video (Jellyfin shows the extra
     token as the track title, so the selector says "Castilian"; sidecar; nothing is muxed or re-encoded, fully reversible: delete the file).
  5. Record a JSON entry in the cache dir keyed by show+episode identity (NOT
     path/mtime -- files get renamed/restaged) holding the written file's
     sha256. Overwrite protection: an existing .es.Castilian.srt / legacy .es.srt is replaced ONLY if it is
     recorded there and its hash is unchanged (i.e. it is still our own output).
     Anything else (a hand-made or downloaded subtitle) is never touched.

Retry-aware: failures are recorded with status "failed" and simply come up again
on the next run (they still probe as NEEDS); finished files probe as OK and are
skipped, so re-runs are cheap and only pick up new/failed files.

Castilian library ONLY -- refuses roots that are the -english libraries.

Usage:
    whisper-generate.py [--show TEXT] [--file PATH] [--limit N] [--dry-run]
                        [--regen] [--postprocess] [--beam-size N] [--min-cpm N] [ROOT ...]

Env: WHISPER_SERVER_URL (default https://whisper.bjtn.xyz),
     WHISPER_CACHE_DIR (default /home/bjtn/whisper-subtitle-cache),
     WHISPER_TMP (default /tmp).
Optional per-show glossary prompt: <cache dir>/glossaries/<show-slug>.txt
(whisper's initial_prompt, 224-token budget, literal names/terms only).
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import whisper_client as C
import whisper_media as M
import whisper_srt as S
import whisper_chunks as K

DEFAULT_ROOTS = ["/mnt/vault/tv", "/mnt/vault/movies"]
CACHE_DIR = os.environ.get("WHISPER_CACHE_DIR", "/home/bjtn/whisper-subtitle-cache")
TMP_DIR = os.environ.get("WHISPER_TMP", "/tmp")
LOG = os.environ.get("WHISPER_LOG", "/home/bjtn/logs/whisper-generate.log")
LOCK = "/tmp/whisper-generate.lock"

# Jellyfin parses "<video>.es.Castilian.srt" as language=Spanish, TITLE="Castilian" (verified against its DB), so our
# own subtitles are labelled "Castilian" in the subtitle selector, distinct from other Spanish tracks. Files that
# are NOT ours keep whatever name they have. LEGACY_SUFFIX is the name used before 2026-09-20.
SRT_SUFFIX = ".es.Castilian.srt"
LEGACY_SUFFIX = ".es.srt"


def sidecar_target(video):
    return os.path.splitext(video)[0] + SRT_SUFFIX


def existing_sidecar(video):
    """The subtitle file we would own for this video: the new name if present, else the legacy name, else None."""
    stem = os.path.splitext(video)[0]
    for suffix in (SRT_SUFFIX, LEGACY_SUFFIX):
        if os.path.exists(stem + suffix):
            return stem + suffix
    return None

EP_RE = re.compile(r"S(\d{1,3})E(\d{1,3})(?:-?E(\d{1,3}))?", re.I)
PROMPT_MAX_CHARS = 700  # ~224 tokens; whisper keeps the LAST tokens if over


def log(msg):
    line = f"[{time.strftime('%F %T')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def slug(s):
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def identity(path):
    """show/movie + episode identity, stable across renames within a title."""
    parts = os.path.normpath(path).split(os.sep)
    root_i = next((i for i, p in enumerate(parts) if p in ("tv", "movies")), None)
    title = parts[root_i + 1] if root_i is not None and root_i + 1 < len(parts) else parts[-2]
    m = EP_RE.search(os.path.basename(path))
    ep = f"s{int(m.group(1)):02d}e{int(m.group(2)):02d}" + (f"-e{int(m.group(3)):02d}" if m.group(3) else "") if m else "movie"
    return slug(title), ep


def cache_path(path):
    show, ep = identity(path)
    return os.path.join(CACHE_DIR, f"{show}__{ep}.json")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_cache(path):
    try:
        return json.load(open(cache_path(path)))
    except (OSError, ValueError):
        return None


def save_cache(path, rec):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = cache_path(path) + ".tmp"
    json.dump(rec, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, cache_path(path))


def glossary_prompt(path):
    show, _ = identity(path)
    g = os.path.join(CACHE_DIR, "glossaries", f"{show}.txt")
    try:
        txt = open(g, encoding="utf-8").read().strip()
    except OSError:
        return None
    return txt[-PROMPT_MAX_CHARS:] or None


def known_variants(path):
    show, _ = identity(path)
    try:
        return json.load(open(os.path.join(CACHE_DIR, "glossaries", f"{show}.variants.json"),
                              encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def process(path, res, args):
    stem = os.path.splitext(path)[0]
    target = stem + SRT_SUFFIX
    rec = load_cache(path)

    existing = existing_sidecar(path)
    if existing:
        ours = rec and rec.get("status") == "ok" and rec.get("srt_sha256") == sha256(existing)
        if not ours:
            log(f"SKIP (existing {os.path.basename(existing)} is not our unchanged output; never overwritten): {path}")
            return "skipped"

    dur = res["duration_min"]
    timeout = int(dur * 60 + 300)
    log(f"generate: {path} (audio track {res['audio_index']} {res['audio_name']!r}, {dur} min)")
    if args.dry_run:
        return "dry-run"

    wav = os.path.join(TMP_DIR, f"whisper-{os.getpid()}.wav")
    t0 = time.time()
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path,
                        "-map", f"0:a:{res['audio_index']}", "-ac", "1", "-ar", "16000",
                        "-c:a", "pcm_s16le", wav], check=True, timeout=1800)
        prompt = glossary_prompt(path)
        extra = {"beam_size": str(args.beam_size)} if args.beam_size and args.beam_size > 1 else {}
        chunk_stats = None
        if getattr(args, "chunked", False):
            # speech-chunked: VAD finds real speech, packs of <= 27 s are transcribed separately and mapped back
            # (fixes cues placed tens of seconds early/late after long silences; see whisper_chunks.py)
            def _one(p):
                return C.transcribe(p, timeout=300, prompt=prompt, extra=extra)[0]
            raw, chunk_stats = K.transcribe_chunked(wav, _one, S.parse_srt)
            secs = time.time() - t0
        else:
            text, secs = C.transcribe(wav, timeout=timeout, prompt=prompt, extra=extra)
            raw = S.parse_srt(text)
        if not raw:
            raise RuntimeError("whisper returned no parsable cues")
        cleaned, report = S.clean(raw)
        cleaned, name_fixes = S.correct_names(cleaned, known_variants(path))
        srt = S.format_srt(cleaned)
        tmp = target + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(srt)
        os.replace(tmp, target)
        legacy = stem + LEGACY_SUFFIX
        if existing == legacy and os.path.exists(legacy):   # only reachable when it was verified as ours above
            os.remove(legacy)
        save_cache(path, {
            "status": "ok", "video": path, "srt": target, "srt_sha256": sha256(target),
            "audio_track": res["audio_index"], "audio_name": res["audio_name"],
            "beam_size": args.beam_size, "glossary": bool(prompt),
            "chunked": bool(chunk_stats), "chunk_stats": chunk_stats,
            "cues_raw": len(raw), "cues_written": len(cleaned),
            "collapsed_runs": len(report["collapsed_runs"]),
            "flagged_repeats": report["flagged_repeats"],
            "suspect_phrases": report["suspect_phrases"],
            "dropped_hallucinations": report["dropped_hallucinations"],
            "name_fixes": name_fixes[:20],
            "whisper_seconds": round(secs, 1), "generated_at": time.strftime("%F %T")})
        log(f"OK: {len(raw)} cues -> {len(cleaned)} written ({len(report['collapsed_runs'])} loop(s) collapsed, "
            f"{len(report['flagged_repeats'])} flagged, {sum(n for _a, _b, n in name_fixes)} name fixes, "
            f"{len(report['dropped_hallucinations'])} hallucination cue(s) dropped) in {secs:.0f}s -> {target}")
        return "ok"
    except Exception as e:
        prev = (rec or {}).get("attempts", 0) if (rec or {}).get("status") == "failed" else 0
        save_cache(path, {"status": "failed", "video": path, "error": str(e)[:300],
                          "attempts": prev + 1, "failed_at": time.strftime("%F %T")})
        log(f"FAILED: {path}: {e}")
        return "failed"
    finally:
        if os.path.exists(wav):
            os.remove(wav)
        for junk in (target + ".part",):
            if os.path.exists(junk):
                os.remove(junk)


def postprocess(args):
    """Offline pass over our OWN output (cache record + matching sha256 only): re-run the cleaner (incl. reflow),
    apply curated name fixes, and migrate the file to the SRT_SUFFIX name. Never touches files that are not ours."""
    files = [args.file] if args.file else list(M.walk_videos(args.roots, args.show))
    n_changed = n_same = n_skipped = n_renamed = 0
    for path in files:
        stem = os.path.splitext(path)[0]
        target = stem + SRT_SUFFIX
        src = existing_sidecar(path)
        rec = load_cache(path)
        if not (src and rec and rec.get("status") == "ok" and rec.get("srt_sha256") == sha256(src)):
            n_skipped += 1
            continue
        old_text = open(src, encoding="utf-8").read()
        raw = S.parse_srt(old_text)
        cleaned, report = S.clean(raw)
        cleaned, fixes = S.correct_names(cleaned, known_variants(path))
        new = S.format_srt(cleaned)
        needs_rename = src != target
        if new == old_text and not needs_rename:
            n_same += 1
            continue
        if args.dry_run:
            log(f"would change: {os.path.basename(target)} ({len(raw)}->{len(cleaned)} cues, "
                f"{sum(n for _a, _b, n in fixes)} name fixes{', rename' if needs_rename else ''})")
            n_changed += 1
            n_renamed += needs_rename
            continue
        tmp = target + ".part"
        open(tmp, "w", encoding="utf-8").write(new)
        os.replace(tmp, target)
        if needs_rename and os.path.exists(src):
            os.remove(src)
            n_renamed += 1
        rec.update({"srt": target, "srt_sha256": sha256(target), "cues_written": len(cleaned), "name_fixes": fixes[:20],
                    "dropped_hallucinations": report["dropped_hallucinations"],
                    "wrapped_cues": report.get("wrapped_cues"), "split_cues": report.get("split_cues"),
                    "postprocessed_at": time.strftime("%F %T")})
        save_cache(path, rec)
        n_changed += 1
    log(f"postprocess summary: changed={n_changed} (renamed={n_renamed}) unchanged={n_same} skipped(not ours)={n_skipped}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="*", default=DEFAULT_ROOTS)
    ap.add_argument("--show")
    ap.add_argument("--file")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--regen", action="store_true",
                    help="also redo files whose subtitle is our own unchanged output (e.g. after a cleaner fix)")
    ap.add_argument("--chunked", action="store_true",
                    help="VAD-chunked transcription: only speech is sent to whisper and timestamps are mapped back "
                         "(needs ~/venvs/vad; fixes mistimed/hallucinated cues after long silences)")
    ap.add_argument("--postprocess", action="store_true",
                    help="no transcription: re-run the cleaner (incl. line reflow) + curated name fixes on our own existing subtitles and migrate them to the .es.Castilian.srt name")
    ap.add_argument("--beam-size", type=int, default=5)
    ap.add_argument("--min-cpm", type=float, default=M.DEFAULT_MIN_CUES_PER_MIN)
    args = ap.parse_args()

    roots = args.roots
    for r in roots:
        if "-english" in os.path.basename(os.path.realpath(r)):
            sys.exit(f"refusing {r}: this pipeline is Castilian-library only")

    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("another whisper-generate run is in progress")

    if args.postprocess:
        return postprocess(args)

    if not args.dry_run and not C.server_up():
        sys.exit("whisper server not reachable -- nothing done")

    files = [args.file] if args.file else list(M.walk_videos(roots, args.show))
    done = {"ok": 0, "failed": 0, "skipped": 0, "dry-run": 0, "not-needed": 0}
    for path in files:
        try:
            res = M.probe(path, args.min_cpm)
        except Exception as e:
            log(f"PROBE FAILED: {path}: {e}")
            continue
        if args.regen and res["verdict"] == "OK" and res["audio_index"] is not None:
            tgt = existing_sidecar(path)
            rec = load_cache(path)
            if tgt and rec and rec.get("status") == "ok" and rec.get("srt_sha256") == sha256(tgt):
                res["verdict"] = "NEEDS"  # our own output: safe to redo
        if res["verdict"] != "NEEDS":
            done["not-needed"] += 1
            if res["verdict"] in ("REVIEW", "SKIP"):
                log(f"{res['verdict']}: {res['audio_note']}: {path}")
            continue
        done[process(path, res, args)] += 1
        if args.limit and done["ok"] + done["failed"] + done["dry-run"] >= args.limit:
            break
    log(f"summary: {done}")


if __name__ == "__main__":
    main()
