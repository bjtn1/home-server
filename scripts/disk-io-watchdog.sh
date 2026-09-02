#!/bin/bash
# Watches the kernel log for repeated "I/O error, dev sdX" lines (the
# failure signature hit twice on 2026-08-23 -- see media-drive-incident
# memory). On a real burst (not a single transient blip) on a device the
# WRITERS actually depend on, stops those containers -- an idempotent,
# low-risk action already proven safe by hand twice -- and pushes a
# down/alert status to a Kuma Push monitor so it's visible without anyone
# needing to notice mid-incident. Does NOT attempt automated recovery
# (unmount/USB-reauthorize/remount) -- that sequence is validated by hand
# exactly once and isn't trusted to run unsupervised yet.
#
# Device scoping (added 2026-09-02): confirmed during the 2026-09-01
# incident that every I/O error that day was actually on an unrelated
# backup SSD, never on vault/games -- yet the original any-sdX-letter match
# stopped the writers anyway, for hardware they don't even use. Device
# letters for vault/games are resolved fresh on every check (fstab UUID ->
# blkid), not hardcoded, since USB reconnects/hub resets can and do
# reassign sdX letters.
set -uo pipefail

CONF=/home/bjtn/docker/scripts/disk-io-watchdog.conf
# shellcheck source=/etc/disk-io-watchdog.conf
source "$CONF"

STATE_DIR=/var/lib/disk-io-watchdog
STATE_FILE="$STATE_DIR/last-alert"
mkdir -p "$STATE_DIR"

log() { echo "disk-io-watchdog: $1"; }

# Prints the current base kernel device name (e.g. "sdb") for each
# configured mountpoint, resolved via its fstab UUID -> blkid, fresh every
# call. A mountpoint that can't be resolved (missing from fstab, or not
# currently present on any device) is skipped with a warning rather than
# treated as a wildcard match.
resolve_watched_devices() {
    local mp uuid dev
    for mp in $WATCHED_MOUNTPOINTS; do
        uuid=$(awk -v mp="$mp" '$1 ~ /^UUID=/ && $2==mp {sub(/^UUID=/,"",$1); print $1}' /etc/fstab)
        if [ -z "$uuid" ]; then
            log "WARNING: no UUID entry in /etc/fstab for $mp -- cannot watch it"
            continue
        fi
        dev=$(blkid -U "$uuid" 2>/dev/null)
        if [ -z "$dev" ]; then
            log "WARNING: $mp (UUID=$uuid) is not currently present on any device"
            continue
        fi
        basename "$dev" | sed -E 's/[0-9]+$//'
    done
}

seconds_since_last_alert() {
    if [ -f "$STATE_FILE" ]; then
        echo $(( $(date +%s) - $(cat "$STATE_FILE") ))
    else
        echo 999999999
    fi
}

push_kuma() {
    local msg="$1"
    if [ -z "${KUMA_PUSH_URL:-}" ]; then
        log "KUMA_PUSH_URL not configured in $CONF -- skipping push (create a Push monitor in Kuma first)"
        return
    fi
    curl -s -G "$KUMA_PUSH_URL" \
        --data-urlencode "status=down" \
        --data-urlencode "msg=$msg" \
        >/dev/null 2>&1 || log "failed to reach Kuma push URL"
}

handle_incident() {
    local device="$1"
    local count="$2"
    log "INCIDENT: $count I/O errors on dev $device (a watched device) within ${WINDOW_SECONDS}s -- stopping writers"

    # Idempotent -- safe to re-run even if already stopped.
    # shellcheck disable=SC2086
    docker stop $WRITERS >/dev/null 2>&1
    log "stopped: $WRITERS"

    local since
    since=$(seconds_since_last_alert)
    if [ "$since" -ge "$COOLDOWN_SECONDS" ]; then
        push_kuma "I/O errors on /dev/$device ($count in ${WINDOW_SECONDS}s). Stopped: $WRITERS. Manual recovery needed (unmount, replug/USB-reauthorize the drive, remount, restart containers) -- see media-drive-incident memory."
        date +%s > "$STATE_FILE"
        log "alert pushed to Kuma (cooldown reset)"
    else
        log "within cooldown (${since}s < ${COOLDOWN_SECONDS}s since last alert) -- containers re-stopped but no duplicate alert"
    fi
}

watched_now=$(resolve_watched_devices | tr '\n' ' ')
log "started, watching kernel log for I/O errors on: ${watched_now:-<none resolved>} (threshold=$ERROR_THRESHOLD/$WINDOW_SECONDS s, cooldown=${COOLDOWN_SECONDS}s)"

declare -a timestamps=()

journalctl -k -f -n 0 -o cat 2>/dev/null | grep --line-buffered -E "I/O error, dev sd[a-z]" | while IFS= read -r line; do
    now=$(date +%s)
    device=$(echo "$line" | grep -oE "dev sd[a-z]" | awk '{print $2}')

    # Re-resolve on every line -- cheap, and correct even if a USB
    # reconnect reshuffled letters since the last check.
    mapfile -t watched < <(resolve_watched_devices)
    is_watched=0
    for w in "${watched[@]:-}"; do
        [ "$device" = "$w" ] && is_watched=1 && break
    done

    if [ "$is_watched" -eq 0 ]; then
        log "I/O error on dev $device -- not a watched device (${watched[*]:-none resolved}), ignoring"
        continue
    fi

    timestamps+=("$now")
    # prune entries older than the window
    new_timestamps=()
    for t in "${timestamps[@]}"; do
        if [ $(( now - t )) -le "$WINDOW_SECONDS" ]; then
            new_timestamps+=("$t")
        fi
    done
    timestamps=("${new_timestamps[@]}")

    count=${#timestamps[@]}
    if [ "$count" -ge "$ERROR_THRESHOLD" ]; then
        handle_incident "$device" "$count"
        timestamps=()
    fi
done
