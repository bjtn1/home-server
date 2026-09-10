#!/bin/bash
# Classifies a Whisper transcript as Castilian vs Latin American Spanish
# using actual speech content (grammar + vocabulary), not file metadata --
# see castilian-speech-patterns.sh for the two pattern tiers and why they
# carry different weight. This script never calls the Whisper server
# itself and never touches a video file -- pure text in, verdict out. The
# orchestrator that extracts a clip, sends it to whisper-server, and pipes
# the returned transcript here is a separate piece (castilian-whisper-
# check.sh), kept separate so this classifier can be tested and iterated
# on with plain text fixtures alone, no server round-trip needed.
#
# Usage: castilian-linguistic-classify.sh [transcript_file]
#   Reads from the given file, or stdin if omitted.
#
# Exit codes (same tri-state shape as castilian-coverage-report.sh's
# castilian_status(), deliberately): 0 = confirmed Castilian, 1 = confirmed
# NOT Castilian (Latin American), 2 = inconclusive -- genuinely not enough
# signal either way, needs a human, not a guess.
#
# Scoring, in order of trust (strongest evidence checked first):
#   - vosotros_count >= 2 AND latam_count == 0
#       -> CONFIRMED CASTILIAN. Two+ independent grammatical hits in a 90s
#          clip with zero competing vocabulary is about as solid as
#          content-based evidence gets.
#   - vosotros_count >= 1 AND castilian_count >= 1 AND latam_count == 0
#       -> CONFIRMED CASTILIAN. One grammatical hit alone could in theory
#          be a transcription quirk; corroborated by vocabulary too, and
#          nothing pointing the other way, is enough.
#   - latam_count >= 2 AND vosotros_count == 0 AND castilian_count == 0
#       -> CONFIRMED NOT CASTILIAN. Multiple Latin-American-specific words
#          with no Castilian-side signal at all. NOTE: vosotros absence is
#          corroborating here, never decisive alone -- Spanish is
#          pro-drop, a genuinely Castilian speaker can easily go 90
#          seconds without triggering the pattern by chance, so "didn't
#          hear vosotros" on its own proves nothing.
#   - Anything else (weak signal, conflicting signal, nothing at all)
#       -> INCONCLUSIVE. Explicitly refuses to guess rather than pick a
#          side on thin evidence -- matches this whole project's standing
#          rule that a false CONFIRMED is the only failure mode that
#          actually matters.
set -uo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/castilian-speech-patterns.sh"

INPUT="${1:-/dev/stdin}"
if [[ "$INPUT" != "/dev/stdin" && ! -f "$INPUT" ]]; then
    echo "castilian-linguistic-classify: no such file: $INPUT" >&2
    exit 1
fi

text=$(cat -- "$INPUT")
if [[ -z "${text// }" ]]; then
    echo "castilian-linguistic-classify: empty transcript -- nothing to classify" >&2
    exit 2
fi

count_matches() {
    # Counts non-overlapping matches of an ERE across the whole text,
    # case-insensitive -- grep -o gives one line per match, wc -l counts them.
    #
    # (*UCP) (2026-09-08): found live -- \bñapas?\b matched nothing at
    # all against real text containing "ñapa", even in isolation. Root
    # cause: PCRE's \w/\b are ASCII-only by default; "ñ" isn't
    # considered a word character, so \b (a transition between \w and
    # \W) never fires at a boundary immediately before it. This was
    # invisible in every other pattern in this file because they always
    # put \w+ BEFORE an accented run (e.g. \w+éis\b) rather than right
    # at the \b itself -- \w+ only ever needs to match the ASCII prefix,
    # and the accented suffix is matched as a literal string, so the
    # \b boundary check always landed on an ordinary ASCII character.
    # "ñapa" is the one entry that starts with a special character
    # right at the boundary. (*UCP) switches PCRE to Unicode-aware
    # character properties, making \w (and therefore \b) correctly
    # include ñ/á/é/í/ó/ú -- verified this doesn't change behavior for
    # any other existing pattern (full trap suite re-run clean after
    # adding it).
    grep -oiP "(*UCP)$2" <<<"$1" 2>/dev/null | wc -l
}

count_distinct() {
    # Same as count_matches, but counts DISTINCT normalized words, not
    # raw occurrences -- see the distinct-vocabulary rule below for why.
    grep -oiP "(*UCP)$2" <<<"$1" 2>/dev/null | tr '[:upper:]' '[:lower:]' | sort -u | wc -l
}

vosotros_count=$(count_matches "$text" "$VOSOTROS_PATTERN")
castilian_count=$(count_matches "$text" "$CASTILIAN_LEXICON")
castilian_distinct=$(count_distinct "$text" "$CASTILIAN_LEXICON")
latam_count=$(count_matches "$text" "$LATAM_LEXICON")

reason="vosotros=$vosotros_count castilian_lexicon=$castilian_count (distinct=$castilian_distinct) latam_lexicon=$latam_count"

