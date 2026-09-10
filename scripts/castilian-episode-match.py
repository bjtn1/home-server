#!/usr/bin/env python3
"""
Spanish-to-English content-verified episode matcher (2026-09-09, rewritten
same night). Given a Spanish-dub source file and a show's target library
directory, finds which physical target file(s) it actually belongs to --
not by trusting its own SxxExx/NxNN number (proven unreliable: regional
dub numbering doesn't track the English broadcast order for at least this
show), and NOT by machine-translating its title and fuzzy-matching (the
first version of this script did that; dropped per explicit direction --
translation is approximate and unverifiable, and turned out to be
unnecessary anyway).

Instead: the Spanish source's own numbering was found to exactly follow
TheTVDB's documented "Alternate DVD Order" for this show (confirmed by
matching titles at every entry, all 4 seasons, against TVDB's public
order pages -- no login or API key needed, see castilian-dvd-order-map.json
for the full story and the actual verified mapping). That map is the
authoritative signal now: Spanish NxNN -> DVD-order episode -> real aired
episode number(s), a deterministic lookup, not a guess. Audio
cross-correlation (reusing castilian-audio-align.py, same as before)
stays as the final confirm-before-trust gate -- a mapping alone is never
enough to call something "confirmed" without a real correlation score
against actual physical-file audio.

A series with no entry in castilian-dvd-order-map.json has no
authoritative mapping available -- this script says so explicitly and
stops, rather than falling back to the just-rejected translation
approach or guessing from the source's own numbering.

Runs under the bare system python3 (no longer needs scripts/.venv --
dropping deep-translator as a dependency was a side effect of dropping
translation entirely; only numpy is needed now, same as
castilian-content-verify.py and castilian-audio-align.py).

Usage:
    python3 castilian-episode-match.py \\
        <source_video> <target_dir> <sonarr_series_id> [--mode tv|movie]

    SONARR_URL / SONARR_KEY read from the environment (same convention
    as castilian-drop-scan.sh).

Prints a JSON object to stdout: one entry per segment parsed from the
source filename (a Courage-style "+"-joined file has two; most shows
would have one), each either "confirmed" (a real physical file, an
episode identity, and the correlation score that proved it) or not --
never a guess. Never touches the library itself; this only proposes,
same as castilian-audio-align.py -- a human approves via castilian
-control's /review-matches before anything real happens.
"""
import argparse
import importlib.util
import json
import os
import re
import sys
import urllib.request

NARROW_SEARCH = 15.0
DVD_ORDER_MAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "castilian-dvd-order-map.json")

SXXEXX_RANGE_RE = re.compile(r"[Ss](\d{1,3})[Ee](\d{1,3})(?:-[Ee](\d{1,3}))?")
NXNN_RE = re.compile(r"^\s*(\d{1,2})[xX](\d{1,3})\b")


def log(*a):
    print("[castilian-episode-match]", *a, file=sys.stderr)


