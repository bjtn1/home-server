#!/usr/bin/env python3
"""Build a per-show whisper glossary (initial_prompt) for whisper-generate.py.

Why: dub-localized character/place names are nearly absent from whisper's
training data, so it substitutes phonetically-close words ("Gallas" for
"Agallas", "Motel Gatos" for "Motel Gatoz"). whisper's initial_prompt biases
decoding toward literal vocabulary (style/vocabulary mimicry, NOT semantics --
a title or episode number alone does nothing). 224-token budget, the LAST
tokens win, so the most important terms go LAST.

Sources, in priority order (lowest priority first in the output):
  3. RECURRING words from this show's already-generated .es.srt files
     (capitalized mid-sentence in >= MIN_EPISODES episodes, almost never
     lowercase) -- BUT any candidate that is a near-miss (similarity >= 0.75)
     of an authoritative term is treated as a MISSPELLING of it and dropped, so
     a wrong recurring transcription ("Gallas") can never reinforce itself.
  2. Proper nouns in episode TITLES (capitalized, not sentence-initial, never
     seen lowercase in any title) -- the titles come from the dub's own episode
     list, so they carry the dub's spellings ("Gatoz").
  1. The show folder name's words (highest priority, placed last).

Writes <cache dir>/glossaries/<show-slug>.txt (the prompt) and <show-slug>.variants.proposed.json (candidate
misspellings, for human review) -- only a curated <show-slug>.variants.json is ever applied by whisper_srt.correct_names -- both read by whisper-generate.py.
Never touches the library.

Usage:
    whisper-glossary.py "SHOW FOLDER NAME" [--print] [--min-episodes N]
"""
import argparse
import collections
import difflib
import glob
import json
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import whisper_srt as S

ROOTS = ["/mnt/vault/tv", "/mnt/vault/movies"]
CACHE_DIR = os.environ.get("WHISPER_CACHE_DIR", "/home/bjtn/whisper-subtitle-cache")
MIN_EPISODES = 3
MAX_CHARS = 350  # short on purpose: a long word-salad prompt can leak into the output
STOP = set("""el la los las un una unos unas y e o u de del al en a con por para sin sobre entre
que se su sus mi mis tu tus lo le les me te nos es son era fue ser estar hay no si mas pero como
cuando donde quien todo toda todos todas este esta estos estas ese esa esos esas aqui alli ahi
ya muy tan asi bien mal hoy ayer yo tu el ella nosotros ellos ellas usted ustedes vamos vaya
oye mira venga bueno vale gracias hola adios senor senora don dona""".split())


