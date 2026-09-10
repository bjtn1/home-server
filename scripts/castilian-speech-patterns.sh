# Shared linguistic markers for telling Castilian (Peninsular) Spanish
# apart from Latin American Spanish from actual transcribed SPEECH, as
# opposed to castilian-patterns.sh's LATAM_PATTERN/CASTILIAN_PATTERN, which
# match release-metadata TITLE TEXT (formal labels like "Castellano" or
# "Latino" that a release group typed into a tag). Deliberately a separate
# file, not an extension of that one: title text is a naming convention a
# human chose when packaging a release; a transcript is naturally-occurring
# speech, and the two vocabularies don't overlap -- nobody says "european
# spanish" mid-sentence, and no release is titled "vosotros". Built
# 2026-09-08 as the Whisper-based fallback for files where per-track
# metadata (language/language_ietf/title) gives no dialect signal at all --
# the "und" + generic "[Spa]" title case found live on the Courage the
# Cowardly Dog batch, where the title convention only says "this audio is
# Spanish", never which Spanish.
#
# Two signal tiers, not one flat list, because they carry very different
# strength:
#
# VOSOTROS_PATTERN -- grammatical, not lexical. "vosotros/vosotras" (the
# 2nd-person-plural informal pronoun) and its distinctive verb-ending
# family (-áis/-éis endings, the "-ad/-ed/-id" imperative plural, "os" as
# the corresponding object/reflexive pronoun) are essentially unique to
# Peninsular Spanish -- Latin American Spanish collapsed the informal/
# formal plural distinction entirely and uses "ustedes" (+ standard "-an/
# -en" verb forms) for both, centuries ago. A real, sustained vosotros
# pattern in a transcript is about as close to unambiguous dialect
# evidence as spoken language gets. Spanish is pro-drop (subject pronouns
# are routinely omitted, the verb ending alone carries the person/number),
# so leaning on the pronoun alone would under-detect -- most real sentences
# never say "vosotros" out loud even when conjugating for it.
#
# CASTILIAN_LEXICON/LATAM_LEXICON -- vocabulary, not grammar. Much weaker
# individually (any single word could be a coincidence, a character name,
# a loanword, ASR mis-transcription) -- these are corroborating evidence,
# meant to be counted/weighted by the classifier that sources this file,
# never trusted on a single hit the way a real vosotros pattern can be.
# Deliberately excludes words with a second, unrelated common meaning that
# would make a hit ambiguous on its own (e.g. "papa" -- potato in Latin
# America, but also "Pope"/"dad" everywhere; "plata" -- money slang in
# much of Latin America, but also literally "silver").
#
# Not sourced by any of the metadata-based detection scripts (archive-
# castilian-audio.sh, mux-castilian-audio.sh, castilian-drop-scan.sh,
# castilian-coverage-report.sh) -- only by the Whisper-transcript
# classifier, once it exists.

