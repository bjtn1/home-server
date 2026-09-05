#!/bin/bash
# Reports every show/movie in the library that doesn't yet have Castilian
# ("Castellano") Spanish audio on ALL of its files -- the "what should I
# go hunt down next" list for the drop-scan pipeline. Read-only: never
# touches a library file, just reports.
#
# Usage: castilian-coverage-report.sh [output_file]
#   Defaults to stdout if no output_file given.
#
# Same per-file detection logic as archive-castilian-audio.sh's
# choose_castilian_track() (language=spa present, reject if it matches
# LATAM_PATTERN, refuse to guess if multiple Spanish tracks and none
# clearly Castilian) -- kept as its own copy rather than shared, same
# reasoning as that script's header: sharing detection logic risks
# regressions in already-tested code for a cosmetic win; the pattern
# constants are shared via castilian-patterns.sh so the two can't drift
# apart on what counts as Castilian vs Latino.
#
# Every file missing it live is also cross-checked against
# $CASTILIAN_ARCHIVE_ROOT (mirrors archive-castilian-audio.sh's own path
# layout: <root>/{shows,movies}/<relative path>.mka) -- a library file
# can lose a previously-muxed Castellano track on a Sonarr/Radarr quality
# upgrade or re-import, and if the archive still has it, that's a
# one-command restore (mux-castilian-audio.sh against the archive), not
# something to re-hunt from scratch. Found by a direct question, not
# built in from the start -- worth actually checking rather than
# assuming "missing live" and "needs hunting" are the same thing.
set -uo pipefail

LIBRARY_TV_ROOT="${CASTILIAN_TV_ROOT:-/mnt/vault/tv}"
LIBRARY_MOVIES_ROOT="${CASTILIAN_MOVIES_ROOT:-/mnt/vault/movies}"
ARCHIVE_ROOT="${CASTILIAN_ARCHIVE_ROOT:-/mnt/vault/castilian-audio-tracks}"
OUT="${1:-/dev/stdout}"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/castilian-patterns.sh"

for cmd in mkvmerge jq; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "castilian-coverage-report: missing required command: $cmd" >&2; exit 1; }
done

log() { echo "[$(date '+%F %T')] castilian-coverage-report: $*" >&2; }

