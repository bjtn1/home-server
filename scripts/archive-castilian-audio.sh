#!/bin/bash
# Pulls just the Castilian ("Castellano") Spanish audio track out of any
# library file that has one, into a standalone archive under
# $ARCHIVE_ROOT/{shows,movies}/, mirroring the library's own folder/file
# layout. Built 2026-09-04 after realizing the expensive part of this whole
# effort is finding a Castilian release at all (the Regular Show job alone
# pulled 163GB over MEGA to add tracks that, once extracted, are only
# ~25MB/episode) -- once a track is safely muxed into a library file, keep
# just the audio forever so it never has to be hunted down again.
#
# Two modes:
#   archive-castilian-audio.sh [--full] [--dry-run]
#       Scans the whole library ($LIBRARY_TV_ROOT + $LIBRARY_MOVIES_ROOT).
#       Not really "one-time" -- safe to re-run anytime as an audit
#       (idempotent, only the first run over the whole library is the
#       expensive one). Flocked so two full scans don't both do the same
#       expensive mkvmerge -J pass over everything at once.
#   archive-castilian-audio.sh <target_dir> [--movie] [--dry-run]
#       Scans just that one show/movie folder -- cheap enough to call
#       after every castilian-queue.sh job with no noticeable cost (see
#       the hook in that script's cmd_run()).
#
# For each video file found, independently checks whether it carries a
# genuinely-Castilian audio track (same two-step logic as
# mux-castilian-audio.sh's own process_pair(): language spa present, reject
# outright if it matches LATAM_PATTERN, refuse to guess if ambiguous) --
# this does NOT share detection code with that script (regression risk to
# already-tested logic for a cosmetic win), but DOES share the pattern
# constants via castilian-patterns.sh so the two can't drift apart on what
# counts as "Castilian" vs "Latino".
#
# If found and not already archived, extracts the track via mkvmerge into
# a small audio-only .mka (preserves language=spa/title=Castellano tags --
# that's what makes the archived file directly usable as-is later, see
# below). Extraction is atomic (temp file + mv) and idempotent (existing
# archive files are never touched or re-derived).
#
# Archive path mirrors the *entire relative path* under whichever library
# root, not a derived "show name" -- some shows may store episodes under
# Season N/ subfolders rather than flat, and deriving "show name" from a
# file's immediate parent directory would get that wrong. Keeping the
# original filename (extension swapped to .mka) means any SxxExx marker
# survives, so mux-castilian-audio.sh's existing episode matching works
# against the archive with zero changes.
#
# ** Using the archive later **
# If a library file ever loses its Castellano track (Sonarr/Radarr quality
# upgrade, re-import), restore it without a fresh MEGA hunt -- the archive
# is just another mux-castilian-audio.sh source:
#   mux-castilian-audio.sh /mnt/vault/castilian-audio-tracks/shows/<Show> /mnt/vault/tv/<Show>
#   mux-castilian-audio.sh /mnt/vault/castilian-audio-tracks/movies/<Movie> /mnt/vault/movies/<Movie> --movie
#
# Needs mkvmerge (mkvtoolnix) and jq. No ffprobe needed -- unlike
# mux-castilian-audio.sh this never compares durations between two files,
# it only ever reads one file at a time.
set -uo pipefail

LIBRARY_TV_ROOT="${CASTILIAN_TV_ROOT:-/mnt/vault/tv}"
LIBRARY_MOVIES_ROOT="${CASTILIAN_MOVIES_ROOT:-/mnt/vault/movies}"
ARCHIVE_ROOT="${CASTILIAN_ARCHIVE_ROOT:-/mnt/vault/castilian-audio-tracks}"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/castilian-patterns.sh"

for cmd in mkvmerge jq; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "archive-castilian-audio: missing required command: $cmd" >&2; exit 1; }
done

log() { echo "archive-castilian-audio: $*"; }

DRY_RUN=0
MOVIE_MODE=0
FULL_MODE=0
TARGET_DIR=""
args=()
for a in "$@"; do
    case "$a" in
        --dry-run) DRY_RUN=1 ;;
        --movie)   MOVIE_MODE=1 ;;
        --full)    FULL_MODE=1 ;;
        *)         args+=("$a") ;;
    esac
done
set -- "${args[@]}"