# Word-boundary-anchored; case-insensitive matching is the caller's job
# (grep -i / jq test("...";"i")), same convention as castilian-patterns.sh.
# 2026-09-08: dropped a bare `\bos\b` alternative that used to be here.
# Found live: whisper.cpp's own transcript wraps mid-word across line
# breaks ("dedos" -> "ded" / "os" as two separate lines), and the orphaned
# "os" matched as if it were the vosotros object pronoun -- a real false
# positive, not hypothetical (it landed safely that one time only because
# a competing Latino signal happened to also be present in the same
# transcript; without that, it would have been a wrong CONFIRMED
# CASTILIAN verdict on a confirmed-Latino file, the exact failure mode
# this whole project treats as the only one that matters). "vosotros"/
# "vosotras" and the -áis/-éis verb-ending pattern are both long, specific
# substrings essentially immune to this kind of coincidental wrap-induced
# match -- a 2-character standalone word is not, so it's gone rather than
# patched around.
# 2026-09-08: added vuestro/vuestra/vuestros/vuestras -- the vosotros
# POSSESSIVE form (your/yours, 2nd-person-plural-informal), missed
# entirely until now. Found live in a real transcript: "yo os dejo a lo
# vuestro" -- Latin American Spanish uses "su/sus" for this instead
# (matching "ustedes"), so this is exactly as dialect-exclusive as the
# pronoun and verb-ending checks already here, just a different word
# class. Full words, not a short fragment like the "os" that was removed
# earlier for wrap-artifact risk -- safe the same way "vosotros" itself is.
# 2026-09-08: added irregular vosotros conjugations -- "-áis/-éis" only
# covers REGULAR verbs. Several extremely common verbs conjugate
# irregularly for vosotros and don't carry the accent at all: ser->sois,
# ir->vais, ver->veis (present indicative), dar->deis (present
# subjunctive; dar's indicative "dais" also lacks the accent), and ir's
# imperative id/idos/iros (irregular; "id" alone excluded -- too likely to
# collide with "ID" as in identification in modern-ish dialogue, unlike
# these longer, more specific forms). Found live reading a real transcript
# directly: "¿Veis estos paños?" is a genuine vosotros form that neither
# this regex nor the -áis/-éis-only LLM checklist description caught,
# because "ver" simply doesn't follow the regular pattern -- the checklist
# only ever described the regular case.
VOSOTROS_PATTERN='\bvosotros\b|\bvosotras\b|\bvuestro\b|\bvuestra\b|\bvuestros\b|\bvuestras\b|\b\w+(áis|éis)\b|\bsois\b|\bvais\b|\bveis\b|\bdais\b|\bdeis\b|\biros\b|\bidos\b|\b(dejad|seguid|mirad|vestid|poned|venid|callad|tened|haced|decid|salid|escuchad|esperad|entrad|traed|volved|parad|corred|abrid|cerrad)(me|lo|la|los|las|le|les|nos)?\b|\b(vivís|sentís|decís|salís|escribís|abrís|recibís|subís|partís|insistís|permitís|venís|dormís|pedís|servís|seguís|oís|reís|sonreís|elegís|repetís|construís)\b|\b\w+(asteis|isteis|abais|íais|aréis|eréis|iréis|aríais|eríais|iríais)\b|\bdaros\b|\bibais\b|\berais\b|\b\w+(arais|ierais)\b'
# 2026-09-08, added the 4 remaining vosotros tense endings the LLM
# checklist has always described in words -- preterite (-asteis/
# -isteis), imperfect (-abais/-íais), future (-aréis/-eréis/-iréis),
# conditional (-aríais/-eríais/-iríais) -- but that were NEVER actually
# in this regex (only present tense -áis/-éis was). Found via a direct
# corpus-wide scan of every cached transcript from the real 49-file
# batch (methodology note: the first scan attempt looked contaminated --
# turned up "cantabais"/"cantasteis"/"comíais" etc., which turned out to
# be the LLM's OWN echoed checklist example text ("e.g. cantabais/
# comíais") bleeding into the grep, not genuine dialogue -- re-ran
# excluding log/LLM-response lines and got 5 genuinely real hits:
# "sufriréis"x3, "queréis"x3 (matches the real "os queréis perder" catch
# elsewhere in this file's history), "veréis", "llevasteis"). Trusted as
# general suffix wildcards, unlike bare -ís (collides with país/anís/
# maniquís) or general -ad/-ed/-id (collides with usted/ciudad), because
# these are long, specific 5-8 letter suffixes -- checked against a real
# Spanish dictionary's full affix-expanded wordlist and every match
# found (estuvisteis, bendijisteis, disteis, fuisteis, hubisteis,
# previsteis, visteis, estabais, dabais) was ITSELF a genuine vosotros
# verb form, zero unrelated collisions.
# 2026-09-08, added 3 more forms found by cross-referencing an external
# Spanish grammar reference (hackettpublishing.com/spanish-grammar/
# GENGRAM/vosotros.html), at the user's prompting -- confirmed the rest
# of that page (present/preterite/future/conditional/present-subjunctive)
# was already fully covered by the patterns above (future/conditional
# always attach to the FULL infinitive regardless of conjugation class,
# so the -aréis/-eréis/-iréis and -aríais/-eríais/-iríais splits already
# catch every regular verb; present subjunctive's -éis/-áis pair is just
# the existing \w+(áis|éis) wildcard's two endings swapped between verb
# classes, already matched either way). Three genuine gaps, all now
# added: the irregular imperfect "ibais" (ir) and "erais" (ser) -- too
# short for the -abais/-íais patterns above to reach (ibais is only 5
# letters, shorter than the 5-letter "abais" suffix itself would need,
# so \w+abais\b structurally can't match it) -- and the entire imperfect
# subjunctive mood (-arais/-ierais, e.g. "hablarais"/"recibierais"),
# missed outright. Collision-checked against the same real Spanish
# dictionary: "ibais"/"erais" are in it as themselves (no other meaning
# found); every -arais/-ierais dictionary hit (estuvierais, dierais,
# hubierais, previerais, vierais) was itself a genuine vosotros form.
# No hits for any of the three in the current 49-file corpus (imperfect
# subjunctive is rare in casual dialogue) -- doesn't move this batch,
# but it's real, safe, permanent completeness.
# 2026-09-08: expanded the -ís present-tense list with 10 more common
# -ir verbs (venís/dormís/pedís/servís/seguís/oís/reís/sonreís/elegís/
# repetís/construís) after a web search cross-check of irregular-verb
# conjugation tables confirmed my existing suffix patterns already
# correctly cover every irregular verb's vosotros forms across every
# tense (fuisteis/hubisteis/estuvisteis/estabais/habíais/habéis etc. all
# route through the general suffix patterns above, since Spanish
# irregular verbs keep REGULAR vosotros endings even when their stems
# are irregular) -- the only real remaining gap was simply not having
# listed enough common -ir verbs for the explicit -ís list. Each
# collision-checked the same way as the original 11; no corpus hits in
# this batch either, same completeness-not-yield reasoning as above.
# 2026-09-08, added an explicit list of the REGULAR vosotros imperative
# (-ad/-ed/-id) and 3rd-conjugation (-ir verbs) present tense (-ís)
# forms, after finding this was a major real gap: personally reading all
# 23 files this batch left unresolved found 6 of 9 genuine catches were
# this exact category (seguid/dejad/dejadme/mirad/vestid/ponedme) plus a
# 7th from the -ís family (sentís) -- and the local LLM's own checklist
# ALREADY described both forms in words ("-ad/-ed/-id", "-áis/-éis/-ís")
# yet still missed nearly every real occurrence across a long noisy
# combined transcript, confirming this is a recall/attention problem an
# LLM doesn't reliably solve, not a wording gap in the checklist -- the
# real fix is mechanical certainty, not hoping a model reads carefully.
#
# The general form of both patterns (\w+(ad|ed|id)\b, bare \w+ís\b) was
# already investigated and rejected earlier in this file's history for
# real collisions (usted/ciudad/verdad/país/anís/maniquís). This is
# NOT the same change -- it's an explicit, individually-vetted list of
# specific verb roots (exactly the curr/mol precedent above), not a
# reopened wildcard. Checked each candidate by hand against real
# Spanish vocabulary (a generic aspell Spanish wordlist turned out to be
# too sparse to trust for this -- it doesn't even contain "usted" or
# "país", both confirmed common and confirmed risky by direct reasoning
# earlier -- so this was a manual audit, not a tooling shortcut):
# dejad/seguid/mirad/vestid/poned/venid/callad/tened/haced/decid/salid/
# escuchad/esperad/entrad/traed/volved/parad/corred/abrid/cerrad and
# vivís/sentís/decís/salís/escribís/abrís/recibís/subís/partís/insistís/
# permitís -- none share a spelling with any common unrelated Spanish
# noun/adjective the way the rejected general forms did. The imperative
# group also accepts one common enclitic object pronoun suffix (me/lo/
# la/los/las/le/les/nos, e.g. "dejadme"/"ponedlo") since that's exactly
# as safe as the bare form (still anchored to the same vetted verb
# root) and doubles real coverage -- "¡Ponedme a Velvet D!" was missed
# by every prior pass, including a human (this session's own) re-read
# of that exact file, until this specific combination was checked for.
#
# The general form of the third category found live -- infinitive+"os"
# constructions beyond the reflexive-imperative case already in the LLM
# prompt (e.g. "daros" in "intentando daros forma") -- stays rejected as
# a wildcard: these need a genuinely open verb root (any infinitive +
# os), and the earlier -aos/-eos/-íos investigation already found real
# common-word collisions (caos, museos/trofeos/empleos, ríos/fríos/
# vacíos) for every one of those suffix families. Confirmed again,
# empirically this time, 2026-09-08: mined every \w+(aros|eros|iros)\b
# hit across the full 49-file real corpus -- 13 of 14 unique matches
# were ordinary unrelated nouns (caballeros, pájaros, bomberos,
# solteros, cruceros, granjeros, prisioneros...), only "daros" itself
# was a genuine infinitive+os construction. That lopsided a collision
# rate rules out both a wildcard AND a reasonably-sized safe explicit
# list (there's nothing else common enough to list). But "daros" alone,
# checked against literally every occurrence available in the real
# corpus with zero collisions found, is safe as a single fully-specified
# word -- same technique as the imperative list, just a list of one.
# 2026-09-08: considered, and rejected, a general regex for the REGULAR
# vosotros imperative (-ad/-ed/-id, e.g. "cantad"/"comed"/"vivid", and its
# reflexive contraction -aos/-eos/-íos, e.g. "levantaos"/"sentaos") --
# this is the one vosotros form family that's still LLM-checklist-only,
# unlike every other form already promoted to this regex. Found live: a
# real transcript's "reparaos los corales" (song lyric) went unresolved --
# the LLM's own checklist explicitly lists "-aos/-eos/-íos when reflexive"
# but it missed this occurrence (a recall miss, not a fabrication -- it
# returned honest UNCERTAIN/NONE rather than inventing a match, so the
# existing grounding/consistency checks had nothing to catch here).
# Tried writing the obvious safe-looking regex anyway and checked it
# against real vocabulary before adding it -- glad we checked: EVERY
# candidate suffix collides with common, everyday non-verb words.
# \b\w+aos\b matches "caos" (chaos, an ordinary noun -- "es un caos"/"un
# caos total" is completely mundane dialogue). \b\w+eos\b matches
# "museos"/"trofeos"/"empleos" (museums/trophies/jobs). \b\w+íos\b
# matches "ríos"/"fríos"/"vacíos" (rivers/colds-or-cold/empty). Non-
# reflexive -ad/-ed/-id is worse: \b\w+ed\b would match "usted"/
# "ustedes" themselves -- the actual LATIN AMERICAN formal-plural
# pronoun -- which would be a direct, severe false-positive-for-the-
# wrong-dialect bug, not just noise. Unlike "iros"/"idos" (kept: long,
# specific, no competing common word), there is no safe stem-length cutoff
# here because the suffix families themselves, not just short instances of
# them, collide with ordinary vocabulary. This is exactly the class of
# pattern the LLM checklist exists for (full-sentence context can tell
# "reparaos los corales" from "un caos total"; a blind regex structurally
# can't) -- so this stays LLM-only by design, not by oversight. The
# residual gap is the LLM's own recall on rarer/song-lyric phrasing of
# this specific form, which would need prompt-level work (e.g. an
# explicit worked example) to close further, not a mechanical regex.