def load_vocab():
    try:
        return json.load(open(os.path.join(CACHE_DIR, "es_vocab.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return {}


REAL_WORD_MIN = 2  # seen this often in REAL human-made Spanish subtitles => a real word


def slug(s):
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def fold(w):
    return "".join(c for c in unicodedata.normalize("NFKD", w.lower()) if not unicodedata.combining(c))


WORD_RE = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ'’-]*")


def words(text):
    return WORD_RE.findall(text)


def similar(a, b):
    return difflib.SequenceMatcher(None, fold(a), fold(b)).ratio() >= 0.75


def episode_titles(show_dir):
    titles = []
    for p in sorted(glob.glob(os.path.join(show_dir, "**", "*.mkv"), recursive=True)):
        name = os.path.splitext(os.path.basename(p))[0]
        m = re.search(r"S\d+E\d+(?:-?E\d+)?\s*-\s*(.+)$", name)
        if m:
            # multi-episode files join two titles with a double space
            titles.extend(t.strip() for t in re.split(r"\s{2,}", m.group(1)) if t.strip())
    return titles


def build(show_dir, min_episodes=MIN_EPISODES):
    folder = os.path.basename(show_dir.rstrip("/"))
    # 1. folder words
    tier1 = [w for w in words(folder) if fold(w) not in STOP and len(w) >= 3]

    # 2. proper nouns in titles
    titles = episode_titles(show_dir)
    lower_seen = set()
    cap_seen = collections.Counter()
    for t in titles:
        ws = words(t)
        # Title Case titles ("La Cortina de la Crueldad") carry no proper-noun
        # signal in their capitalization; only sentence-case titles do.
        content = [w for w in ws[1:] if fold(w) not in STOP]
        if len(content) >= 3 and sum(w[0].isupper() for w in content) / len(content) >= 0.7:
            lower_seen.update(fold(w) for w in ws)  # treat as common vocabulary
            continue
        for i, w in enumerate(ws):
            if w[0].islower():
                lower_seen.add(fold(w))
            elif i > 0:
                cap_seen[w] += 1
    # a proper noun recurs across titles (Muriel, Gatoz, Shirley) or has an internal
    # capital (McPhearson); a one-off capitalized word in a short title is usually
    # just a common noun ("Extraterrestre") and would only add noise to the prompt
    tier2 = [w for w, n in cap_seen.most_common()
             if fold(w) not in STOP and fold(w) not in lower_seen and len(w) >= 4
             and (n >= 2 or re.search(r"[a-z][A-Z]", w))]

    authoritative = tier1 + tier2

    # 3. recurring capitalized words from generated subtitles (Option B)
    per_ep_cap = collections.defaultdict(set)
    lower_count = collections.Counter()
    tok_count = collections.Counter()
    example = {}
    for srt in (glob.glob(os.path.join(glob.escape(show_dir), "**", "*.es.srt"), recursive=True)
                + glob.glob(os.path.join(glob.escape(show_dir), "**", "*.es.Castilian.srt"), recursive=True)):
        cues = S.parse_srt(open(srt, encoding="utf-8", errors="replace").read())
        for _s, _e, text in cues:
            for m in WORD_RE.finditer(text):
                w = m.group(0)
                tok_count[fold(w)] += 1
                example.setdefault(fold(w), text.replace('\n', ' ')[:80])
                if w[0].islower():
                    lower_count[fold(w)] += 1
                    continue
                before = text[:m.start()].rstrip()
                # sentence-initial words are capitalized regardless; only mid-sentence
                # capitals signal proper nouns
                if before and before[-1] not in ".!?¡¿\"\u2014-:" and not before.endswith("..."):
                    per_ep_cap[w].add(srt)
    # a real recurring name shows up in a sizeable share of episodes; stray
    # mishearings ("Gayas", English "Courage") appear in only a few
    n_srt = (len(glob.glob(os.path.join(glob.escape(show_dir), "**", "*.es.srt"), recursive=True))
             + len(glob.glob(os.path.join(glob.escape(show_dir), "**", "*.es.Castilian.srt"), recursive=True)))
    min_episodes = max(min_episodes, int(n_srt * 0.15))
    tier3 = []
    # most widespread first, so among spelling variants of one name the most frequent
    # (canonical) spelling wins and the rest are dropped as misspellings of it
    for w, eps in sorted(per_ep_cap.items(), key=lambda kv: -len(kv[1])):
        if len(eps) < min_episodes or fold(w) in STOP or len(w) < 4:
            continue
        if lower_count[fold(w)] > len(eps) * 0.2:  # commonly lowercase => common word
            continue
        if any(similar(w, a) for a in authoritative + tier3):  # variant/misspelling of a kept term
            continue
        tier3.append(w)

    ordered = []
    seen = set()
    for w in tier3[::-1] + tier2[::-1] + tier1[::-1]:  # least important first
        if fold(w) not in seen:
            seen.add(fold(w))
            ordered.append(w)
    ordered.reverse()  # now most important first; trim from the LOW end below
    kept, total = [], 0
    for w in ordered[:30]:
        if total + len(w) + 2 > MAX_CHARS:
            break
        kept.append(w)
        total += len(w) + 2
    prompt = ", ".join(reversed(kept))  # most important LAST
    # Proper-noun list: a word that is ALSO very common lowercase in this show
    # ("perro", "historia") is an ordinary noun, not a name -- keep it out.
    names = []
    for w in tier1 + tier2 + tier3:
        f = fold(w)
        total = tok_count[f]
        # mostly-lowercase in the show's own text => an ordinary noun ("perro",
        # "cobarde", "historia"), not a name; "agallas" (22% lowercase) stays
        if total >= 5 and lower_count[f] / total >= 0.5:
            continue
        if f not in {fold(x) for x in names}:
            names.append(w)

    # Explicit variant -> name map (the ONLY thing whisper_srt.correct_names applies).
    # A token qualifies iff it is a near-miss of a name, occurs >= 3 times, is rarely
    # seen lowercase (so it is not ordinary vocabulary) and is not a stopword. Ordinary
    # words such as "pero" (lowercase count in the hundreds) can never qualify.
    vocab = load_vocab()
    variants, rejected_real = {}, {}
    for f, n in tok_count.items():
        if n < 3 or f in STOP or len(f) < 4:
            continue
        best, score = None, 0.0
        for nm in names:
            if fold(nm) == f:
                best = None
                break
            r = difflib.SequenceMatcher(None, f, fold(nm)).ratio()
            if r > score:
                best, score = nm, r
        if not (best and score >= 0.75):
            continue
        real = vocab.get(f, 0)
        if real >= REAL_WORD_MIN:  # a real Spanish word: NEVER a misspelling of a name
            rejected_real[f] = {"to": best, "count": n, "real_spanish_count": real}
            continue
        mostly_cap = lower_count[f] / n <= 0.5
        variants[f] = {"to": best, "count": n, "lowercase_count": lower_count[f],
                       "similarity": round(score, 2), "example": example.get(f, ""),
                       "confidence": "likely" if (mostly_cap and vocab) else "review"}
    return prompt, {"names": names, "variants": variants, "rejected_real": rejected_real, "vocab_loaded": bool(vocab), "folder_words": tier1,
                    "title_terms": tier2[:40], "recurring": tier3[:40],
                    "titles": len(titles)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("show")
    ap.add_argument("--print", action="store_true", help="print, don't write")
    ap.add_argument("--min-episodes", type=int, default=MIN_EPISODES)
    a = ap.parse_args()
    show_dir = next((os.path.join(r, a.show) for r in ROOTS if os.path.isdir(os.path.join(r, a.show))), None)
    if not show_dir:
        sys.exit(f"no such show folder under {ROOTS}: {a.show}")
    prompt, info = build(show_dir, a.min_episodes)
    print(f"sources: {info}", file=sys.stderr)
    print(prompt)
    if not a.print:
        out = os.path.join(CACHE_DIR, "glossaries", slug(a.show) + ".txt")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        open(out, "w", encoding="utf-8").write(prompt + "\n")
        print(f"wrote {out} ({len(prompt)} chars)", file=sys.stderr)
        prop = out[:-4] + ".variants.proposed.json"
        json.dump(info["variants"], open(prop, "w", encoding="utf-8"), ensure_ascii=False,
                  indent=1, sort_keys=True)
        high = {v: d["to"] for v, d in info["variants"].items() if d["confidence"] == "likely"}
        print(f"vocab loaded: {info['vocab_loaded']} | {len(info['variants'])} proposals "
              f"({len(high)} likely) | {len(info['rejected_real'])} auto-rejected as real Spanish words",
              file=sys.stderr)
        for v, d in sorted(info["variants"].items(), key=lambda kv: -kv[1]["count"]):
            print(f"   [{d['confidence']:6}] {v!r:14} -> {d['to']!r:11} {d['count']:4}x (lc {d['lowercase_count']}, sim {d['similarity']})  e.g. {d['example']!r}",
                  file=sys.stderr)
        for v, d in sorted(info["rejected_real"].items(), key=lambda kv: -kv[1]["count"])[:12]:
            print(f"   [REJECT] {v!r:14} -> {d['to']!r:11} real Spanish word ({d['real_spanish_count']}x in human subs)", file=sys.stderr)
        print(f"nothing applied: copy the approved subset to {out[:-4]}.variants.json "
              f"(only a human-curated file is ever applied; auto-approval was removed after it "
              f"proposed 'habichuela'->'Abichuela')", file=sys.stderr)


if __name__ == "__main__":
    main()
