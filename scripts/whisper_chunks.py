#!/usr/bin/env python3
"""Speech-chunked whisper transcription (fixes timestamps that drift/jump after long silences).

Problem (Iron Kid S01E01): decoding a whole 22-minute episode in one pass, whisper puts the first line after a long
quiet stretch at the WRONG time -- "Aquel al que buscabas ya no existe, Gath." was placed at 132 s but is spoken at
168 s (36 s early), and it invents dialogue over music. Around 5-14% of cues sit outside any speech.

Fix: (1) Silero VAD (scripts/speech_vad.py, CPU) finds where speech really is; (2) speech regions are packed into
windows of <= MAX_PACK_S seconds separated by SEP_S of silence, so whisper never decodes long silence/music;
(3) each pack is transcribed on its own and its timestamps are mapped back onto the episode timeline.

Pure standard library (the VAD runs in its own venv via a subprocess). The transcriber is injected, so all of this is
testable without a whisper server.
"""
import json
import os
import subprocess
import tempfile
import wave

SR = 16000
MAX_PACK_S = 27.0        # whisper decodes 30 s windows; stay under it
SEP_S = 1.0              # silence inserted between speech pieces inside a pack (long enough that whisper breaks segments there)
MERGE_GAP_S = 0.6        # speech regions closer than this are one region
MIN_PIECE_S = 0.25
MIN_OVERLAP_S = 0.4      # a cue must overlap a piece by this much to count as being IN it
SPLIT_GAP_S = 2.0        # pieces further apart than this (in the episode) are not one continuous stretch
VAD_PY = os.path.expanduser("~/venvs/vad/bin/python")
VAD_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "speech_vad.py")


