#!/bin/bash
# Run this by hand after you've actually fixed a real disk-io-watchdog
# incident (unmounted, replugged/USB-reauthorized the drive, remounted,
# restarted the stopped containers) -- see media-drive-incident memory for
# that recovery sequence.
#
# Why this exists: the Kuma Push monitor backing disk-io-watchdog only ever
# gets pushed "down" (on a real incident) -- it never gets pushed "up" on
# its own, since disk-io-watchdog.sh doesn't attempt automated recovery or
# verify anything got fixed. Without this, the monitor (and its ntfy alert)
# would sit "down" forever after the first real incident, and a second,
# unrelated incident later would just silently extend an already-alarmed
# state instead of firing a fresh alert. This resets both the Kuma status
# and the local alert-cooldown state, so the next real incident starts
# clean and actually notifies you again.
set -uo pipefail

CONF=/home/bjtn/docker/scripts/disk-io-watchdog.conf
# shellcheck source=/etc/disk-io-watchdog.conf
source "$CONF"

STATE_FILE=/var/lib/disk-io-watchdog/last-alert

if [ -z "${KUMA_PUSH_URL:-}" ]; then
    echo "disk-io-watchdog-resolved: KUMA_PUSH_URL not configured in $CONF -- nothing to reset in Kuma"
else
    resp=$(curl -s -m 15 -w '\n%{http_code}' -G "$KUMA_PUSH_URL" \
        --data-urlencode "status=up" \
        --data-urlencode "msg=Incident manually resolved via disk-io-watchdog-resolved.sh")
    http_code=$(echo "$resp" | tail -1)
    if [ "$http_code" = "200" ]; then
        echo "disk-io-watchdog-resolved: pushed 'up' to Kuma successfully"
    else
        echo "disk-io-watchdog-resolved: FAILED to push to Kuma (HTTP ${http_code:-unreachable}) -- check KUMA_PUSH_URL / connectivity"
    fi
fi

if [ -f "$STATE_FILE" ]; then
    rm -f "$STATE_FILE"
    echo "disk-io-watchdog-resolved: cleared local alert-cooldown state -- next real incident will alert immediately"
else
    echo "disk-io-watchdog-resolved: no local cooldown state to clear"
fi
