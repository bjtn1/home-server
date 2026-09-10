#!/bin/bash
# Ties the pieces together into one content-based Castilian/Latin-American
# verdict for one audio track: extract a clip (castilian-extract-clip.sh,
# local ffmpeg) -> transcribe it (whisper-server, over the network -- the
# only network call in this chain, and the only step that costs real GPU
# time) -> classify the transcript (castilian-linguistic-classify.sh,
# local, free). Built as the Whisper-based fallback for files where
# metadata (language/language_ietf/title) gives no dialect signal at all.
#
# Usage:
#   castilian-whisper-check.sh <video_file> <audio_relative_index>
#
#   audio_relative_index -- see castilian-extract-clip.sh's header for why
#   this is ffmpeg's 0-based audio-relative index, NOT mkvmerge's `id`.
#
# Env:
#   WHISPER_SERVER_URL   default http://100.117.170.34:8178
#   CASTILIAN_VERDICT_CACHE_DIR default /mnt/vault/mega-staging/queue/
#                        castilian-verdict-cache -- every real check
#                        (any verdict, 0-3) writes a permanent record
#                        here: <hash>.verdict, <hash>.transcript.log,
#                        <hash>.json (source path/audio idx/timestamp).
#                        The castilian-control review page reads from
#                        here, and writes <hash>.human_verdict.json when
#                        a person gives their final call -- checked
#                        FIRST, before anything else, and wins outright
#                        if present (this script only ever reads that
#                        file, never writes it). Failing that, a
#                        decisive (0/1) cached verdict for the same
#                        resolved source path + audio index skips
#                        re-running entirely (pure duplicate-run
#                        protection).
#   ANTHROPIC_API_KEY    required for the LLM fallback tier (see below);
#                        no default, fallback is skipped (mechanical
#                        result stands) if unset
#   ANTHROPIC_MODEL      default claude-sonnet-5
#   DISABLE_LLM_FALLBACK default false -- set true to force mechanical-
#                        only results (e.g. for testing without API cost)
#
# Exit codes: 0 confirmed Castilian, 1 confirmed not, 2 inconclusive (got
# real transcript content across every attempt, never enough dialect
# signal), 3 check failed (never got usable content at all -- extraction
# failed every time, server unreachable, or every attempt's transcript was
# too hallucination-corrupted to salvage even after truncation).
#
# 2026-09-08, rewritten after a real 49-file batch against known-Castilian
# content came back only 3/49 confirmed -- not because the content wasn't
# Castilian (it was, independently human-verified) but because of two
# compounding, fixable problems with the FIRST version of this script:
#
#  1. A single fixed clip window (one offset, one attempt) means real
#     content that happens to land outside that window is never seen at
#     all. Fix: try up to 3 windows spread across the episode (early/
#     middle/late), stopping as soon as one attempt gives a decisive
#     verdict. Non-overlapping offsets, not re-samples of the same
#     stretch, so a bad draw at one point doesn't doom every attempt.
#
#  2. Whisper's repetition-loop hallucinations (see castilian-extract-
#     clip.sh and earlier testing -- these dubs have frequent musical
#     numbers, a common trigger) used to mean throwing away the ENTIRE
#     transcript the moment a loop was detected -- even when real,
#     genuine dialogue preceded the loop by hundreds of real words (seen
#     directly in testing: "El rey del flan" had ~250 real words of
#     dialogue before degenerating into "¡Suscríbete al canal!" x263).
#     Fix: truncate_point() finds the line where a loop first starts
#     recurring and keeps only the real content strictly before it,
#     instead of discarding the whole attempt. Whisper's own segment-per-
#     line output made every hallucination instance actually observed
#     (4 distinct cases across testing) a LINE-level repeat -- either the
#     same line verbatim, or two lines alternating -- so line-frequency
#     is what's checked, not a generic n-gram scan; simpler, and matches
#     what was actually seen going wrong.
set -uo pipefail

