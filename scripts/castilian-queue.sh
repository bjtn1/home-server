#!/bin/bash
# A small work queue for getting Castilian ("Castellano") Spanish audio
# muxed into existing library files, built after doing this by hand for
# Gravity Falls/Adventure Time got unwieldy (2026-09-03). See
# mux-castilian-audio.sh for the actual muxing logic -- this script's job
# is: get a source (from MEGA, or already sitting on disk) staged, and
# hand it off to that script.
#
# A queued source can be either:
#   - a MEGA link (any form -- see below), which gets downloaded first, or
#   - a local file or directory already on disk (you found/downloaded it
#     yourself), used directly with no download step.
#
# Sites like the one this was built against don't always give you a direct
# MEGA folder link -- a share is often a single "Importante leer.txt" (or a
# .rar/.zip containing one) whose only content is the real folder link one
# hop further in. resolve_link() follows that chain (txt -> link,
# archive -> txt -> link) up to MAX_HOPS deep so you can just paste
# whatever link you were given. Local sources skip this entirely.
#
# This deliberately does NOT try to guess which show/movie a source is for
# -- you give the target directory explicitly on `add`. Auto-matching a
# Spanish folder name (e.g. "Agallas, el perro cobarde") to the right
# Sonarr series (Courage the Cowardly Dog) is a real translation problem,
# and guessing wrong here means muxing audio into the wrong show's files.
# Not worth it.
#
# Usage:
#   castilian-queue.sh add [--movie] <mega_link_or_local_path> <target_dir>
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
MEGARC="${CASTILIAN_MEGARC:-/mnt/vault/mega-staging/.megarc}"
MUX_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/mux-castilian-audio.sh"
MAX_HOPS=5
MEGA_LINK_RE='^https://mega\.nz/(folder|file)/'

mkdir -p "$QUEUE_DIR"
touch "$QUEUE_FILE"

log() { echo "castilian-queue: $*" >&2; }

megadl_cfg() {
    if [[ -f "$MEGARC" ]]; then
        megadl --config="$MEGARC" "$@"
    else
        megadl "$@"
    fi
}