def detect_speech(wav_path, timeout=1800):
    """-> [(start_s, end_s)] speech regions, via the isolated Silero VAD venv."""
    r = subprocess.run([VAD_PY, VAD_SCRIPT, "--json", wav_path], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("VAD failed: " + r.stderr.strip()[-200:])
    return [(float(a), float(b)) for a, b in json.loads(r.stdout)]


def merge_regions(segs, gap=MERGE_GAP_S):
    out = []
    for a, b in sorted(segs):
        if out and a - out[-1][1] <= gap:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return [(a, b) for a, b in out if b - a >= MIN_PIECE_S]


def plan_packs(segs, max_s=MAX_PACK_S, sep=SEP_S):
    """Group speech regions into packs. A pack is a list of pieces (orig_a, orig_b, pack_offset), all in seconds.
    Regions longer than max_s are cut into max_s slices. Every pack's total length is <= max_s."""
    pieces = []
    for a, b in merge_regions(segs):
        while b - a > max_s:
            pieces.append((a, a + max_s))
            a += max_s
        pieces.append((a, b))
    packs, cur, used = [], [], 0.0
    for a, b in pieces:
        need = (b - a) + (sep if cur else 0.0)
        if cur and used + need > max_s:
            packs.append(cur)
            cur, used = [], 0.0
            need = b - a
        off = used + (sep if cur else 0.0)
        cur.append((a, b, off))
        used = off + (b - a)
    if cur:
        packs.append(cur)
    return packs


def pack_length(pack):
    a, b, off = pack[-1]
    return off + (b - a)


def map_time(pack, t, is_end):
    """Pack time -> episode time. Times that fall in an inserted separator snap to the next piece's start (for a
    cue START) or the previous piece's end (for a cue END); times past the pack clamp to its last piece."""
    for i, (a, b, off) in enumerate(pack):
        length = b - a
        if t < off:                                   # in the separator BEFORE this piece
            if is_end and i > 0:
                pa, pb, _ = pack[i - 1]
                return pb
            return a
        if t <= off + length:
            return a + (t - off)
    a, b, off = pack[-1]
    return b


def place_cue(pack, s, e, text, min_overlap=MIN_OVERLAP_S, split_gap=SPLIT_GAP_S):
    """Pack-time cue -> [(orig_start_s, orig_end_s, text)] on the episode timeline.

    Whisper sometimes glues the tail of one speech piece to the start of the next into one segment. Starting the cue
    where the FIRST word was heard would place it at the wrong end of a long silence (Iron Kid: a line spoken at
    168.65 s was placed at 43 s). So: a cue counts as being 'in' a piece only if it overlaps it by >= min_overlap;
    a cue in a single piece (or in pieces that are contiguous in the episode) keeps ONE cue, clamped to those pieces;
    a cue that genuinely spans pieces far apart is split, sharing its words in proportion to the overlaps; a cue that
    lies entirely in the inserted silence (a hallucination on digital silence) is dropped."""
    if e <= s:                      # whisper occasionally emits zero/negative-length cues: give them a minimum span
        e = s + 0.5
    ov = []
    for i, (a, b, off) in enumerate(pack):
        o = min(e, off + (b - a)) - max(s, off)
        if o > 0:
            ov.append((i, o))
    if not ov or max(o for _, o in ov) < 0.15:
        return []
    big = max(o for _, o in ov)
    keep = [(i, o) for i, o in ov if o >= min(min_overlap, big)]
    groups = [[keep[0]]]
    for i, o in keep[1:]:
        prev_i = groups[-1][-1][0]
        if pack[i][0] - pack[prev_i][1] <= split_gap:
            groups[-1].append((i, o))
        else:
            groups.append([(i, o)])
    words = text.split()
    total = sum(o for g in groups for _, o in g)
    out, done, cum = [], 0, 0.0
    for gi, g in enumerate(groups):
        first, last = g[0][0], g[-1][0]
        fa, fb, foff = pack[first]
        la, lb, loff = pack[last]
        start = fa + (max(s, foff) - foff)
        end = la + (min(e, loff + (lb - la)) - loff)
        if len(groups) == 1:
            piece_text = text
        else:
            cum += sum(o for _, o in g)
            upto = len(words) if gi == len(groups) - 1 else max(done + 1, round(len(words) * cum / total))
            piece_text = " ".join(words[done:upto])
            done = upto
        if piece_text.strip():
            out.append((start, max(end, start + 0.5), piece_text))
    return out


def build_pack_wav(src_frames, pack, out_path, sep=SEP_S):
    """Write the pack's audio (speech pieces separated by digital silence) as 16 kHz mono 16-bit WAV."""
    silence = b"\x00\x00" * int(sep * SR)
    with wave.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        for i, (a, b, off) in enumerate(pack):
            if i:
                w.writeframes(silence)
            w.writeframes(src_frames[int(a * SR) * 2:int(b * SR) * 2])


def transcribe_chunked(wav_path, transcribe_fn, parse_srt, segs=None, log=None):
    """Transcribe an episode pack by pack. transcribe_fn(wav_path) -> SRT text. -> (cues_ms, stats).
    cues_ms are [start_ms, end_ms, text] on the ORIGINAL episode timeline, sorted by start."""
    with wave.open(wav_path) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (SR, 1, 2):
            raise ValueError("expected 16 kHz mono 16-bit WAV")
        frames = w.readframes(w.getnframes())
    segs = detect_speech(wav_path) if segs is None else segs
    packs = plan_packs(segs)
    cues = []
    for n, pack in enumerate(packs):
        tmp = tempfile.mktemp(suffix=".wav", dir=os.path.dirname(wav_path) or None)
        try:
            build_pack_wav(frames, pack, tmp)
            text = transcribe_fn(tmp)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        for s, e, t in parse_srt(text):
            for a, b, piece_text in place_cue(pack, s / 1000.0, e / 1000.0, t):
                cues.append([int(round(a * 1000)), int(round(b * 1000)), piece_text])
        if log:
            log(f"chunk {n + 1}/{len(packs)}: {len(pack)} piece(s), {pack_length(pack):.1f}s")
    cues.sort(key=lambda c: (c[0], c[1]))
    speech_s = sum(b - a for a, b in merge_regions(segs))
    return cues, {"packs": len(packs), "speech_s": round(speech_s, 1), "regions": len(merge_regions(segs))}
