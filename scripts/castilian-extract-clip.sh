#!/bin/bash
# Extracts a short, representative clip from one audio track of a video
# file, downmixed/resampled to mono 16kHz WAV -- whisper.cpp's native
# expected input format (confirmed against the real whisper-server
# instance, 2026-09-08). Pure local ffmpeg work, no network calls -- this
# script never talks to the Whisper server itself, it just prepares what
# gets sent to it.
#
# Usage:
#   castilian-extract-clip.sh <video_file> <audio_relative_index> <output.wav> [duration_sec] [start_pct]
#
#   audio_relative_index  0-based index among AUDIO streams only, i.e.
#                         exactly what ffmpeg's `-map 0:a:N` expects. NOT
#                         mkvmerge's `id` field -- the two tools don't
#                         share a numbering scheme, and asserting they
#                         always line up is exactly the kind of unverified
#                         cross-tool assumption this whole project has
#                         been finding bugs in all week. The caller (which
#                         already iterates mkvmerge's audio-track list to
#                         pick a track) computes this trivially: it's just
#                         that track's position among audio-only entries
#                         in the same list, counting from 0.
#   duration_sec          clip length, default 90.
#   start_pct             where to start, as a fraction of total runtime,
#                         default 0.30. Not 0: the opening theme song is
#                         often identical (or near-identical, same backing
#                         track) across dubs of completely different
#                         dialects, which would starve the transcript of
#                         any real dialogue to find markers in. Not too
#                         close to 1.0 either, to stay clear of end credits
#                         (frequently a song, not dialogue, in dubbed
#                         children's TV -- exactly this project's library).
#
# Exits 0 with the clip written on success. Exits 1 (nothing written) if
# duration can't be read, or the file is too short for the requested
# clip+offset -- caller should treat that as "couldn't get a sample",
# same as any other "can't tell" case elsewhere in this pipeline, not as
# a reason to guess.
set -uo pipefail

for cmd in ffmpeg ffprobe; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "castilian-extract-clip: missing required command: $cmd" >&2; exit 1; }
done

if [[ $# -lt 3 ]]; then
    echo "Usage: $0 <video_file> <audio_relative_index> <output.wav> [duration_sec] [start_pct]" >&2
    exit 1
fi
VIDEO="$1"
AUDIO_IDX="$2"
OUT="$3"
# 2026-09-08: was 90 -- found live, across 4 real test files (2 confirmed
# Castilian, 2 confirmed Latino), a single 90s window came back with zero
# dialect markers 4/4 times, not because detection was broken but because
# kids'-cartoon dialogue in any given 90s slice often just doesn't happen
# to use vosotros grammar or lexicon words (short exclamations, simple
# sentences). A 400-600s sample from the same files did contain real
# markers. Longer now that castilian-whisper-check.sh's hallucination-loop
# guard exists as a backstop -- before that guard, a longer clip meant
# more exposure to repetition-loop corruption with no way to catch it.
CLIP_DUR="${4:-300}"
START_PCT="${5:-0.30}"

log() { echo "[$(date '+%F %T')] castilian-extract-clip: $*" >&2; }

if [[ ! -f "$VIDEO" ]]; then
    log "ERROR: no such file: $VIDEO"
    exit 1
fi

total_dur=$(ffprobe -v quiet -print_format json -show_entries format=duration -- "$VIDEO" 2>/dev/null | jq -r '.format.duration // empty')
if [[ -z "$total_dur" ]]; then
    log "ERROR: couldn't read duration: $VIDEO"
    exit 1
fi

start=$(awk -v d="$total_dur" -v p="$START_PCT" 'BEGIN{printf "%.2f", d*p}')
# If the file's too short for start+clip to fit, pull back to start=0 and
# cap the clip at whatever's actually available rather than failing --
# short files (a cold open, a short) shouldn't be unconditionally refused.
end_needed=$(awk -v s="$start" -v c="$CLIP_DUR" 'BEGIN{print s+c}')
fits=$(awk -v e="$end_needed" -v d="$total_dur" 'BEGIN{print (e<=d) ? 1 : 0}')
if [[ "$fits" -ne 1 ]]; then
    start=0
    CLIP_DUR=$(awk -v d="$total_dur" -v c="$CLIP_DUR" 'BEGIN{print (d<c)?d:c}')
fi
# Too short to yield anything meaningful to transcribe -- refuse rather
# than send a near-empty clip that could look "inconclusive" for the
# wrong reason (no audio to judge) instead of "genuinely ambiguous".
min_usable=$(awk -v c="$CLIP_DUR" 'BEGIN{print (c<5) ? 1 : 0}')
if [[ "$min_usable" -eq 1 ]]; then
    log "ERROR: file too short for a usable clip ($total_dur s total): $VIDEO"
    exit 1
fi

mkdir -p "$(dirname -- "$OUT")"
# -nostdin: without it, ffmpeg reads/interacts with whatever stdin its
# caller has -- normally invisible, but found live: a batch loop shaped
# `find ... -print0 | while read -r -d '' f; do ... this script ...; done`
# feeds filenames to the while-read loop over that SAME stdin, and ffmpeg
# (run as a descendant inside the loop body, with no redirection of its
# own) competes with the loop for bytes on it. Result: the loop silently
# lost roughly every other filename mid-batch -- not a metadata problem
# with those files at all (confirmed: manually re-running the exact same
# check against a "skipped" file worked fine outside the loop). This is a
# well-documented ffmpeg-in-a-shell-loop gotcha; -nostdin is the standard
# fix (ffmpeg won't touch stdin at all, so it can never race a caller for it).
if ffmpeg -y -nostdin -v error -ss "$start" -i "$VIDEO" -t "$CLIP_DUR" \
    -map "0:a:${AUDIO_IDX}" -vn -ac 1 -ar 16000 -sample_fmt s16 \
    "$OUT" 2>&1; then
    if [[ -s "$OUT" ]]; then
        log "OK: ${CLIP_DUR}s clip from ${start}s -> $OUT"
        exit 0
    fi
    log "ERROR: ffmpeg reported success but output is empty/missing: $OUT"
    exit 1
else
    log "ERROR: ffmpeg failed extracting audio track $AUDIO_IDX from $VIDEO"
    rm -f -- "$OUT"
    exit 1
fi