WHISPER_SERVER_URL="${WHISPER_SERVER_URL:-http://100.117.170.34:8178}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# A normalized line (case-folded, inverted punctuation stripped) recurring
# this many times anywhere in one attempt's transcript marks where a
# hallucination loop starts -- everything from its first occurrence
# onward is discarded, everything before it is kept and classified.
LINE_REPEAT_THRESHOLD="${LINE_REPEAT_THRESHOLD:-3}"
# After truncation (or if there was nothing to truncate), an attempt needs
# at least this many words of real transcript to be worth classifying --
# below this, "no markers found" would mean "there was barely anything to
# find markers in", not real evidence of anything.
MIN_WORDS="${MIN_WORDS:-15}"
# Offsets (fraction of total runtime) for successive attempts -- spread
# across early/middle/late rather than re-rolling the same stretch, so one
# bad draw (a musical number, a low-dialogue scene) doesn't cost every
# attempt. Capped at 3: diminishing returns past that for the added
# compute, per testing.
OFFSETS=(0.15 0.45 0.75)
CLIP_DUR_PER_ATTEMPT="${CLIP_DUR_PER_ATTEMPT:-200}"
# Escalation tier duration -- see its use far below. 600s from offset
# 0.05 and again from 0.55 covers close to the whole episode (a typical
# ~1300-1400s episode here) across just 2 extra attempts, only spent on
# files the 3 standard attempts didn't resolve.
ESCALATION_DUR="${ESCALATION_DUR:-600}"

for cmd in curl jq; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "castilian-whisper-check: missing required command: $cmd" >&2; exit 3; }
done

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <video_file> <audio_relative_index>" >&2
    exit 3
fi
VIDEO="$1"
AUDIO_IDX="$2"

log() { echo "[$(date '+%F %T')] castilian-whisper-check: $*" >&2; }

# 2026-09-08: every attempt's usable transcript gets appended here too,
# regardless of that attempt's own individual verdict -- found live: a
# real file had one lexicon hit in one window and a different lexicon hit
# in another, each alone below the confirm threshold, never combined
# since each attempt was classified in isolation. If nothing resolves
# per-attempt, one final pass classifies everything gathered together --
# same thresholds, same conservatism, just not artificially blind to
# evidence that happened to land in different windows.
COMBINED="$(mktemp)"
cleanup_combined() { rm -f -- "$COMBINED"; }
trap cleanup_combined EXIT

# 2026-09-08: tracks the richest (most words) attempt's offset as this
# check runs -- found live, the review page's first version picked a
# blind fixed offset (30% in, 25s) for every track's playback snippet,
# with no idea whether that spot actually has spoken dialogue versus
# theme music, silence, or a stray exclamation -- exactly the kind of
# useless snippet that defeats a human reviewer's whole purpose. Every
# attempt already made here that clears MIN_WORDS is proof positive that
# offset has real, substantial dialogue (that's what MIN_WORDS already
# gates on) -- picking the one with the MOST words biases toward a
# richer, more useful sample, not just "any" usable one. Recorded as an
# offset only, not a duration -- the review page always pulls its own
# short, human-listening-appropriate duration starting there, not
# whatever (possibly much longer, escalation-tier) window this pipeline
# itself used for transcription.
BEST_SNIPPET_OFFSET=""
BEST_SNIPPET_WORDS=0

# 2026-09-08: permanent, durable record of every real check -- promoted
# out of an ad-hoc scratch cache used only during this script's own
# development/testing into the script itself, so every actual
# production run leaves a record (verdict + full transcript + source
# path), not just ones I happened to be testing with. This is what
# castilian-control's review page (built the same day) lists both its
# UNRESOLVED and CONFIRMED buckets from -- it never re-derives anything,
# it reads these files directly.
#
# Keyed by a hash of the resolved source path + audio index, not a
# sanitized filename -- a filename-based key (what the scratch version
# used) risks a real collision between two same-named files living in
# different directories, and needs no sanitizing no matter what
# characters or length the real path has. The resolved path itself is
# never reconstructed or guessed back from the key -- it's always read
# straight from the .json sidecar.
CACHE_DIR="${CASTILIAN_VERDICT_CACHE_DIR:-/mnt/vault/mega-staging/queue/castilian-verdict-cache}"
mkdir -p "$CACHE_DIR" 2>/dev/null
RESOLVED_VIDEO="$(readlink -f -- "$VIDEO" 2>/dev/null || echo "$VIDEO")"
CACHE_KEY="$(printf '%s::%s' "$RESOLVED_VIDEO" "$AUDIO_IDX" | sha256sum | cut -d' ' -f1)"

