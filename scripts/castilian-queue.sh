#!/bin/bash
# A small work queue for getting Castilian ("Castellano") Spanish audio
# muxed into existing library files, built after doing this by hand for
# Gravity Falls/Adventure Time got unwieldy (2026-09-03). See
# mux-castilian-audio.sh for the actual muxing logic -- this script's job
# is: get a local source staged, and hand it off to that script.
#
# MEGA-source support (download + "Importante leer.txt"/.rar indirection
# resolution) was dropped 2026-09-04 -- castilian-drop-scan.sh now covers
# the day-to-day case (drop a file, it gets matched and queued
# automatically), so hunting down and pasting MEGA links by hand stopped
# being the normal path. A queued source is always a local file or
# directory already on disk now.
#
# This deliberately does NOT try to guess which show/movie a source is for
# -- you give the target directory explicitly on `add`. Auto-matching a
# Spanish folder name (e.g. "Agallas, el perro cobarde") to the right
# Sonarr series (Courage the Cowardly Dog) is a real translation problem,
# and guessing wrong here means muxing audio into the wrong show's files --
# castilian-drop-scan.sh does take that on, but safely (confident match
# against the owned library via Sonarr/Radarr lookup, never a guess); this
# script still never does.
#
# Usage:
#   castilian-queue.sh add [--movie] <local_path> <target_dir>
#                                                      queue a job
#   castilian-queue.sh run                            process all PENDING jobs
#   castilian-queue.sh status [--json]                show the queue
#   castilian-queue.sh halt                           mark any RUNNING job
#                                                      STOPPED (state only --
#                                                      pair with actually
#                                                      killing the process)
#   castilian-queue.sh resume <id>|--all               mark one (or, with
#                                                       --all, every)
#                                                       STOPPED job PENDING
#                                                       again
#   castilian-queue.sh preview <id>                    print the JSON
#                                                       preview (dry-run,
#                                                       per-pair action +
#                                                       reason) for one
#                                                       PENDING job
#
# --movie matches mux-castilian-audio.sh's own --movie: source and target
# must each contain exactly one video file, matched directly (no episode-
# number parsing). Omit it for TV (the default).
#
# State lives on the vault, not /tmp -- learned the hard way that /tmp here
# is a small RAM-backed tmpfs (filled it solid downloading into it earlier
# tonight).
set -uo pipefail

QUEUE_DIR="${CASTILIAN_QUEUE_DIR:-/mnt/vault/mega-staging/queue}"
QUEUE_FILE="$QUEUE_DIR/queue.tsv"
MUX_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/mux-castilian-audio.sh"
ARCHIVE_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/archive-castilian-audio.sh"

mkdir -p "$QUEUE_DIR"
touch "$QUEUE_FILE"

log() { echo "[$(date '+%F %T')] castilian-queue: $*" >&2; }

cmd_add() {
    local mode="tv"
    if [[ "${1:-}" == "--movie" ]]; then
        mode="movie"
        shift
    fi
    local usage="usage: castilian-queue.sh add [--movie] <local_path> <target_dir>"
    local source="${1:?$usage}"
    local target="${2:?$usage}"
    if [[ ! -d "$target" ]]; then
        echo "castilian-queue: target dir does not exist: $target" >&2
        return 1
    fi
    if [[ ! -e "$source" ]]; then
        echo "castilian-queue: no such local path: $source" >&2
        return 1
    fi
    source=$(realpath -- "$source")
    local id
    id=$(( $(cut -f1 "$QUEUE_FILE" 2>/dev/null | sort -n | tail -1) + 1 ))
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$id" "$source" "$target" "PENDING" "-" "$mode" >> "$QUEUE_FILE"
    log "queued job $id ($mode): $source -> $target"
}

