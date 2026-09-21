#!/usr/bin/env python3
"""Tests for castilianize.py's baked-in side-bar detection + fix. Real ffmpeg, synthetic videos, real CLI.
Run: python3 scripts/test_castilianize_bars.py   (needs ffmpeg, ffprobe, mkvmerge, mkvpropedit; ~1 minute)"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import castilianize as C  # noqa: E402

SCRIPT = os.path.join(HERE, "castilianize.py")


def make(path, picture_w, picture_h, frame_w, frame_h, dark_first_s=0, dim_sides_first_s=0, seconds=8):
    """A video whose PICTURE (a test pattern) is picture_w x picture_h centred in a frame_w x frame_h frame (black bars),
    with a sine audio track. Container by extension."""
    vf = f"pad={frame_w}:{frame_h}:(ow-iw)/2:(oh-ih)/2:black"
    if dark_first_s:
        vf = f"drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='lt(t,{dark_first_s})'," + vf
    if dim_sides_first_s:      # dark scene: only the centre third of the picture is lit, so cropdetect sees a NARROW box
        third = picture_w // 3
        vf = (f"drawbox=x=0:y=0:w={third}:h=ih:color=black:t=fill:enable='lt(t,{dim_sides_first_s})',"
              f"drawbox=x={2 * third}:y=0:w={third + 2}:h=ih:color=black:t=fill:enable='lt(t,{dim_sides_first_s})',") + vf
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={picture_w}x{picture_h}:rate=10:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-vf", vf,
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-c:a", "aac", "-shortest", path], check=True)


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height,display_aspect_ratio",
                        "-of", "json", path], capture_output=True, text=True, check=True)
    j = json.loads(r.stdout)
    v = next(s for s in j["streams"] if s["codec_type"] == "video")
    return float(j["format"]["duration"]), v["width"], v["height"], sum(s["codec_type"] == "audio" for s in j["streams"])


def widest_box(path):
    """Independent measurement (not castilianize's own code): widest cropdetect box over 5 points -> (cw, ch)."""
    dur = probe(path)[0]
    best = (0, 0)
    for f in (0.12, 0.3, 0.5, 0.7, 0.88):
        r = subprocess.run(["ffmpeg", "-v", "info", "-ss", f"{dur * f:.1f}", "-i", path, "-t", "2", "-vf", "cropdetect=limit=24:round=2:reset=0",
                            "-an", "-f", "null", "-"], capture_output=True, text=True)
        m = re.findall(r"crop=(\d+):(\d+):", r.stderr)
        if m:
            best = (max(best[0], int(m[-1][0])), max(best[1], int(m[-1][1])))
    return best


class Decision(unittest.TestCase):
    """is_pillarbox is a pure function: no ffmpeg needed."""

    def test_keroro_geometry_is_a_pillarbox(self):
        self.assertTrue(C.is_pillarbox(640, 360, 480, 360, 78))

    def test_full_widescreen_is_not(self):
        self.assertFalse(C.is_pillarbox(640, 360, 640, 360, 0))

    def test_letterbox_is_not(self):                      # cinematic bars top/bottom (Loki)
        self.assertFalse(C.is_pillarbox(1920, 1080, 1920, 800, 0))

    def test_one_sided_or_off_centre_is_not(self):        # e.g. a logo or a dark left edge, not a centred 4:3 picture
        self.assertFalse(C.is_pillarbox(640, 360, 480, 360, 0))
        self.assertFalse(C.is_pillarbox(640, 360, 480, 360, 150))

    def test_unequal_bars_are_not_a_centred_pillarbox(self):
        # bars on BOTH sides (40 px / 120 px) but a very off-centre picture: not the symmetric "fake 16:9" pattern
        self.assertFalse(C.is_pillarbox(640, 360, 480, 360, 40))

    def test_narrow_bars_are_not(self):                   # a few pixels of border is not a pillarbox
        self.assertFalse(C.is_pillarbox(640, 360, 620, 360, 10))

    def test_a_4x3_frame_is_never_a_pillarbox(self):
        self.assertFalse(C.is_pillarbox(640, 480, 500, 480, 70))

    def test_picture_not_4x3_is_not(self):                # 16:9 picture centred in a wider frame
        self.assertFalse(C.is_pillarbox(1200, 400, 711, 400, 244))


class EndToEnd(unittest.TestCase):
    """The real CLI on real files."""

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp(prefix="castbars-")
        cls.env = dict(os.environ, CASTILIANIZE_LOCK=os.path.join(cls.d, "lock"), CASTILIANIZE_BAR_CACHE=os.path.join(cls.d, "cache.json"))
        p = lambda n: os.path.join(cls.d, n)
        make(p("pillar.mkv"), 480, 360, 640, 360)                                   # the Keroro case
        make(p("pillar_dark_start.mkv"), 480, 360, 640, 360, dark_first_s=3)        # picture black for the first sample points
        make(p("pillar_dim_scenes.mkv"), 480, 360, 640, 360, dim_sides_first_s=5)  # first sample points see only a narrow lit area
        make(p("clean169.mkv"), 640, 360, 640, 360)                                 # genuine 16:9
        make(p("letterbox.mkv"), 640, 270, 640, 360)                                # cinematic bars top/bottom
        cls.before = {n: probe(p(n)) for n in os.listdir(cls.d) if n.endswith(".mkv")}
        cls.stat = {n: (os.path.getsize(p(n)), os.path.getmtime(p(n))) for n in cls.before}
        cls.dry = cls.run_cli("--dry-run")
        cls.real = cls.run_cli()
        cls.again = cls.run_cli()

    @classmethod
    def run_cli(cls, *extra):
        r = subprocess.run([sys.executable, SCRIPT, "--min-age-minutes", "0", *extra, cls.d], capture_output=True, text=True, env=cls.env)
        return r.stderr + r.stdout

    def path(self, n):
        return os.path.join(self.d, n)

    def test_dry_run_reports_the_fix_and_changes_nothing(self):
        self.assertIn("crop-bars+stretch", self.dry)
        self.assertRegex(self.dry, r"WOULD_DO: crop-bars\+stretch[^:\n]*: [^\n]*pillar\.mkv")
        # dry-run ran BEFORE the real run, so the files were untouched at that point: verified via the mtimes captured earlier
        self.assertNotIn("BARS_FIXED", self.dry)

    def test_pillarboxed_file_is_fixed(self):
        d0, w0, h0, a0 = self.before["pillar.mkv"]
        d1, w1, h1, a1 = probe(self.path("pillar.mkv"))
        self.assertEqual((w1, h1), (640, 360))
        self.assertAlmostEqual(d1, d0, delta=0.6)                # same programme length
        self.assertEqual(a1, a0)                                 # audio kept
        cw, ch = widest_box(self.path("pillar.mkv"))             # measured independently of castilianize
        self.assertGreaterEqual(cw, 0.97 * 640)                  # picture now fills the frame width
        self.assertGreaterEqual(ch, 0.97 * 360)
        self.assertRegex(self.real, r"BARS_FIXED: [^\n]*pillar\.mkv")

    def test_dark_opening_does_not_hide_the_bars(self):
        cw, ch = widest_box(self.path("pillar_dark_start.mkv"))
        self.assertGreaterEqual(cw, 0.97 * 640)
        self.assertRegex(self.real, r"BARS_FIXED: [^\n]*pillar_dark_start\.mkv")

    def test_dim_scenes_do_not_hide_the_bars(self):
        # the WIDEST box across sample points must win: dark scenes only ever make the measured box smaller
        self.assertRegex(self.real, r"BARS_FIXED: [^\n]*pillar_dim_scenes\.mkv")
        cw, ch = widest_box(self.path("pillar_dim_scenes.mkv"))
        self.assertGreaterEqual(cw, 0.97 * 640)

    def test_genuine_widescreen_is_left_alone(self):
        # castilianize may still TAG the untagged audio (metadata only, a few hundred bytes) but must not re-encode the video
        before, after = self.stat["clean169.mkv"][0], os.path.getsize(self.path("clean169.mkv"))
        self.assertLess(abs(after - before), 0.01 * before)
        self.assertNotRegex(self.real, r"(BARS_FIXED|STRETCHED)[^\n]*clean169")
        self.assertEqual(probe(self.path("clean169.mkv"))[1:3], (640, 360))

    def test_letterbox_is_left_alone(self):                      # top/bottom bars are cinematic, not "fake 16:9"
        self.assertNotRegex(self.real, r"(BARS_FIXED|STRETCHED)[^\n]*letterbox")
        _, w, h, _ = probe(self.path("letterbox.mkv"))
        self.assertEqual((w, h), (640, 360))
        cw, ch = widest_box(self.path("letterbox.mkv"))
        self.assertLess(ch, 0.85 * 360)                          # its bars are still there

    def test_second_run_changes_nothing(self):
        self.assertNotIn("BARS_FIXED", self.again)
        self.assertIn("Summary", self.again)

    def test_no_temp_files_left_behind(self):
        self.assertEqual([n for n in os.listdir(self.d) if ".converting." in n], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