# Follow file/archive indirection down to a real mega.nz/folder/ link.
# Prints the resolved folder link on success; prints nothing and returns 1
# on failure (dead end, or too many hops).
resolve_link() {
    local link="$1" hop work
    work=$(mktemp -d)
    for ((hop = 0; hop < MAX_HOPS; hop++)); do
        if [[ "$link" == *"/folder/"* ]]; then
            echo "$link"
            rm -rf "$work"
            return 0
        fi
        if [[ "$link" != *"/file/"* ]]; then
            log "resolve: not a recognizable mega.nz link: $link"
            rm -rf "$work"
            return 1
        fi
        rm -f "$work"/*
        if ! megadl_cfg --path="$work" "$link" >/dev/null 2>&1; then
            log "resolve: failed to download $link"
            rm -rf "$work"
            return 1
        fi
        local got
        got=$(find "$work" -maxdepth 1 -type f | head -1)
        if [[ -z "$got" ]]; then
            log "resolve: nothing came down for $link"
            rm -rf "$work"
            return 1
        fi
        case "$got" in
            *.txt)
                link=$(grep -oE 'https://mega\.nz/(folder|file)/[A-Za-z0-9_-]+#[A-Za-z0-9_-]+' "$got" | head -1)
                if [[ -z "$link" ]]; then
                    log "resolve: $got had no mega.nz link inside it"
                    rm -rf "$work"
                    return 1
                fi
                ;;
            *.rar)
                mkdir -p "$work/extracted"
                if ! unrar e -p- -o+ -inul "$got" "$work/extracted/" 2>/dev/null; then
                    log "resolve: couldn't extract $got -- likely password-protected (these 'Importante leer' rars often are; check the site page for a password)"
                    rm -rf "$work"
                    return 1
                fi
                local inner
                inner=$(find "$work/extracted" -iname '*.txt' | head -1)
                if [[ -z "$inner" ]]; then
                    log "resolve: no .txt found inside $got"
                    rm -rf "$work"
                    return 1
                fi
                link=$(grep -oE 'https://mega\.nz/(folder|file)/[A-Za-z0-9_-]+#[A-Za-z0-9_-]+' "$inner" | head -1)
                if [[ -z "$link" ]]; then
                    log "resolve: $inner had no mega.nz link inside it"
                    rm -rf "$work"
                    return 1
                fi
                ;;
            *)
                log "resolve: don't know how to follow $(basename "$got") (not .txt/.rar)"
                rm -rf "$work"
                return 1
                ;;
        esac
    done
    log "resolve: gave up after $MAX_HOPS hops"
    rm -rf "$work"
    return 1
}

cmd_add() {
    local mode="tv"
    if [[ "${1:-}" == "--movie" ]]; then
        mode="movie"
        shift
    fi
    local usage="usage: castilian-queue.sh add [--movie] <mega_link_or_local_path> <target_dir>"
    local source="${1:?$usage}"
    local target="${2:?$usage}"
    if [[ ! -d "$target" ]]; then
        echo "castilian-queue: target dir does not exist: $target" >&2
        return 1
    fi
    if [[ ! "$source" =~ $MEGA_LINK_RE ]]; then
        if [[ ! -e "$source" ]]; then
            echo "castilian-queue: not a MEGA link (https://mega.nz/folder/... or /file/...) and no such local path: $source" >&2
            return 1
        fi
        source=$(realpath -- "$source")
    fi
    local id
    id=$(( $(cut -f1 "$QUEUE_FILE" 2>/dev/null | sort -n | tail -1) + 1 ))
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$id" "$source" "$target" "PENDING" "-" "$mode" >> "$QUEUE_FILE"
    log "queued job $id ($mode): $source -> $target"
}

cmd_status() {
    local id link target status note mode
    if [[ "${1:-}" == "--json" ]]; then
        local rows=()
        while IFS=$'\t' read -r id link target status note mode; do
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
    while IFS=$'\t' read -r id link target status note mode; do
        [[ -z "$id" ]] && continue
        printf '%-4s %-10s %-6s %-45s %s\n' "$id" "$status" "${mode:-tv}" "$(basename "$target")" "$note"
    done < "$QUEUE_FILE"
}

update_row() {
    local id="$1" status="$2" note="$3"
    local tmp rid link target rstatus rnote rmode
    tmp=$(mktemp)
    while IFS=$'\t' read -r rid link target rstatus rnote rmode; do
        [[ -z "$rid" ]] && continue
        rmode="${rmode:-tv}"
        if [[ "$rid" == "$id" ]]; then
            printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$rid" "$link" "$target" "$status" "$note" "$rmode"
        else
            printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$rid" "$link" "$target" "$rstatus" "$rnote" "$rmode"
        fi
    done < "$QUEUE_FILE" > "$tmp"
    mv "$tmp" "$QUEUE_FILE"
}

cmd_run() {
    local rows id link target status note mode

    # This function only ever runs one-at-a-time (guarded by the flock in
    # the `run` case below), so any row still marked RUNNING when a fresh
    # invocation starts can only be left over from a previous run that
    # died without finishing -- killed process, crash, container restart.
    # Treat those as recoverable rather than stuck forever.
    while IFS=$'\t' read -r id link target status note mode; do
        [[ -z "$id" || "$status" != "RUNNING" ]] && continue
        log "job $id: found stuck in RUNNING (previous run died mid-job) -- resetting to PENDING to retry"
        update_row "$id" "PENDING" "retrying after an interrupted previous attempt"
    done < "$QUEUE_FILE"

    rows=$(grep -P '^\d+\t.*\tPENDING\t' "$QUEUE_FILE" || true)
    if [[ -z "$rows" ]]; then
        log "nothing pending"
        return 0
    fi
    while IFS=$'\t' read -r id link target status note mode; do
        [[ -z "$id" ]] && continue
        mode="${mode:-tv}"
        log "--- job $id ($mode): $target ---"

        local stage
        if [[ "$link" =~ $MEGA_LINK_RE ]]; then
            update_row "$id" "RUNNING" "resolving link"
            local resolved
            resolved=$(resolve_link "$link")
            if [[ -z "$resolved" ]]; then
                update_row "$id" "FAILED" "could not resolve link to a folder"
                log "job $id FAILED: could not resolve link"
                continue
            fi
            log "job $id: resolved to $resolved"

            stage="$QUEUE_DIR/job-$id"
            mkdir -p "$stage"
            update_row "$id" "RUNNING" "downloading"
            if ! echo "all" | megadl_cfg --choose-files --path="$stage" "$resolved" >"$stage/download.log" 2>&1; then
                update_row "$id" "FAILED" "download errored, see job-$id/download.log"
                log "job $id FAILED: download error"
                continue
            fi
        else
            # Local source -- given to us as-is on `add`, used directly.
            # No download step, but it could've been moved/deleted since.
            if [[ ! -e "$link" ]]; then
                update_row "$id" "FAILED" "local source no longer exists: $link"
                log "job $id FAILED: local source missing"
                continue
            fi
            stage="$link"
            log "job $id: local source $stage"
        fi

        local n
        n=$(find "$stage" -type f \( -iname '*.mkv' -o -iname '*.mp4' \) | wc -l)
        if [[ "$n" -eq 0 ]]; then
            update_row "$id" "FAILED" "no media files found at source"
            log "job $id FAILED: no media files at $stage"
            continue
        fi
        log "job $id: $n media file(s) staged, muxing into $target"

        update_row "$id" "RUNNING" "muxing"
        local mux_out
        if [[ "$mode" == "movie" ]]; then
            mux_out=$(bash "$MUX_SCRIPT" "$stage" "$target" --movie 2>&1)
        else
            mux_out=$(bash "$MUX_SCRIPT" "$stage" "$target" 2>&1)
        fi
        local logdir="$QUEUE_DIR/job-$id"
        mkdir -p "$logdir"
        echo "$mux_out" > "$logdir/mux.log"
        local summary
        summary=$(echo "$mux_out" | grep '^mux-castilian-audio: done:' | sed 's/^mux-castilian-audio: //')
        if [[ -z "$summary" ]]; then
            update_row "$id" "FAILED" "mux script errored, see job-$id/mux.log"
            log "job $id FAILED: mux script errored"
            continue
        fi
        update_row "$id" "DONE" "$summary"
        log "job $id DONE: $summary"
    done <<< "$rows"
}

# Marks any RUNNING row STOPPED. Pure state -- doesn't touch the actual OS
# process (something else, e.g. castilian-control, has to have already
# killed it); this just stops self-heal from treating it as a crash and
# auto-retrying it on the next `run`. Safe to call anytime, including when
# nothing is running.
cmd_halt() {
    local id link target status note mode found=0
    while IFS=$'\t' read -r id link target status note mode; do
        [[ -z "$id" || "$status" != "RUNNING" ]] && continue
        update_row "$id" "STOPPED" "stopped by request"
        log "job $id: marked STOPPED"
        found=1
    done < "$QUEUE_FILE"
    [[ "$found" -eq 0 ]] && log "nothing was RUNNING"
}

# Marks a STOPPED row (or, with --all, every STOPPED row) back to PENDING
# so the next `run` picks it up again (a fresh download/local-source pass,
# not a mid-file resume -- MEGA doesn't support that; already-downloaded
# files are skipped, not redone).
#
# Requires either an id or --all -- no bare "resume" that silently means
# everything. Jobs can end up STOPPED for different reasons (one crashed,
# another you told to stop and don't want back yet), and resuming all of
# them by accident already restarted a real download once. --all is still
# here for when you genuinely do want that, deliberately spelled out.
cmd_resume() {
    local arg="${1:?usage: castilian-queue.sh resume <id>|--all}"
    local id link target status note mode found=0
    if [[ "$arg" == "--all" ]]; then
        while IFS=$'\t' read -r id link target status note mode; do
            [[ -z "$id" || "$status" != "STOPPED" ]] && continue
            update_row "$id" "PENDING" "resumed after being stopped"
            log "job $id: marked PENDING (resumed)"
            found=1
        done < "$QUEUE_FILE"
        [[ "$found" -eq 0 ]] && log "nothing was STOPPED"
        return 0
    fi
    while IFS=$'\t' read -r id link target status note mode; do
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
        *)
            echo "Usage: $0 {add [--movie] <mega_link_or_local_path> <target_dir> | run | status [--json] | halt | resume <id>|--all}" >&2
            exit 1
            ;;
    esac
fi