# Human override (2026-09-08) -- if castilian-control's review page has
# already recorded a person's final call for this exact source+track,
# that wins outright, checked before anything else below (including the
# ordinary decisive-cache-skip right after this) -- a human's explicit
# override should never be shadowed by an older, possibly-wrong
# mechanical verdict this same script once produced. This is what lets
# "mark CASTILIAN in the review page" actually mean something: the
# review page moves the source file back into the drop-zone, and the
# NEXT time castilian-drop-scan.sh's normal pipeline reaches it (metadata
# still inconclusive, same as before), it calls this script again --
# which now returns the human's verdict instantly instead of re-running
# the whole extract/transcribe/classify pipeline, and drop-scan's
# already-tested lookup/queue logic takes it from there unchanged.
# Strictly read-only from this side: this script writes its OWN verdict/
# transcript/json into $CACHE_DIR, but <hash>.human_verdict.json is
# exclusively the review page's file to write -- the boundary only ever
# goes one direction, so a later re-run here can never silently
# overwrite what a person already decided.
HUMAN_VERDICT_FILE="$CACHE_DIR/${CACHE_KEY}.human_verdict.json"
if [[ -f "$HUMAN_VERDICT_FILE" ]]; then
    human_verdict="$(python3 -c "
import json, sys
try:
    print(json.load(open(sys.argv[1])).get('verdict', ''))
except Exception:
    pass
" "$HUMAN_VERDICT_FILE" 2>/dev/null)"
    case "$human_verdict" in
        castilian)
            log "human override found (castilian) -- trusting it, skipping the check entirely"
            exit 0
            ;;
        not_castilian)
            log "human override found (not_castilian) -- trusting it, skipping the check entirely"
            exit 1
            ;;
    esac
fi

# Skip re-running entirely if this exact source+track already has a
# decisive (0/1) verdict cached -- pure duplicate-run protection (the
# same file dropped or queued twice by accident). The human-override
# check just above already runs first every time, so this can never
# shadow a person's final call -- reaching this point at all already
# means there either isn't one, or (structurally impossible given the
# check above always exits first) it would already have been trusted.
if [[ -f "$CACHE_DIR/${CACHE_KEY}.verdict" ]]; then
    cached_verdict="$(cat -- "$CACHE_DIR/${CACHE_KEY}.verdict" 2>/dev/null)"
    if [[ "$cached_verdict" == "0" || "$cached_verdict" == "1" ]]; then
        log "already checked (cached verdict $cached_verdict) -- skipping re-run for $VIDEO track $AUDIO_IDX"
        exit "$cached_verdict"
    fi
fi

# The one place every real code path below funnels through -- writes
# (or overwrites) this check's permanent cache entry, then exits with
# the given verdict. $COMBINED is still intact here regardless of which
# tier resolved things (cleanup_combined's trap only fires once this
# function's own `exit` actually runs).
finish() {
    local verdict="$1"
    echo "$verdict" > "$CACHE_DIR/${CACHE_KEY}.verdict"
    cp -f -- "$COMBINED" "$CACHE_DIR/${CACHE_KEY}.transcript.log" 2>/dev/null
    python3 -c '
import json, sys
path, audio_idx, verdict, checked_at, snippet_offset, snippet_words, out = sys.argv[1:8]
data = {
    "source_path": path,
    "audio_idx": int(audio_idx),
    "verdict": int(verdict),
    "checked_at": checked_at,
    "basename": path.rsplit("/", 1)[-1],
}
if snippet_offset:
    data["good_snippet_offset"] = float(snippet_offset)
    data["good_snippet_words"] = int(snippet_words)
json.dump(data, open(out, "w"), ensure_ascii=False)
' "$RESOLVED_VIDEO" "$AUDIO_IDX" "$verdict" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
      "$BEST_SNIPPET_OFFSET" "$BEST_SNIPPET_WORDS" "$CACHE_DIR/${CACHE_KEY}.json" 2>/dev/null
    exit "$verdict"
}