cmd_status() {
    local id source target status note mode
    if [[ "${1:-}" == "--json" ]]; then
        local rows=()
        while IFS=$'\t' read -r id source target status note mode; do
            [[ -z "$id" ]] && continue
            rows+=("$(jq -n --arg id "$id" --arg status "$status" --arg mode "${mode:-tv}" \
                --arg target "$(basename "$target")" --arg note "$note" \
                '{id:$id,status:$status,mode:$mode,target:$target,note:$note}')")
        done < "$QUEUE_FILE"
        if [[ "${#rows[@]}" -eq 0 ]]; then
            echo '[]'
        else
            printf '%s\n' "${rows[@]}" | jq -s '.'
        fi
        return
    fi
    printf '%-4s %-10s %-6s %-45s %s\n' "ID" "STATUS" "MODE" "TARGET" "NOTE"
    while IFS=$'\t' read -r id source target status note mode; do
        [[ -z "$id" ]] && continue
        printf '%-4s %-10s %-6s %-45s %s\n' "$id" "$status" "${mode:-tv}" "$(basename "$target")" "$note"
    done < "$QUEUE_FILE"
}

update_row() {
    local id="$1" status="$2" note="$3"
    local tmp rid source target rstatus rnote rmode
    tmp=$(mktemp)
    while IFS=$'\t' read -r rid source target rstatus rnote rmode; do
        [[ -z "$rid" ]] && continue
        rmode="${rmode:-tv}"
        if [[ "$rid" == "$id" ]]; then
            printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$rid" "$source" "$target" "$status" "$note" "$rmode"
        else
            printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$rid" "$source" "$target" "$rstatus" "$rnote" "$rmode"
        fi
    done < "$QUEUE_FILE" > "$tmp"
    mv "$tmp" "$QUEUE_FILE"
}

cmd_run() {
    local rows id source target status note mode

    # This function only ever runs one-at-a-time (guarded by the flock in
    # the `run` case below), so any row still marked RUNNING when a fresh
    # invocation starts can only be left over from a previous run that
    # died without finishing -- killed process, crash, container restart.
    # Treat those as recoverable rather than stuck forever.
    while IFS=$'\t' read -r id source target status note mode; do
        [[ -z "$id" || "$status" != "RUNNING" ]] && continue
        log "job $id: found stuck in RUNNING (previous run died mid-job) -- resetting to PENDING to retry"
        update_row "$id" "PENDING" "retrying after an interrupted previous attempt"
    done < "$QUEUE_FILE"

    rows=$(grep -P '^\d+\t.*\tPENDING\t' "$QUEUE_FILE" || true)
    if [[ -z "$rows" ]]; then
        log "nothing pending"
        return 0
    fi
    while IFS=$'\t' read -r id source target status note mode; do
        [[ -z "$id" ]] && continue
        mode="${mode:-tv}"
        log "--- job $id ($mode): $target ---"

        # Given to us as-is on `add`, used directly -- no download step, but
        # it could've been moved/deleted since.
        local stage
        if [[ ! -e "$source" ]]; then
            update_row "$id" "FAILED" "local source no longer exists: $source"
            log "job $id FAILED: local source missing"
            continue
        fi
        stage="$source"
        log "job $id: local source $stage"

        # .mka included since 2026-09-04 -- castilian-drop-scan.sh stages
        # audio-only extracts here (no video at all), which this check
        # would otherwise fail as "no media files found" before ever
        # reaching mux-castilian-audio.sh (which already accepts .mka).
        local n
        n=$(find "$stage" -type f \( -iname '*.mkv' -o -iname '*.mp4' -o -iname '*.mka' \) | wc -l)
        if [[ "$n" -eq 0 ]]; then
            update_row "$id" "FAILED" "no media files found at source"
            log "job $id FAILED: no media files at $stage"
            continue
        fi
        log "job $id: $n media file(s) staged, muxing into $target"

        update_row "$id" "RUNNING" "muxing"
        # 2026-09-08, "manual matching" feature: a human can redirect or
        # supply a specific source->target pairing via the castilian-
        # control preview UI before running -- stored per-job so it
        # survives across a stuck-job retry, and picked up automatically
        # here if present. No override file just means normal automatic
        # episode-number matching, same as always.
        local override_file="$QUEUE_DIR/job-$id.overrides.tsv" override_arg=()
        [[ -f "$override_file" ]] && override_arg=(--override-pairs="$override_file")
        local mux_out
        if [[ "$mode" == "movie" ]]; then
            mux_out=$(bash "$MUX_SCRIPT" "$stage" "$target" --movie "${override_arg[@]}" 2>&1)
        else
            mux_out=$(bash "$MUX_SCRIPT" "$stage" "$target" "${override_arg[@]}" 2>&1)
        fi
        local logdir="$QUEUE_DIR/job-$id"
        mkdir -p "$logdir"
        echo "$mux_out" > "$logdir/mux.log"
        local summary
        summary=$(echo "$mux_out" | grep 'mux-castilian-audio: done:' | sed 's/^.*mux-castilian-audio: //')
        if [[ -z "$summary" ]]; then
            update_row "$id" "FAILED" "mux script errored, see job-$id/mux.log"
            log "job $id FAILED: mux script errored"
            continue
        fi
        update_row "$id" "DONE" "$summary"
        log "job $id DONE: $summary"

        # Archive whatever Castellano track just landed in $target -- see
        # archive-castilian-audio.sh. Deliberately non-fatal: never let an
        # archiving problem flip an otherwise-successful job to FAILED, and
        # deliberately not redirected anywhere so a failure still shows up
        # in this same log rather than vanishing silently.
        if [[ "$mode" == "movie" ]]; then
            bash "$ARCHIVE_SCRIPT" "$target" --movie 2>&1 | sed "s/^/castilian-queue: job $id archive: /"
        else
            bash "$ARCHIVE_SCRIPT" "$target" 2>&1 | sed "s/^/castilian-queue: job $id archive: /"
        fi
    done <<< "$rows"
}

