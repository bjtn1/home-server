#!/bin/bash
# Radarr/Sonarr don't run recurring "search for missing content" or "search
# for cutoff-unmet upgrades" tasks by design -- RSS Sync only catches
# releases as they're freshly posted, it never re-scans an indexer's
# historical catalog. Content that's fully missing (nothing downloaded) or
# that was imported at a suboptimal language/quality score (e.g. missing
# the es/en target) can sit stuck indefinitely unless something explicitly
# triggers a full catalog search. This runs both search types periodically
# so neither requires manual intervention (found via the Spider-Man: No Way
# Home ita/eng-only import, and Detective Conan sitting fully missing, both
# 2026-08-24).
#
# Overlap-safety (added 2026-09-02): the library is large enough (3300+
# missing Sonarr episodes) that a single full-library search can take far
# longer than the 6h timer interval -- confirmed live, a MissingEpisodeSearch
# ran 9+ hours without finishing. Firing blindly every 6h stacked concurrent
# overlapping searches indefinitely (3 simultaneous Sonarr search commands
# observed at once, none of them mutually exclusive), which measurably
# degraded indexer health -- Prowlarr had auto-disabled Torrent9 and
# LimeTorrents days before this was diagnosed, from repeated failures under
# the load. Each trigger below now checks the app's own command queue first
# and skips firing if the same search type is already in flight, so runs
# only ever queue up behind themselves one at a time rather than stacking.
#
# That remote check alone has a TOCTOU race if the *script itself* is ever
# invoked twice concurrently (e.g. a manual run overlapping the timer, or
# an OnBootSec catch-up firing close to a regular cycle): both instances
# can see "nothing running yet" during their GET before either has POSTed,
# and both fire -- confirmed live against a mock server 2026-09-02 (two
# concurrent runs both got a 201, not one skipping). A local flock closes
# this: only one instance of this script can ever be mid-run on this host,
# so the remote check never has to race against a sibling of itself.
set -uo pipefail

LOCKFILE="/tmp/arr-cutoff-search.lock"
KUMA_PUSH_URL="https://kuma.bjtn.xyz/api/push/0r50felYTg"
exec 9>"$LOCKFILE"
flock -n 9 || { echo "arr-cutoff-search: already running (lock held), exiting"; exit 0; }

RADARR_URL="https://radarr.bjtn.xyz"
RADARR_KEY="${RADARR_KEY:?RADARR_KEY not set -- set via the systemd unit Environment=}"
SONARR_URL="https://sonarr.bjtn.xyz"
SONARR_KEY="${SONARR_KEY:?SONARR_KEY not set -- set via the systemd unit Environment=}"

FAILED=0

log() { echo "arr-cutoff-search: $1"; }

# True (exit 0) if a command with this name is already queued or running.
already_running() {
    local url="$1" key="$2" command="$3"
    local resp
    resp=$(curl -s -m 30 -H "X-Api-Key: $key" "$url/api/v3/command") || return 1
    echo "$resp" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for c in data:
    if c.get('name') == '$command' and c.get('status') in ('started', 'queued'):
        sys.exit(0)
sys.exit(1)
" 2>/dev/null
}

trigger() {
    local name="$1" url="$2" key="$3" command="$4"
    local raw http_code body status

    if already_running "$url" "$key" "$command"; then
        log "$name ($command): SKIPPED - already running from a previous cycle"
        return
    fi

    raw=$(curl -s -m 30 -w '\n%{http_code}' -X POST -H "X-Api-Key: $key" -H "Content-Type: application/json" \
        -d "{\"name\":\"$command\"}" "$url/api/v3/command")
    http_code=$(echo "$raw" | tail -1)
    body=$(echo "$raw" | sed '$d')

    if [ "$http_code" != "201" ]; then
        log "$name ($command): FAILED - HTTP ${http_code:-timeout/unreachable}: $(echo "$body" | head -c 200)"
        FAILED=1
        return
    fi

    status=$(echo "$body" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null) || status="parse-error"
    if [ "$status" = "parse-error" ]; then
        log "$name ($command): FAILED - HTTP 201 but couldn't parse response body: $(echo "$body" | head -c 200)"
        FAILED=1
    else
        log "$name ($command): $status"
    fi
}

trigger "Radarr" "$RADARR_URL" "$RADARR_KEY" "MissingMoviesSearch"
trigger "Radarr" "$RADARR_URL" "$RADARR_KEY" "CutoffUnmetMoviesSearch"
trigger "Sonarr" "$SONARR_URL" "$SONARR_KEY" "MissingEpisodeSearch"
trigger "Sonarr" "$SONARR_URL" "$SONARR_KEY" "CutoffUnmetEpisodeSearch"

if [ "$FAILED" -eq 1 ]; then
    curl -fsS -m 10 "$KUMA_PUSH_URL?status=down&msg=one+or+more+triggers+failed" >/dev/null 2>&1
    exit 1
fi
curl -fsS -m 10 "$KUMA_PUSH_URL?status=up&msg=OK" >/dev/null 2>&1
exit 0
