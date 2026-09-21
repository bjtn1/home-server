#!/usr/bin/env python3
"""Regression tests for whisper_srt.py, built from failures seen on real output.
Run: python3 scripts/test_whisper_srt.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import whisper_srt as W  # noqa: E402


def cue(s, e, t):
    return [s, e, t]


class Parse(unittest.TestCase):
    def test_roundtrip_and_crlf(self):
        src = "1\r\n00:00:01,000 --> 00:00:02,500\r\nHola\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\nAdiós\r\n"
        cues = W.parse_srt(src)
        self.assertEqual(cues, [[1000, 2500, "Hola"], [3000, 4000, "Adiós"]])
        self.assertEqual(W.parse_srt(W.format_srt(cues)), cues)

    def test_missing_index_and_bad_block_skipped(self):
        src = "00:00:01,000 --> 00:00:02,000\nHola\n\ngarbage block\n\n00:00:x --> nope\nX\n"
        self.assertEqual(W.parse_srt(src), [[1000, 2000, "Hola"]])


class Loops(unittest.TestCase):
    def test_stuck_loop_same_timestamp_collapses(self):
        # six identical cues at (almost) the same timestamp
        t = "¿Qué quieres jugar con el fútbol?"
        cues = [cue(506810, 506910, t) for _ in range(6)]
        out, rep = W.clean(cues)
        self.assertEqual(len(out), 1)
        self.assertEqual(rep["collapsed_runs"][0]["repeats"], 6)

    def test_different_lines_touching_in_time_not_merged(self):
        # distinct dialogue back-to-back must survive (guards the SIMILARITY threshold)
        cues = [cue(1000, 2000, "¿Dónde está el mapa?"), cue(2000, 3000, "¿Dónde está la mapa vieja?"),
                cue(3000, 4000, "Estaba en la cueva")]
        out, rep = W.clean(cues)
        self.assertEqual(len(out), 3)
        self.assertFalse(rep["collapsed_runs"])

    def test_real_repetition_after_silence_kept(self):
        cues = [cue(1000, 2000, "¡Corre!"), cue(9000, 10000, "¡Corre!")]
        out, _ = W.clean(cues)
        self.assertEqual(len(out), 2)

    def test_spread_repeat_flagged_not_modified(self):
        cues = [cue(i * 5000, i * 5000 + 1500, "No") for i in range(5)]
        out, rep = W.clean(cues)
        self.assertEqual(len(out), 5)
        self.assertTrue(rep["flagged_repeats"])

    def test_intra_cue_loop_truncated(self):
        out, rep = W.clean([cue(0, 5000, "sí, sí, sí, sí, sí, sí, sí, sí, sí, sí")])
        self.assertEqual(out[0][2].count("sí"), W.INTRA_KEEP)
        self.assertTrue(rep["intra_truncated"])

    def test_short_natural_repeat_untouched(self):
        out, rep = W.clean([cue(0, 3000, "no, no, no")])
        self.assertEqual(out[0][2], "no, no, no")
        self.assertFalse(rep["intra_truncated"])


class Timing(unittest.TestCase):
    def test_overlong_cue_end_trimmed_start_kept(self):
        # a 25-char line displayed for 130 s
        out, rep = W.clean([cue(1000, 131000, "¡Vamos a la playa ahora!!")])
        self.assertEqual(out[0][0], 1000)
        self.assertLess(out[0][1] - out[0][0], 5000)
        self.assertEqual(rep["trimmed_durations"], 1)

    def test_normal_cue_not_trimmed(self):
        out, rep = W.clean([cue(1000, 3500, "Buenos días a todos")])
        self.assertEqual(out[0], [1000, 3500, "Buenos días a todos"])
        self.assertEqual(rep["trimmed_durations"], 0)

    def test_zero_or_negative_duration_fixed(self):
        out, rep = W.clean([cue(5000, 5000, "Hola"), cue(9000, 8000, "Adiós")])
        self.assertTrue(all(c[1] > c[0] for c in out))
        self.assertEqual(rep["fixed_durations"], 2)


class Hallucinations(unittest.TestCase):
    def test_pure_hallucination_dropped(self):
        out, rep = W.clean([cue(0, 2000, "¡Suscríbete al canal!"), cue(3000, 5000, "Hola, Dipper")])
        self.assertEqual([c[2] for c in out], ["Hola, Dipper"])
        self.assertEqual(len(rep["dropped_hallucinations"]), 1)

    def test_phrase_inside_real_dialogue_only_flagged(self):
        text = "Dijo que te suscríbete o algo así"
        out, rep = W.clean([cue(0, 4000, text)])
        self.assertEqual(out[0][2], text)
        self.assertTrue(rep["suspect_phrases"])

    def test_clean_does_not_mutate_input(self):
        src = [cue(0, 0, "Hola")]
        W.clean(src)
        self.assertEqual(src, [[0, 0, "Hola"]])


class Names(unittest.TestCase):
    V = {"gallas": "Agallas", "beeper": "Dipper", "pine": "Pines"}

    def test_explicit_map_applies(self):
        out, fixes = W.correct_names([cue(0, 2000, "Beeper y Gallas")], self.V)
        self.assertEqual(out[0][2], "Dipper y Agallas")
        self.assertEqual(len(fixes), 2)

    def test_real_word_never_touched(self):
        # "Pero" must never become "Perro" (fuzzy-correction bug, 2026-09): only exact map keys apply
        out, fixes = W.correct_names([cue(0, 2000, "Pero el perro no vino")], self.V)
        self.assertEqual(out[0][2], "Pero el perro no vino")
        self.assertEqual(fixes, [])

    def test_empty_map_is_noop(self):
        out, fixes = W.correct_names([cue(0, 1000, "Hola")], {})
        self.assertEqual((out, fixes), ([[0, 1000, "Hola"]], []))

    def test_idempotent(self):
        once, _ = W.correct_names([cue(0, 2000, "Beeper")], self.V)
        twice, fixes = W.correct_names(once, self.V)
        self.assertEqual(once, twice)
        self.assertEqual(fixes, [])


class Reflow(unittest.TestCase):
    NARRATION = "Cuando todo parecía perdido, un guerrero humano, Eon, equipado con un poderoso puño, se alzó victorioso, destruyendo al general y a sí mismo."

    def _lines_ok(self, cues):
        return all(c[2].count("\n") <= 1 and all(len(l) <= W.MAX_LINE for l in c[2].split("\n")) for c in cues)

    def test_long_narration_split_into_readable_cues(self):
        # Iron Kid S01E01: 141 chars on one line held 10.5 s filled half the screen
        out, rep = W.clean([cue(15200, 25700, self.NARRATION)])
        self.assertGreater(len(out), 1)
        self.assertTrue(self._lines_ok(out))
        self.assertEqual(" ".join(" ".join(c[2].split()) for c in out), self.NARRATION)   # no word lost or changed
        self.assertEqual(out[0][0], 15200)                                                  # starts where it started
        self.assertLessEqual(out[-1][1], 25700)
        self.assertTrue(all(b[0] >= a[1] for a, b in zip(out, out[1:])))                    # no overlaps introduced
        self.assertEqual(rep["split_cues"], 1)

    def test_medium_cue_wrapped_to_two_balanced_lines_not_split(self):
        out, _ = W.clean([cue(0, 5000, "La humanidad aplastada por el ejército de un tiránico cíbor")])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][2].count("\n"), 1)
        self.assertTrue(self._lines_ok(out))

    def test_short_cue_untouched(self):
        out, _ = W.clean([cue(1000, 3500, "¡Vamos, Scooby!")])
        self.assertEqual(out[0], [1000, 3500, "¡Vamos, Scooby!"])

    def test_unspaced_hallucination_loop_collapsed_and_bounded(self):
        # Keroro S04E36: one 315-char 'cucucu...' token with no spaces
        out, _ = W.clean([cue(0, 5000, "Cu" * 150)])
        self.assertLessEqual(len(out[0][2]), 12)
        out2, _ = W.clean([cue(0, 5000, "x" * 400)])   # unbreakable garbage must still never exceed 2 lines
        self.assertTrue(self._lines_ok(out2))

    def test_fast_cue_extended_only_into_the_gap(self):
        out, rep = W.clean([cue(0, 600, "Esto es una frase bastante larga para leer"), cue(2000, 4000, "Vale.")])
        # 43 chars at ~17 cps needs ~2.5 s, more than the 1.4 s available, so it stretches exactly to the cap
        self.assertEqual(out[0][1], 2000 - W.MIN_GAP_MS)         # ...and never into the next cue
        self.assertEqual(out[1], [2000, 4000, "Vale."])

    def test_extension_does_not_create_mergeable_duplicates(self):
        # identical neighbours must keep a gap >= STUCK_GAP_MS or the next run would merge them
        same = "No pienso ir a esa fiesta de disfraces mañana por la noche"   # ~3.5 s of reading time
        out, _ = W.clean([cue(0, 400, same), cue(3600, 5000, same)])
        self.assertEqual(len(out), 2)
        self.assertGreater(out[1][0] - out[0][1], W.STUCK_GAP_MS)   # extended, but leaves a gap the collapse rule won't merge
        self.assertEqual(W.clean(out)[0], out)                       # and a second pass leaves it alone

    def test_clean_is_idempotent_including_split_duplicates(self):
        # a cue that repeats its own sentence splits into two identical touching cues -> must merge, then stay merged
        rep = "El emperador de la Alius, el emperador de la Alius, el emperador de la Alius, el emperador de la Alius."
        once, _ = W.clean([cue(0, 9000, rep), cue(12000, 13000, "Vale."), cue(14000, 30000, self.NARRATION)])
        twice, _ = W.clean(once)
        self.assertEqual(once, twice)

    def test_nested_counting_hallucination_is_stable(self):
        # Detective Conan S11E04: whisper counted "6, 7, 1, 2, 3, 1, 2, 3, ..." -- loops nested in loops
        junk = "6, 7, 1, 2, 3, 4, 5, 6, 7, " + "1, 2, 3, " * 12 + "1"
        once, _ = W.clean([cue(0, 5000, junk)])
        self.assertLess(len(" ".join(once[0][2].split())), len(junk) / 2)
        self.assertEqual(W.clean(once)[0], once)


if __name__ == "__main__":
    unittest.main(verbosity=2)
