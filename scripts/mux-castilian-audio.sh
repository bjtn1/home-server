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
#       [--json-out=<path>] [--override-pairs=<path>]
#
#   source_dir  Directory of the newly-downloaded release (recursed into).
#   target_dir  The existing library folder for the same show/movie.
#   --dry-run   Report what would happen; touches nothing on disk.
#   --movie     Movie mode: source_dir and target_dir must each contain
#               exactly one video file -- those two are matched directly,
#               no episode-number parsing. Without this flag (TV mode),
#               every file in both dirs must carry a SxxExx or NxNN episode
#               marker in its filename, and files are matched by that.
#   --json-out=<path>       Write a JSON array of {action, source, target,
#                           reason} -- one entry per source file, action is
#                           mux/skip/fail -- alongside the normal log
#                           output. Same info, just structured; used by
#                           castilian-review's match-preview UI.
#   --override-pairs=<path> TSV (source_path<TAB>target_path), TV mode
#                           only: process these exact pairs first (full
#                           safety checks still apply), excluding both
#                           sides from the automatic episode-number
#                           matching below. Lets a human redirect or supply
#                           a match the automatic pairing got wrong or
#                           missed, without turning off any of the safety
#                           checks that decide whether the mux actually
#                           happens.
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

# LATAM_PATTERN/CASTILIAN_PATTERN -- see castilian-patterns.sh (shared with
# arr-audio-lang-check.sh and archive-castilian-audio.sh so the three bash
# copies can't drift out of sync with each other).
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/castilian-patterns.sh"

# 2026-09-08, found live: a batch of 20 files a human had explicitly
# reviewed and approved on the castilian-review review page came back "0
# muxed" -- this script's track_is_castilian() below is metadata-only, and
# never had any awareness of castilian-whisper-check.sh or the human-
# override cache the review page writes to, unlike castilian-drop-scan.sh's
# own copy of this same fallback. Same fix mirrored here (kept as its own
# copy rather than shared code, same reasoning as drop-scan's header: a
# regression in shared detection logic risks two already-tested scripts at
# once for a cosmetic win). WHISPER_SERVER_URL unset/empty means "not
# configured" -- this whole fallback is then simply skipped, same as
# before.
WHISPER_CHECK_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/castilian-whisper-check.sh"

# 2026-09-09, found live: pure SxxExx/NxNN number matching (episode_key()
# below) has no way to catch a genuinely mismatched pairing -- 35 of 41
# "duration mismatch" files in a real batch turned out to be wrong-episode
# matches entirely (regional dub numbering doesn't track the English
# broadcast order for this show), not timing issues. The duration check
# happened to catch every one of those by accident, not by design -- a
# wrong-episode pair with a coincidentally similar duration would sail
# through with zero protection. See that script's own header for the full
# story and exit-code contract; only exit 1 means "block this mux," every
# other outcome (including the script or python3/numpy simply not being
# available -- this runs both on the host and inside this container,
# which doesn't have python3 at all) is deliberately permissive.
CONTENT_VERIFY_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/castilian-content-verify.py"

DRY_RUN=0
MOVIE_MODE=0
JSON_OUT=""
OVERRIDE_FILE=""
args=()
for a in "$@"; do
    case "$a" in
        --dry-run) DRY_RUN=1 ;;
        --movie)   MOVIE_MODE=1 ;;
        # 2026-09-08, "manual matching" feature: --json-out gives
        # castilian-review's preview UI a structured, per-pair record of
        # what would happen (and why) instead of having to regex-parse
        # free-text log lines -- same information, just also machine-
        # readable. --override-pairs lets a human redirect a specific
        # source file at a target OTHER than what automatic episode-number
        # matching would pick (or provide a match where automatic matching
        # found none) -- built after a real session where automatic
        # duration-mismatch skips came back "0 muxed" with no way to fix
        # any individual pairing without editing files by hand.
        --json-out=*)        JSON_OUT="${a#--json-out=}" ;;
        --override-pairs=*)  OVERRIDE_FILE="${a#--override-pairs=}" ;;
        *)         args+=("$a") ;;
    esac
done
set -- "${args[@]}"

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 <source_dir> <target_dir> [--dry-run] [--movie] [--json-out=<path>] [--override-pairs=<path>]" >&2
    exit 1
fi
SOURCE_DIR="$1"
TARGET_DIR="$2"

