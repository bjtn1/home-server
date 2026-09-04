#!/bin/bash
# Adds a Castilian ("Castellano"/European) Spanish audio track from a
# separately-downloaded release into the matching file(s) already in the
# library, WITHOUT touching video quality or existing audio tracks -- built
# after finding that a "fully Castilian" release (Gravity Falls, 2026-09-03)
# was actually a lower-resolution WEB rip that would've been a straight
# downgrade (and, for season 1, dropped English audio entirely) if used to
# *replace* the existing 1080p BluRay files. Muxing the one track we
# actually want into what's already on disk keeps everything else intact.
#
# Usage:
#   mux-castilian-audio.sh <source_dir> <target_dir> [--dry-run] [--movie]
#
#   source_dir  Directory of the newly-downloaded release (recursed into).
#   target_dir  The existing library folder for the same show/movie.
#   --dry-run   Report what would happen; touches nothing on disk.
#   --movie     Movie mode: source_dir and target_dir must each contain
#               exactly one video file -- those two are matched directly,
#               no episode-number parsing. Without this flag (TV mode),
#               every file in both dirs must carry a SxxExx or NxNN episode
#               marker in its filename, and files are matched by that.
#
# For each matched pair (episode or movie):
#   1. Skip if the target file already has a Spanish (spa) audio track --
#      it may or may not already be Castilian, but this script only adds,
#      never removes/replaces, so a pre-existing Spanish track (right or
#      wrong dialect) is left for you to sort out manually rather than
#      silently duplicated.
#   2. Skip if the source's Spanish track's language/title matches the
#      Latin-American/neutral pattern (same list as the "Spanish Audio"
#      Sonarr/Radarr custom format's negate clause and
#      arr-audio-lang-check.sh -- kept in sync manually, they're small).
#      Refuses to mux in the exact thing this whole effort is about avoiding.
#   3. Skip if source and target container durations differ by more than
#      DURATION_TOLERANCE seconds -- different cuts/edits between a BluRay
#      rip and a WEB rip would desync the added track from the picture.
#   4. Mux the source's Spanish track into a copy of the target file
#      (video, existing audio/subs, chapters all preserved as-is), verify
#      the result opens and has one more audio track than before, then
#      atomically replace the target file in place -- same filename, so
#      Sonarr/Radarr/Jellyfin never notice anything but the extra track.
#
# Needs mkvmerge/mkvinfo (mkvtoolnix), ffprobe, and jq.
set -uo pipefail

DURATION_TOLERANCE=3   # seconds

# Kept in sync with the "Spanish Audio" custom format's negate-latino regex
# in Sonarr/Radarr and scripts/arr-audio-lang-check.sh (2026-09-03).
LATAM_PATTERN='latino|lat[.-]?am|latin[.-]?america|es[-]?419|mexic|hispanoamerican|neutro|neutral'
CASTILIAN_PATTERN='castellano|castilian|european|espa.a|peninsular|iberian'

DRY_RUN=0
MOVIE_MODE=0
args=()
for a in "$@"; do
    case "$a" in
        --dry-run) DRY_RUN=1 ;;
        --movie)   MOVIE_MODE=1 ;;
        *)         args+=("$a") ;;
    esac
done
set -- "${args[@]}"

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 <source_dir> <target_dir> [--dry-run] [--movie]" >&2
    exit 1
fi
SOURCE_DIR="$1"
TARGET_DIR="$2"

for cmd in mkvmerge ffprobe jq; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "mux-castilian-audio: missing required command: $cmd" >&2; exit 1; }
done

log() { echo "mux-castilian-audio: $*"; }