def _load_align():
    spec = importlib.util.spec_from_file_location(
        "castilian_audio_align",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "castilian-audio-align.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_dvd_order_map():
    with open(DVD_ORDER_MAP_PATH) as f:
        return json.load(f)


def parse_episode_range(name):
    """Returns (season, [episode_numbers]) for a target-library-style
    filename -- SxxExx or SxxExx-Eyy (a real bundled-file pattern found
    tonight: one physical file covering two Sonarr-numbered episodes)."""
    m = SXXEXX_RANGE_RE.search(name)
    if not m:
        return None
    season = int(m.group(1))
    eps = [int(m.group(2))]
    if m.group(3):
        eps.append(int(m.group(3)))
    return season, eps


def parse_source_marker(path):
    """Returns (season, dvd_episode_num, [segment_titles]) from a Spanish
    source filename -- 'NxNN Title1 + Title2.mkv' style, marker always at
    the very start (same convention castilian-drop-scan.sh's episode_key()
    already assumes). The NxNN here is the Spanish release's OWN numbering
    -- looked up against castilian-dvd-order-map.json, never trusted
    directly as an aired-episode number."""
    stem = os.path.splitext(os.path.basename(path))[0]
    m = NXNN_RE.match(stem)
    if not m:
        return None
    season, dvd_ep = int(m.group(1)), int(m.group(2))
    rest = stem[m.end():].strip()
    titles = [t.strip() for t in rest.split("+") if t.strip()]
    return season, dvd_ep, titles


def lookup_aired_episodes(dvd_map, series_id, season, dvd_ep):
    """The authoritative lookup: Spanish DVD-order (season, dvd_ep) ->
    real aired episode number(s), straight from the verified map. Returns
    None (not an empty list) when there's no map for this series/season/
    entry at all -- distinct from "mapped to nothing", which shouldn't
    happen but would also be reported explicitly rather than silently
    treated the same as "no map"."""
    series_map = dvd_map.get(str(series_id))
    if not series_map:
        return None
    season_list = series_map.get("seasons", {}).get(str(season))
    if not season_list:
        return None
    for entry_dvd_ep, aired_eps in season_list:
        if entry_dvd_ep == dvd_ep:
            return aired_eps
    return None


def _season_files(target_dir):
    try:
        names = os.listdir(target_dir)
    except FileNotFoundError:
        return []
    return [n for n in names if n.lower().endswith((".mkv", ".mp4"))]


def find_physical_candidates(target_dir, season, episode_num):
    """Exact filename hits only -- a file whose own SxxExx/range literally
    includes episode_num."""
    out = []
    for name in _season_files(target_dir):
        parsed = parse_episode_range(name)
        if parsed and parsed[0] == season and episode_num in parsed[1]:
            out.append(os.path.join(target_dir, name))
    return out


def find_nearby_physical_candidates(target_dir, season, episode_num):
    """Fallback tried whenever the exact-filename candidate(s) don't
    confirm (including when there IS an exact-name file, but it's simply
    wrong -- found live 2026-09-09, see match_segment()'s comment).
    Rather than an arbitrary +/-N episode-number window -- found live the
    same night that the real content can sit further than 3 away from
    its claimed number -- this searches every OTHER file in the same
    season directory, closest-numbered first (cheap: a season is at most
    a few dozen files, not the whole library). Purely a candidate order;
    nothing here is ever trusted without audio correlation actually
    confirming it in match_segment()."""
    scored = []
    for name in _season_files(target_dir):
        parsed = parse_episode_range(name)
        if not parsed or parsed[0] != season:
            continue
        dist = min(abs(e - episode_num) for e in parsed[1])
        if dist > 0:
            scored.append((dist, os.path.join(target_dir, name)))
    scored.sort(key=lambda x: x[0])
    return [path for _, path in scored]


def confirm_correlation(align, source_path, src_idx, target_path, tgt_idx, src_dur, mode,
                         segment_span=(0.0, 1.0)):
    """Same cheap narrow-search-at-a-few-points approach already proven
    tonight in castilian-content-verify.py -- a real positive signal is
    unmistakable (0.7-0.9+) versus a genuine mismatch (consistently
    ~0.02-0.06), no fine threshold-tuning needed.

    segment_span narrows sampling to where THIS segment actually lives in
    a bundled multi-segment source file -- an estimate (segment
    boundaries aren't exactly even), so this samples three points spread
    across the estimated span with a wider offset search, rather than
    trusting the estimate to be precise."""
    # 2026-09-09: coarsened live -- real matches spike unmistakably
    # (0.7-0.9+) even under sparse sampling, and this batch's own numbers
    # showed correlation confirming only 8 of 45 real matches (the rest
    # needed the transcript fallback below) -- an exhaustive delta search
    # here was mostly just paying cost before falling through anyway.
    # This still catches the same-master case fast; the different-master
    # case was never going to be caught by correlation at any sampling
    # density (see confirm_transcript()).
    import numpy as np
    threshold = align.MIN_CORRELATION[mode]
    span_start, span_end = segment_span
    span_dur = (src_dur or 60.0) * (span_end - span_start)
    search = NARROW_SEARCH if segment_span == (0.0, 1.0) else NARROW_SEARCH * 2
    best = 0.0
    for inner_frac in (0.3, 0.7):
        t = (src_dur or 60.0) * span_start + span_dur * inner_frac
        t = max(10.0, t)
        for delta in np.arange(-search, search, 8.0):
            s = align.windowed_correlation(source_path, src_idx, t, target_path, tgt_idx, t + delta, window=15)
            best = max(best, s)
        if best >= threshold:
            return best
    return best


TRANSCRIPT_CONFIRM_THRESHOLD = 0.5   # comfortably between the real 0.95 (genuine
                                      # match, different master) and 0.19 (genuine
                                      # mismatch) observed testing this live
TRANSCRIPT_FALLBACK_TOP_N = 3        # kept as a real cap, not just a historical
                                      # comment -- see match_segment(): every
                                      # candidate now GETS a correlation score first,
                                      # and only if NONE of the top-N confirm does
                                      # this widen further to the rest of the season.
                                      # Found live 2026-09-09 (1x11 case): the top-N
                                      # cutoff alone genuinely missed real matches
                                      # when several wrong files scored deceptively
                                      # close to the true one -- but transcribing
                                      # EVERY season file up front for EVERY segment
                                      # is real, avoidable Whisper cost most segments
                                      # never need (most confirm within the top few).
                                      # transcribe_clip()'s cache means the source
                                      # side is only ever paid once per segment
                                      # regardless of how far this widens.


def confirm_transcript(align, source_path, src_idx, target_path, tgt_idx, src_dur, segment_span=(0.0, 1.0)):
    """Last-resort confirmation when raw audio correlation stays low
    despite a plausible candidate. Found live 2026-09-09: a source and
    target that are genuinely the same episode but from DIFFERENT audio
    masters/mixes (different release group, re-recorded or remixed
    dialogue track) can correlate near-zero forever, no matter the
    offset or tempo tried -- because correlation compares waveform
    shape, not content. The same pair transcribed to near-identical text
    (0.95 similarity) despite ~0.05 correlation. This is deliberately
    NOT the primary signal (correlation is faster and already proven for
    the far more common same-master case) -- only tried after
    correlation has already failed every candidate.

    Tries multiple points within the estimated span, not just one --
    found live 2026-09-09: a fixed single sample point (30% into the
    span) repeatedly landed in the WRONG segment for one half of several
    bundled 2-segment files, because segment_span's 50/50 split is only
    an estimate and real segment boundaries aren't always even. A
    correctly-identified target file was being reported unconfirmed
    purely because the one sample point happened to miss. transcribe_clip()
    caches by (path, t), so re-sampling the SAME source point across
    different candidate targets stays free; only genuinely new points
    cost a fresh Whisper call."""
    span_start, span_end = segment_span
    span_frac = span_end - span_start
    tgt_dur = align.container_duration(target_path) or src_dur or 60.0
    best_sim, best_src_text, best_tgt_text = 0.0, None, None
    for inner_frac in (0.15, 0.35, 0.55, 0.75):
        src_t = max(10.0, (src_dur or 60.0) * (span_start + span_frac * inner_frac))
        tgt_t = max(10.0, tgt_dur * (span_start + span_frac * inner_frac))
        src_text = align.transcribe_clip(source_path, src_idx, src_t)
        if not src_text:
            continue
        tgt_text = align.transcribe_clip(target_path, tgt_idx, tgt_t)
        if not tgt_text:
            continue
        sim = align.transcript_similarity(src_text, tgt_text)
        if sim > best_sim:
            best_sim, best_src_text, best_tgt_text = sim, src_text, tgt_text
        if best_sim >= TRANSCRIPT_CONFIRM_THRESHOLD:
            break
    return best_sim, best_src_text, best_tgt_text


def match_segment(align, source_path, segment_title, season, aired_ep, target_dir, episodes_by_num, mode,
                   segment_span=(0.0, 1.0)):
    result = {
        "segment_title": segment_title, "segment_span": segment_span,
        "authoritative_aired_episode": f"S{season:02d}E{aired_ep:02d}",
        "candidates_tried": [], "confirmed": None,
    }
    ep = episodes_by_num.get((season, aired_ep))
    result["sonarr_title"] = ep["title"] if ep else None

    src_ref = align.find_track(source_path, align.is_english_props)
    if not src_ref:
        result["reason"] = "source has no shared reference (English) track to confirm against -- can't verify"
        return result
    src_dur = align.container_duration(source_path)

    # Always try both tiers (exact-filename first, nearby-numbers second),
    # not just when the exact search comes up empty -- found live
    # 2026-09-09 that an exact-filename hit can still be WRONG content
    # (Sonarr's own file records turned out to be independently
    # cross-wired for this same show, a real filename-vs-content mismatch
    # that has nothing to do with the Spanish dub at all), so "a file
    # named S01E10 exists" is not by itself grounds to stop looking.
    # Nearby candidates are still gated entirely by audio correlation
    # below, never trusted just for being close.
    exact_files = find_physical_candidates(target_dir, season, aired_ep)
    nearby_files = find_nearby_physical_candidates(target_dir, season, aired_ep)
    tiers = [(True, exact_files), (False, nearby_files)]

    any_candidates = False
    tgt_refs = {}  # physical_file -> (mkvmerge_id, ffmpeg_idx), reused below for the transcript fallback
    for is_exact, physical_files in tiers:
        for pf in physical_files:
            any_candidates = True
            tgt_ref = align.find_track(pf, align.is_english_props)
            if not tgt_ref:
                result["candidates_tried"].append({
                    "physical_file": pf, "exact_filename_match": is_exact, "correlation": None,
                    "note": "target file has no shared reference track -- can't confirm",
                })
                continue
            tgt_refs[pf] = tgt_ref
            corr = confirm_correlation(align, source_path, src_ref[1], pf, tgt_ref[1], src_dur, mode, segment_span)
            result["candidates_tried"].append({
                "physical_file": pf, "exact_filename_match": is_exact, "correlation": round(corr, 3),
            })
            if corr >= align.MIN_CORRELATION[mode]:
                result["confirmed"] = {
                    "target": pf, "episode": f"S{season:02d}E{aired_ep:02d}",
                    "title": ep["title"] if ep else None, "correlation": round(corr, 3), "confirmed_via": "correlation",
                    "exact_filename_match": is_exact,
                }
                return result
        if result["confirmed"]:
            break

    if not any_candidates:
        result["reason"] = (f"no video file at all exists in target_dir's season {season} -- "
                             f"can't resolve S{season:02d}E{aired_ep:02d} without any candidates")
        return result

    # Correlation didn't confirm anything -- last resort before giving up:
    # transcript-compare the highest-correlation candidates first (the
    # real answer reliably scored highest even under threshold, found
    # live tonight), and if NONE of those confirm, widen to the rest of
    # the season too -- found live tonight (the 1x11 case) that the
    # top-N-only cutoff genuinely missed real matches when several wrong
    # files scored deceptively close to the true one. transcribe_clip()
    # caches the source-side call, so widening only costs one extra
    # Whisper round-trip (the target side) per additional candidate, not
    # two.
    scored = sorted(
        (c for c in result["candidates_tried"] if c["correlation"] is not None),
        key=lambda c: -c["correlation"])
    tiers = [scored[:TRANSCRIPT_FALLBACK_TOP_N], scored[TRANSCRIPT_FALLBACK_TOP_N:]]
    for tier in tiers:
        for c in tier:
            pf = c["physical_file"]
            tgt_ref = tgt_refs.get(pf)
            if not tgt_ref:
                continue
            sim, src_text, tgt_text = confirm_transcript(align, source_path, src_ref[1], pf, tgt_ref[1], src_dur, segment_span)
            c["transcript_similarity"] = round(sim, 3)
            if sim >= TRANSCRIPT_CONFIRM_THRESHOLD:
                c["transcript_snippets"] = {"source": src_text, "target": tgt_text}
                result["confirmed"] = {
                    "target": pf, "episode": f"S{season:02d}E{aired_ep:02d}",
                    "title": ep["title"] if ep else None, "transcript_similarity": round(sim, 3),
                    "confirmed_via": "transcript", "exact_filename_match": c["exact_filename_match"],
                }
                return result

    result["reason"] = ("none of the candidate physical files (exact-name or nearby) confirmed via "
                         "correlation OR transcript comparison, after checking the entire season -- "
                         "possible mislabeled/corrupt file, or the real content genuinely isn't in "
                         "this library yet; needs a human look")
    return result


def fetch_episodes(sonarr_url, sonarr_key, series_id):
    url = f"{sonarr_url.rstrip('/')}/api/v3/episode?seriesId={series_id}&apikey={sonarr_key}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("target_dir")
    ap.add_argument("series_id", type=int)
    ap.add_argument("--mode", default="tv", choices=["tv", "movie"])
    ap.add_argument("--sonarr-url", default=os.environ.get("SONARR_URL"))
    ap.add_argument("--sonarr-key", default=os.environ.get("SONARR_KEY"))
    args = ap.parse_args()

    if not args.sonarr_url or not args.sonarr_key:
        print(json.dumps({"error": "SONARR_URL/SONARR_KEY not set (env or --sonarr-url/--sonarr-key)"}))
        sys.exit(1)

    align = _load_align()
    dvd_map = load_dvd_order_map()

    try:
        episodes = fetch_episodes(args.sonarr_url, args.sonarr_key, args.series_id)
    except Exception as e:
        print(json.dumps({"error": f"couldn't fetch episode list from Sonarr: {e!r}"}))
        sys.exit(1)
    episodes_by_num = {(e["seasonNumber"], e["episodeNumber"]): e for e in episodes}

    parsed = parse_source_marker(args.source)
    if not parsed:
        print(json.dumps({"error": "couldn't parse a season/episode marker from source filename"}))
        sys.exit(1)
    season, dvd_ep, segments = parsed
    if not segments:
        print(json.dumps({"error": "couldn't parse any segment titles from source filename"}))
        sys.exit(1)

    aired_eps = lookup_aired_episodes(dvd_map, args.series_id, season, dvd_ep)
    if aired_eps is None:
        print(json.dumps({
            "error": f"no authoritative DVD-order mapping available for series {args.series_id} "
                     f"season {season} (or the series has no map at all) -- can't resolve "
                     f"S{season:02d}E{dvd_ep:02d} (Spanish numbering) without guessing",
        }))
        sys.exit(1)

    if len(aired_eps) != len(segments):
        log(f"WARNING: source has {len(segments)} segment title(s) but the authoritative map says "
            f"S{season:02d}E{dvd_ep:02d} covers {len(aired_eps)} aired episode(s) ({aired_eps}) -- "
            f"pairing by position anyway, but this mismatch itself is worth a human look")

    n = len(segments)
    results = []
    for i, seg in enumerate(segments):
        span = (i / n, (i + 1) / n)
        aired_ep = aired_eps[i] if i < len(aired_eps) else aired_eps[-1]
        log(f"matching segment {i+1}/{n} (estimated span {span[0]:.0%}-{span[1]:.0%}): "
            f"{seg!r} -> authoritative S{season:02d}E{aired_ep:02d}")
        results.append(match_segment(align, args.source, seg, season, aired_ep, args.target_dir,
                                      episodes_by_num, args.mode, span))

    print(json.dumps({"source": args.source, "segments": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
