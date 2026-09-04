#!/bin/bash
# Drop-folder auto-pipeline for Castilian Spanish audio, 2026-09-04.
#
# The rest of this pipeline (castilian-queue.sh add -> mux-castilian-audio.sh
# -> archive-castilian-audio.sh) requires you to explicitly name the target
# library file for every source -- auto-matching a Spanish release name to
# the right English library title was deliberately left out of that design,
# since a wrong guess means muxing the wrong dialect into the wrong show's
# files (see castilian-queue.sh's own header).
#
# This script closes that gap for the common case, safely: drop a file (or
# a pile of them) into $CASTILIAN_DROP_DIR and it works out, on its own,
# whether each one is genuinely Castilian, and which library item it
# belongs to -- using Sonarr's/Radarr's own /lookup endpoints (live
# TVDB/TMDB search) rather than fuzzy string-guessing. A lookup result only
# ever counts as a match if it cross-references something you ALREADY OWN
# (by tvdbId/tmdbId, never by title text) -- anything that doesn't resolve
# to something owned is left alone, never guessed at. Once matched, this
# hands off to castilian-queue.sh (the existing, already-tested pipeline)
# for everything after that.
#
# Two passes:
#   Pass 1 -- for every video file under $CASTILIAN_DROP_DIR: detect
#     whether it carries a genuinely-Castilian audio track (mirrors
#     archive-castilian-audio.sh's choose_castilian_track(), single file,
#     no target to compare against yet), and bucket it:
#       - unreadable/corrupt          -> left in place, logged as an error
#       - readable, not Castilian     -> moved to drop-rejected/
#       - genuinely Castilian         -> track extracted to a staging .mka,
#                                        classified TV (SxxExx/NxNN marker
#                                        present) or "type-ambiguous" (no
#                                        marker -- NOT assumed to be a
#                                        movie; resolved in pass 2)
#   Pass 2 -- dedupe titles, look each up in Sonarr (TV) and/or Radarr
#     (type-ambiguous queries both, independently, no short-circuit
#     ordering), cross-check EVERY result (not just the top one) against
#     the owned library, and only act on a confident match: exactly one
#     distinct owned title among all results. Confident matches get queued
#     via castilian-queue.sh add (grouped by resolved target path for TV,
#     never grouped for movies); anything else is left untouched in
#     $CASTILIAN_DROP_DIR for the next scan, and reported once via ntfy.
#
# Flocked ($CASTILIAN_DROP_DIR/.scan.lock, non-blocking) so a double-click
# on the "Scan Drop Folder" button can't race a second scan over the same
# files -- same pattern as archive-castilian-audio.sh's full-scan lock.
#
# Recovering from a wrong quarantine: move the file from drop-rejected/
# back into $CASTILIAN_DROP_DIR and re-scan -- quarantining never marks the
# file itself, just relocates it, so it reprocesses normally.
#
# Env vars (all optional except the Sonarr/Radarr ones, which just mean
# every drop-scan job silently ends up unmatched if left unset):
#   CASTILIAN_DROP_DIR      default /mnt/vault/mega-staging/drop-zone
#   CASTILIAN_TV_ROOT       default /mnt/vault/tv       (real path a
#                            Sonarr series' container-internal /tv/... path
#                            translates to)
#   CASTILIAN_MOVIES_ROOT   default /mnt/vault/movies   (same, for Radarr's
#                            /movies/... and /movies-archive/...)
#   CASTILIAN_QUEUE_DIR     passed straight through to castilian-queue.sh
#   SONARR_URL / SONARR_KEY  e.g. http://sonarr:8989 / <api key>
#   RADARR_URL / RADARR_KEY  e.g. http://radarr:7878 / <api key>
#
# Needs mkvmerge (mkvtoolnix), jq, and curl.
set -uo pipefail

DROP_DIR="${CASTILIAN_DROP_DIR:-/mnt/vault/mega-staging/drop-zone}"
BASE_DIR="$(dirname -- "$DROP_DIR")"
REJECTED_DIR="$BASE_DIR/drop-rejected"
PROCESSED_DIR="$BASE_DIR/drop-processed"
STAGE_ROOT="$BASE_DIR/drop-staging"
TV_ROOT="${CASTILIAN_TV_ROOT:-/mnt/vault/tv}"
MOVIES_ROOT="${CASTILIAN_MOVIES_ROOT:-/mnt/vault/movies}"
NTFY_URL="https://ntfy.bjtn.xyz/homelab-alerts"
QUEUE_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/castilian-queue.sh"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/castilian-patterns.sh"

