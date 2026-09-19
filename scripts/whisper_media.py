#!/usr/bin/env python3
"""Probing + coverage logic shared by whisper-scan.py and whisper-generate.py
(see scripts/whisper-subtitle-plan.md, "Detection logic").

Two lessons from real library files are baked in:
  * Language TAGS are unreliable. A real Inazuma Eleven file has two audio
    tracks BOTH tagged spa/es-ES: "Castellano" (the dub) and "Japonés" (the
    original Japanese audio, mislabeled). pick_spanish_audio() therefore reads
    the track NAME, not just the tag, and never merges same-tagged tracks.
  * Track FLAGS are unreliable too ("Subs. Forzados" reads forced_track=false),
    so subtitle coverage is judged by cue count per minute, not by tags/flags.

Only Matroska is supported (mkvmerge -J gives per-track cue counts); that is
all the rebuilt Castilian library contains.
"""
import glob
import json
import os
import re
import subprocess

from whisper_srt import parse_srt

VIDEO_EXT = (".mkv",)
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

DEFAULT_MIN_CUES_PER_MIN = 4.0  # real Inazuma dialogue ~13/min; its forced track 0.6/min


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


def _count_ass_dialogue(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return sum(1 for l in f if l.startswith("Dialogue:"))
    except OSError:
        return 0


def sidecar_subs(video_path):
    """External subtitle files next to the video -> [(path, lang_token, cues)]."""
    stem, _ = os.path.splitext(video_path)
    out = []
    for p in glob.glob(glob.escape(stem) + "*"):
        ext = os.path.splitext(p)[1].lower()
        if ext not in (".srt", ".ass", ".ssa", ".vtt") or p == video_path:
            continue
        toks = os.path.basename(p)[len(os.path.basename(stem)):].lower().strip(".").split(".")
        lang = next((t for t in toks if t in SPANISH_CODES or re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]+)?", t)), "")
        if ext == ".srt" or ext == ".vtt":
            cues = len(parse_srt(open(p, encoding="utf-8", errors="replace").read()))
        else:
            cues = _count_ass_dialogue(p)
        out.append((p, lang, cues))
    return out


def probe(path, min_cpm=DEFAULT_MIN_CUES_PER_MIN):
    """Full detection result for one video."""
    info = mkv_info(path)
    dur_min = info["container"]["properties"].get("duration", 0) / 6e10
    tracks = info["tracks"]
    a_idx, a_name, a_note = pick_spanish_audio(tracks)

    es_cues = 0
    unknown = False
    und_high = []
    for t in tracks:
        if t["type"] != "subtitles":
            continue
        p = t["properties"]
        n = p.get("num_index_entries")
        if _is_spanish_tag(p):
            if n is None:
                unknown = True
            else:
                es_cues += n
        elif (p.get("language") or "und").lower() == "und" and n and dur_min and n / dur_min >= min_cpm:
            und_high.append(t["id"])
    for _p, lang, cues in sidecar_subs(path):
        if lang in SPANISH_CODES:
            es_cues += cues

    cpm = es_cues / dur_min if dur_min else 0.0
    if a_idx is None:
        verdict = "SKIP"
    elif unknown:
        verdict = "REVIEW"
    elif cpm >= min_cpm:
        verdict = "OK"
    else:
        verdict = "NEEDS"
    return {"path": path, "duration_min": round(dur_min, 1), "audio_index": a_idx,
            "audio_name": a_name, "audio_note": a_note, "spanish_cues": es_cues,
            "cues_per_min": round(cpm, 2), "und_high_candidates": und_high,
            "verdict": verdict}


def walk_videos(roots, show=None):
    for root in roots:
        for dp, dn, fn in os.walk(root):
            dn.sort()
            for f in sorted(fn):
                if f.lower().endswith(VIDEO_EXT):
                    full = os.path.join(dp, f)
                    if show and show.lower() not in full.lower():
                        continue
                    yield full