# Prints the 1-based line number to truncate BEFORE (exclusive) if any
# normalized line recurs >= LINE_REPEAT_THRESHOLD times in $1; prints
# NONE otherwise. Reports the EARLIEST such line's first occurrence, not
# just whichever repeated line is found first while scanning, so an
# alternating two-line loop (A, B, A, B, ...) truncates at A's first
# appearance, not B's.
truncate_point() {
    awk -v thresh="$LINE_REPEAT_THRESHOLD" '
    {
        norm = tolower($0)
        gsub(/[¡!¿?.,]/, "", norm)
        gsub(/^[ \t]+|[ \t]+$/, "", norm)
        if (norm == "") next
        count[norm]++
        if (!(norm in first)) first[norm] = NR
    }
    END {
        cut = 0
        for (k in count) {
            if (count[k] >= thresh) {
                if (cut == 0 || first[k] < cut) cut = first[k]
            }
        }
        print (cut == 0) ? "NONE" : cut
    }
    ' "$1"
}

# One full attempt at one offset/duration: extract, transcribe, truncate-
# before-hallucination if needed, classify. Prints the transcript used (to
# stderr, for logging) and returns the classifier's exit code, or 3 if
# this attempt never produced anything usable.
try_once() {
    local start_pct="$1" clip_dur="$2"
    local clip transcript response cut wc_words

    clip="$(mktemp --suffix=.wav)"
    transcript="$(mktemp)"

    if ! "$SCRIPT_DIR/castilian-extract-clip.sh" "$VIDEO" "$AUDIO_IDX" "$clip" "$clip_dur" "$start_pct" >&2; then
        log "attempt at offset $start_pct: extraction failed"
        rm -f -- "$clip" "$transcript"
        return 3
    fi

    # language=es/translate=false: forces real Spanish transcription
    # rather than trusting auto-detect (found live: without it, a known-
    # Castilian clip came back as hallucinated ENGLISH). no_context=true/
    # temperature=0: the standard hallucination mitigation -- helps, does
    # not eliminate the problem, hence truncate_point() as the real
    # backstop below.
    response=$(curl -fsS -m 90 "$WHISPER_SERVER_URL/inference" \
        -F "file=@${clip}" -F "response_format=json" \
        -F "language=es" -F "translate=false" \
        -F "no_context=true" -F "temperature=0" 2>&1)
    rm -f -- "$clip"
    if [[ $? -ne 0 || -z "$response" ]]; then
        log "attempt at offset $start_pct: whisper-server unreachable or request failed: $response"
        rm -f -- "$transcript"
        return 3
    fi

    jq -r '.text // empty' <<<"$response" 2>/dev/null > "$transcript"
    if [[ ! -s "$transcript" ]]; then
        log "attempt at offset $start_pct: no transcript text in response: $response"
        rm -f -- "$transcript"
        return 3
    fi

    cut=$(truncate_point "$transcript")
    if [[ "$cut" != "NONE" ]]; then
        head -n "$((cut - 1))" "$transcript" > "${transcript}.trunc"
        mv -f -- "${transcript}.trunc" "$transcript"
        log "attempt at offset $start_pct: hallucination loop detected, truncated to the $((cut - 1)) real line(s) before it"
    fi

    wc_words=$(wc -w < "$transcript")
    if [[ "$wc_words" -lt "$MIN_WORDS" ]]; then
        log "attempt at offset $start_pct: only $wc_words usable word(s) after truncation, not enough to classify"
        rm -f -- "$transcript"
        return 3
    fi

    if [[ "$wc_words" -gt "$BEST_SNIPPET_WORDS" ]]; then
        BEST_SNIPPET_OFFSET="$start_pct"
        BEST_SNIPPET_WORDS="$wc_words"
    fi

    log "attempt at offset $start_pct ($wc_words words): $(cat -- "$transcript")"
    cat -- "$transcript" >> "$COMBINED"
    echo "" >> "$COMBINED"
    "$SCRIPT_DIR/castilian-linguistic-classify.sh" "$transcript"
    local verdict=$?
    rm -f -- "$transcript"
    return "$verdict"
}