# Marks any RUNNING row STOPPED. Pure state -- doesn't touch the actual OS
# process (something else, e.g. castilian-control, has to have already
# killed it); this just stops self-heal from treating it as a crash and
# auto-retrying it on the next `run`. Safe to call anytime, including when
# nothing is running.
cmd_halt() {
    local id source target status note mode found=0
    while IFS=$'\t' read -r id source target status note mode; do
        [[ -z "$id" || "$status" != "RUNNING" ]] && continue
        update_row "$id" "STOPPED" "stopped by request"
        log "job $id: marked STOPPED"
        found=1
    done < "$QUEUE_FILE"
    [[ "$found" -eq 0 ]] && log "nothing was RUNNING"
}

# Marks a STOPPED row (or, with --all, every STOPPED row) back to PENDING
# so the next `run` picks it up again -- a fresh pass over the local
# source, not a mid-file resume. Cheap either way: mux-castilian-audio.sh's
# own process_pair() skips any target that already has a Spanish track, so
# a re-run doesn't redo work that already landed.
#
# Requires either an id or --all -- no bare "resume" that silently means
# everything. Jobs can end up STOPPED for different reasons (one crashed,
# another you told to stop and don't want back yet), and resuming all of
# them by accident already restarted a real download once. --all is still
# here for when you genuinely do want that, deliberately spelled out.
cmd_resume() {
    local arg="${1:?usage: castilian-queue.sh resume <id>|--all}"
    local id source target status note mode found=0
    if [[ "$arg" == "--all" ]]; then
        while IFS=$'\t' read -r id source target status note mode; do
            [[ -z "$id" || "$status" != "STOPPED" ]] && continue
            update_row "$id" "PENDING" "resumed after being stopped"
            log "job $id: marked PENDING (resumed)"
            found=1
        done < "$QUEUE_FILE"
        [[ "$found" -eq 0 ]] && log "nothing was STOPPED"
        return 0
    fi
    while IFS=$'\t' read -r id source target status note mode; do
        [[ "$id" == "$arg" ]] || continue
        if [[ "$status" != "STOPPED" ]]; then
            log "job $id: not STOPPED (currently $status), nothing to resume"
            return 1
        fi
        update_row "$id" "PENDING" "resumed after being stopped"
        log "job $id: marked PENDING (resumed)"
        return 0
    done < "$QUEUE_FILE"
    log "no such job: $arg"
    return 1
}

