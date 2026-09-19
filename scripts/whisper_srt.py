#!/usr/bin/env python3
"""SRT parse/format plus the loop-detection post-processor for whisper output
(see scripts/whisper-subtitle-plan.md, "Known quality issues" #1).

whisper.cpp can get stuck inside one decode window and emit the same cue many
times at (almost) the same timestamp -- e.g. six identical
"¿Qué quieres jugar con el fútbol?" cues at 00:08:26,810 --> 00:08:26,910.
no_context / VAD do not reliably prevent it, so every generated SRT goes
through clean() before being written next to a video.

clean() rules (deliberately conservative -- real dialogue repeats too):
  1. DUPLICATE/STUCK LOOP -> collapse. Consecutive cues with identical or
     near-identical text that overlap or touch in time (next.start <
     prev.end + STUCK_GAP_MS) are merged into one cue spanning them. This
     covers both the true stuck loop (same timestamp repeated) and whisper
     re-emitting one shouted line across a segment boundary. A genuine
     repetition separated by real silence (>= STUCK_GAP_MS) is NOT touched.
  2. SPREAD REPEAT -> flag only. The same text repeated >= FLAG_RUN_MIN times
     in a row with normal timestamps is reported, never modified.
  3. INTRA-CUE LOOP -> truncate. A word/phrase (1-4 words) repeated more than
     INTRA_MAX times back-to-back inside one cue ("sí, sí, sí, sí, ...") is cut
     to INTRA_KEEP repeats.
  4. Cues whose end <= start get a minimum duration (whisper emits some).
  5. Known silence-hallucination phrases inside longer cues are FLAGGED; a cue that is
     ONLY such a phrase ("¡Suscríbete al canal!") is DROPPED (rule 7).
  6. OVER-LONG CUES -> trim. A cue displayed much longer than its text needs
     (allowed = 1 s + 70 ms/char, clamped to 1.5-10 s, plus 1 s slack) gets its
     END pulled in to start + allowed. Starts are left alone.
Everything changed or flagged is listed in the returned report.

CLI:
    whisper_srt.py clean IN.srt [-o OUT.srt] [--report REPORT.json]
    whisper_srt.py stats IN.srt            (cue count, span)
"""
import argparse
import difflib
import json
import re
import sys
import unicodedata

STUCK_GAP_MS = 300
SIMILARITY = 0.92
FLAG_RUN_MIN = 4
INTRA_MAX = 5
INTRA_KEEP = 3
MIN_DURATION_MS = 500
# whisper.cpp stretches a cue's END across silence until the next speech (seen: a
# 25-char line on screen for 130 s). Cap display time by text length.
TRIM_BASE_MS = 1000
TRIM_PER_CHAR_MS = 70
TRIM_MIN_MS = 1500
TRIM_MAX_MS = 10000
TRIM_SLACK_MS = 1000

SUSPECT_PHRASES = [
    r"subt[ií]tulos (realizados )?por la comunidad de amara\.org",
    r"amara\.org",
    r"gracias por (ver|vernos|su atenci[oó]n)",
    r"suscr[ií]bete",
    r"subtitulado por",
    r"visita (nuestra|mi) web",
]
_SUSPECT_RE = re.compile("|".join(SUSPECT_PHRASES), re.I)

# A cue that is NOTHING but one of these is a silence hallucination (seen on real
# cartoon output: "¡Suscríbete al canal!" at 00:00:00) and is dropped, not just flagged.
_DROP_CUE_RE = re.compile(
    r"^\W*(suscr[ií]bete( al canal)?|subt[ií]tulos (realizados )?por la comunidad de amara\.org|"
    r"amara\.org|subtitulado por [\w .]+|gracias por (ver|vernos)( el v[ií]deo)?)\W*$", re.I)

_TS = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})")


def _ms(ts):
    m = _TS.fullmatch(ts.strip())
    if not m:
        raise ValueError(f"bad timestamp: {ts!r}")
    h, mi, s, frac = m.groups()
    return ((int(h) * 60 + int(mi)) * 60 + int(s)) * 1000 + int(frac.ljust(3, "0"))


def _ts(ms):
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, frac = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{frac:03d}"


def parse_srt(text):
    """-> list of [start_ms, end_ms, text]. Tolerant of missing indexes/CRLF."""
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = [l for l in block.split("\n")]
        while lines and not lines[0].strip():
            lines.pop(0)
        if not lines:
            continue
        if "-->" not in lines[0]:
            lines = lines[1:]  # drop the numeric index
        if not lines or "-->" not in lines[0]:
            continue
        a, b = lines[0].split("-->")
        try:
            cues.append([_ms(a), _ms(b.split()[0]), "\n".join(lines[1:]).strip()])
        except ValueError:
            continue
    return cues


def format_srt(cues):
    out = []
    for i, (s, e, t) in enumerate(cues, 1):
        out.append(f"{i}\n{_ts(s)} --> {_ts(e)}\n{t}\n")
    return "\n".join(out)


