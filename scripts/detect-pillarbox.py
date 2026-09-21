#!/usr/bin/env python3
"""Detect 4:3 pictures baked into 16:9 frames (black bars on the sides). Read-only.
usage: detect-pillarbox.py [--per-show N | --all] [--shows 'A,B'] [--out FILE]"""
import argparse, json, os, re, subprocess, sys, collections
from concurrent.futures import ThreadPoolExecutor

def probe(p):
    r = subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width,height:format=duration","-of","json",p],capture_output=True,text=True)
    j = json.loads(r.stdout); s = j["streams"][0]
    return int(s["width"]), int(s["height"]), float(j["format"].get("duration") or 0)

def crops(p, dur):
    out = []
    for frac in (0.12, 0.3, 0.5, 0.7, 0.88):
        t = max(5, dur * frac)
        r = subprocess.run(["nice","-n","19","ffmpeg","-v","info","-ss",f"{t:.1f}","-i",p,"-t","2","-vf","cropdetect=limit=24:round=2:reset=0","-an","-f","null","-"],capture_output=True,text=True)
        m = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", r.stderr)
        if m: out.append(tuple(int(x) for x in m[-1]))
    return out

def analyse(p):
    try:
        w, h, dur = probe(p)
        cs = crops(p, dur)
        if not cs: return p, None
        cw = max(c[0] for c in cs); ch = max(c[1] for c in cs)
        xs = sorted(c[2] for c in cs if c[0] == cw) or [cs[0][2]]
        x = xs[len(xs)//2]
        # widest content box across samples (dark scenes only ever make the box SMALLER, so max is safe)
        pillar = (w/h) > 1.6 and ch >= 0.96*h and cw <= 0.86*w and abs((cw/ch) - 4/3) < 0.045
        letter = (w/h) > 1.2 and cw >= 0.96*w and ch <= 0.86*h
        return p, {"w": w, "h": h, "crop": (cw, ch, x), "pillar": pillar, "letter": letter}
    except Exception as e:
        return p, None

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-show", type=int, default=0)
    ap.add_argument("--shows", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    files = []
    for r in ("/mnt/vault/tv", "/mnt/vault/movies"):
        for dp, dn, fn in os.walk(r):
            for f in sorted(fn):
                if f.lower().endswith((".mkv", ".mp4")) and ".converting." not in f and not f.startswith("._"):
                    files.append(os.path.join(dp, f))
    show_of = lambda p: p.split("/")[4]
    if a.shows: keep = set(s.strip() for s in a.shows.split(",")); files = [f for f in files if show_of(f) in keep]
    if a.per_show:
        by = collections.defaultdict(list)
        for f in files: by[show_of(f)].append(f)
        files = [f for s, fl in by.items() for f in fl[:: max(1, len(fl)//a.per_show)][:a.per_show]]
    res = []
    with ThreadPoolExecutor(3) as ex:
        for p, r in ex.map(analyse, files): res.append((p, r))
    tab = collections.defaultdict(lambda: collections.Counter())
    for p, r in res:
        s = show_of(p)
        tab[s]["unreadable" if r is None else ("PILLARBOX" if r["pillar"] else ("letterbox" if r["letter"] else "clean"))] += 1
    print(f"analysed {len(res)} files")
    for s in sorted(tab): print(f"  {s[:44]:44} {dict(tab[s])}")
    if a.out: json.dump({p: r for p, r in res}, open(a.out, "w"))
