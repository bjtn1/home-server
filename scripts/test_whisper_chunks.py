#!/usr/bin/env python3
"""Tests for whisper_chunks.py (no whisper server, no GPU, no VAD model needed).
Run: python3 scripts/test_whisper_chunks.py"""
import os
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import whisper_chunks as K  # noqa: E402
import whisper_srt as W  # noqa: E402


def make_wav(path, seconds):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(K.SR)
        w.writeframes(b"\x00\x00" * int(seconds * K.SR))


def srt(*cues):
    return W.format_srt([list(c) for c in cues])


class Planning(unittest.TestCase):
    def test_close_regions_merge_and_tiny_ones_drop(self):
        self.assertEqual(K.merge_regions([(1.0, 2.0), (2.4, 3.0), (10.0, 10.1)]), [(1.0, 3.0)])

    def test_every_pack_fits_and_all_speech_is_covered_in_order(self):
        segs = [(i * 40.0, i * 40.0 + 9.0) for i in range(30)] + [(2000.0, 2100.0)]   # last region is 100 s long
        packs = K.plan_packs(segs)
        for p in packs:
            self.assertLessEqual(K.pack_length(p), K.MAX_PACK_S + 1e-6)
        pieces = [x for p in packs for x in p]
        self.assertEqual(sum(b - a for a, b, _ in pieces), sum(b - a for a, b in segs))   # nothing lost
        self.assertEqual([a for a, _, _ in pieces], sorted(a for a, _, _ in pieces))       # order kept

    def test_offsets_are_consistent_inside_a_pack(self):
        pack = K.plan_packs([(0.0, 5.0), (20.0, 24.0), (50.0, 53.0)])[0]
        self.assertEqual(pack[0][2], 0.0)
        self.assertAlmostEqual(pack[1][2], 5.0 + K.SEP_S)
        self.assertAlmostEqual(pack[2][2], 5.0 + K.SEP_S + 4.0 + K.SEP_S)

    def test_long_silence_never_reaches_whisper(self):
        # Iron Kid S01E01: narration to 43 s, then NOTHING until 168.65 s. The packed audio must not contain the gap.
        packs = K.plan_packs([(2.41, 20.0), (31.88, 43.35), (168.65, 176.15)])
        total = sum(K.pack_length(p) for p in packs)
        self.assertLess(total, 60)          # ~37 s of speech + separators, not 176 s


class Mapping(unittest.TestCase):
    PACK = [(100.0, 110.0, 0.0), (200.0, 205.0, 10.6)]

    def test_inside_pieces(self):
        self.assertAlmostEqual(K.map_time(self.PACK, 3.0, False), 103.0)
        self.assertAlmostEqual(K.map_time(self.PACK, 12.6, False), 202.0)

    def test_time_in_separator_snaps_by_role(self):
        self.assertAlmostEqual(K.map_time(self.PACK, 10.3, False), 200.0)   # a START in the gap -> next piece start
        self.assertAlmostEqual(K.map_time(self.PACK, 10.3, True), 110.0)    # an END in the gap -> previous piece end

    def test_past_the_end_clamps(self):
        self.assertAlmostEqual(K.map_time(self.PACK, 99.0, True), 205.0)