def _norm(t):
    t = unicodedata.normalize("NFKD", t.casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", " ", t).strip()


def _same(a, b):
    if a == b:
        return True
    return bool(a) and bool(b) and difflib.SequenceMatcher(None, a, b).ratio() >= SIMILARITY


def _cut_intra(text):
    """Truncate back-to-back repeats of a 1-4 word phrase. -> (text, changed)."""
    words = text.split()
    changed = False
    for n in (1, 2, 3, 4):
        i = 0
        out = []
        while i < len(words):
            unit = words[i:i + n]
            if len(unit) < n:
                out.extend(words[i:])
                break
            key = [_norm(w) for w in unit]
            reps = 1
            while (i + (reps + 1) * n <= len(words)
                   and [_norm(w) for w in words[i + reps * n:i + (reps + 1) * n]] == key):
                reps += 1
            if reps > INTRA_MAX and any(key):
                out.extend(unit * INTRA_KEEP)
                i += reps * n
                changed = True
            else:
                out.extend(words[i:i + n] if reps == 1 else words[i:i + reps * n])
                i += reps * n
        words = out
    return " ".join(words), changed


def clean(cues):
    """-> (cleaned_cues, report). Input cues are not mutated."""
    cues = [list(c) for c in cues]
    report = {"cues_in": len(cues), "collapsed_runs": [], "flagged_repeats": [],
              "intra_truncated": [], "fixed_durations": 0, "trimmed_durations": 0,
              "suspect_phrases": [], "dropped_hallucinations": []}

    # 4. minimum duration / negative duration
    for c in cues:
        if c[1] <= c[0]:
            c[1] = c[0] + MIN_DURATION_MS
            report["fixed_durations"] += 1

    # 1. stuck-loop collapse
    out = []
    i = 0
    while i < len(cues):
        j = i
        base = _norm(cues[i][2])
        end = cues[i][1]
        while (j + 1 < len(cues) and cues[j + 1][0] < end + STUCK_GAP_MS
               and _same(base, _norm(cues[j + 1][2]))):
            j += 1
            end = max(end, cues[j][1])
        if j > i:
            report["collapsed_runs"].append(
                {"at": _ts(cues[i][0]), "text": cues[i][2][:80], "repeats": j - i + 1})
            out.append([cues[i][0], end, cues[i][2]])
        else:
            out.append(cues[i])
        i = j + 1

    # 2. spread repeats: flag only
    i = 0
    while i < len(out):
        j = i
        while j + 1 < len(out) and _same(_norm(out[i][2]), _norm(out[j + 1][2])):
            j += 1
        if j - i + 1 >= FLAG_RUN_MIN and _norm(out[i][2]):
            report["flagged_repeats"].append(
                {"at": _ts(out[i][0]), "text": out[i][2][:80], "repeats": j - i + 1})
        i = j + 1

    # 3. intra-cue loops + 5. suspect phrases
    for c in out:
        new, ch = _cut_intra(c[2])
        if ch:
            report["intra_truncated"].append({"at": _ts(c[0]), "was": c[2][:80], "now": new[:80]})
            c[2] = new
        if _SUSPECT_RE.search(c[2]):
            report["suspect_phrases"].append({"at": _ts(c[0]), "text": c[2][:80]})

    # 6. trim over-long display durations (after collapsing, so merged spans are
    # sized by their text too)
    for c in out:
        allowed = min(TRIM_MAX_MS, max(TRIM_MIN_MS, TRIM_BASE_MS + TRIM_PER_CHAR_MS * len(c[2])))
        if c[1] - c[0] > allowed + TRIM_SLACK_MS:
            c[1] = c[0] + allowed
            report["trimmed_durations"] += 1

    # 7. drop pure hallucination cues
    kept = []
    for c in out:
        if _DROP_CUE_RE.match(c[2]):
            report["dropped_hallucinations"].append({"at": _ts(c[0]), "text": c[2][:80]})
        else:
            kept.append(c)
    out = kept

    report["cues_out"] = len(out)
    return out, report


_WORD = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ'’-]*")


def correct_names(cues, variants):
    """Apply an EXPLICIT variant -> canonical-name map (folded lowercase key ->
    proper spelling), e.g. {"gallas": "Agallas", "eustachio": "Eustaquio"}.

    The map is learned per show by whisper-glossary.py from the whole show's
    subtitles and deliberately contains only tokens that are NOT ordinary
    vocabulary of that show, so real words ("pero", "gatos") can never be touched.
    Never guesses per file. -> (cues, [(from, to, count)])."""
    if not variants:
        return cues, []
    fixes = {}

    def repl(m):
        w = m.group(0)
        to = variants.get(_norm(w))
        if to and w != to:
            fixes[(w, to)] = fixes.get((w, to), 0) + 1
            return to
        return w

    out = [[s_, e_, _WORD.sub(repl, t)] for s_, e_, t in cues]
    return out, [(a, b, n) for (a, b), n in sorted(fixes.items(), key=lambda kv: -kv[1])]


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("clean")
    c.add_argument("src")
    c.add_argument("-o", "--out")
    c.add_argument("--report")
    s = sub.add_parser("stats")
    s.add_argument("src")
    a = ap.parse_args()
    cues = parse_srt(open(a.src, encoding="utf-8", errors="replace").read())
    if a.cmd == "stats":
        span = (cues[-1][1] - cues[0][0]) / 60000 if cues else 0
        print(json.dumps({"cues": len(cues), "span_minutes": round(span, 1)}))
        return
    cleaned, rep = clean(cues)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(format_srt(cleaned))
    if a.report:
        json.dump(rep, open(a.report, "w"), ensure_ascii=False, indent=1)
    print(json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in rep.items()}),
          file=sys.stderr)


if __name__ == "__main__":
    main()