if [[ $# -eq 0 ]]; then
    FULL_MODE=1
elif [[ $# -eq 1 ]]; then
    TARGET_DIR="$1"
else
    echo "Usage: $0 [--full] [--dry-run]  OR  $0 <target_dir> [--movie] [--dry-run]" >&2
    exit 1
fi

# Same detection logic as mux-castilian-audio.sh's process_pair() steps
# 1-2, minus the "does the target already have one" check (not applicable
# here -- there's no separate target, we're archiving from a file that
# already has the track). Prints one JSON object (the chosen track) on
# success, or nothing if there's no usable Castilian track.
choose_castilian_track() {
    local file="$1"
    local tracks n chosen track_title check_text
    tracks=$(mkvmerge -J "$file" 2>/dev/null | jq -c '
      .tracks[]
      | select(.type == "audio")
      | select((.properties.language // "" | ascii_downcase) == "spa")
      | {id, lang: (.properties.language // ""), title: (.properties.track_name // "")}
    ')
    [[ -z "$tracks" ]] && return 1
    n=$(echo "$tracks" | wc -l)
    if [[ "$n" -gt 1 ]]; then
        chosen=$(echo "$tracks" | jq -c --arg pat "$CASTILIAN_PATTERN" 'select(.title | test($pat; "i"))' | head -1)
        [[ -z "$chosen" ]] && return 1   # ambiguous, none clearly Castilian -- refuse to guess
        tracks="$chosen"
    fi
    track_title=$(echo "$tracks" | jq -r '.title')
    check_text="$track_title $(echo "$tracks" | jq -r '.lang')"
    echo "$check_text" | grep -qiE "$LATAM_PATTERN" && return 1   # looks Latino/neutral -- refuse
    echo "$tracks"
    return 0
}

ARCHIVED=0 PRESENT=0 FAILED=0

# archive_one <file> <dest_mka>
archive_one() {
    local file="$1" dest="$2"
    local chosen track_id tmp

    if [[ -e "$dest" ]]; then
        PRESENT=$((PRESENT+1))
        return
    fi

    chosen=$(choose_castilian_track "$file") || return   # no usable track -- silently skip, not an error

    track_id=$(echo "$chosen" | jq -r '.id')

    if [[ "$DRY_RUN" -eq 1 ]]; then
        log "WOULD ARCHIVE $dest <- track $track_id from $(basename "$file")"
        ARCHIVED=$((ARCHIVED+1))
        return
    fi

    mkdir -p "$(dirname "$dest")"
    tmp="${dest}.tmp.$$"
    if mkvmerge -q -o "$tmp" --audio-tracks "$track_id" --no-video --no-subtitles --no-chapters --no-attachments \
        --language "${track_id}:spa" --track-name "${track_id}:Castellano" "$file"; then
        mv -f -- "$tmp" "$dest"
        log "ARCHIVED $dest"
        ARCHIVED=$((ARCHIVED+1))
    else
        log "FAILED extracting from $(basename "$file")"
        rm -f -- "$tmp"
        FAILED=$((FAILED+1))
    fi
}

scan_root() {
    local root="$1" subdir="$2"
    [[ -d "$root" ]] || { log "root does not exist, skipping: $root"; return; }
    local f rel dest
    while IFS= read -r -d '' f; do
        rel="${f#"$root"/}"
        dest="$ARCHIVE_ROOT/$subdir/${rel%.*}.mka"
        archive_one "$f" "$dest"
    done < <(find "$root" -type f \( -iname '*.mkv' -o -iname '*.mp4' \) -print0)
}

if [[ "$FULL_MODE" -eq 1 ]]; then
    mkdir -p "$ARCHIVE_ROOT"
    exec 9>"$ARCHIVE_ROOT/.scan.lock"
    flock -n 9 || { log "a full scan is already running (lock held), exiting"; exit 0; }
    scan_root "$LIBRARY_TV_ROOT" "shows"
    scan_root "$LIBRARY_MOVIES_ROOT" "movies"
else
    if [[ ! -d "$TARGET_DIR" ]]; then
        echo "archive-castilian-audio: no such directory: $TARGET_DIR" >&2
        exit 1
    fi
    subdir="shows"
    [[ "$MOVIE_MODE" -eq 1 ]] && subdir="movies"
    scan_root "$TARGET_DIR" "$subdir/$(basename -- "$TARGET_DIR")"
fi

log "archived: $ARCHIVED new, $PRESENT already present, $FAILED failed"
[[ "$FAILED" -gt 0 ]] && exit 1
exit 0
