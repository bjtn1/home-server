#!/usr/bin/env python3
"""Read-only scan: which Castilian-library videos lack real Spanish subtitle
coverage and so need whisper transcription (see whisper_media.py for the
detection rules and scripts/whisper-subtitle-plan.md for the why).

Scope is the Castilian library ONLY (/mnt/vault/tv, /mnt/vault/movies) --
never the -english libraries. Never writes anything.

Usage:
    whisper-scan.py [--show TEXT] [--min-cpm N] [--json] [--only VERDICT] [ROOT ...]

  verdict OK      real Spanish subs already present (>= --min-cpm cues/min)
          NEEDS   no/insufficient Spanish subs -> candidate for generation
          REVIEW  a Spanish sub track has unknown cue count
          SKIP    no usable Castilian audio track found
"""
import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import whisper_media as M

DEFAULT_ROOTS = ["/mnt/vault/tv", "/mnt/vault/movies"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="*", default=DEFAULT_ROOTS)
    ap.add_argument("--show", help="only paths containing this text")
    ap.add_argument("--min-cpm", type=float, default=M.DEFAULT_MIN_CUES_PER_MIN)
    ap.add_argument("--json", action="store_true", help="one JSON object per line")
    ap.add_argument("--only", choices=["OK", "NEEDS", "REVIEW", "SKIP"])
    a = ap.parse_args()

    for r in a.roots:
        real = os.path.realpath(r)
        if "-english" in os.path.basename(real):
            sys.exit(f"refusing to scan {r}: this pipeline is Castilian-library only")

    counts = collections.Counter()
    for path in M.walk_videos(a.roots, a.show):
        try:
            res = M.probe(path, a.min_cpm)
        except Exception as e:  # keep scanning; report the bad file
            res = {"path": path, "verdict": "ERROR", "audio_note": str(e)[:120]}
        counts[res["verdict"]] += 1
        if a.only and res["verdict"] != a.only:
            continue
        if a.json:
            print(json.dumps(res, ensure_ascii=False))
        else:
            print(f"{res['verdict']:6} {res.get('cues_per_min', '-'):>6} cpm  "
                  f"{res.get('duration_min', '-'):>5} min  audio={res.get('audio_name')!r} "
                  f"{'[' + res['audio_note'] + ']' if res.get('audio_note') not in (None, 'ok') else ''} "
                  f"{os.path.relpath(path, '/mnt/vault')}")
    print(f"\nsummary: {dict(counts)}", file=sys.stderr)


if __name__ == "__main__":
    main()