class PlaceCue(unittest.TestCase):
    # two pieces far apart in the episode: A = 37.3-43.35 (6.05 s, pack 0-6.05), B = 168.65-172.79 (pack 7.05-11.19)
    PACK = [(37.3, 43.35, 0.0), (168.65, 172.79, 6.05 + K.SEP_S)]

    def test_iron_kid_glued_cue_starts_in_the_piece_where_it_mostly_is(self):
        # whisper glued the tail of piece A to the line in piece B: starts 0.1 s before A ends, runs 4 s into B
        out = K.place_cue(self.PACK, 5.95, 11.0, "He pasado mucho tiempo. Aquel al que buscabas ya no existe, Gath.")
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0][0], 168.65, places=2)          # NOT 43.2 (the wrong end of the silence)
        self.assertLess(out[0][1], 173.0)

    def test_cue_really_spanning_two_far_apart_pieces_is_split_with_its_words(self):
        out = K.place_cue(self.PACK, 3.05, 10.05, "uno dos tres cuatro cinco seis siete ocho")
        self.assertEqual(len(out), 2)
        self.assertEqual(" ".join(o[2] for o in out), "uno dos tres cuatro cinco seis siete ocho")   # words kept, in order
        self.assertTrue(37.3 <= out[0][0] < out[0][1] <= 43.4)
        self.assertTrue(168.6 <= out[1][0] < out[1][1] <= 172.9)

    def test_cue_inside_the_inserted_silence_is_dropped(self):
        self.assertEqual(K.place_cue(self.PACK, 6.2, 6.9, "Gracias por ver el vídeo"), [])

    def test_cue_inside_one_piece_is_just_shifted(self):
        out = K.place_cue(self.PACK, 1.0, 3.0, "Hola")
        self.assertAlmostEqual(out[0][0], 38.3, places=2)
        self.assertAlmostEqual(out[0][1], 40.3, places=2)

    def test_contiguous_pieces_from_a_sliced_long_region_stay_one_cue(self):
        pack = [(100.0, 127.0, 0.0), (127.0, 135.0, 27.0 + K.SEP_S)]      # one long region cut in two: NO gap in the episode
        out = K.place_cue(pack, 25.0, 31.0, "una frase que cruza el corte")
        self.assertEqual(len(out), 1)


class EndToEnd(unittest.TestCase):
    def test_iron_kid_line_after_long_silence_lands_where_it_is_spoken(self):
        # three regions that share ONE pack: two narration pieces, then speech 140 s later (the silent gap must NOT be sent)
        segs = [(2.41, 12.0), (20.0, 26.0), (168.65, 172.79)]
        packs = K.plan_packs(segs)
        self.assertEqual(len(packs), 1)
        self.assertEqual(len(packs[0]), 3)
        # find the pack piece for the 168.65 s speech and let a fake whisper "hear" its first words 0.3 s into it
        (pi, piece), = [(i, x) for i, p in enumerate(packs) for x in p if x[0] == 168.65]
        heard_at = piece[2] + 0.3

        calls = {"n": 0}

        def fake_whisper(path):
            n = calls["n"]
            calls["n"] += 1                                    # transcribe_chunked calls packs in order
            with wave.open(path) as w:                         # the audio sent must be exactly the pack, no long silence
                self.assertAlmostEqual(w.getnframes() / K.SR, K.pack_length(packs[n]), places=2)
            if any(x[0] == 168.65 for x in packs[n]):
                return srt((int(heard_at * 1000), int((heard_at + 3) * 1000), "Aquel al que buscabas ya no existe, Gath."))
            return srt((500, 3500, "narración"))

        with tempfile.TemporaryDirectory() as d:
            wav = os.path.join(d, "ep.wav")
            make_wav(wav, 200)
            cues, stats = K.transcribe_chunked(wav, fake_whisper, W.parse_srt, segs=segs)
        gath = [c for c in cues if "Gath" in c[2]][0]
        self.assertAlmostEqual(gath[0] / 1000, 168.65 + 0.3, places=2)      # NOT 132.46 (the old, wrong timestamp)
        self.assertEqual(cues, sorted(cues, key=lambda c: (c[0], c[1])))
        self.assertGreaterEqual(stats["packs"], 1)

    def test_bad_wav_format_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.wav")
            with wave.open(p, "wb") as w:
                w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100); w.writeframes(b"\x00" * 400)
            with self.assertRaises(ValueError):
                K.transcribe_chunked(p, lambda q: "", W.parse_srt, segs=[(0, 1)])

    def test_zero_length_cue_gets_a_minimum_duration(self):
        segs = [(1.0, 5.0)]
        with tempfile.TemporaryDirectory() as d:
            wav = os.path.join(d, "ep.wav"); make_wav(wav, 10)
            cues, _ = K.transcribe_chunked(wav, lambda q: srt((1000, 1000, "Hola")), W.parse_srt, segs=segs)
        self.assertGreater(cues[0][1], cues[0][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
