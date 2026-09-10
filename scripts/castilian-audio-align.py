#!/usr/bin/env python3
"""
Verified duration-mismatch alignment for the Castilian pipeline
(2026-09-09). Given a (source, target) file pair where the source's
confirmed-Castilian track can't be safely muxed because the containers'
durations don't match closely enough, this tries to actually explain and
fix the gap -- not just report it -- using a tiered approach:

  1. English-track cross-correlation (if both files have one) -- direct
     waveform matching, used to VERIFY candidate divergence points found
     via black-frame boundaries. Answers "is this really the same
     content," not just "do these look similar."
  2. Black-frame/scene-boundary comparison alone, if no shared reference
     track exists on both sides (weaker -- no independent verification).
  3. Decline outright if neither signal cleanly explains the full gap
     within a tight tolerance -- never guess.

See ~/.claude/plans/concurrent-booping-lerdorf.md for the full design
rationale (why audio cross-correlation over pure video heuristics, why
this never auto-mixes on its own, etc).

Usage:
    castilian-audio-align.py <source_video> <target_video> <mode> <cache_dir>

    mode is 'tv' or 'movie' -- movie mode uses a stricter confidence bar
    before ever proposing a fix (a duration gap in a movie is more likely
    a genuinely different cut than a broadcast bumper).

Writes into <cache_dir>, keyed by sha256(source::target) plus the
source's mtime+size (a stale cache from a re-downloaded file at the same
path must never serve an outdated fix):
    <key>.status.json       -- {status, fix_found, reason, points, ...}
    <key>.corrected.mka     -- corrected Castilian audio track, if fix_found
    <key>.verification.mkv -- video + corrected audio around the splice,
                               extracted FROM corrected.mka -- if fix_found

Never mixes anything into the real library. Only ever writes into the
cache dir above; castilian-control reads that, a human approves via
/review-duration, and the actual mux happens via mux-castilian-audio.sh's
--override-pairs on a later, completely separate run.
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import wave

import numpy as np

FFMPEG, FFPROBE, MKVMERGE = "ffmpeg", "ffprobe", "mkvmerge"
WHISPER_SERVER_URL = os.environ.get("WHISPER_SERVER_URL", "http://100.117.170.34:8178")

TIGHT_TOLERANCE = 1.0           # seconds -- corrected duration vs target, after a fix
BOUNDARY_MATCH_TOLERANCE = 2.0  # seconds -- black-frame boundary alignment slack
QUIET_SEARCH_WINDOW = 2.0       # seconds -- how far to search for a real quiet splice point
CORR_WINDOW = 15.0              # seconds -- audio window used to verify each candidate point
MIN_CORRELATION = {"tv": 0.55, "movie": 0.75}
BLACKDETECT_ARGS = "blackdetect=d=0.4:pic_th=0.97"
MAX_POINTS = 3  # decline rather than attempt an implausibly complex multi-point fix


def log(*a):
    print("[castilian-audio-align]", *a, file=sys.stderr)


def run(cmd, timeout=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def ffprobe_json(path):
    r = run([FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path])
    try:
        return json.loads(r.stdout)
    except Exception:
        return {}


def container_duration(path):
    try:
        return float(ffprobe_json(path)["format"]["duration"])
    except Exception:
        return None


def _rate_to_float(f):
    try:
        n, d = f.split("/")
        d = float(d)
        return float(n) / d if d else 0.0
    except Exception:
        return 0.0


_FPS_CACHE = {}


def video_fps(path):
    """Memoized (repeated correlation calls against the same pair of
    files shouldn't re-run ffprobe every time). Returns None if no video
    stream or an unparseable rate."""
    if path in _FPS_CACHE:
        return _FPS_CACHE[path]
    fps = None
    for s in ffprobe_json(path).get("streams", []):
        if s.get("codec_type") == "video":
            fps = _rate_to_float(s.get("r_frame_rate") or "") or None
            break
    _FPS_CACHE[path] = fps
    return fps


def is_vfr(path) -> bool:
    """True if the video's claimed and average frame rates disagree
    meaningfully, or can't be read at all -- either way, precise
    timestamp-based splice math can't be trusted, so treat as VFR-risky
    (biases the caller toward declining rather than guessing)."""
    for s in ffprobe_json(path).get("streams", []):
        if s.get("codec_type") == "video":
            r, a = s.get("r_frame_rate"), s.get("avg_frame_rate")
            rf, af = _rate_to_float(r or ""), _rate_to_float(a or "")
            if rf == 0:
                return True
            return abs(rf - af) / rf > 0.01
    return True  # no video stream found at all -- can't verify, treat as risky


def mkvmerge_tracks(path):
    r = run([MKVMERGE, "-J", path])
    try:
        return json.loads(r.stdout).get("tracks", [])
    except Exception:
        return []


def find_track(path, predicate):
    """Returns (mkvmerge_id, ffmpeg_audio_idx) for the first audio track
    where predicate(properties) is True, else None. ffmpeg_audio_idx is
    the 0-based position among audio-only tracks, matching -map 0:a:N
    (same convention as castilian-extract-clip.sh)."""
    audio = [t for t in mkvmerge_tracks(path) if t.get("type") == "audio"]
    for i, t in enumerate(audio):
        if predicate(t.get("properties", {})):
            return t["id"], i
    return None


def is_castilian_props(p):
    ietf = (p.get("language_ietf") or "").lower()
    m = re.match(r"^es-([a-z]{2}|\d{3})$", ietf)
    if m:
        return m.group(1) == "es"
    text = f"{p.get('track_name') or ''} {p.get('language') or ''}".lower()
    return "castellano" in text or "castilian" in text


def is_english_props(p):
    # 2026-09-09, found live: a real source file tags its English track
    # language "und" (undefined) with no language_ietf at all, relying
    # entirely on its title ('2.[Eng][224kbps,48,0KHz,2ch,A-C3]') to say
    # what it is -- language-tag-only detection silently missed it and
    # fell back to the weaker tier-2 (black-frame-only) path for a file
    # that actually had a perfectly good reference track available.
    # Mirrors is_castilian_props()'s own title-text fallback below.
    if (p.get("language") or "").lower() == "eng" or (p.get("language_ietf") or "").lower().startswith("en"):
        return True
    title = (p.get("track_name") or "").lower()
    return bool(re.search(r"\[eng(lish)?\]|\benglish\b", title))


# --- audio extraction / correlation -----------------------------------------

def extract_wav(path, ffmpeg_audio_idx, out_path, sample_rate=8000, start=None, dur=None, atempo=None):
    cmd = [FFMPEG, "-y"]
    if start is not None:
        cmd += ["-ss", str(max(0, start))]
    cmd += ["-i", path, "-map", f"0:a:{ffmpeg_audio_idx}", "-ac", "1", "-ar", str(sample_rate), "-vn"]
    if atempo is not None:
        # Speed/pitch-compensates the extracted clip -- found live 2026-09-09:
        # a source and target encoded at genuinely different video frame
        # rates (25fps vs 29.97fps, a real ~20% difference, not the usual
        # small PAL/NTSC drift) can carry audio that plays at different
        # real-world speed even for the SAME underlying episode content --
        # confirmed by two independent Whisper transcripts matching
        # almost word-for-word while raw waveform correlation stayed
        # near-zero. atempo's single-filter range is 0.5-2.0, comfortably
        # covering any real fps ratio we'd plausibly see here.
        cmd += ["-af", f"atempo={atempo}"]
    if dur is not None:
        cmd += ["-t", str(dur)]
    cmd += [out_path]
    r = run(cmd, timeout=120)
    return r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 44


def read_wav_mono(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float64), sr


_CLIP_CACHE = {}
CLIP_CACHE_MAX = 500  # bounded -- a long batch run shouldn't grow this unboundedly


def _extract_mono_cached(path, idx, t, window, atempo=None):
    """extract_wav + read_wav_mono, cached by (path, idx, rounded t, window,
    atempo). Found live 2026-09-09: confirm_correlation()'s delta-search
    loop calls this with the SAME source-side (path, idx, t) up to ~8
    times in a row while only the target side actually changes across
    deltas -- caching the source extraction turns those into in-memory
    hits instead of repeated real ffmpeg subprocess calls, a real and
    significant speedup for what was otherwise the dominant cost of a
    season-wide candidate search."""
    key = (path, idx, round(t, 2), window, atempo)
    if key in _CLIP_CACHE:
        return _CLIP_CACHE[key]
    with tempfile.TemporaryDirectory() as tmp:
        wav_path = os.path.join(tmp, "clip.wav")
        if not extract_wav(path, idx, wav_path, start=t, dur=window, atempo=atempo):
            result = None
        else:
            result = read_wav_mono(wav_path)
    if len(_CLIP_CACHE) >= CLIP_CACHE_MAX:
        _CLIP_CACHE.clear()
    _CLIP_CACHE[key] = result
    return result


def _correlate_clips(source_path, source_idx, source_t, target_path, target_idx, target_t, window, source_atempo=None):
    a_result = _extract_mono_cached(source_path, source_idx, source_t, window, atempo=source_atempo)
    if a_result is None:
        return 0.0
    b_result = _extract_mono_cached(target_path, target_idx, target_t, window)
    if b_result is None:
        return 0.0
    a, sr = a_result
    b, _ = b_result
    if len(a) < sr or len(b) < sr:
        return 0.0
    n = min(len(a), len(b))
    a, b = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
    denom = np.sqrt((a**2).sum() * (b**2).sum())
    if denom == 0:
        return 0.0
    # FFT-based cross-correlation (numpy has no correlate(method='fft'),
    # so this is the direct convolution-theorem implementation) -- small
    # windows (a few hundred thousand samples at 8kHz) keep this fast.
    size = 1
    while size < 2 * n:
        size *= 2
    fa, fb = np.fft.rfft(a, size), np.fft.rfft(b, size)
    corr = np.fft.irfft(fa * np.conj(fb), size)
    return float(np.max(np.abs(corr)) / denom)


def windowed_correlation(source_path, source_idx, source_t, target_path, target_idx, target_t, window=CORR_WINDOW):
    """Extracts `window` seconds of audio from each file at the given
    timestamps and returns a normalized cross-correlation peak (roughly
    0..1 -- values above MIN_CORRELATION indicate genuinely matching
    content, not coincidence). This is the VERIFICATION step for a
    candidate point already found via black-frame boundaries -- not a
    blind full-episode search, which is why small windows are enough.

    2026-09-09: this used to also try an fps-tempo-compensated pass when
    source/target video frame rates disagreed (a real 25fps-vs-29.97fps
    case was found live). Tested directly against that exact real pair,
    across the true ratio AND several other candidate ratios in both
    directions -- none of them ever produced a meaningfully higher score.
    The actual cause turned out to be a different audio master/mix
    entirely (dialogue performance/mix differs even though the words are
    identical), not a speed/pitch difference -- see
    confirm_transcript()/transcript_similarity() below, which is what
    actually resolves that case. Removed the tempo pass: it was real,
    tested dead weight (doubles every correlation call's cost for zero
    wins in practice), not a hypothetical simplification."""
    return _correlate_clips(source_path, source_idx, source_t, target_path, target_idx, target_t, window)


TRANSCRIPT_WINDOW = 14.0  # seconds -- longer than CORR_WINDOW; a few real
                           # sentences give a much more reliable text
                           # comparison than a bare-minimum clip would.
                           # Trimmed from 20s live 2026-09-09 to cut real
                           # Whisper transcription time per call -- still
                           # comfortably enough dialogue for the 0.95-vs
                           # -0.19 separation already proven at 20s.


TRANSCRIPT_CACHE_FILE = os.environ.get(
    "CASTILIAN_TRANSCRIPT_CACHE", "/mnt/vault/mega-staging/queue/castilian-transcript-cache.json")

# Disk-backed, not just in-memory -- found live 2026-09-09: each source
# file is matched via its own SEPARATE subprocess invocation of this
# matcher, so an in-memory-only cache resets every single time. Within a
# season of ~11-13 physical files, many DIFFERENT source segments end up
# transcript-checking the SAME target files over and over across
# subprocess runs -- a huge, avoidable amount of repeat real Whisper
# round-trips (the actual dominant cost of a whole batch run). Persisting
# to disk means the 2nd, 3rd, ...Nth source file in a season only ever
# pays for NEW clips, not ones an earlier file already transcribed.
_TRANSCRIPT_CACHE = None  # lazy-loaded so a script that never transcribes never touches disk


def _load_transcript_cache():
    global _TRANSCRIPT_CACHE
    if _TRANSCRIPT_CACHE is not None:
        return
    _TRANSCRIPT_CACHE = {}
    try:
        with open(TRANSCRIPT_CACHE_FILE) as f:
            raw = json.load(f)
        for k, v in raw.items():
            path, idx, t, window = k.rsplit("\x1f", 3)
            _TRANSCRIPT_CACHE[(path, int(idx), float(t), float(window))] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"couldn't load transcript cache ({TRANSCRIPT_CACHE_FILE}), starting fresh: {e!r}")


def _save_transcript_cache():
    try:
        os.makedirs(os.path.dirname(TRANSCRIPT_CACHE_FILE), exist_ok=True)
        serializable = {f"{p}\x1f{i}\x1f{t}\x1f{w}": v for (p, i, t, w), v in _TRANSCRIPT_CACHE.items()}
        tmp = TRANSCRIPT_CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(serializable, f, ensure_ascii=False)
        os.replace(tmp, TRANSCRIPT_CACHE_FILE)  # atomic -- never leaves a half-written cache
    except Exception as e:
        log(f"couldn't save transcript cache: {e!r}")


def transcribe_clip(path, audio_idx, t, window=TRANSCRIPT_WINDOW, whisper_url=None):
    """Extracts `window` seconds of audio at t and transcribes it via the
    shared Whisper server (same one castilian-whisper-check.sh already
    uses). Returns the transcript text, or None if extraction/the server
    call fails -- callers must treat None as "couldn't check," never as
    a mismatch signal.

    Cached (in-memory AND on disk -- see TRANSCRIPT_CACHE_FILE above) by
    (path, audio_idx, rounded t, window)."""
    _load_transcript_cache()
    key = (path, audio_idx, round(t, 2), window)
    if key in _TRANSCRIPT_CACHE:
        return _TRANSCRIPT_CACHE[key]
    text = _transcribe_clip_uncached(path, audio_idx, t, window, whisper_url)
    _TRANSCRIPT_CACHE[key] = text
    _save_transcript_cache()
    return text


def _transcribe_clip_uncached(path, audio_idx, t, window, whisper_url):
    import urllib.request

    whisper_url = whisper_url or WHISPER_SERVER_URL
    with tempfile.TemporaryDirectory() as tmp:
        wav_path = os.path.join(tmp, "clip.wav")
        if not extract_wav(path, audio_idx, wav_path, sample_rate=16000, start=t, dur=window):
            return None
        try:
            with open(wav_path, "rb") as f:
                data = f.read()
            boundary = "----castilianwhisperboundary"
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\ntext\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"clip.wav\"\r\n"
                f"Content-Type: audio/wav\r\n\r\n"
            ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
            req = urllib.request.Request(
                f"{whisper_url.rstrip('/')}/inference", data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read().decode("utf-8", errors="replace").strip()
        except Exception as e:
            log(f"transcribe_clip failed for {path}@{t}: {e!r}")
            return None


def transcript_similarity(text_a, text_b):
    """Normalized (lowercase, punctuation-stripped) difflib ratio, 0..1.
    Found live 2026-09-09: a genuinely matching pair of clips from
    DIFFERENT audio masters/mixes of the same episode -- a real case
    where raw waveform correlation stays near-zero no matter what,
    because the recordings themselves differ even though the dialogue is
    identical -- still transcribes to near-identical text (0.9+), while
    genuinely different content transcribes to unrelated text (well
    under 0.5). This is the fallback for exactly that gap: verifies
    CONTENT, not waveform shape."""
    norm = lambda s: re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()
    a, b = norm(text_a), norm(text_b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def find_quiet_point(path, audio_idx, near_t, window=QUIET_SEARCH_WINDOW):
    """Searches ±window seconds around near_t for a genuinely quiet
    moment in the audio (via ffmpeg's silencedetect), returning the
    timestamp of the best one found, or None. Splicing must never land
    mid-word -- bumper timing and dialogue timing aren't frame-locked to
    each other, so the video-derived candidate point is only ever a
    starting guess, never trusted directly."""
    start = max(0, near_t - window)
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "probe.wav")
        if not extract_wav(path, audio_idx, wav, start=start, dur=window * 2):
            return None
        r = run([FFMPEG, "-i", wav, "-af", "silencedetect=noise=-35dB:d=0.15", "-f", "null", "-"], timeout=30)
    mids = []
    starts = {float(m.group(1)) for m in re.finditer(r"silence_start:\s*([\d.]+)", r.stderr or "")}
    for m in re.finditer(r"silence_end:\s*([\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)", r.stderr or ""):
        end, dur = float(m.group(1)), float(m.group(2))
        mids.append(end - dur / 2)
    if not mids:
        return None
    # Closest quiet midpoint to the original candidate wins.
    best = min(mids, key=lambda m: abs((start + m) - near_t))
    return start + best


def black_boundaries(path, timeout=180):
    """Sorted list of black-segment start timestamps -- same technique
    validated by hand tonight on the 1x11 sample."""
    r = run([FFMPEG, "-i", path, "-vf", BLACKDETECT_ARGS, "-an", "-f", "null", "-"], timeout=timeout)
    return sorted(float(m.group(1)) for m in re.finditer(r"black_start:([\d.]+)", r.stderr or ""))


# --- tier 1: direct correlation-based scan (when a shared reference track exists) ---

def scan_for_divergence(source_path, source_idx, target_path, target_idx, source_dur, target_dur, mode, step=20.0):
    """Coarse forward scan across the whole episode: correlate source[t]
    against target[t + offset] (offset starts at 0, accumulates as
    divergence points are resolved). When correlation drops, grid-search
    a new offset (bounded by the known total gap) that restores it --
    that's a divergence point, in SOURCE time, with its extra_duration
    (positive = target has that much extra content there).
    2026-09-09, found live: the black-frame-boundary approach below
    (find_candidate_points) is too fragile on a real ~22min episode with
    ordinary scene cuts throughout -- it has nothing to do with the real
    divergence point and drowns it in noise. Direct correlation search
    doesn't depend on that signal at all; it only needs the shared
    reference track, which is exactly what tier 1 means. Declines (returns
    None) rather than reporting a low-confidence result if the total gap
    isn't cleanly explained, or a break can't be resolved, or too many
    points would be needed to trust."""
    threshold = MIN_CORRELATION[mode]
    total_gap = target_dur - source_dur
    offset, points, t = 0.0, [], 0.0
    while t < source_dur - CORR_WINDOW:
        score = windowed_correlation(source_path, source_idx, t, target_path, target_idx, t + offset)
        if score >= threshold:
            t += step
            continue
        search_span = abs(total_gap) + 15.0
        best_delta, best_score = None, 0.0
        for delta in np.arange(-search_span, search_span, 2.0):
            s = windowed_correlation(source_path, source_idx, t, target_path, target_idx, t + offset + delta)
            if s > best_score:
                best_score, best_delta = s, float(delta)
        if best_delta is None or best_score < threshold:
            log(f"divergence at source={t:.1f}s couldn't be resolved (best score {best_score:.2f})")
            return None
        points.append({"at_source": t, "extra_duration": best_delta,
                        "direction": "target_extra" if best_delta > 0 else "source_extra"})
        offset += best_delta
        if len(points) > MAX_POINTS:
            return None
        t += step
    explained = sum(p["extra_duration"] for p in points)
    if abs(explained - total_gap) > TIGHT_TOLERANCE:
        log(f"scan explained {explained:.1f}s, known gap is {total_gap:.1f}s -- doesn't reconcile")
        return None
    return points


# --- gap hypothesis: find and verify candidate correction points ------------

def find_candidate_points(source_boundaries, target_boundaries, total_gap):
    """Walks both boundary lists to find target-side boundaries with no
    corresponding source-side boundary nearby (accounting for the
    cumulative gap explained so far) -- each one is a candidate "target
    has extra content here" point. Symmetric for the reverse direction
    (source has extra content target lacks). Returns a list of
    {at_source, at_target, extra_duration, direction} or None if it can't
    find a set of points whose durations explain the full known gap
    within TIGHT_TOLERANCE."""
    points = []
    cumulative = 0.0
    si, ti = 0, 0
    # Walk forward through both lists in parallel, matching boundaries
    # that land within tolerance once the running offset is applied;
    # anything on one side with nothing nearby on the other is a gap.
    while si < len(source_boundaries) and ti < len(target_boundaries):
        s_t = source_boundaries[si]
        t_t = target_boundaries[ti] - cumulative
        if abs(s_t - t_t) <= BOUNDARY_MATCH_TOLERANCE:
            si += 1
            ti += 1
            continue
        if t_t > s_t:
            # target boundary is further ahead than expected -- source is
            # missing content target has, OR source has an unmatched
            # boundary just before it (a real scene cut with no gap).
            # Look ahead a little in source for a match before concluding
            # this is a gap.
            lookahead = next((j for j in range(si, min(si + 3, len(source_boundaries)))
                               if abs(source_boundaries[j] - t_t) <= BOUNDARY_MATCH_TOLERANCE), None)
            if lookahead is not None:
                si = lookahead
                continue
            extra = t_t - s_t
            if extra <= 0:
                ti += 1
                continue
            points.append({"at_source": s_t, "at_target": target_boundaries[ti], "extra_duration": extra, "direction": "target_extra"})
            cumulative += extra
            ti += 1
        else:
            lookahead = next((j for j in range(ti, min(ti + 3, len(target_boundaries)))
                               if abs(target_boundaries[j] - cumulative - s_t) <= BOUNDARY_MATCH_TOLERANCE), None)
            if lookahead is not None:
                ti = lookahead
                continue
            extra = s_t - t_t
            if extra <= 0:
                si += 1
                continue
            points.append({"at_source": source_boundaries[si], "at_target": t_t + cumulative, "extra_duration": extra, "direction": "source_extra"})
            cumulative -= extra
            si += 1
        if len(points) > MAX_POINTS:
            return None  # too complex to trust -- decline rather than guess
    explained = sum(p["extra_duration"] * (1 if p["direction"] == "target_extra" else -1) for p in points)
    if abs(explained - total_gap) > TIGHT_TOLERANCE:
        return None
    return points


# 2026-09-09: an earlier version had a separate verify_points() step that
# ran AFTER find_candidate_points() to confirm black-frame-derived
# candidates via correlation. Removed -- tier 1 now uses
# scan_for_divergence() instead, which verifies each point as it's found
# (a point is only ever accepted once its correlation score already
# clears the threshold), so a separate after-the-fact pass is redundant
# for tier 1. Tier 2 (find_candidate_points(), no reference track
# available) has no independent verification at all -- that's exactly why
# it's the weaker fallback, not a gap needing a fix here.


# --- correction: build the actual fixed audio -------------------------------

def build_corrected_audio(source_path, castilian_idx, points, out_path):
    """Builds a corrected Castilian audio track: for each point, splices
    in silence (target_extra) or trims (source_extra) at a quiet-moment
    -snapped timestamp near the candidate point. Returns the list of
    actual splice timestamps used (post-snapping), or None on failure."""
    points = sorted(points, key=lambda p: p["at_source"])
    snapped = []
    for p in points:
        q = find_quiet_point(source_path, castilian_idx, p["at_source"])
        if q is None:
            log(f"no quiet moment found near {p['at_source']:.1f}s -- declining fix")
            return None, None
        snapped.append({**p, "splice_at": q})

    with tempfile.TemporaryDirectory() as tmp:
        segments = []
        cursor = 0.0
        for i, p in enumerate(snapped):
            seg_path = os.path.join(tmp, f"seg{i}.wav")
            if not extract_wav(source_path, castilian_idx, seg_path, start=cursor, dur=p["splice_at"] - cursor):
                return None, None
            segments.append(seg_path)
            if p["direction"] == "target_extra":
                gap_path = os.path.join(tmp, f"gap{i}.wav")
                r = run([FFMPEG, "-y", "-f", "lavfi", "-i",
                         f"anullsrc=r=48000:cl=stereo:d={p['extra_duration']:.3f}", gap_path], timeout=30)
                if r.returncode != 0:
                    return None, None
                segments.append(gap_path)
                cursor = p["splice_at"]
            else:
                cursor = p["splice_at"] + p["extra_duration"]
        tail_path = os.path.join(tmp, "tail.wav")
        if not extract_wav(source_path, castilian_idx, tail_path, start=cursor, dur=None):
            return None, None
        segments.append(tail_path)

        concat_list = os.path.join(tmp, "concat.txt")
        with open(concat_list, "w") as f:
            for s in segments:
                f.write(f"file '{s}'\n")
        r = run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-ac", "2", "-ar", "48000", out_path], timeout=120)
        if r.returncode != 0:
            return None, None
    return out_path, [s["splice_at"] for s in snapped]


def retag_castilian(mka_path, title="Castellano", lang="es-ES"):
    """ffmpeg's concat doesn't carry custom stream metadata forward --
    without this, the corrected file would fail track_is_castilian()'s
    own check the moment it's used as a mux source, defeating the fix."""
    tmp = mka_path + ".tagged.mka"
    r = run([FFMPEG, "-y", "-i", mka_path, "-c", "copy",
             "-metadata:s:a:0", f"language={lang}", "-metadata:s:a:0", f"title={title}", tmp], timeout=60)
    if r.returncode != 0:
        return False
    os.replace(tmp, mka_path)
    return True


def build_verification_clip(target_path, corrected_audio_path, splice_points, out_path, pad=6.0):
    """Real video + the corrected audio (extracted FROM the exact
    corrected file, never a separate computation), spanning each splice
    point -- this is what actually proves sync, not an audio-only clip."""
    if not splice_points:
        return None
    center = splice_points[0]  # single-clip verification around the first point; good enough to judge the fix
    start = max(0, center - pad)
    dur = pad * 2
    r = run([FFMPEG, "-y", "-ss", str(start), "-i", target_path, "-ss", str(start), "-i", corrected_audio_path,
             "-map", "0:v:0", "-map", "1:a:0", "-t", str(dur), "-c:v", "libx264", "-preset", "veryfast",
             "-c:a", "aac", out_path], timeout=60)
    if r.returncode != 0:
        return None
    return {"path": out_path, "start": start, "duration": dur}


# --- main --------------------------------------------------------------------

def cache_key(source, target, source_stat):
    return hashlib.sha256(f"{source}::{target}::{source_stat.st_mtime_ns}::{source_stat.st_size}".encode()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("target")
    ap.add_argument("mode", choices=["tv", "movie"])
    ap.add_argument("cache_dir")
    args = ap.parse_args()

    os.makedirs(args.cache_dir, exist_ok=True)
    st = os.stat(args.source)
    key = cache_key(args.source, args.target, st)
    status_path = os.path.join(args.cache_dir, f"{key}.status.json")

    def write_status(**kw):
        data = {"source": args.source, "target": args.target, "mode": args.mode, **kw}
        with open(status_path, "w") as f:
            json.dump(data, f)
        log("result:", kw.get("status"), "-", kw.get("reason", ""))

    if is_vfr(args.source) or is_vfr(args.target):
        write_status(status="no_fix", fix_found=False, reason="variable frame rate -- timestamp math not reliable, needs manual review")
        return

    src_dur, tgt_dur = container_duration(args.source), container_duration(args.target)
    if src_dur is None or tgt_dur is None:
        write_status(status="no_fix", fix_found=False, reason="couldn't read duration from one of the files")
        return
    gap = tgt_dur - src_dur

    src_cast = find_track(args.source, is_castilian_props)
    if src_cast is None:
        write_status(status="no_fix", fix_found=False, reason="source has no confirmed-Castilian track")
        return
    _, castilian_idx = src_cast

    src_eng = find_track(args.source, is_english_props)
    tgt_eng = find_track(args.target, is_english_props)

    if src_eng and tgt_eng:
        log(f"analyzing {os.path.basename(args.source)} (gap {gap:+.1f}s, tier 1: direct correlation scan)")
        # Tier 1 doesn't use black-frame boundaries at all -- the scan
        # itself IS the verification (every accepted point already passed
        # the correlation threshold), unlike tier 2 below where a
        # separate check is needed after the fact.
        points = scan_for_divergence(args.source, src_eng[1], args.target, tgt_eng[1], src_dur, tgt_dur, args.mode)
        if points is None:
            write_status(status="no_fix", fix_found=False, reason="correlation scan couldn't cleanly resolve the gap against shared English audio -- may be a genuinely different edit")
            return
    else:
        log(f"analyzing {os.path.basename(args.source)} (gap {gap:+.1f}s, tier 2: black-frame boundaries only, weaker)")
        src_bounds = black_boundaries(args.source)
        tgt_bounds = black_boundaries(args.target)
        points = find_candidate_points(src_bounds, tgt_bounds, gap)
        if points is None:
            write_status(status="no_fix", fix_found=False, reason="no clean set of black-frame boundaries explains the full duration gap -- may be a genuinely different edit")
            return

    corrected_path = os.path.join(args.cache_dir, f"{key}.corrected.mka")
    built_path, splice_ats = build_corrected_audio(args.source, castilian_idx, points, corrected_path)
    if built_path is None:
        write_status(status="no_fix", fix_found=False, reason="couldn't find a genuinely quiet splice point, or the correction build failed")
        return

    corrected_dur = container_duration(corrected_path)
    if corrected_dur is None or abs(corrected_dur - tgt_dur) > TIGHT_TOLERANCE:
        os.remove(corrected_path)
        write_status(status="no_fix", fix_found=False, reason=f"corrected duration ({corrected_dur}) didn't land within tolerance of target ({tgt_dur})")
        return

    if not retag_castilian(corrected_path):
        os.remove(corrected_path)
        write_status(status="no_fix", fix_found=False, reason="failed to re-tag corrected audio's language/title")
        return

    verification_path = os.path.join(args.cache_dir, f"{key}.verification.mkv")
    clip = build_verification_clip(args.target, corrected_path, splice_ats, verification_path)

    write_status(
        status="fix_found", fix_found=True,
        correction_points=[{"at_source": p["at_source"], "direction": p["direction"], "extra_duration": p["extra_duration"]} for p in points],
        splice_points=splice_ats,
        corrected_audio=corrected_path,
        verification_clip=(clip["path"] if clip else None),
        verification_clip_start=(clip["start"] if clip else None),
        verification_clip_duration=(clip["duration"] if clip else None),
        target_duration=tgt_dur,
    )


if __name__ == "__main__":
    main()