best=3
for off in "${OFFSETS[@]}"; do
    try_once "$off" "$CLIP_DUR_PER_ATTEMPT"
    v=$?
    if [[ "$v" -eq 0 || "$v" -eq 1 ]]; then
        finish "$v"
    fi
    [[ "$v" -eq 2 ]] && best=2
done

# Escalation tier (2026-09-08): the 3 standard-size attempts above didn't
# resolve it -- rather than give up, spend more compute on the files that
# actually need it (most files resolve well before this point, so this
# tier is cheap in aggregate even though each attempt here is expensive).
# Two large, complementary windows covering nearly the whole episode in
# one shot each, instead of three separate small slices -- much better
# odds of a real marker showing up somewhere, and truncate_point() means
# a hallucination loop partway through a big window still keeps
# everything real before it, so a bigger window is strictly more
# information, never less.
for esc in "0.05:${ESCALATION_DUR}" "0.55:${ESCALATION_DUR}"; do
    off="${esc%%:*}"; dur="${esc##*:}"
    log "escalating: standard attempts didn't resolve, trying a larger window at offset $off (${dur}s)"
    try_once "$off" "$dur"
    v=$?
    if [[ "$v" -eq 0 || "$v" -eq 1 ]]; then
        finish "$v"
    fi
    [[ "$v" -eq 2 ]] && best=2
done

# Nothing resolved per-attempt -- last resort: classify everything
# gathered across every attempt together, in case the real evidence was
# split across windows rather than missing entirely (see COMBINED's
# definition above for the real case that motivated this).
if [[ "$best" -eq 2 ]]; then
    log "no single attempt resolved it -- trying one final classification on all attempts combined"
    "$SCRIPT_DIR/castilian-linguistic-classify.sh" "$COMBINED"
    v=$?
    if [[ "$v" -eq 0 || "$v" -eq 1 ]]; then
        finish "$v"
    fi
fi

# LLM fallback (2026-09-08) -- for files real dialogue was recovered for
# (best==2, never best==3: an LLM judging thin air is no better than the
# mechanical classifier judging thin air) but the mechanical lexicon/
# grammar classifier still can't confirm.
#
# 2026-09-08, switched from a local Llama-3.1-8B-Instruct/Qwen2.5-14B
# server (llama.cpp, same GPU as whisper-server) to the real Anthropic
# API, after a real 49-file production batch made the local model's
# actual contribution measurable rather than theoretical: of 24 files
# that fell back to it, only 1 was a genuine, verified catch ("vale",
# grounded and correct); the other 2 decisive verdicts it returned were
# BOTH hallucinated quotes that don't actually appear in the transcript
# (one merged two separate lines into a fabricated sentence, the other
# invented text outright) -- caught only by quote_is_grounded() below,
# not because the model itself was trustworthy. Net: +1 resolved file
# per 24 fallback attempts, against a 2-in-3 hallucination rate on its
# own decisive calls. Not worth maintaining a whole separate local-LLM-
# server stack (a real build effort on its own machine) for that return,
# at the actual usage cadence this pipeline runs at (a handful of batches
# a month) -- so the local-only design constraint that motivated
# LOCAL_LLM_URL in the first place was deliberately dropped here, with
# the user's explicit sign-off, in favor of a stronger model.
# Kept UNCHANGED, and this is the part that actually matters: the
# constrained-checklist, quote-required prompt (LLM_PROMPT_SYSTEM/
# LLM_PROMPT_USER_PREFIX below) and quote_is_grounded()'s verbatim check.
# Swapping in a stronger model doesn't remove the need for grounding --
# it was the grounding check, not the local model's judgment, that kept
# both hallucinations above from becoming wrong shipped verdicts, and
# that stays true regardless of which model sits behind this call.
# ANTHROPIC_API_KEY -- normally just an inherited env var (set by whoever
# invokes this script, or by castilian-control's container env_file when
# run that way). Also checked directly against castilian-control/.env as
# a fallback, so a standalone/cron/manual invocation on the host (no
# container involved at all) picks up the same key without needing it
# exported separately -- avoids a single secret needing to be maintained
# in two places.
if [[ -z "${ANTHROPIC_API_KEY:-}" && -f "$SCRIPT_DIR/../castilian-control/.env" ]]; then
    ANTHROPIC_API_KEY=$(grep -m1 '^ANTHROPIC_API_KEY=' "$SCRIPT_DIR/../castilian-control/.env" | cut -d= -f2-)
