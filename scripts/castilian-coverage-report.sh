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
set -uo pipefail

LIBRARY_TV_ROOT="${CASTILIAN_TV_ROOT:-/mnt/vault/tv}"
LIBRARY_MOVIES_ROOT="${CASTILIAN_MOVIES_ROOT:-/mnt/vault/movies}"
OUT="${1:-/dev/stdout}"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/castilian-patterns.sh"

for cmd in mkvmerge jq; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "castilian-coverage-report: missing required command: $cmd" >&2; exit 1; }
done

log() { echo "[$(date '+%F %T')] castilian-coverage-report: $*" >&2; }

has_castilian_track() {
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
        [[ -z "$chosen" ]] && return 1
        tracks="$chosen"
    fi
    track_title=$(echo "$tracks" | jq -r '.title')
    check_text="$track_title $(echo "$tracks" | jq -r '.lang')"
    echo "$check_text" | grep -qiE "$LATAM_PATTERN" && return 1
    return 0
}

# Populates the MISSING_* / TOTAL_* globals for one library root.
scan_category() {
    local root="$1"
    local item total with_track name
    while IFS= read -r -d '' item; do
        name="$(basename "$item")"
        total=0
        with_track=0
        while IFS= read -r -d '' f; do
            total=$((total+1))
            has_castilian_track "$f" && with_track=$((with_track+1))
        done < <(find "$item" -type f \( -iname '*.mkv' -o -iname '*.mp4' \) -print0)
        [[ "$total" -eq 0 ]] && continue
        ITEMS_CHECKED=$((ITEMS_CHECKED+1))
        log "checked ($ITEMS_CHECKED): $name ($with_track/$total)"
        if [[ "$with_track" -lt "$total" ]]; then
            MISSING+=("$(printf '%s\t%d\t%d' "$name" "$with_track" "$total")")
        fi
    done < <(find "$root" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)
}

ITEMS_CHECKED=0

log "scanning movies: $LIBRARY_MOVIES_ROOT"
MISSING=()
if [[ -d "$LIBRARY_MOVIES_ROOT" ]]; then
    scan_category "$LIBRARY_MOVIES_ROOT"
fi
MOVIES_MISSING=("${MISSING[@]}")
MOVIES_TOTAL=$(find "$LIBRARY_MOVIES_ROOT" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)

log "scanning shows: $LIBRARY_TV_ROOT"
MISSING=()
if [[ -d "$LIBRARY_TV_ROOT" ]]; then
    scan_category "$LIBRARY_TV_ROOT"
fi
SHOWS_MISSING=("${MISSING[@]}")
SHOWS_TOTAL=$(find "$LIBRARY_TV_ROOT" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)

{
    echo "Castilian (Castellano) Spanish audio coverage report"
    echo "Generated $(date '+%F %T')"
    echo "Library: $LIBRARY_TV_ROOT ($SHOWS_TOTAL shows), $LIBRARY_MOVIES_ROOT ($MOVIES_TOTAL movies)"
    echo ""
    echo "A show/movie is listed if even one file in it is missing a"
    echo "genuinely-Castilian audio track -- \"X/Y\" means X of Y files"
    echo "already have it. Detection is metadata-based (embedded"
    echo "language/title tags), same logic as the rest of this pipeline --"
    echo "it can't listen to a track, only trust what it's tagged as."
    echo ""
    echo "=== MOVIES MISSING CASTILIAN AUDIO (${#MOVIES_MISSING[@]} of $MOVIES_TOTAL) ==="
    if [[ "${#MOVIES_MISSING[@]}" -eq 0 ]]; then
        echo "  (none -- every movie already has it)"
    else
        printf '%s\n' "${MOVIES_MISSING[@]}" | sort -f -t $'\t' -k1,1 | while IFS=$'\t' read -r name with total; do
            if [[ "$with" -eq 0 ]]; then
                echo "  $name"
            else
                echo "  $name -- $with/$total (partial)"
            fi
        done
    fi
    echo ""
    echo "=== SHOWS MISSING/PARTIAL CASTILIAN AUDIO (${#SHOWS_MISSING[@]} of $SHOWS_TOTAL) ==="
    if [[ "${#SHOWS_MISSING[@]}" -eq 0 ]]; then
        echo "  (none -- every show already has it on every episode)"
    else
        printf '%s\n' "${SHOWS_MISSING[@]}" | sort -f -t $'\t' -k1,1 | while IFS=$'\t' read -r name with total; do
            if [[ "$with" -eq 0 ]]; then
                echo "  $name -- 0/$total episodes"
            else
                echo "  $name -- $with/$total episodes (partial)"
            fi
        done
    fi
} > "$OUT"

log "done: ${#MOVIES_MISSING[@]}/$MOVIES_TOTAL movies missing, ${#SHOWS_MISSING[@]}/$SHOWS_TOTAL shows missing/partial"