for cmd in mkvmerge jq curl; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "castilian-drop-scan: missing required command: $cmd" >&2; exit 1; }
done

mkdir -p "$DROP_DIR" "$REJECTED_DIR" "$PROCESSED_DIR" "$STAGE_ROOT"
exec 9>"$DROP_DIR/.scan.lock"
flock -n 9 || { echo "castilian-drop-scan: a scan is already running (lock held), exiting"; exit 0; }

log() { echo "[$(date '+%F %T')] castilian-drop-scan: $*"; }

RUN_ID="$(date '+%Y%m%d-%H%M%S')-$$"
RUN_STAGE="$STAGE_ROOT/$RUN_ID"
EXTRACT_DIR="$RUN_STAGE/extracted"
mkdir -p "$EXTRACT_DIR"
MANIFEST="$RUN_STAGE/.manifest.tsv"
: > "$MANIFEST"
TV_MATCHES="$RUN_STAGE/.tv-matches.tsv"
: > "$TV_MATCHES"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Moves $2 to a path under $1 that mirrors $3 (relative path), suffixing
# with the source's own mtime+size if the destination already exists rather
# than silently overwriting an earlier rejection/processed file.
move_aside() {
    local base="$1" src="$2" rel="$3"
    local dest="$base/$rel"
    mkdir -p "$(dirname -- "$dest")"
    if [[ -e "$dest" ]]; then
        local mtime size ext stem
        mtime=$(stat -c %Y -- "$src" 2>/dev/null || echo 0)
        size=$(stat -c %s -- "$src" 2>/dev/null || echo 0)
        if [[ "$dest" == *.* ]]; then
            ext="${dest##*.}"; stem="${dest%.*}"
            dest="${stem}.${mtime}-${size}.${ext}"
        else
            dest="${dest}.${mtime}-${size}"
        fi
    fi
    mv -- "$src" "$dest"
    echo "$dest"
}

# Strips extension, dots/underscores, bracketed/parenthesized release-group
# and quality tags, and common unbracketed quality/source/codec tokens --
# nothing upstream (episode_key() included) does any of this today, and a
# dirty search term risks a real Castilian file landing in the no-match
# bucket not because it's unowned, but because Sonarr/Radarr's lookup never
# saw a clean enough title to resolve.
clean_title() {
    local name="${1%.*}"
    name=$(sed -E 's/\[[^]]*\]//g; s/\([^)]*\)//g' <<<"$name")
    name=$(tr '._' '  ' <<<"$name")
    name=$(sed -E 's/\b(1080p|720p|480p|2160p|4k|8k|web-?dl|webrip|web|bluray|bdrip|brrip|hdrip|dvdrip|hdtv|hdtc|x264|x265|hevc|avc|aac|ac3|dts|remux|hdr|10bit|8bit)\b//Ig' <<<"$name")
    name=$(sed -E 's/[[:space:]]+/ /g; s/^[[:space:]]+//; s/[[:space:]]+$//' <<<"$name")
    echo "$name"
}

# Season/episode marker, copied from mux-castilian-audio.sh's episode_key()
# so the two can't drift apart on what counts as a marker.
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

# Prints the part of $1 before the season/episode marker (same regex order
# as episode_key(), so the two never disagree about where the marker is),
# or fails if there's no marker.
tv_title_prefix() {
    local name="$1"
    if [[ "$name" =~ [Ss][0-9]{1,3}[Ee][0-9]{1,3} ]]; then
        echo "${name%%"${BASH_REMATCH[0]}"*}"
        return 0
    elif [[ "$name" =~ [0-9]{1,2}[xX][0-9]{1,3} ]]; then
        echo "${name%%"${BASH_REMATCH[0]}"*}"
        return 0
    fi
    return 1
}

