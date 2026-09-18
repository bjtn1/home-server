#!/bin/bash
# Run this by hand after you've actually fixed a real disk-io-watchdog
# incident (unmounted, replugged/USB-reauthorized the drive, remounted,
# restarted the stopped containers) -- see media-drive-incident memory for
# that recovery sequence.
#
# Why this exists: clears the local alert-cooldown state, so a second,
# unrelated incident later doesn't get silently suppressed by an old
# cooldown window and actually notifies you again.
set -uo pipefail

STATE_FILE=/var/lib/disk-io-watchdog/last-alert

if [ -f "$STATE_FILE" ]; then
    rm -f "$STATE_FILE"
    echo "disk-io-watchdog-resolved: cleared local alert-cooldown state -- next real incident will alert immediately"
else
    echo "disk-io-watchdog-resolved: no local cooldown state to clear"
fi