fi
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-claude-sonnet-5}"
DISABLE_LLM_FALLBACK="${DISABLE_LLM_FALLBACK:-false}"

# 2026-09-08, rewritten after two open-ended-prompt failures: both the 8B
# AND (after upgrading) a 14B model, asked to freely reason about
# "vocabulary/grammar/register", each confidently produced a WRONG
# verdict built on fabricated linguistic claims -- one invented a "vos"
# conjugation that never appears in the transcript at all; the other
# (bigger model, real quote this time) claimed the object pronoun "te"
# attached to an infinitive is a Latin-American marker, which is false --
# "te" is used identically in both dialects. Bigger model, same failure
# shape: this is a general LLM tendency to construct SOME justification
# for a forced binary choice, not a pure model-size problem.
#
# Fix: stop asking for open linguistic reasoning at all. Restrict the
# model to the SAME fixed checklist castilian-speech-patterns.sh already
# trusts (VOSOTROS_PATTERN/CASTILIAN_LEXICON/LATAM_LEXICON) -- it may only
# report a match against one of these specific, pre-vetted items, never
# invent a new pattern. Validated live against the exact failing
# transcript: correctly returned UNCERTAIN instead of fabricating a
# verdict, and separately correctly identified genuine checklist evidence
# (a real vosotros-conjugated verb) in a known-good transcript.
LLM_PROMPT_SYSTEM='You follow instructions exactly and never add reasoning beyond what is explicitly permitted.'
LLM_PROMPT_USER_PREFIX='You are checking a Spanish transcript ONLY for a specific, fixed checklist of known dialect markers. You do not invent new linguistic rules or reasoning beyond this checklist -- if nothing on it genuinely appears, you must say UNCERTAIN. The transcript was auto-transcribed by speech recognition and may contain occasional recognition errors/garbled words -- do not treat an unrecognizable word as evidence.

CASTILIAN markers (any genuine occurrence of):
- "vosotros" or "vosotras" (pronoun)
- "vuestro"/"vuestra"/"vuestros"/"vuestras" (possessive)
- a verb genuinely conjugated for the vosotros subject (2nd person plural, informal), in ANY tense -- present (-áis/-éis/-ís, e.g. "habláis"/"coméis"/"vivís"), imperfect (-abais/-íais, e.g. "cantabais"/"comíais"), preterite (-asteis/-isteis, e.g. "cantasteis"/"vivisteis"), future (-aréis/-eréis/-iréis), conditional (-aríais/-eríais/-iríais), or imperative (-ad/-ed/-id, or -aos/-eos/-íos when reflexive, e.g. "cantad", "callaos", "sentaos", "vestíos" -- but NOT a coincidental unrelated word that happens to end in these letters, e.g. "verdad"/"ciudad"/"amistad" are NOT vosotros forms (unrelated nouns), and "país"/"anís" are NOT "-ís" verb forms either (unrelated nouns: country, anise) -- only count a genuine verb form)
- an IRREGULAR vosotros form that does not follow the regular endings above -- these are real, common, and easy to miss: "sois" (ser), "vais" (ir), "veis" (ver), "dais"/"deis" (dar), "id"/"idos"/"iros" (ir, imperative)
- a COLLOQUIAL vosotros reflexive imperative formed by attaching "-os" directly to the infinitive instead of the prescriptively "correct" -aos/-eos/-íos form -- e.g. "callaros" (colloquial) alongside/instead of "callaos" (prescriptive), "sentaros" alongside "sentaos". This is extremely common in real spoken Spanish. Do NOT confuse this with ordinary -ero/-eros nouns that are not related to any verb at all, e.g. "compañeros" (colleagues), "caballeros" (gentlemen), "toreros" (bullfighters), "extranjeros" (foreigners) are NOT vosotros forms -- only count it when there is a genuine verb+os construction.
- vocabulary: ordenador, móvil, coche(s), patata(s), zumo, gafas, vale, guay, mola/molaba/molar, flipar/flipante, chaval, curro/currar/currando, coger (only in an ordinary non-sexual sense like "coger el autobús")