# 2026-09-08: dropped "tío"/"tía" and "tronco". Found live: a real
# transcript's 4 "castilian_lexicon" hits were all "tío" used as the
# literal kinship word (a character named "tío Angus" -- an actual
# uncle), not the Spain-slang "dude" usage at all -- caught by direct
# user review, not by inspection. Both words are genuinely ambiguous
# between a universal-Spanish literal meaning (uncle/aunt; tree trunk or
# torso) and a Spain-only slang meaning (dude/girl; dude), and a simple
# word-boundary regex can't tell which sense is meant -- unlike every
# remaining entry here, none of which have a common literal meaning that
# would plausibly recur in everyday dialogue. "vale"/"móvil"/"molar" carry
# a smaller version of this same ambiguity (voucher; mobile/movable;
# molar tooth) but their alternate senses are comparatively rare in
# casual spoken dialogue -- kept, but noted as a real residual risk
# rather than a settled one.
# 2026-09-08: added patata(s)/coche(s)/gafas -- classic, well-established
# Spain-vs-Latin-America minimal pairs (patata/papa, coche/carro,
# gafas/lentes-anteojos) that were missing entirely, found live: both
# "gafas" and "patatas" appeared directly in real unresolved Courage
# transcripts during this same batch, never matching anything because
# they were never in the list. Skipped "conducir" as a same-shaped
# candidate -- on reflection it's pan-Hispanic in formal contexts
# ("licencia de conducir" is standard across many Latin American
# countries too), not the clean minimal pair the others are.
# 2026-09-08: verb entries (coger/currar/flipar/molar) were matching only
# their bare infinitive -- found live: a real clean transcript used
# "cogido" (past participle), a completely ordinary conjugated form, and
# it matched nothing since "coger" != "cogido" as an exact word. Real
# dialogue is almost entirely conjugated verbs, never infinitives, so an
# infinitive-only check was structurally blind to nearly every real
# occurrence of these words.
#
# First attempt at fixing this used bare stem+wildcard (\bmol\w*\b etc.)
# -- caught before deploying, not after: "mol" as an open stem also
# matches "molestar"/"molesto" (to bother/annoying, a very common and
# totally unrelated word), and "curr" also matches "currículum" (résumé).
# Both would have been real, frequent false-positive vectors. "flip" and
# "cog" don't have that problem (no common unrelated Spanish word shares
# those stems, "cogote"/nape being the only real exception, and rare) --
# left as open wildcards. "mol"/"curr" instead get an explicit list of
# the actual common conjugations, not a wildcard, trading a little
# coverage (an unlisted rare conjugation) for not matching unrelated words.
# 2026-09-08: added plural/gender forms across the board (ordenador ->
# ordenadores, chulo -> chula/chulos/chulas, etc.) -- found live doing a
# full audit after the "cogido" and "veis" misses: exact-singular-only
# matching has the exact same structural blindness to real speech as
# infinitive-only verb matching did, just for nouns/adjectives instead of
# verbs. manejar/platicar (LATAM side) got the same conjugation fix
# already applied to coger/currar -- "manej"/"platic" checked safe stems
# (no common unrelated word shares either), unlike "bot" (botar), which
# collides with bote/botella/botón and stays infinitive-only rather than
# risk that.
VALE_INTERJECTION='¡vale!|\bvale\s+vale\b|(?<!ya )(?<!mí )(?<!me )(?<!te )(?<!le )(?<!nos )(?<!os )(?<!les )\bvale\b[,.]|¿\s*vale\s*\?|,\s*vale\s*\?|bueno,?\s+vale\b'
CASTILIAN_LEXICON="$VALE_INTERJECTION"'|\bguay\b|\bmola(s|n|ba|ban|ría|rían)?\b|\bmolar\b|\bflip\w*\b|\bordenador(es)?\b|\bmóvil(es)?\b|\bzumos?\b|\bchaval(es)?\b|\bcurr(o|as|a|amos|áis|an|aba|ando|ado)\b|\bcog\w*\b|\bchul[oa]s?\b|\bpatatas?\b|\bcoches?\b|\bgafas\b|\bcole\b|\bcremallera(s)?\b|\bescaparate(s)?\b|(?<!nueva )\bjersey(s)?\b|\bcacahuete(s)?\b|\bbragas\b|\bfrigorífico(s)?\b|\bmechero(s)?\b|\bbolera(s)?\b|\bchabolas?\b|\bultramarinos\b|\bhucha(s)?\b|\bfontanero(s)?\b|\bpárvulos?\b|\baparcamiento(s)?\b|\btebeos?\b|\bcamarero(s)?\b|\bbañador(es)?\b|\bgabardinas?\b|\bforofos?\b'
# 2026-09-08, second pass on the same Moreno de Alba source, prompted
# by the user explicitly wanting broader coverage for future shows, not
# just what happens to appear in this one -- cacahuete/bragas/
# frigorífico/mechero/bolera/chabolas/ultramarinos/hucha/fontanero/
# párvulos (peanut/panties/fridge/lighter/bowling-alley/shantytown/
# corner-grocery/piggy-bank/plumber/preschooler), each Madrid's clear,
# genuinely EXCLUSIVE majority answer -- several near-candidates from
# the same chapter were rejected specifically for NOT being exclusive
# (kiosco/manicura/pizarra/deberes/calificaciones/lechuza were all also
# a majority answer in at least one American city too, so useless as a
# one-directional marker) or for having an unrelated common meaning
# (tapas also means lids; cazadora also means "female hunter"; vuelta
# is far too generic; diga is just decir's regular subjunctive; sobar
# is unrelated/inappropriate content anyway). "hucha" (piggy bank) was
# found live in the real corpus, in a file ("1x02") that had been stuck
# unresolved on a single other word all session.
# 2026-09-08, third pass on the same source (continued mining chapter
# VII at the user's request -- "more the merrier" for future shows):
# aparcamiento/tebeos/camarero (parking lot/comic books/waiter). Also
# switched count_matches/count_distinct to PCRE's (*UCP) Unicode mode
# in castilian-linguistic-classify.sh around this same pass -- see that
# file for why (a real, separate bug: bare \b before a leading accented
# character like ñ never matched at all under plain ASCII \w).
# "camarero" specifically: like every entry in this file, this is a
# majority-preference finding (the source's own 51%+ survey threshold),
# not a claim of absolute impossibility elsewhere -- same standard
# already applied throughout.
# Rejected from the same stretch of reading: doncella (collides with
# the ordinary "maiden/damsel" sense -- real risk in fairy-tale-
# adjacent content); chuletas as slang for a cheat-sheet (collides with
# the literal, much more common "pork chop" sense); novillos/acordeón
# for skipping school (collide with "young bulls"/bullfighting and
# "accordion" respectively); guardabarro, tarta, plátano, historietas
# (each also documented as commonly used in Spain OR in America
# depending on direction, i.e. not actually exclusive either way).
# 2026-09-08, fourth pass, continued mining at the user's explicit
# "keep going" -- bañador/gabardina/forofos. "bañador" (men's swim
# trunks) is as clean as this source gets: it explicitly states "La
# última voz no se emplea en América" (that word isn't used in America
# at all), not just a majority preference like everything else here.
# "forofos" (sports fan) is similarly explicit: "parece exclusiva
# denominación madrileña" (appears to be an exclusively Madrid word).
# Rejected from this same stretch: gemelos/mancuernas for cufflinks
# (gemelos also/mainly means "twins"; mancuernas can mean gym dumbbells);
# pajarita for bow-tie (also means "little bird"/paper-crane origami);
# aretes, alianza, horquillas, postizo, tintorería, imperdible,
# limpiabotas, césped, colcha, canicas, esquelas, cabaret (each shown
# in the same survey data to be used about equally in Madrid AND
# multiple American cities, not exclusive either direction); bombilla
# (also means the metal drinking-straw for yerba mate in Argentina/
# Uruguay/Paraguay -- a real, common secondary meaning, not safe).
# 2026-09-08: added 3 more from an actual academic source (José G.
# Moreno de Alba, "Diferencias léxicas entre España y América", MAPFRE,
# 1992 -- a real published dialectology monograph based on structured
# surveys across Madrid + 19 Latin American capitals, not a language-
# learning blog) after being asked to find genuinely reputable research
# rather than repeat shallow web searches. cremallera/jersey/escaparate
# (zipper/sweater/shop-window) were Madrid's clear majority answer vs.
# zípper/suéter/vitrina in America. "jersey" needed a guard: this is an
# American cartoon, and "Jersey" is also a real place name (New Jersey)
# that could plausibly appear in dialogue -- excluded via negative
# lookbehind for "nueva " (requires the -P switch already made for the
# vale disambiguation above). Many other pairs from the same source
# (billete/boleto, sello/estampilla, carnet/licencia, cubo/balde,
# depósito/tanque, girar/doblar, etc.) were deliberately NOT added --
# each has either an unrelated common meaning of its own (billete also
# means banknote; sello also means any kind of seal/stamp; cubo also
# means a cube) or is automotive/bicycle/postal vocabulary unlikely to
# ever appear in this show's actual dialogue -- not worth the risk for
# words that would rarely fire anyway.
# 2026-09-08: replaced bare \bvale\b with a context-aware whitelist,
# found live -- the user pointed out "¿vale la pena?" (a completely
# standard, pan-Hispanic idiom, "is it worth it") would blindly count
# as Castilian evidence under the old bare-word match, and asked for
# context, not just presence. Pulled EVERY real "vale" occurrence across
# the full 49-file corpus (24 total) and hand-classified each: roughly
# half were genuine discourse-marker/interjection use ("Vale, vamos",
# "¿vale?" as a tag question, standalone "¡Vale!"), the other half were
# the unrelated literal "is worth"/idiom senses this needed to exclude:
# "no vale nada" (worth nothing -- the user's exact concern), "más vale
# irse"/"más vale que" (the "más vale" idiom, as in "más vale tarde que
# nunca" -- pan-Hispanic, not Castilian-specific), "por mí vale" (works
# for me, a predicate use), "ya vale" (that's enough -- a different,
# separate idiom from the discourse-marker "vale"), and one that was
# actively backwards: "¡Me vale! ¡Me estoy concentrando!" -- "me vale"
# meaning "I don't care" is a well-known MEXICAN/Latin American idiom,
# the opposite direction entirely. Built as a whitelist (only match
# shapes that are genuinely discourse-marker-shaped: standalone
# exclamation, comma-continuation, tag-question, paired with "bueno",
# or bare repetition) rather than a blacklist of every literal
# collocation, since a blacklist can only ever cover phrasings already
# seen. The negative lookbehinds (excluding "ya"/"mí"/object-pronoun-
# preceded "vale") needed PCRE -- switched count_matches/count_distinct
# in castilian-linguistic-classify.sh from `grep -E` to `grep -P`
# accordingly (confirmed identical \w/accented-character behavior in
# this environment's UTF-8 locale before switching). Verified against
# all 24 real corpus occurrences by hand before deploying: 16 genuine
# cases all still matched, all 11 literal/idiomatic/backwards cases
# (including one from an early miss, "vale vale" bare-repeated, fixed
# before shipping) correctly excluded.
# 2026-09-08: added "cole" (informal for "colegio"/school -- Latin
# American Spanish generally doesn't shorten it this way) -- found live
# reading a real unresolved transcript directly ("una representación
# floral del cole"). Checked against a real Spanish dictionary: not
# present as any other word. Considered and REJECTED adding "venga" at
# the same time despite also finding it live as a Castilian-flavored
# interjection ("¡Venga, vámonos!") -- unlike "cole", "venga" has a
# second, unrelated, completely ordinary meaning pan-Hispanically (the
# subjunctive/formal-command form of "venir", e.g. "que venga" = "let
# him come", "¡Venga usted!" = formal "come!") that a blind regex can't
# distinguish from the interjection use -- the exact same class of
# ambiguity that got "tío"/"tía" rejected earlier in this file's
# history. Stays LLM/manual-read-only.
# 2026-09-08: added jugo(s)/lentes/anteojos -- these were already promised
# to the LLM checklist but never actually added here, a real inconsistency
# between what the prompt claimed to check and what this file covered.
LATAM_LEXICON='\bcarros?\b|\bcomputadoras?\b|\bcelulares?\b|\bmanej\w*\b|\bplatic\w*\b|\bchevere\b|\bchévere\b|\bgüey\b|\bpanas?\b|\bbotar\b|\bpapalotes?\b|\bjugos?\b|\blentes\b|\banteojos\b|\bcachetes\b|\barvejas\b|\bduraznos?\b|\bbilletera(s)?\b|\blustr\w*\b|(?<!en )(?<!de )\bbalde(s)?\b|\bmaní\b|\bporotos?\b|\bplomero(s)?\b|\bmanicurista(s)?\b|\bchequera(s)?\b|\bkínder\b|\bpizarrón\b|\balcancía(s)?\b|\btinterillo(s)?\b|\baló\b|\bfrazada(s)?\b|\byapas?\b|\bñapas?\b|\bparqueo(s)?\b|\bmeser[oa]s?\b|\bbanano(s)?\b|\bgaseosas?\b|\bmancuernillas?\b|\bpeluquín(es)?\b|\bapagador(es)?\b|\bvolantín(es)?\b|\bllantas?\b|\babarrotes\b|\bpulpería(s)?\b|\bchoclos?\b|\belotes?\b|\bchanchos?\b|\bpaltas?\b|\bpicaflor(es)?\b|\bchompipes?\b|\bguajolotes?\b'
# 2026-09-08, fifth pass, mined a genuinely different section of the
# same source (the food/kitchen semantic field) for choclo/elote
# (corn on the cob)/chancho (pig, very widely validated: Tegucigalpa,
# Managua, San José, Quito, Lima, Montevideo, Buenos Aires, Santiago)/
# palta (avocado, Southern Cone)/picaflor (hummingbird)/chompipe+
# guajolote (turkey, Mexico/Central America). Also llanta (tire)/
# abarrotes (groceries)/pulpería (corner store) from the earlier
# automotive/commerce stretch, once the clearly-ambiguous entries in
# that section were filtered out (rejected in the same pass: pito for
# horn -- also whistle, and vulgar slang in some countries; baúl/cofre
# for trunk -- both also mean "chest" generally; capó/claxon/
# portafolios/maletín -- each also documented as used in Madrid, not
# exclusive). Also explored the book's archaism chapter (words obsolete
# in Spain, still live in America) and found it unproductive for this
# purpose -- overwhelmingly Mexico-specific rural/dialectal terms, both
# too regionally narrow for a general LATAM marker and frequently
# ambiguous with an ordinary meaning (palo=tree but also just "stick";
# cuero=skin but also just "leather"; tata=father but a common
# affectionate title too) -- abandoned that chapter, went back to the
# structured-survey chapter instead. "picaflor" carries a secondary
# idiomatic sense (a person who flits between romantic partners,
# "flirt/womanizer") but the bird sense is still common and the overall
# word isn't confusable with an unrelated CONCEPT the way earlier
# rejections were -- included with lower confidence than the others in
# this batch.
# 2026-09-08, second pass on the same source, same "broader coverage
# for future shows" reasoning as the Castilian side above -- maní/
# porotos/plomero/manicurista/chequera/kínder/pizarrón/alcancía/
# tinterillo/aló/frazada/yapa-ñapa (peanut/beans/plumber/manicurist/
# checkbook/kindergarten/chalkboard/piggy-bank/shady-lawyer/phone-
# greeting/blanket/bonus-item). Same exclusion standard as the
# Castilian side: rejected refrigerador (too ubiquitous, used in Spain
# too as a synonym), nevera (also common in Spain), cobija/manta
# (regionally inconsistent/too generic), talonario (also used as
# primary or secondary in several American cities, not exclusive),
# vaqueros/de-vaqueros (collides with the unrelated "jeans" sense),
# boliche (has its own separate meaning, "small store/tavern", in some
# South American countries -- ambiguous within the dialect itself).
# 2026-09-08, fourth pass, same "keep going" mining as the Castilian
# side above -- gaseosa (soft drink)/mancuernillas (cufflinks)/
# peluquín (toupee)/apagador (light switch)/volantín (kite, Chile/
# Bolivia). "lustrabotas" (shoeshine) was ALSO found this pass but
# needed no new entry -- already covered by the existing \blustr\w*\b
# wildcard from the earlier "lustrar" addition. Rejected: rastrillo for
# razor (also/mainly means garden rake); cuates for twins (Mexico-only,
# too narrow); gemelos/mellizos (used inconsistently across America,
# and gemelos collides with cufflinks/twins ambiguity already noted);
# hinchas/fanáticos for sports fans (fanáticos too generic/universal;
# hinchas also means "swollen" from hinchar); foco for lightbulb (also
# commonly means "focus/focal point"); coche fúnebre/carroza for hearse
# (carroza also means old-fashioned carriage, or slang for "old
# person"); boliche for the cup-and-ball toy (a THIRD, different sense
# of a word already rejected twice for the bowling-alley and tavern
# senses -- confirms it's simply too overloaded a word to use safely
# for anything).
# 2026-09-08: added 6 more from the same Moreno de Alba academic source
# (see CASTILIAN_LEXICON's comment above for full citation/methodology)
# -- cachetes/arvejas/durazno/billetera/lustrar/balde (cheeks/peas/
# peach/wallet/to-polish/bucket) were America's clear majority answer
# vs. mejillas/guisantes/melocotón/cartera/limpiar/cubo in Madrid.
# "balde" needed a guard: "en balde"/"de balde" (in vain / for free) is
# a completely separate, pan-Hispanic idiomatic sense of the same word,
# used in Spain too -- excluded via negative lookbehind for "en "/"de ".
# Same exclusions as the Castilian side for anything with its own
# unrelated common meaning (cartera also means briefcase/folder; cubo
# also means a geometric cube) or unlikely-to-appear technical/
# automotive/postal vocabulary.