# 0 = confirmed Castellano present, 1 = no Spanish track at all (or a
# confirmed Latino one), 2 = ambiguous -- 2+ Spanish tracks, none of them
# title-labeled as Castilian, so which one (if either) is Castellano is
# genuinely unknown rather than absent.
castilian_status() {
    local file="$1"
    local tracks n chosen track_title check_text
    tracks=$(mkvmerge -J "$file" 2>/dev/null | jq -c '
      .tracks[]?
      | select(.type == "audio")
      | select((.properties.language // "" | ascii_downcase) == "spa")
      | {id, lang: (.properties.language // ""), title: (.properties.track_name // "")}
    ' 2>/dev/null)
    [[ -z "$tracks" ]] && return 1
    n=$(echo "$tracks" | wc -l)
    if [[ "$n" -gt 1 ]]; then
        chosen=$(echo "$tracks" | jq -c --arg pat "$CASTILIAN_PATTERN" 'select(.title | test($pat; "i"))' | head -1)
        [[ -z "$chosen" ]] && return 2
        tracks="$chosen"
    fi
    track_title=$(echo "$tracks" | jq -r '.title')
    check_text="$track_title $(echo "$tracks" | jq -r '.lang')"
    echo "$check_text" | grep -qiE "$LATAM_PATTERN" && return 1
    return 0
}

archive_path_for() {
    local file="$1" root="$2" subdir="$3"
    local rel="${file#"$root"/}"
    echo "$ARCHIVE_ROOT/$subdir/${rel%.*}.mka"
}

# Populates the MISSING array for one library root: each entry is
# "name\twith_track\trestorable\tambiguous\ttotal".
scan_category() {
    local root="$1" subdir="$2"
    local item total with_track restorable ambiguous name f status apath
    while IFS= read -r -d '' item; do
        name="$(basename "$item")"
        total=0; with_track=0; restorable=0; ambiguous=0
        while IFS= read -r -d '' f; do
            total=$((total+1))
            castilian_status "$f"
            status=$?
            if [[ "$status" -eq 0 ]]; then
                with_track=$((with_track+1))
            elif [[ "$status" -eq 2 ]]; then
                ambiguous=$((ambiguous+1))
            else
                apath=$(archive_path_for "$f" "$root" "$subdir")
                [[ -f "$apath" ]] && restorable=$((restorable+1))
            fi
        done < <(find "$item" -type f \( -iname '*.mkv' -o -iname '*.mp4' \) -print0)
        [[ "$total" -eq 0 ]] && continue
        ITEMS_CHECKED=$((ITEMS_CHECKED+1))
        log "checked ($ITEMS_CHECKED): $name ($with_track/$total, $restorable restorable, $ambiguous ambiguous)"
        if [[ "$with_track" -lt "$total" ]]; then
            MISSING+=("$(printf '%s\t%d\t%d\t%d\t%d' "$name" "$with_track" "$restorable" "$ambiguous" "$total")")
        fi
    done < <(find "$root" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)
}

format_list() {
    # $1 = singular noun for the "X episodes"/"" suffix (empty for movies)
    local suffix="$1"
    sort -f -t $'\t' -k1,1 | while IFS=$'\t' read -r name with restorable ambiguous total; do
        local missing=$((total - with - restorable - ambiguous))
        local bits=()
        [[ "$restorable" -gt 0 ]] && bits+=("$restorable restorable from archive")
        [[ "$ambiguous" -gt 0 ]] && bits+=("$ambiguous ambiguous")
        [[ "$missing" -gt 0 ]] && bits+=("$missing need hunting")
        local detail=""
        if [[ "${#bits[@]}" -gt 0 ]]; then
            local joined
            joined=$(printf '%s, ' "${bits[@]}")
            joined="${joined%, }"   # drop the trailing separator
            detail=" ($joined)"
        fi
        if [[ -n "$suffix" ]]; then
            echo "  $name -- $with/$total $suffix$detail"
        else
            echo "  $name$detail"
        fi
    done
}

ITEMS_CHECKED=0

log "scanning movies: $LIBRARY_MOVIES_ROOT"
MISSING=()
[[ -d "$LIBRARY_MOVIES_ROOT" ]] && scan_category "$LIBRARY_MOVIES_ROOT" "movies"
MOVIES_MISSING=("${MISSING[@]}")
MOVIES_TOTAL=$(find "$LIBRARY_MOVIES_ROOT" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)

log "scanning shows: $LIBRARY_TV_ROOT"
MISSING=()
[[ -d "$LIBRARY_TV_ROOT" ]] && scan_category "$LIBRARY_TV_ROOT" "shows"
SHOWS_MISSING=("${MISSING[@]}")
SHOWS_TOTAL=$(find "$LIBRARY_TV_ROOT" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)

{
    echo "Castilian (Castellano) Spanish audio coverage report"
    echo "Generated $(date '+%F %T')"
    echo "Library: $LIBRARY_TV_ROOT ($SHOWS_TOTAL shows), $LIBRARY_MOVIES_ROOT ($MOVIES_TOTAL movies)"
    echo "Archive checked: $ARCHIVE_ROOT"
    echo ""
    echo "A show/movie is listed if even one file in it currently lacks a"
    echo "confirmed-Castellano audio track. Three different reasons get"
    echo "distinguished, since they need very different next steps:"
    echo "  - restorable from archive: the track was already extracted to"
    echo "    the archive at some point (this file used to have it, e.g."
    echo "    before a Sonarr/Radarr quality upgrade or re-import removed"
    echo "    it) -- a one-command mux from the archive, no hunting needed."
    echo "  - ambiguous: 2+ Spanish audio tracks present, but none is"
    echo "    title-labeled as the Castilian one -- needs listening +"
    echo "    re-tagging (mkvpropedit), not a fresh download either."
    echo "  - need hunting: no Spanish track at all, and nothing archived --"
    echo "    this is the only case that actually needs a fresh drop."
    echo "Detection is metadata-based (embedded language/title tags) -- it"
    echo "can't listen to a track, only trust what it's tagged as."
    echo ""
    echo "=== MOVIES MISSING CASTILIAN AUDIO (${#MOVIES_MISSING[@]} of $MOVIES_TOTAL) ==="
    if [[ "${#MOVIES_MISSING[@]}" -eq 0 ]]; then
        echo "  (none -- every movie already has it)"
    else
        printf '%s\n' "${MOVIES_MISSING[@]}" | format_list ""
    fi
    echo ""
    echo "=== SHOWS MISSING/PARTIAL CASTILIAN AUDIO (${#SHOWS_MISSING[@]} of $SHOWS_TOTAL) ==="
    if [[ "${#SHOWS_MISSING[@]}" -eq 0 ]]; then
        echo "  (none -- every show already has it on every episode)"
    else
        printf '%s\n' "${SHOWS_MISSING[@]}" | format_list "episodes have it"
    fi
} > "$OUT"

log "done: ${#MOVIES_MISSING[@]}/$MOVIES_TOTAL movies missing, ${#SHOWS_MISSING[@]}/$SHOWS_TOTAL shows missing/partial"