LATIN_AMERICAN markers (any genuine occurrence of):
- "ustedes" used for informal plural you
- vocabulary: computadora, celular, carro(s), papa(s) (as in potato), jugo, lentes, anteojos, manejar, platicar, chevere, güey, pana

Respond in EXACTLY this format, three lines:
VERDICT: CASTILIAN, LATIN_AMERICAN, or UNCERTAIN
QUOTE: the exact word or short phrase from the transcript that matches one of the checklist items above -- must be copied verbatim. Write NONE if nothing on the checklist genuinely appears.
MATCHED_ITEM: which specific checklist item above the quote matches. Write NONE if QUOTE is NONE.

Do not use any grammar, register, tone, or vocabulary reasoning outside this checklist. If nothing on the checklist appears verbatim, respond UNCERTAIN with QUOTE: NONE and MATCHED_ITEM: NONE -- this is expected and correct for many transcripts, not a failure. VERDICT must be UNCERTAIN whenever MATCHED_ITEM is NONE -- never report a confirmed dialect without a genuine matched checklist item.

Transcript:
'

# Verifies the LLM actually quoted real text rather than inventing
# evidence -- see this tier's header for why this exists: found live, the
# 8B model confidently classified a file as Latin American citing "vos"
# conjugation and a word ("papache") that appear NOWHERE in the actual
# transcript -- a real hallucination, not a borderline call, that would
# have produced a wrong confirmed verdict without this check. Normalizes
# case/whitespace before substring-matching so trivial formatting
# differences don't cause a false rejection of genuine evidence.
# nfc(): Unicode-normalizes to NFC (precomposed accents, e.g. a single í
# codepoint) before comparing. Found live: a genuinely-correct LLM verdict
# got wrongly rejected here because its JSON response encoded an accented
# character differently (NFD, a base letter + separate combining accent
# mark) than the source transcript file (NFC) -- visually identical,
# byte-for-byte different, so a plain substring/tr comparison silently
# failed to match real, correct evidence. tr's own case-folding also
# doesn't touch accented letters at all (confirmed: "MÍO" -> "mÍo", not
# "mío") -- python's str.lower() handles that correctly where tr can't.
# 2026-09-08, fixed a real false-rejection bug found live: a genuinely
# correct, verbatim-grounded LLM quote ("os queréis perder") was rejected
# because the transcript happened to have punctuation ("perder, peza?")
# immediately after the quoted words, no space. Root cause wasn't
# Unicode at all this time -- `nfc <<<"$quote"` used a bash here-string,
# which ALWAYS appends an implicit trailing newline to what it feeds the
# command; `tr -s '[:space:]' ' '` then collapsed that trailing newline
# into a trailing SPACE that became part of norm_quote even though the
# real quote has no trailing space. So norm_quote silently became "...
# perder " (trailing space) which can never match "...perder," (comma,
# no space) in the source -- any correct quote ending right before
# punctuation (extremely common -- commas, periods, question marks) was
# systematically, silently rejected. `printf '%s'` (no `<<<`) writes
# exactly the string with no implicit newline, fixing the root cause;
# the explicit trim is defense-in-depth in case the LLM's own quote text
# ever carries real leading/trailing whitespace of its own.
nfc() { python3 -c "import sys, unicodedata; print(unicodedata.normalize('NFC', sys.stdin.read()).lower())"; }

trim() { local s="$1"; s="${s# }"; s="${s% }"; printf '%s' "$s"; }