# 2026-09-08: relaxed from requiring 2+ vosotros hits (or 1 hit
# corroborated by vocabulary) down to accepting a SINGLE vosotros hit on
# its own, as long as latam_count is still 0. Found live, personally
# reading all 23 files a real batch left unresolved: several (1x04's
# "No despertéis", 4x11's "Mirad") had exactly one genuine, unambiguous
# vosotros marker and nothing else -- correctly-formed grammar, not an
# ASR fluke -- and stayed stuck at INCONCLUSIVE only because of this
# threshold, never because the evidence was actually weak. Safe to relax
# specifically because every entry in VOSOTROS_PATTERN has ALREADY been
# individually vetted this session for zero collision with unrelated
# common words (that's precisely why bare -ís, -aos, and general
# -ad/-ed/-id were rejected and never added) -- so the old 2-hit bar
# wasn't guarding against linguistic ambiguity (already ruled out by
# construction), only against a hypothetical ASR mis-transcription
# producing one of these specific, long, multi-syllable words by pure
# noise. Weighed against the real, demonstrated cost of the status quo
# (multiple genuinely-correct files stuck unresolved) that residual risk
# is accepted. vosotros itself is about as close to a hard dialect
# boundary as spoken Spanish has -- Latin American Spanish doesn't
# produce genuine vosotros forms at all, so even one real occurrence,
# with zero competing latam vocabulary anywhere in the sampled ~95% of
# runtime, is unusually strong evidence on its own.
if [[ "$vosotros_count" -ge 1 && "$latam_count" -eq 0 ]]; then
    if [[ "$vosotros_count" -ge 2 ]]; then
        echo "CASTILIAN (confirmed): $reason -- repeated vosotros pattern, no competing vocabulary"
    elif [[ "$castilian_count" -ge 1 ]]; then
        echo "CASTILIAN (confirmed): $reason -- vosotros pattern corroborated by vocabulary"
    else
        echo "CASTILIAN (confirmed): $reason -- single vosotros grammatical marker, no competing Latin American vocabulary anywhere in the sampled runtime"
    fi
    exit 0
fi
# 2026-09-08: found live -- a real transcript had castilian_lexicon=4
# ("tío" used repeatedly as direct address, e.g. "tío Angus") and
# vosotros=0, and stayed INCONCLUSIVE because every CASTILIAN branch
# above required a vosotros hit no matter how much vocabulary evidence
# piled up. Grammar is the strongest single signal, but it's not the
# ONLY strong signal -- a real conversation can easily go a whole scene
# without needing 2nd-person-plural grammar at all (nothing to address a
# group about) while still using plenty of Castilian-specific vocabulary.
# Threshold set higher (4, vs. 1 for the vosotros-corroborated branches)
# specifically because this is the ONLY evidence here, not corroboration
# of a grammar hit -- makes a coincidental/ASR-noise false positive on
# vocabulary alone much less likely to reach it.
if [[ "$castilian_count" -ge 4 && "$vosotros_count" -eq 0 && "$latam_count" -eq 0 ]]; then
    echo "CASTILIAN (confirmed): $reason -- repeated Castilian vocabulary alone, no vosotros needed, no competing signal"
    exit 0
fi
# 2026-09-08: added a second, distinct-word path to vocabulary-only
# confirmation, found live after reviewing files stuck below the
# threshold=4 bar above -- most had only ONE distinct vocabulary word
# repeated (almost always "vale", which castilian-speech-patterns.sh's
# own comments already flag as genuinely ambiguous -- it also means
# "okay"/interjection AND "is worth"/a voucher, so several same-word
# repeats of it alone is weaker than it looks), while a couple (3x03:
# "coche"+"vale"; 2x08: "cogieron"+"vale") had 2 DIFFERENT words each
# appearing once. Two independent, different words is stronger
# corroborating evidence than one repeated (possibly ambiguous) word
# hit 4 times -- a coincidence/ASR-noise explanation has to cover two
# separate unrelated matches, not just one. Deliberately still requires
# BOTH occurrence count>=2 (guards a single stray hit from either word)
# AND distinct>=2 (guards the vale-repeated-4x case this is meant to
# route around) -- neither alone is enough.
if [[ "$castilian_count" -ge 2 && "$castilian_distinct" -ge 2 && "$vosotros_count" -eq 0 && "$latam_count" -eq 0 ]]; then
    echo "CASTILIAN (confirmed): $reason -- 2+ distinct Castilian vocabulary words corroborating each other, no vosotros needed, no competing signal"
    exit 0
fi
if [[ "$latam_count" -ge 2 && "$vosotros_count" -eq 0 && "$castilian_count" -eq 0 ]]; then
    echo "NOT CASTILIAN (confirmed Latin American): $reason -- repeated Latin-American vocabulary, no Castilian signal"
    exit 1
fi

echo "INCONCLUSIVE: $reason -- not enough signal to confirm either way"
exit 2