# Pull "S01E02" or "1x02" style markers out of a filename -> normalized "1-2"
episode_key() {
    local name="$1" s e
    if [[ "$name" =~ [Ss]([0-9]{1,3})[Ee]([0-9]{1,3}) ]]; then
        s="${BASH_REMATCH[1]}"; e="${BASH_REMATCH[2]}"
    elif [[ "$name" =~ ([0-9]{1,2})[xX]([0-9]{1,3}) ]]; then
        s="${BASH_REMATCH[1]}"; e="${BASH_REMATCH[2]}"
    else
        return 1
    fi
    echo "$((10#$s))-$((10#$e))"
}

container_duration() {
    ffprobe -v quiet -print_format json -show_entries format=duration -- "$1" 2>/dev/null | jq -r '.format.duration // empty'
}

# Find the source's Spanish audio track id (mkvmerge track id, not ffprobe
# stream index) plus its language/title, or nothing if none/ambiguous.
find_spanish_track() {
    local file="$1"
    mkvmerge -J "$file" 2>/dev/null | jq -c '
      .tracks[]
      | select(.type == "audio")
      | select((.properties.language // "" | ascii_downcase) == "spa")
      | {id, lang: (.properties.language // ""), title: (.properties.track_name // "")}
    '
}

MUXED=0 SKIPPED=0 FAILED=0

# Runs the full check-then-mux sequence for one (src, tgt) pair, labeled
# for logging. Mutates the MUXED/SKIPPED/FAILED counters directly.
process_pair() {
    local src="$1" tgt="$2" label="$3"
    local existing src_tracks n chosen track_id track_title check_text
    local d_src d_tgt diff out new_audio_count old_audio_count

    # 1. already have a Spanish track in the target?
    existing=$(find_spanish_track "$tgt")
    if [[ -n "$existing" ]]; then
        log "SKIP $label -- target already has a Spanish audio track ($(echo "$existing" | jq -r '.title // .lang' | paste -sd, -))"
        SKIPPED=$((SKIPPED+1))
        return
    fi

    # 2. does the source even have a Spanish track, and is it the dialect we want?
    src_tracks=$(find_spanish_track "$src")
    if [[ -z "$src_tracks" ]]; then
        log "SKIP $label -- source has no Spanish audio track: $(basename "$src")"
        SKIPPED=$((SKIPPED+1))
        return
    fi
    n=$(echo "$src_tracks" | wc -l)
    if [[ "$n" -gt 1 ]]; then
        # prefer one whose title explicitly says Castilian; else bail rather than guess
        chosen=$(echo "$src_tracks" | jq -c --arg pat "$CASTILIAN_PATTERN" 'select(.title | test($pat; "i"))' | head -1)
        if [[ -z "$chosen" ]]; then
            log "SKIP $label -- source has $n Spanish tracks and none is clearly Castilian, refusing to guess: $(echo "$src_tracks" | jq -r '.title // .lang' | paste -sd, -)"
            SKIPPED=$((SKIPPED+1))
            return
        fi
        src_tracks="$chosen"
    fi
    track_id=$(echo "$src_tracks" | jq -r '.id')
    track_title=$(echo "$src_tracks" | jq -r '.title')
    check_text="$track_title $(echo "$src_tracks" | jq -r '.lang')"
    if echo "$check_text" | grep -qiE "$LATAM_PATTERN"; then
        log "SKIP $label -- source's Spanish track looks Latin-American/neutral, not Castilian ('$track_title'), refusing to mux it in"
        SKIPPED=$((SKIPPED+1))
        return
    fi

    # 3. duration sanity check
    d_src=$(container_duration "$src")
    d_tgt=$(container_duration "$tgt")
    if [[ -z "$d_src" || -z "$d_tgt" ]]; then
        log "SKIP $label -- couldn't read duration from one of the files"
        SKIPPED=$((SKIPPED+1))
        return
    fi
    diff=$(awk -v a="$d_src" -v b="$d_tgt" 'BEGIN{d=a-b; if(d<0)d=-d; print d}')
    if awk -v d="$diff" -v t="$DURATION_TOLERANCE" 'BEGIN{exit !(d>t)}'; then
        log "SKIP $label -- duration mismatch (target ${d_tgt}s vs source ${d_src}s, diff ${diff}s > ${DURATION_TOLERANCE}s) -- likely a different cut, would desync"
        SKIPPED=$((SKIPPED+1))
        return
    fi

    if [[ "$DRY_RUN" -eq 1 ]]; then
        log "WOULD MUX $label <- track $track_id '$track_title' from $(basename "$src")"
        MUXED=$((MUXED+1))
        return
    fi

    out="${tgt}.muxtmp.mkv"
    if mkvmerge -q -o "$out" "$tgt" \
        --audio-tracks "$track_id" --no-video --no-subtitles --no-chapters --no-attachments \
        --language "${track_id}:spa" --track-name "${track_id}:Castellano" \
        "$src"; then
        new_audio_count=$(mkvmerge -J "$out" 2>/dev/null | jq '[.tracks[] | select(.type=="audio")] | length')
        old_audio_count=$(mkvmerge -J "$tgt" 2>/dev/null | jq '[.tracks[] | select(.type=="audio")] | length')
        if [[ "$new_audio_count" -eq $((old_audio_count + 1)) ]]; then
            mv -f -- "$out" "$tgt"
            log "MUXED $label -- added Castellano track (now $new_audio_count audio tracks)"
            MUXED=$((MUXED+1))
        else
            log "FAILED $label -- output has $new_audio_count audio tracks, expected $((old_audio_count + 1)); left target untouched, output at $out for inspection"
            FAILED=$((FAILED+1))
        fi
    else
        log "FAILED $label -- mkvmerge exited non-zero; left target untouched"
        rm -f -- "$out"
        FAILED=$((FAILED+1))
    fi
}

if [[ "$MOVIE_MODE" -eq 1 ]]; then
    mapfile -d '' -t src_files < <(find "$SOURCE_DIR" -type f \( -iname '*.mkv' -o -iname '*.mp4' \) -print0)
    mapfile -d '' -t tgt_files < <(find "$TARGET_DIR" -type f \( -iname '*.mkv' -o -iname '*.mp4' \) -print0)
    if [[ "${#src_files[@]}" -ne 1 ]]; then
        echo "mux-castilian-audio: --movie requires exactly one video file in source_dir, found ${#src_files[@]}: $SOURCE_DIR" >&2
        exit 1
    fi
    if [[ "${#tgt_files[@]}" -ne 1 ]]; then
        echo "mux-castilian-audio: --movie requires exactly one video file in target_dir, found ${#tgt_files[@]}: $TARGET_DIR" >&2
        exit 1
    fi
    process_pair "${src_files[0]}" "${tgt_files[0]}" "$(basename "${tgt_files[0]}")"
else
    declare -A TARGETS
    while IFS= read -r -d '' f; do
        key=$(episode_key "$(basename "$f")") || continue
        TARGETS["$key"]="$f"
    done < <(find "$TARGET_DIR" -type f \( -iname '*.mkv' -o -iname '*.mp4' \) -print0)

    while IFS= read -r -d '' src; do
        key=$(episode_key "$(basename "$src")") || { log "SKIP (no episode marker): $(basename "$src")"; SKIPPED=$((SKIPPED+1)); continue; }
        tgt="${TARGETS[$key]:-}"
        if [[ -z "$tgt" ]]; then
            log "SKIP S${key/-/E} -- no matching file in target dir: $(basename "$src")"
            SKIPPED=$((SKIPPED+1))
            continue
        fi
        process_pair "$src" "$tgt" "S${key/-/E} ($(basename "$tgt"))"
    done < <(find "$SOURCE_DIR" -type f \( -iname '*.mkv' -o -iname '*.mp4' \) -print0)
fi

log "done: $MUXED muxed, $SKIPPED skipped, $FAILED failed"
[[ "$FAILED" -gt 0 ]] && exit 1
exit 0
