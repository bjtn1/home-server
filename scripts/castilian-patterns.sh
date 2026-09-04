# Shared regex constants for telling Castilian (European) Spanish audio
# apart from Latin-American/neutral Spanish, sourced by mux-castilian-audio.sh,
# arr-audio-lang-check.sh, and archive-castilian-audio.sh so the three bash
# copies can't independently drift out of sync with each other (2026-09-04 --
# pulled out after an unrelated drift, the Sonarr/Radarr cutoffFormatScore
# issue, caused real problems the same night this was written).
#
# A 4th copy of this same regex lives in the "Spanish Audio" custom format
# in Sonarr/Radarr itself (release-title matching) -- that one can't source
# a bash file (it's stored in their DB via API), so it stays manually
# synced. If you change these patterns, update that custom format too.
#
# Not a function/logic file -- just the two string constants. Intentionally
# minimal: sharing detection *logic* (find_spanish_track() etc.) across
# scripts would risk regressions in already-tested code for a cosmetic win;
# sharing these two plain strings carries no such risk.

LATAM_PATTERN='latino|lat[.-]?am|latin[.-]?america|es[-]?419|mexic|hispanoamerican|neutro|neutral'
CASTILIAN_PATTERN='castellano|castilian|european|espa.a|peninsular|iberian'