for cmd in mkvmerge ffprobe jq; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "mux-castilian-audio: missing required command: $cmd" >&2; exit 1; }
done

log() { echo "[$(date '+%F %T')] mux-castilian-audio: $*"; }

# Structured twin of log() -- one JSON object per source file's outcome,
# collected here and written out as an array at the very end if --json-out
# was given. action is always one of mux/skip/fail (dry-run's "would
# happen" uses "mux" too -- the caller cares whether it WOULD mux, not
# whether this was a dry run) so a caller can color/route rows without
# parsing free-text reason strings. Used by castilian-queue.sh's own
# `preview` command (CLI) -- --dry-run/--override-pairs/--json-out are all
# meant to be used directly from the command line, not just by a web UI.
JSON_LINES=()
emit_json() {
    local action="$1" source="$2" target="$3" reason="$4"
    [[ -z "$JSON_OUT" ]] && return
    JSON_LINES+=("$(jq -nc --arg action "$action" --arg source "$source" \
        --arg target "$target" --arg reason "$reason" \
        '{action:$action, source:$source, target:(if $target=="" then null else $target end), reason:$reason}')")
}
write_json_out() {
    [[ -z "$JSON_OUT" ]] && return
    if [[ "${#JSON_LINES[@]}" -eq 0 ]]; then
        echo "[]" > "$JSON_OUT"
    else
        printf '%s\n' "${JSON_LINES[@]}" | jq -s '.' > "$JSON_OUT"
    fi
}

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
      | {id, lang: (.properties.language // ""), ietf: (.properties.language_ietf // ""), title: (.properties.track_name // "")}
    '
}

# 2026-09-05: see archive-castilian-audio.sh's identical addition for the
# full story (Camp Lazlo S01E13: language_ietf=es-419, generic untitled-
# in-effect title, wrongly treated as Castilian by the old "reject only on
# LATAM_PATTERN match" check). Requires positive evidence -- an explicit
# es-ES region tag, or a title/lang match on CASTILIAN_PATTERN with no
# LATAM_PATTERN match -- rather than defaulting to true on the mere
# absence of a red flag. language_ietf wins over title text when present.
track_is_castilian() {
    local ietf="$1" title="$2" lang="$3"
    if [[ "$ietf" =~ ^[Ee][Ss]-([A-Za-z]{2}|[0-9]{3})$ ]]; then
        [[ "${BASH_REMATCH[1],,}" == "es" ]]
        return
    fi
    local check_text="$title $lang"
    echo "$check_text" | grep -qiE "$CASTILIAN_PATTERN" && ! echo "$check_text" | grep -qiE "$LATAM_PATTERN"
}

MUXED=0 SKIPPED=0 FAILED=0

# Runs the full check-then-mux sequence for one (src, tgt) pair, labeled
# for logging. Mutates the MUXED/SKIPPED/FAILED counters directly.
#
# force_duration (4th arg, optional): "1" skips ONLY the duration-mismatch
# check below -- every other safety check (existing-track, dialect
# confirmation) still applies unchanged. Never set by automatic matching;
# only ever comes from a human's explicit per-pair override (see
# --override-pairs' 3rd column) -- "force" means a person looked at this
# specific pair and decided the mismatch is safe to mux through, not that
# any check gets silently loosened for everyone.
process_pair() {
    local src="$1" tgt="$2" label="$3" force_duration="${4:-}"
    local existing src_tracks t ietf lang title chosen="" count=0 track_id track_title
    local d_src d_tgt diff out new_audio_count old_audio_count

    # 1. does the target already have a CONFIRMED Castilian Spanish track?
    # 2026-09-08, found live: the old rule ("skip if the target has ANY
    # Spanish track at all") blocked every single episode of a real batch
    # (Courage the Cowardly Dog) -- these are "MULTI" WEB-DL releases that
    # already ship their own Latin-American (es-419) Spanish track, which
    # is exactly the wrong-dialect situation this whole pipeline exists to
    # fix. Blocking on its mere presence defeated the point. Now only a
    # true duplicate (an existing track ALREADY confirmed Castilian) is
    # skipped -- a confirmed-LATAM or unconfirmed/generic existing track
    # no longer blocks the add. Safe to tell the two apart now that the
    # newly-added track gets a genuine es-ES language tag (see the
    # mkvmerge invocation below), not just a cosmetic title -- so this
    # never creates an actually-ambiguous pair, only a correctly labeled
    # one.
    existing=$(find_spanish_track "$tgt")
    existing_note=""
    if [[ -n "$existing" ]]; then
        local ex_castilian=0 et eietf etitle elang
        while IFS= read -r et; do
            eietf=$(jq -r '.ietf' <<<"$et")
            etitle=$(jq -r '.title' <<<"$et")
            elang=$(jq -r '.lang' <<<"$et")
            track_is_castilian "$eietf" "$etitle" "$elang" && ex_castilian=1
        done <<<"$existing"
        if [[ "$ex_castilian" -eq 1 ]]; then
            log "SKIP $label -- target already has a CONFIRMED Castilian Spanish audio track ($(echo "$existing" | jq -r '.title // .lang' | paste -sd, -)), nothing to do"
            emit_json skip "$src" "$tgt" "target already has a confirmed Castilian Spanish track"
            SKIPPED=$((SKIPPED+1))
            return
        fi
        existing_note=" (target already had a non-Castilian Spanish track: $(echo "$existing" | jq -r '.title // .lang' | paste -sd, -) -- adding alongside it, not replacing)"
    fi

    # 2. does the source even have a Spanish track, and is it the dialect we
    # want? Requires positive evidence for EVERY candidate (an explicit
    # es-ES region tag, or a title/lang match on CASTILIAN_PATTERN with no
    # LATAM_PATTERN match) -- an untitled/region-less track is refused the
    # same as a confirmed-Latino one, never defaulted to accepted.
    src_tracks=$(find_spanish_track "$src")
    if [[ -z "$src_tracks" ]]; then
        log "SKIP $label -- source has no Spanish audio track: $(basename "$src")"
        emit_json skip "$src" "$tgt" "source has no Spanish audio track"
        SKIPPED=$((SKIPPED+1))
        return
    fi
    while IFS= read -r t; do
        ietf=$(jq -r '.ietf' <<<"$t")
        title=$(jq -r '.title' <<<"$t")
        lang=$(jq -r '.lang' <<<"$t")
        if track_is_castilian "$ietf" "$title" "$lang"; then
            count=$((count+1))
            chosen="$t"
        fi
    done <<<"$src_tracks"

    # Metadata alone couldn't confirm -- try the Whisper content-based
    # fallback (which checks the human-override cache FIRST, before
    # anything else -- see castilian-whisper-check.sh's own header), same
    # scope castilian-drop-scan.sh already restricts this to: exactly ONE
    # Spanish-tagged track, metadata gives no dialect signal at all
    # (count==0). A multi-track case (count>1) is a different, still-
    # unresolved problem -- which of several plausible tracks is right --
    # and Whisper can't answer that without knowing which one to even
    # listen to, so it's left alone same as before.
    if [[ "$count" -eq 0 && "$(wc -l <<<"$src_tracks")" -eq 1 && -n "${WHISPER_SERVER_URL:-}" ]]; then
        local single_id audio_idx whisper_verdict
        single_id=$(jq -r '.id' <<<"$src_tracks")
        # 0-based position among ALL audio tracks (not just Spanish ones)
        # -- what ffmpeg's -map 0:a:N (and so castilian-extract-clip.sh)
        # expects, same as castilian-drop-scan.sh's identical computation.
        audio_idx=$(mkvmerge -J "$src" 2>/dev/null | jq -r --argjson tid "$single_id" '
            [.tracks[] | select(.type=="audio")] as $all
            | ($all | map(.id) | index($tid)) // empty
        ')
        if [[ -n "$audio_idx" ]]; then
            log "$label -- metadata inconclusive, trying Whisper content check (track $single_id, audio index $audio_idx): $(basename -- "$src")"
            "$WHISPER_CHECK_SCRIPT" "$src" "$audio_idx" >&2
            whisper_verdict=$?
            if [[ "$whisper_verdict" -eq 0 ]]; then
                log "$label -- Whisper confirmed Castilian: $(basename -- "$src")"
                count=1
                chosen="$src_tracks"
            else
                log "$label -- Whisper did not confirm (exit $whisper_verdict, 1=not-Castilian 2=inconclusive 3=check-failed): $(basename -- "$src")"
            fi
        fi
    fi

    if [[ "$count" -ne 1 ]]; then
        log "SKIP $label -- source has no single confirmed-Castilian Spanish track ($count positive match(es) among: $(echo "$src_tracks" | jq -r '.title // .lang' | paste -sd, -)), refusing to guess"
        emit_json skip "$src" "$tgt" "source has no single confirmed-Castilian Spanish track ($count positive match(es))"
        SKIPPED=$((SKIPPED+1))
        return
    fi
    src_tracks="$chosen"
    track_id=$(echo "$src_tracks" | jq -r '.id')
    track_title=$(echo "$src_tracks" | jq -r '.title')

    # 3. duration sanity check
    d_src=$(container_duration "$src")
    d_tgt=$(container_duration "$tgt")
    if [[ -z "$d_src" || -z "$d_tgt" ]]; then
        log "SKIP $label -- couldn't read duration from one of the files"
        emit_json skip "$src" "$tgt" "couldn't read duration from one of the files"
        SKIPPED=$((SKIPPED+1))
        return
    fi
    diff=$(awk -v a="$d_src" -v b="$d_tgt" 'BEGIN{d=a-b; if(d<0)d=-d; print d}')
    if awk -v d="$diff" -v t="$DURATION_TOLERANCE" 'BEGIN{exit !(d>t)}'; then
        if [[ "$force_duration" == "1" ]]; then
            log "$label -- duration mismatch (target ${d_tgt}s vs source ${d_src}s, diff ${diff}s > ${DURATION_TOLERANCE}s) -- proceeding anyway, human-approved override"
            existing_note="$existing_note (duration mismatch diff ${diff}s -- human-approved anyway)"
        else
            log "SKIP $label -- duration mismatch (target ${d_tgt}s vs source ${d_src}s, diff ${diff}s > ${DURATION_TOLERANCE}s) -- likely a different cut, would desync"
            emit_json skip "$src" "$tgt" "duration mismatch (target ${d_tgt}s vs source ${d_src}s, diff ${diff}s) -- likely a different cut, would desync"
            SKIPPED=$((SKIPPED+1))
            return
        fi
    fi

    # 3.5 content verification -- see CONTENT_VERIFY_SCRIPT's own header
    # for the full story. Runs even when force_duration bypassed the
    # check above: an overridden pair is exactly where this matters most,
    # since its duration safety net was deliberately turned off. Only
    # exit code 1 (a positive, deliberate "confirmed mismatch" signal)
    # blocks anything -- every other outcome (0, 3, python3 missing
    # entirely) is intentionally permissive, so a missing dependency in
    # this container can never masquerade as a real mismatch.
    if [[ -f "$CONTENT_VERIFY_SCRIPT" ]] && command -v python3 >/dev/null 2>&1; then
        mode_arg="tv"; [[ "$MOVIE_MODE" -eq 1 ]] && mode_arg="movie"
        python3 "$CONTENT_VERIFY_SCRIPT" "$src" "$tgt" "$mode_arg"
        cv_rc=$?
        if [[ "$cv_rc" -eq 1 ]]; then
            log "SKIP $label -- content verification failed: source and target don't appear to be the same episode despite matching numbers"
            emit_json skip "$src" "$tgt" "content verification failed -- audio doesn't correlate, likely a wrong-episode match despite matching SxxExx numbers"
            SKIPPED=$((SKIPPED+1))
            return
        fi
    fi

    if [[ "$DRY_RUN" -eq 1 ]]; then
        log "WOULD MUX $label <- track $track_id '$track_title' from $(basename "$src")$existing_note"
        emit_json mux "$src" "$tgt" "would add '$track_title' track$existing_note"
        MUXED=$((MUXED+1))
        return
    fi

    out="${tgt}.muxtmp.mkv"
    if mkvmerge -q -o "$out" "$tgt" \
        --audio-tracks "$track_id" --no-video --no-subtitles --no-chapters --no-attachments \
        --language "${track_id}:es-ES" --track-name "${track_id}:Castellano" \
        "$src"; then
        new_audio_count=$(mkvmerge -J "$out" 2>/dev/null | jq '[.tracks[] | select(.type=="audio")] | length')
        old_audio_count=$(mkvmerge -J "$tgt" 2>/dev/null | jq '[.tracks[] | select(.type=="audio")] | length')
        if [[ "$new_audio_count" -eq $((old_audio_count + 1)) ]]; then
            mv -f -- "$out" "$tgt"
            log "MUXED $label -- added Castellano (es-ES) track (now $new_audio_count audio tracks)$existing_note"
            emit_json mux "$src" "$tgt" "added Castellano (es-ES) track$existing_note"
            MUXED=$((MUXED+1))
        else
            log "FAILED $label -- output has $new_audio_count audio tracks, expected $((old_audio_count + 1)); left target untouched, output at $out for inspection"
            emit_json fail "$src" "$tgt" "output had $new_audio_count audio tracks, expected $((old_audio_count + 1))"
            FAILED=$((FAILED+1))
        fi
    else
        log "FAILED $label -- mkvmerge exited non-zero; left target untouched"
        emit_json fail "$src" "$tgt" "mkvmerge exited non-zero"
        rm -f -- "$out"
        FAILED=$((FAILED+1))
    fi
}

if [[ "$MOVIE_MODE" -eq 1 ]]; then
    # Source can be an audio-only .mka (e.g. from archive-castilian-audio.sh's
    # archive) as well as a real video file -- target is always a real
    # library file, never .mka.
    mapfile -d '' -t src_files < <(find "$SOURCE_DIR" -type f \( -iname '*.mkv' -o -iname '*.mp4' -o -iname '*.mka' \) -print0)
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
    write_json_out
else
    # Manual overrides go first, and win outright -- each overridden source
    # is excluded from the automatic loop below (already handled), and each
    # overridden target is excluded from the automatic TARGETS map (so
    # automatic matching can't ALSO pick it for a different source, double-
    # using one target file). Overrides still go through process_pair's
    # full safety checks (duration, existing-Castilian, track selection) --
    # "manual" means picking WHICH target, never bypassing why it's safe.
    declare -A OVERRIDDEN_SRC OVERRIDDEN_TGT
    if [[ -n "$OVERRIDE_FILE" && -f "$OVERRIDE_FILE" ]]; then
        # 3rd column (optional) is a comma-separated flag list -- currently
        # only "force_duration" is recognized (see process_pair's own
        # comment on what that does and doesn't bypass). Older 2-column
        # override files still work fine -- oflags is just empty for them.
        while IFS=$'\t' read -r osrc otgt oflags; do
            [[ -z "$osrc" || -z "$otgt" ]] && continue
            [[ -f "$osrc" && -f "$otgt" ]] || { log "SKIP override -- source or target no longer exists: $osrc -> $otgt"; continue; }
            OVERRIDDEN_SRC["$(readlink -f -- "$osrc")"]=1
            OVERRIDDEN_TGT["$(readlink -f -- "$otgt")"]=1
            force_dur=0
            [[ ",$oflags," == *,force_duration,* ]] && force_dur=1
            process_pair "$osrc" "$otgt" "OVERRIDE ($(basename -- "$otgt"))" "$force_dur"
        done < "$OVERRIDE_FILE"
    fi

    declare -A TARGETS
    while IFS= read -r -d '' f; do
        [[ -n "${OVERRIDDEN_TGT[$(readlink -f -- "$f")]:-}" ]] && continue
        key=$(episode_key "$(basename "$f")") || continue
        TARGETS["$key"]="$f"
    done < <(find "$TARGET_DIR" -type f \( -iname '*.mkv' -o -iname '*.mp4' \) -print0)

    while IFS= read -r -d '' src; do
        [[ -n "${OVERRIDDEN_SRC[$(readlink -f -- "$src")]:-}" ]] && continue
        key=$(episode_key "$(basename "$src")") || {
            log "SKIP (no episode marker): $(basename "$src")"
            emit_json skip "$src" "" "no episode marker in filename"
            SKIPPED=$((SKIPPED+1)); continue
        }
        tgt="${TARGETS[$key]:-}"
        if [[ -z "$tgt" ]]; then
            log "SKIP S${key/-/E} -- no matching file in target dir: $(basename "$src")"
            emit_json skip "$src" "" "no matching S${key/-/E} file in target dir"
            SKIPPED=$((SKIPPED+1))
            continue
        fi
        process_pair "$src" "$tgt" "S${key/-/E} ($(basename "$tgt"))"
    done < <(find "$SOURCE_DIR" -type f \( -iname '*.mkv' -o -iname '*.mp4' -o -iname '*.mka' \) -print0)
    write_json_out
fi

log "done: $MUXED muxed, $SKIPPED skipped, $FAILED failed"
[[ "$FAILED" -gt 0 ]] && exit 1
exit 0
