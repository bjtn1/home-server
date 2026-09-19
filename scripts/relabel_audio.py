#!/usr/bin/env python3
"""
Fix mis-tagged audio tracks in .mkv files.

An earlier tag-everything pass labeled every audio track spa/es-ES. This relabels only tracks
that are currently spa AND whose track NAME clearly identifies another language
(Japones -> jpn/ja, Ingles/English/[Eng] -> eng/en, Catalan / Catalan titles -> cat/ca,
Japanese-script names -> jpn/ja). Track names, Castilian tracks and ambiguous
(unnamed / unrecognised) tracks are never touched. Metadata-only (mkvpropedit), idempotent.

Dry-run by default; pass --apply to write.
    python3 relabel_audio.py /mnt/vault/tv /mnt/vault/movies
    python3 relabel_audio.py --apply --exclude "Inazuma Eleven" /mnt/vault/tv

Origin: written by pc-claude (Windows session), reviewed/adapted on the server: added
--min-age-minutes (default 30) so a file that is still being copied in is never edited.
"""
import argparse, json, os, re, shutil, subprocess, sys, time, unicodedata

WIN = os.name == "nt"


def tool(name):
    p = shutil.which(name)
    if p:
        return p
    cand = os.path.join(r"C:\Program Files\MKVToolNix", name + ".exe")
    if os.path.exists(cand):
        return cand
    sys.exit(f"{name} not found")


def norm(s):
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


RULES = [
    (re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]"), ("jpn", "ja")),
    (re.compile(r"\bjapon|\bjapanese|\[jap\]|\[jpn\]|\bjpn\b"), ("jpn", "ja")),
    (re.compile(r"\bingles\b|\benglish\b|\[eng\]|\beng\b"), ("eng", "en")),
    (re.compile(r"\bcatal|\bjoc de\b"), ("cat", "ca")),
]


def decide(name):
    n = norm(name)
    if not n.strip() or "castellano" in n or "european spanish" in n:
        return None
    for rx, lang in RULES:
        if rx.search(n):
            return lang
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="+")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--exclude", action="append", default=[], help="top-level folder names to skip")
    ap.add_argument("--min-age-minutes", type=float, default=30,
                    help="skip files modified more recently than this (default 30; 0 disables)")
    a = ap.parse_args()
    mkvmerge, mkvpropedit = tool("mkvmerge"), tool("mkvpropedit")
    changed = files = errs = recent = 0
    for root in a.roots:
        walk_root = "\\\\?\\" + os.path.abspath(root) if WIN else root
        for d, dirs, fs in os.walk(walk_root):
            rel = os.path.relpath(d, walk_root)
            if rel != "." and rel.split(os.sep)[0] in a.exclude:
                dirs[:] = []
                continue
            if rel == ".":
                dirs[:] = [x for x in dirs if x not in a.exclude]
            for f in fs:
                if not f.lower().endswith(".mkv"):
                    continue
                p = os.path.join(d, f)
                if a.min_age_minutes and time.time() - os.path.getmtime(p) < a.min_age_minutes * 60:
                    recent += 1
                    continue
                files += 1
                try:
                    r = subprocess.run([mkvmerge, "-J", p], capture_output=True, timeout=120)
                    tracks = json.loads(r.stdout.decode("utf-8"))["tracks"]
                except Exception as e:
                    errs += 1
                    print("ERR probe", p, e)
                    continue
                edits = []
                for t in tracks:
                    if t["type"] != "audio":
                        continue
                    pr = t["properties"]
                    if pr.get("language") != "spa":
                        continue
                    new = decide(pr.get("track_name", ""))
                    if new:
                        edits.append((t["id"] + 1, pr.get("track_name", ""), new))
                if not edits:
                    continue
                for num, nm, new in edits:
                    print(("SET " if a.apply else "WOULD SET ") + f"track {num} '{nm}' -> {new[0]}/{new[1]}: {p.replace(chr(92)*2+'?'+chr(92), '')}")
                if a.apply:
                    cmd = [mkvpropedit, p]
                    for num, _, new in edits:
                        cmd += ["--edit", f"track:{num}", "--set", f"language={new[0]}", "--set", f"language-ietf={new[1]}"]
                    r = subprocess.run(cmd, capture_output=True, timeout=300)
                    if r.returncode not in (0, 1):  # mkvpropedit: 0 ok, 1 warnings, 2 error
                        errs += 1
                        print("ERR mkvpropedit", p, r.stdout.decode("utf-8", "replace")[:200])
                        continue
                changed += 1
    print(f"files scanned={files} skipped_recent={recent} files {'changed' if a.apply else 'to change'}={changed} errors={errs}")


if __name__ == "__main__":
    main()
