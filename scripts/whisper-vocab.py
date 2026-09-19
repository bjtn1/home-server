#!/usr/bin/env python3
"""Build a real-Spanish vocabulary from library files that ALREADY have real,
human-made Spanish subtitles (e.g. BoJack Horseman: every episode has a 400+ cue
embedded SRT). whisper-glossary.py uses it to tell a real Spanish word ("miel",
"gatos", "callas") from a whisper misspelling of a name ("gallas", "eustachio"),
which is what made fully automatic name correction unsafe.

Only LOWERCASE tokens are counted (names are capitalized, so they stay out), and
only from tracks tagged Spanish with a healthy cues/min rate. Accent-folded, so a
lookup is insensitive to accents.

Usage:
    whisper-vocab.py [--show "BoJack Horseman"] [--sample 30] [--books /mnt/vault/books] [--out FILE]
Output: <cache dir>/es_vocab.json  {folded_word: count}
"""
import argparse
import collections
import json
import os
import subprocess
import sys
import tempfile
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import whisper_media as M
import whisper_srt as S

CACHE_DIR = os.environ.get("WHISPER_CACHE_DIR", "/home/bjtn/whisper-subtitle-cache")
ROOTS = ["/mnt/vault/tv", "/mnt/vault/movies"]
import re
WORD = re.compile(r"[A-Za-záéíóúüñÁÉÍÓÚÜÑ]+")


def lower_tokens(text):
    """Whole words that start lowercase (a capitalized name never leaks a lowercase fragment)."""
    return [t for t in WORD.findall(text) if t[0].islower()]


def fold(w):
    return "".join(c for c in unicodedata.normalize("NFKD", w) if not unicodedata.combining(c))


def best_spanish_sub(info):
    """-> index among SUBTITLE tracks (ffmpeg -map 0:s:N) of the biggest Spanish track."""
    subs = [t for t in info["tracks"] if t["type"] == "subtitles"]
    best, best_n = None, 0
    for n, t in enumerate(subs):
        p = t["properties"]
        if M._is_spanish_tag(p) and t["codec"].startswith("SubRip") and (p.get("num_index_entries") or 0) > best_n:
            best, best_n = n, p["num_index_entries"]
    return best, best_n


def epub_spanish_words(path):
    """Word counts from a Spanish-language epub (dc:language starts with 'es'); nothing else kept."""
    import zipfile
    out = collections.Counter()
    try:
        z = zipfile.ZipFile(path)
        opf = next((n for n in z.namelist() if n.lower().endswith(".opf")), None)
        if not opf:
            return out
        head = z.read(opf).decode("utf-8", "replace")
        m = re.search(r"<dc:language[^>]*>\s*([A-Za-z-]+)\s*<", head)
        if not m or not m.group(1).lower().startswith("es"):
            return out
        for n in z.namelist():
            if n.lower().endswith((".xhtml", ".html", ".htm")):
                text = re.sub(r"<[^>]+>", " ", z.read(n).decode("utf-8", "replace"))
                text = re.sub(r"&[a-z#0-9]+;", " ", text)
                for tok in lower_tokens(text):
                    out[fold(tok)] += 1
    except (zipfile.BadZipFile, OSError, KeyError):
        pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", default="BoJack Horseman")
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--books", help="also count words from Spanish-language epubs under this dir (counts only)")
    ap.add_argument("--out", default=os.path.join(CACHE_DIR, "es_vocab.json"))
    a = ap.parse_args()

    files = list(M.walk_videos(ROOTS, a.show))
    if not files:
        sys.exit(f"no videos match {a.show!r}")
    step = max(1, len(files) // max(a.sample, 1))
    picked = files[::step][: a.sample]
    counts = collections.Counter()
    used = 0
    for f in picked:
        info = M.mkv_info(f)
        idx, n = best_spanish_sub(info)
        dur = info["container"]["properties"].get("duration", 0) / 6e10
        if idx is None or not dur or n / dur < M.DEFAULT_MIN_CUES_PER_MIN:
            continue
        with tempfile.NamedTemporaryFile(suffix=".srt", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", f, "-map", f"0:s:{idx}", tmp_path],
                               capture_output=True, timeout=600)
            if r.returncode != 0:
                continue
            for _s, _e, text in S.parse_srt(open(tmp_path, encoding="utf-8", errors="replace").read()):
                for tok in lower_tokens(text):
                    counts[fold(tok)] += 1
            used += 1
            print(f"  {used:2}/{len(picked)} {os.path.basename(f)[:60]}: {n} cues", file=sys.stderr)
        finally:
            os.remove(tmp_path)
    if a.books:
        nb = 0
        for dp, _dn, fn in os.walk(a.books):
            for f in sorted(fn):
                if f.lower().endswith(".epub"):
                    c = epub_spanish_words(os.path.join(dp, f))
                    if c:
                        counts.update(c)
                        nb += 1
                        print(f"  epub (es): {f[:60]}: {sum(c.values())} words", file=sys.stderr)
        print(f"books used: {nb}", file=sys.stderr)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(dict(counts.most_common()), open(a.out, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"vocab: {len(counts)} distinct words from {used} files -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
