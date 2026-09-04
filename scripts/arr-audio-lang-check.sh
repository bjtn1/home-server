#!/bin/bash
# Sonarr/Radarr "On Import"/"On Upgrade" custom script.
#
# The "Spanish Audio" custom format only regexes the *release title* --
# it can't see what's actually muxed inside the file. A release whose
# title never mentions dialect at all (no "latino", no anything) sails
# straight through with a Latin-American/neutral Spanish track undetected,
# since the negate-latino check has nothing to match against (found via
# Courage the Cowardly Dog's "Bigfoot" episode, 2026-09-03 -- episodes 1-2
# had a correctly-labeled European Spanish track, episode 3's title gave
# no hint and imported a Latino track instead). This runs after the file
# actually lands, inspects the real audio track language/title tags via
# ffprobe, and pings ntfy if it finds Latin-American/neutral Spanish --
# the only place the dialect is ever reliably labeled.
#
# Needs ffprobe + jq inside the Sonarr/Radarr container (Alpine-based
# linuxserver images ship with neither). In docker-compose.yml for both
# services:
#   DOCKER_MODS=linuxserver/mods:universal-package-install
#   INSTALL_PACKAGES=ffmpeg|jq
# and mount this scripts/ dir into the container (e.g. ../scripts:/scripts:ro),
# then add it as a Custom Script connection (Settings -> Connect) triggered
# on "On Import" and "On Upgrade", path /scripts/arr-audio-lang-check.sh.
#
# Notify-only by design -- never touches the imported file. Auto-deleting
# risks acting on a false positive (e.g. a legit Castilian track whose
# embedded title happens to mention an unrelated region); a human should
# confirm before anything gets removed or blocklisted.
set -uo pipefail

NTFY_URL="https://ntfy.bjtn.xyz/homelab-alerts"

# Kept in sync with the "Spanish Audio" custom format's negate-latino
# regex in Sonarr/Radarr (updated 2026-09-03 -- see that custom format for
# the canonical version if this drifts).
LATAM_PATTERN='latino|lat[.-]?am|latin[.-]?america|es[-]?419|mexic|hispanoamerican|neutro|neutral'

if [[ -n "${sonarr_eventtype:-}" ]]; then
    event="$sonarr_eventtype"
    file="${sonarr_episodefile_path:-}"
    label="${sonarr_series_title:-unknown series} - S${sonarr_episodefile_seasonnumber:-?}E${sonarr_episodefile_episodenumbers:-?}"
elif [[ -n "${radarr_eventtype:-}" ]]; then
    event="$radarr_eventtype"
    file="${radarr_moviefile_path:-}"
    label="${radarr_movie_title:-unknown movie} (${radarr_movie_year:-?})"
else
    echo "arr-audio-lang-check: no sonarr_/radarr_ env vars set -- not running as an *arr custom script, exiting"
    exit 0
fi

# "Download" is the *arr event name for an actual import (initial or
# upgrade) -- ignore Grab, Rename, Test, health checks, etc.
if [[ "$event" != "Download" ]]; then
    exit 0
fi

if [[ -z "$file" || ! -f "$file" ]]; then
    echo "arr-audio-lang-check: no importable file for event=$event -- exiting"
    exit 0
fi

if ! command -v ffprobe >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
    echo "arr-audio-lang-check: ffprobe/jq missing -- add DOCKER_MODS=linuxserver/mods:universal-package-install and INSTALL_PACKAGES=ffmpeg,jq to this container" >&2
    exit 0
fi

streams=$(ffprobe -v quiet -print_format json -show_entries stream=index:stream_tags=language,title -select_streams a -- "$file" 2>/dev/null)
[[ -z "$streams" ]] && exit 0

flagged=$(jq -r --arg pat "$LATAM_PATTERN" '
  .streams[]?
  | (.tags.language // "" | ascii_downcase) as $lang
  | (.tags.title // "") as $title
  | select(($lang | startswith("spa")) or ($title | ascii_downcase | test("span|espa")))
  | select(($lang | test($pat; "i")) or ($title | test($pat; "i")))
  | (if $title != "" then $title else (if $lang != "" then $lang else ("stream " + (.index | tostring)) end) end)
' <<<"$streams" 2>/dev/null | paste -sd '|' -)

if [[ -n "$flagged" ]]; then
    msg="Latin-American/neutral Spanish audio detected: $label -- track(s): $flagged -- $file"
    echo "arr-audio-lang-check: $msg"
    curl -fsS -m 10 \
        -H "Title: Wrong Spanish dub imported" \
        -H "Priority: 4" \
        -H "Tags: warning" \
        -d "$msg" \
        "$NTFY_URL" >/dev/null 2>&1
else
    echo "arr-audio-lang-check: no Latino/neutral Spanish audio flagged for $label"
fi

exit 0