quote_is_grounded() {
    local quote="$1" transcript_file="$2"
    [[ -z "$quote" || "$quote" == "NONE" ]] && return 1
    local norm_quote norm_transcript
    norm_quote=$(trim "$(printf '%s' "$quote" | nfc | tr -s '[:space:]' ' ')")
    norm_transcript=$(trim "$(nfc < "$transcript_file" | tr -s '[:space:]' ' ')")
    [[ "$norm_transcript" == *"$norm_quote"* ]]
}

if [[ "$best" -eq 2 && -s "$COMBINED" && "$DISABLE_LLM_FALLBACK" != "true" ]]; then
    if [[ -z "$ANTHROPIC_API_KEY" ]]; then
        log "ANTHROPIC_API_KEY not set -- skipping LLM fallback, staying at mechanical result"
    else
        log "mechanical classification exhausted -- falling back to Claude ($ANTHROPIC_MODEL) judgment on the combined transcript"
        llm_request=$(jq -n --arg sys "$LLM_PROMPT_SYSTEM" --arg user "${LLM_PROMPT_USER_PREFIX}$(cat -- "$COMBINED")" --arg model "$ANTHROPIC_MODEL" '
            {model: $model, max_tokens: 200, system: $sys,
             messages: [{role: "user", content: $user}]}
        ')
        llm_response=$(curl -fsS -m 120 "https://api.anthropic.com/v1/messages" \
            -H "x-api-key: $ANTHROPIC_API_KEY" \
            -H "anthropic-version: 2023-06-01" \
            -H "Content-Type: application/json" \
            -d "$llm_request" 2>&1)
        if [[ $? -eq 0 && -n "$llm_response" ]]; then
            llm_text=$(jq -r '.content[0].text // empty' <<<"$llm_response" 2>/dev/null)
            if [[ -z "$llm_text" ]]; then
                log "Claude API response had no usable text (possible API error) -- staying at mechanical result: $llm_response"
            else
                log "Claude raw response: $llm_text"
                llm_verdict_word=$(grep -m1 -oiE '^VERDICT:\s*\S+' <<<"$llm_text" | sed -E 's/^VERDICT:\s*//' | tr -d '[:space:]' | tr '[:lower:]' '[:upper:]')
                llm_quote=$(grep -m1 -oiE '^QUOTE:.*' <<<"$llm_text" | sed -E 's/^QUOTE:[[:space:]]*//')
                llm_matched=$(grep -m1 -oiE '^MATCHED_ITEM:.*' <<<"$llm_text" | sed -E 's/^MATCHED_ITEM:[[:space:]]*//')
                # Consistency check (2026-09-08, carried over unchanged from
                # the local-LLM tier): a decisive VERDICT paired with
                # MATCHED_ITEM: NONE is a self-contradiction, never trusted.
                # Only fires for an actually-decisive verdict -- MATCHED_ITEM:
                # NONE alongside an already-UNCERTAIN verdict is the normal,
                # correct case (no real evidence found), not a rejection.
                if [[ "$llm_verdict_word" == "CASTILIAN" || "$llm_verdict_word" == "LATIN_AMERICAN" ]] \
                    && [[ -z "$llm_matched" || "$llm_matched" == "NONE" ]]; then
                    log "REJECTED Claude verdict ($llm_verdict_word) -- MATCHED_ITEM was NONE/empty, contradicting a decisive verdict; treating as unreliable"
                    llm_verdict_word="UNCERTAIN"
                fi
                if [[ "$llm_verdict_word" == "CASTILIAN" || "$llm_verdict_word" == "LATIN_AMERICAN" ]]; then
                    if quote_is_grounded "$llm_quote" "$COMBINED"; then
                        log "Claude quote verified as real text -- accepting verdict $llm_verdict_word"
                        [[ "$llm_verdict_word" == "CASTILIAN" ]] && finish 0
                        finish 1
                    else
                        log "REJECTED Claude verdict ($llm_verdict_word) -- cited quote ('$llm_quote') does not actually appear in the transcript, treating as unreliable/hallucinated rather than trusting it"
                    fi
                else
                    log "Claude returned UNCERTAIN or unparseable response -- staying at mechanical result"
                fi
            fi
        else
            log "Claude API unreachable or request failed -- staying at mechanical result: $llm_response"
        fi
    fi
fi

finish "$best"