# 2026-09-08, duration-mismatch review feature: marks a job PENDING again
# regardless of its current status (DONE/FAILED/STOPPED all count) -- for
# a standing source/target pair (e.g. a drop-zone directory that gets
# re-checked periodically), "done" doesn't mean "never look at this again":
# new files may have arrived, or a human may have just approved a specific
# override (see castilian-control's duration-review page) that's worth
# picking up on the next run. Cheap either way, same reasoning as
# cmd_resume's own comment: mux-castilian-audio.sh's own process_pair()
# skips anything that doesn't actually need touching.
cmd_requeue() {
    local id="${1:?usage: castilian-queue.sh requeue <id>}"
    local rid source target status note mode
    while IFS=$'\t' read -r rid source target status note mode; do
        [[ "$rid" == "$id" ]] || continue
        update_row "$id" "PENDING" "requeued"
        log "job $id: marked PENDING (requeued)"
        return 0
    done < "$QUEUE_FILE"
    log "no such job: $id"
    return 1
}

# 2026-09-08, "manual matching" feature: prints the JSON array
# mux-castilian-audio.sh's --json-out produces for one job's (source,
# target) pair, run in --dry-run so nothing on disk changes -- this is
# what castilian-control's preview UI calls to show every proposed pairing
# (and why) before a human approves the job to actually run. Picks up the
# same per-job override file cmd_run does, so the preview reflects
# whatever's already been manually set.
cmd_preview() {
    local id="${1:?usage: castilian-queue.sh preview <id>}"
    local rid source target status note mode found=0
    while IFS=$'\t' read -r rid source target status note mode; do
        [[ "$rid" == "$id" ]] || continue
        found=1
        mode="${mode:-tv}"
        break
    done < "$QUEUE_FILE"
    if [[ "$found" -eq 0 ]]; then
        echo "castilian-queue: no such job: $id" >&2
        return 1
    fi
    local override_file="$QUEUE_DIR/job-$id.overrides.tsv" override_arg=()
    [[ -f "$override_file" ]] && override_arg=(--override-pairs="$override_file")
    local tmp movie_arg=()
    tmp=$(mktemp)
    [[ "$mode" == "movie" ]] && movie_arg=(--movie)
    bash "$MUX_SCRIPT" "$source" "$target" --dry-run "${movie_arg[@]}" "${override_arg[@]}" --json-out="$tmp" >&2
    cat "$tmp"
    rm -f "$tmp"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    case "${1:-}" in
        add)    shift; cmd_add "$@" ;;
        run)
            # A queued job can run for hours (large downloads) -- this lock
            # keeps a periodic timer from starting a second overlapping
            # `run` (which would race on queue.tsv writes) while one is
            # already in flight, whether that one was started manually or
            # by the timer itself.
            exec 9>"$QUEUE_DIR/.run.lock"
            flock -n 9 || { log "already running (lock held), exiting"; exit 0; }
            cmd_run
            ;;
        status) shift; cmd_status "$@" ;;
        halt)   cmd_halt ;;
        resume) shift; cmd_resume "$@" ;;
        preview) shift; cmd_preview "$@" ;;
        requeue) shift; cmd_requeue "$@" ;;
        *)
            echo "Usage: $0 {add [--movie] <local_path> <target_dir> | run | status [--json] | halt | resume <id>|--all | preview <id> | requeue <id>}" >&2
            exit 1
            ;;
    esac
fi