# Same detection logic as archive-castilian-audio.sh's choose_castilian_track()
# (mkvmerge -J, spa-language audio, prefer a track whose title clearly says
# Castilian if there's more than one, refuse to guess if ambiguous, reject
# outright if it looks Latin-American/neutral) -- kept as its own copy
# rather than sourced, same reasoning as that script's header: sharing
# detection *logic* across scripts risks regressions in already-tested code
# for a cosmetic win, sharing the two pattern constants (already done via
# castilian-patterns.sh) does not.
#
# Prints the chosen track's JSON on success. Returns 2 if the file itself
# couldn't be read (corrupt/damaged -- distinct from "readable, no usable
# Castilian track", which is 1) so the caller never conflates "unknown
# dialect" with "confirmed not Castilian".
choose_castilian_track() {
    local file="$1"
    local raw tracks n chosen track_title check_text
    raw=$(mkvmerge -J "$file" 2>/dev/null) || return 2
    jq -e '.tracks' >/dev/null 2>&1 <<<"$raw" || return 2
    tracks=$(jq -c '
      .tracks[]
      | select(.type == "audio")
      | select((.properties.language // "" | ascii_downcase) == "spa")
      | {id, lang: (.properties.language // ""), title: (.properties.track_name // "")}
    ' <<<"$raw")
    [[ -z "$tracks" ]] && return 1
    n=$(wc -l <<<"$tracks")
    if [[ "$n" -gt 1 ]]; then
        chosen=$(jq -c --arg pat "$CASTILIAN_PATTERN" 'select(.title | test($pat; "i"))' <<<"$tracks" | head -1)
        [[ -z "$chosen" ]] && return 1
        tracks="$chosen"
    fi
    track_title=$(jq -r '.title' <<<"$tracks")
    check_text="$track_title $(jq -r '.lang' <<<"$tracks")"
    grep -qiE "$LATAM_PATTERN" <<<"$check_text" && return 1
    echo "$tracks"
    return 0
}

# ---------------------------------------------------------------------------
# Pass 1 -- detect, extract, bucket
# ---------------------------------------------------------------------------

CORRUPT_FILES=()
processed=0 rejected=0 extracted=0

while IFS= read -r -d '' file; do
    rel="${file#"$DROP_DIR"/}"
    log "processing: $file"

    chosen=$(choose_castilian_track "$file")
    status=$?

    if [[ "$status" -eq 2 ]]; then
        log "ERROR (unreadable/corrupt, left in place): $file"
        CORRUPT_FILES+=("$rel")
        continue
    fi
    if [[ "$status" -ne 0 ]]; then
        dest=$(move_aside "$REJECTED_DIR" "$file" "$rel")
        log "REJECTED (no genuinely Castilian track) -> $dest"
        rejected=$((rejected+1))
        continue
    fi

    track_id=$(jq -r '.id' <<<"$chosen")
    base="$(basename -- "$file")"
    dest_mka="$EXTRACT_DIR/${rel%.*}.mka"
    mkdir -p "$(dirname -- "$dest_mka")"
    tmp="${dest_mka}.tmp.$$"
    if ! mkvmerge -q -o "$tmp" --audio-tracks "$track_id" --no-video --no-subtitles --no-chapters --no-attachments \
        --language "${track_id}:spa" --track-name "${track_id}:Castellano" "$file"; then
        log "ERROR (extraction failed, left in place): $file"
        rm -f -- "$tmp"
        CORRUPT_FILES+=("$rel (extraction failed)")
        continue
    fi
    mv -f -- "$tmp" "$dest_mka"

    if prefix=$(tv_title_prefix "$base"); then
        title=$(clean_title "$prefix")
        ekey=$(episode_key "$base") || ekey="-"
        printf '%s\t%s\t%s\t%s\t%s\n' "$dest_mka" "$file" "tv" "$title" "$ekey" >> "$MANIFEST"
    else
        title=$(clean_title "$base")
        printf '%s\t%s\t%s\t%s\t%s\n' "$dest_mka" "$file" "ambiguous" "$title" "-" >> "$MANIFEST"
    fi
    extracted=$((extracted+1))
done < <(find "$DROP_DIR" -type f \( -iname '*.mkv' -o -iname '*.mp4' \) -print0)

log "pass 1 done: $extracted extracted, $rejected rejected, ${#CORRUPT_FILES[@]} unreadable/failed"

# ---------------------------------------------------------------------------
# Pass 2 -- dedupe, look up, group, hand off
# ---------------------------------------------------------------------------

UNMATCHED_TITLES=()
queued_tv=0 queued_movie=0

if [[ ! -s "$MANIFEST" ]]; then
    log "pass 2: nothing to match"
else
    HAVE_SONARR=1
    [[ -z "${SONARR_URL:-}" || -z "${SONARR_KEY:-}" ]] && HAVE_SONARR=0
    HAVE_RADARR=1
    [[ -z "${RADARR_URL:-}" || -z "${RADARR_KEY:-}" ]] && HAVE_RADARR=0
    [[ "$HAVE_SONARR" -eq 0 ]] && log "SONARR_URL/SONARR_KEY not set -- every TV title this run falls through to no-match"
    [[ "$HAVE_RADARR" -eq 0 ]] && log "RADARR_URL/RADARR_KEY not set -- every ambiguous title's movie side this run falls through to no-match"

    # Owned library, fetched once for the whole run rather than once per
    # title, and written to a file rather than kept as a shell variable --
    # a real library is easily hundreds of KB of JSON (this one's is
    # ~400KB), well past what `jq --argjson` can take as a command-line
    # argument (confirmed live: "Argument list too long", and since that
    # failure was only ever going to be caught by a broad `|| echo "[]"`,
    # it would have silently turned every single real match into a
    # no-match -- found by an end-to-end run against the real library, not
    # by inspection). `--slurpfile` reads from a file instead, no such
    # limit. A fetch failure here still degrades gracefully (never a hard
    # abort) -- everything this run just falls through to no-match
    # uniformly, same "can't tell, don't block/guess" pattern as
    # arr-control's already_running().
    owned_series_file="$RUN_STAGE/.owned-series.json"
    owned_movies_file="$RUN_STAGE/.owned-movies.json"
    echo "[]" > "$owned_series_file"
    echo "[]" > "$owned_movies_file"
    if [[ "$HAVE_SONARR" -eq 1 ]]; then
        if ! curl -fsS -m 30 -H "X-Api-Key: $SONARR_KEY" "$SONARR_URL/api/v3/series" > "$owned_series_file" 2>/dev/null; then
            log "ERROR: could not fetch owned series from Sonarr -- TV matching disabled for this run"
            echo "[]" > "$owned_series_file"; HAVE_SONARR=0
        fi
    fi
    if [[ "$HAVE_RADARR" -eq 1 ]]; then
        if ! curl -fsS -m 30 -H "X-Api-Key: $RADARR_KEY" "$RADARR_URL/api/v3/movie" > "$owned_movies_file" 2>/dev/null; then
            log "ERROR: could not fetch owned movies from Radarr -- movie matching disabled for this run"
            echo "[]" > "$owned_movies_file"; HAVE_RADARR=0
        fi
    fi

    sonarr_lookup() { curl -fsS -m 30 -G --data-urlencode "term=$1" -H "X-Api-Key: $SONARR_KEY" "$SONARR_URL/api/v3/series/lookup" 2>/dev/null; }
    radarr_lookup() { curl -fsS -m 30 -G --data-urlencode "term=$1" -H "X-Api-Key: $RADARR_KEY" "$RADARR_URL/api/v3/movie/lookup" 2>/dev/null; }

    # Distinct owned entries (by internal id) whose $3 (tvdbId/tmdbId) is
    # referenced anywhere in $1 (a lookup result array), cross-checked
    # against $2 (a file holding the owned array -- see above). Prints a
    # JSON array; length 1 is what "confident" means -- 0 is a clean miss,
    # >1 is ambiguous, both treated as no-match by the caller.
    distinct_owned_matches() {
        local lookup_json="$1" owned_file="$2" field="$3"
        [[ -z "$lookup_json" ]] && { echo "[]"; return; }
        jq -c --slurpfile owned "$owned_file" --arg f "$field" '
          ([ .[] | .[$f] as $v | select($v != null) | $owned[0][] | select(.[$f] == $v) ]
           | unique_by(.id)) // []
        ' <<<"$lookup_json" 2>/dev/null || echo "[]"
    }

    # Container-internal Sonarr/Radarr path -> real host path.
    translate_path() {
        local p="$1"
        case "$p" in
            /tv/*)             echo "${TV_ROOT%/}/${p#/tv/}" ;;
            /movies-archive/*) echo "${MOVIES_ROOT%/}/${p#/movies-archive/}" ;;
            /movies/*)         echo "${MOVIES_ROOT%/}/${p#/movies/}" ;;
            *)                 echo "$p" ;;  # unrecognized prefix -- pass through, add_target existence check below still guards it
        esac
    }

    distinct_titles=$(cut -f4 "$MANIFEST" | tr '[:upper:]' '[:lower:]' | sort -u)
    while IFS= read -r title_lower; do
        [[ -z "$title_lower" ]] && continue
        group=$(awk -F'\t' -v t="$title_lower" 'tolower($4)==t' "$MANIFEST")
        group_type="ambiguous"
        grep -qP '\ttv\t' <<<"$group" && group_type="tv"
        term=$(cut -f4 <<<"$group" | head -1)

        series_match="" movie_match=""

        if [[ "$group_type" == "tv" || "$group_type" == "ambiguous" ]] && [[ "$HAVE_SONARR" -eq 1 ]]; then
            lookup=$(sonarr_lookup "$term")
            if [[ -z "$lookup" ]]; then
                log "WARN: Sonarr lookup failed/unreachable for '$term'"
            else
                matches=$(distinct_owned_matches "$lookup" "$owned_series_file" "tvdbId")
                n=$(jq 'length' <<<"$matches" 2>/dev/null || echo 0)
                if [[ "$n" -eq 1 ]]; then
                    series_match=$(jq -c '.[0]' <<<"$matches")
                elif [[ "$n" -gt 1 ]]; then
                    log "AMBIGUOUS: '$term' cross-references $n different owned series -- refusing to guess"
                fi
            fi
        fi

        if [[ "$group_type" == "ambiguous" ]] && [[ "$HAVE_RADARR" -eq 1 ]]; then
            lookup=$(radarr_lookup "$term")
            if [[ -z "$lookup" ]]; then
                log "WARN: Radarr lookup failed/unreachable for '$term'"
            else
                matches=$(distinct_owned_matches "$lookup" "$owned_movies_file" "tmdbId")
                n=$(jq 'length' <<<"$matches" 2>/dev/null || echo 0)
                if [[ "$n" -eq 1 ]]; then
                    movie_match=$(jq -c '.[0]' <<<"$matches")
                elif [[ "$n" -gt 1 ]]; then
                    log "AMBIGUOUS: '$term' cross-references $n different owned movies -- refusing to guess"
                fi
            fi
        fi

        resolved="" resolved_kind=""
        if [[ "$group_type" == "tv" ]]; then
            [[ -n "$series_match" ]] && { resolved="$series_match"; resolved_kind="tv"; }
        else
            if [[ -n "$series_match" && -n "$movie_match" ]]; then
                log "AMBIGUOUS: '$term' resolves confidently to both an owned series AND an owned movie -- refusing to guess, treating as unmatched"
            elif [[ -n "$series_match" ]]; then
                resolved="$series_match"; resolved_kind="tv"
            elif [[ -n "$movie_match" ]]; then
                resolved="$movie_match"; resolved_kind="movie"
            fi
        fi

        if [[ -z "$resolved" ]]; then
            log "NO MATCH: '$term' ($group_type) -- $(wc -l <<<"$group") file(s) left in place"
            UNMATCHED_TITLES+=("$term")
            while IFS=$'\t' read -r mka orig type ttitle ekey; do
                [[ -z "$mka" ]] && continue
                rm -f -- "$mka"
            done <<<"$group"
            continue
        fi

        target=$(translate_path "$(jq -r '.path' <<<"$resolved")")
        if [[ ! -d "$target" ]]; then
            log "NO MATCH: '$term' resolved to $target, which doesn't exist on disk -- treating as unmatched rather than failing loudly (e.g. tracked but no files imported yet)"
            UNMATCHED_TITLES+=("$term (target dir missing: $target)")
            while IFS=$'\t' read -r mka orig type ttitle ekey; do
                [[ -z "$mka" ]] && continue
                rm -f -- "$mka"
            done <<<"$group"
            continue
        fi

        if [[ "$resolved_kind" == "tv" ]]; then
            log "MATCHED (TV): '$term' -> $target"
            while IFS=$'\t' read -r mka orig type ttitle ekey; do
                [[ -z "$mka" ]] && continue
                printf '%s\t%s\t%s\n' "$target" "$mka" "$orig" >> "$TV_MATCHES"
            done <<<"$group"
        else
            log "MATCHED (movie): '$term' -> $target"
            while IFS=$'\t' read -r mka orig type ttitle ekey; do
                [[ -z "$mka" ]] && continue
                job_dir="$RUN_STAGE/job-movie-$queued_movie"
                mkdir -p "$job_dir"
                mv -- "$mka" "$job_dir/"
                if bash "$QUEUE_SCRIPT" add --movie "$job_dir" "$target" >>/dev/stdout 2>&1; then
                    rel="${orig#"$DROP_DIR"/}"
                    dest=$(move_aside "$PROCESSED_DIR" "$orig" "$rel")
                    log "queued movie job, original moved -> $dest"
                    queued_movie=$((queued_movie+1))
                else
                    log "ERROR: castilian-queue.sh add --movie failed for $job_dir -> $target -- original left in place"
                fi
            done <<<"$group"
        fi
    done <<<"$distinct_titles"

    # Finalize TV grouping by resolved target path (not raw title text) --
    # two differently-spelled titles that both resolved to the same show
    # still end up in one job here.
    if [[ -s "$TV_MATCHES" ]]; then
        distinct_targets=$(cut -f1 "$TV_MATCHES" | sort -u)
        while IFS= read -r target; do
            [[ -z "$target" ]] && continue
            job_dir="$RUN_STAGE/job-tv-$queued_tv"
            mkdir -p "$job_dir"
            rows=$(awk -F'\t' -v t="$target" '$1==t' "$TV_MATCHES")
            originals=()
            while IFS=$'\t' read -r t mka orig; do
                [[ -z "$mka" ]] && continue
                mv -- "$mka" "$job_dir/"
                originals+=("$orig")
            done <<<"$rows"
            if bash "$QUEUE_SCRIPT" add "$job_dir" "$target" >>/dev/stdout 2>&1; then
                for orig in "${originals[@]}"; do
                    rel="${orig#"$DROP_DIR"/}"
                    dest=$(move_aside "$PROCESSED_DIR" "$orig" "$rel")
                    log "queued TV job, original moved -> $dest"
                done
                queued_tv=$((queued_tv+1))
            else
                log "ERROR: castilian-queue.sh add failed for $job_dir -> $target -- originals left in place"
            fi
        done <<<"$distinct_targets"
    fi
fi

# ---------------------------------------------------------------------------
# Notification -- only if there's something to report (matches
# arr-audio-lang-check.sh's only-notify-when-there's-something-to-flag
# convention; a fully clean scan stays silent).
# ---------------------------------------------------------------------------

if [[ "${#CORRUPT_FILES[@]}" -gt 0 || "${#UNMATCHED_TITLES[@]}" -gt 0 ]]; then
    msg=""
    if [[ "${#UNMATCHED_TITLES[@]}" -gt 0 ]]; then
        msg+="Unmatched: $(printf '%s; ' "${UNMATCHED_TITLES[@]}")"$'\n'
    fi
    if [[ "${#CORRUPT_FILES[@]}" -gt 0 ]]; then
        msg+="Unreadable/failed: $(printf '%s; ' "${CORRUPT_FILES[@]}")"
    fi
    log "notifying: $msg"
    curl -fsS -m 10 \
        -H "Title: Castilian drop-scan needs attention" \
        -H "Priority: 3" \
        -H "Tags: warning" \
        -d "$msg" \
        "$NTFY_URL" >/dev/null 2>&1
fi

# This run's scratch dir (manifest, tv-matches, owned-library snapshots,
# any now-empty extracted/) is only worth keeping around if a job-*/ dir
# in it is still waiting to be picked up by castilian-queue.sh run --
# otherwise every scan, including a fully clean/empty one, would leave a
# permanent empty directory behind forever (caught by actually running the
# deployed button against an empty drop-zone, not by inspection).
if ! find "$RUN_STAGE" -mindepth 1 -maxdepth 1 -name 'job-*' -print -quit 2>/dev/null | grep -q .; then
    rm -rf "$RUN_STAGE"
fi

log "done: $extracted extracted, $rejected rejected, ${#CORRUPT_FILES[@]} unreadable/failed, $queued_tv TV job(s) queued, $queued_movie movie job(s) queued, ${#UNMATCHED_TITLES[@]} unmatched title(s)"
exit 0
