#!/usr/bin/env python3
"""Remove BAKED-IN side bars (a 4:3 picture pillarboxed inside a 16:9 frame) and stretch the picture to fill
the frame, the same policy castilianize applies to declared-4:3 files (crop the bars, widen to 16:9).

Built for Sargento Keroro (640x360 frame, picture at x=79..557). Safety:
  - geometry is MEASURED per file (per-column max luminance over 12 frames); a file whose picture does not
    sit where expected is SKIPPED (UNEXPECTED_GEOMETRY), never guessed at
  - output is verified (duration within 1 s, same audio/subtitle stream counts, bars gone) before the
    original is replaced; any failure leaves the original untouched
  - atomic os.replace(); single-run flock; skips files modified in the last --min-age-minutes
  - idempotent: a file with no bars measures as CLEAN and is skipped, so re-runs are safe
Usage: depillarbox.py [--apply] [--limit N] [--min-age-minutes M] FILE_OR_DIR ...
"""
import argparse, fcntl, os, subprocess, sys, time, json
import numpy as np

CROP = (476, 360, 81, 0)          # w, h, x, y  -> inside the measured picture (x=79..557) with a 2 px safety margin
FRAME = (640, 360)
EXPECT_LEFT = (72, 86)            # allowed measured left-bar width
EXPECT_RIGHT = (76, 90)           # allowed measured right-bar width (picture ends ~557 => right bar ~82)
LOCK = "/tmp/depillarbox.lock"


def measure(path):
    """-> (left_bar, right_bar) in a FRAME-sized coordinate system, or None."""
    w, h = FRAME
    raw = subprocess.run(["nice", "-n", "19", "ffmpeg", "-v", "error", "-ss", "240", "-i", path,
                          "-vf", f"fps=1/25,scale={w}:{h},format=gray", "-frames:v", "12", "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    a = np.frombuffer(raw, np.uint8)
    n = a.size // (w * h)
    if n < 4:
        return None
    col = a[:n * w * h].reshape(n, h, w).max(axis=(0, 1))
    on = np.where(col > 40)[0]
    if on.size == 0:
        return None
    return int(on.min()), int(w - 1 - on.max())


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height", "-of", "json", path],
                       capture_output=True, text=True)
    j = json.loads(r.stdout)
    dur = float(j["format"]["duration"])
    kinds = [s["codec_type"] for s in j["streams"]]
    v = next(s for s in j["streams"] if s["codec_type"] == "video")
    return dur, kinds.count("audio"), kinds.count("subtitle"), (v["width"], v["height"])


def process(path, apply, min_age):
    if ".converting." in path or ".depillar." in path:
        return "SKIP_TEMP"
    if min_age and time.time() - os.path.getmtime(path) < min_age * 60:
        return "SKIPPED_RECENT"
    dur, na, ns, size = probe(path)
    if size != FRAME:
        return f"UNEXPECTED_FRAME {size}"
    m = measure(path)
    if m is None:
        return "MEASURE_FAILED"
    left, right = m
    if left < 6 and right < 6:
        return "CLEAN"                       # no baked-in bars (already fixed / never had them)
    if not (EXPECT_LEFT[0] <= left <= EXPECT_LEFT[1] and EXPECT_RIGHT[0] <= right <= EXPECT_RIGHT[1]):
        return f"UNEXPECTED_GEOMETRY left={left} right={right}"
    if not apply:
        return f"WOULD_FIX left={left} right={right}"
    tmp = path[:-4] + ".depillar.tmp.mkv"
    cw, ch, cx, cy = CROP
    r = subprocess.run(["nice", "-n", "10", "ffmpeg", "-y", "-v", "error", "-i", path,
                        "-map", "0:V", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                        "-vf", f"crop={cw}:{ch}:{cx}:{cy},scale={FRAME[0]}:{FRAME[1]}:flags=lanczos,setsar=1",
                        "-c:a", "copy", "-c:s", "copy", "-c:t", "copy", tmp], capture_output=True, timeout=3 * 3600)
    if r.returncode != 0 or not os.path.exists(tmp):
        if os.path.exists(tmp): os.remove(tmp)
        return "ENCODE_FAILED " + r.stderr.decode(errors="replace")[-120:].replace("\n", " ")
    try:
        d2, na2, ns2, size2 = probe(tmp)
        m2 = measure(tmp)
        ok = (abs(d2 - dur) <= 1.0 and na2 == na and ns2 == ns and size2 == FRAME and m2 is not None and m2[0] < 6 and m2[1] < 6)
        if not ok:
            os.remove(tmp)
            return f"VERIFY_FAILED dur {dur:.1f}->{d2:.1f} audio {na}->{na2} subs {ns}->{ns2} bars-after={m2}"
        os.replace(tmp, path)
        return "FIXED"
    except Exception as e:
        if os.path.exists(tmp): os.remove(tmp)
        return f"VERIFY_ERROR {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-age-minutes", type=float, default=30)
    a = ap.parse_args()
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another depillarbox run is in progress"); return 1
    files = []
    for p in a.paths:
        if os.path.isdir(p):
            for dp, dn, fn in os.walk(p):
                files += [os.path.join(dp, f) for f in sorted(fn) if f.lower().endswith(".mkv") and not f.startswith("._")]
        else:
            files.append(p)
    files.sort()
    if a.limit: files = files[:a.limit]
    tally = {}
    for i, f in enumerate(files, 1):
        st = process(f, a.apply, a.min_age_minutes)
        key = st.split()[0]
        tally[key] = tally.get(key, 0) + 1
        print(f"[{i}/{len(files)}] {st}: {os.path.basename(f)[:70]}", flush=True)
    print("SUMMARY", tally, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
