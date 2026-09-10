"""
Headless tests for Solari's pure logic.

Everything here runs without a display: no Tk root is created, so this works on
a build agent.  The GUI classes are exercised separately by actually running the
app (see README).
"""

import datetime
import json
import os
import tempfile
import unittest
import zoneinfo

import solari as S


class ColourMaths(unittest.TestCase):

    def test_to_rgb_forms(self):
        self.assertEqual(S.to_rgb("#ffffff"), (255, 255, 255))
        self.assertEqual(S.to_rgb("#000000"), (0, 0, 0))
        self.assertEqual(S.to_rgb("#2277ff"), (0x22, 0x77, 0xFF))
        self.assertEqual(S.to_rgb("#fff"), (255, 255, 255))   # short form

    def test_to_hex_clamps(self):
        self.assertEqual(S.to_hex(300, -20, 128), "#ff0080")
        self.assertEqual(S.to_hex(0, 0, 0), "#000000")

    def test_darken_and_lighten_are_bounded(self):
        self.assertEqual(S.darken("#ffffff", 0.0), "#000000")
        self.assertEqual(S.darken("#ffffff", 1.0), "#ffffff")
        self.assertEqual(S.lighten("#000000", 1.0), "#ffffff")
        self.assertEqual(S.lighten("#000000", 0.0), "#000000")

    def test_lighten_lifts_near_black(self):
        # Multiplying would leave pure black untouched; the card gradient needs
        # a dark card colour to actually brighten at the top.
        self.assertNotEqual(S.lighten("#000000", 0.17), "#000000")

    def test_lerp_endpoints_and_midpoint(self):
        self.assertEqual(S.lerp("#000000", "#ffffff", 0.0), "#000000")
        self.assertEqual(S.lerp("#000000", "#ffffff", 1.0), "#ffffff")
        self.assertEqual(S.lerp("#000000", "#ffffff", 0.5), "#7f7f7f")

    def test_quantise_snaps_and_clamps(self):
        self.assertEqual(S.quantise(0.0), 0.0)
        self.assertEqual(S.quantise(1.0), 1.0)
        self.assertEqual(S.quantise(-5.0), 0.0)
        self.assertEqual(S.quantise(9.0), 1.0)
        # Snapping is what keeps the colour caches hitting during animation
        self.assertEqual(len({S.quantise(i / 1000) for i in range(1001)}),
                         int(S.QUANT) + 1)

    def test_smoothstep_shape(self):
        self.assertEqual(S.smoothstep(0.0), 0.0)
        self.assertEqual(S.smoothstep(1.0), 1.0)
        self.assertEqual(S.smoothstep(0.5), 0.5)
        self.assertEqual(S.smoothstep(-1.0), 0.0)     # clamped
        self.assertEqual(S.smoothstep(2.0), 1.0)
        vals = [S.smoothstep(i / 20) for i in range(21)]
        self.assertTrue(all(b >= a for a, b in zip(vals, vals[1:])))

    def test_is_neon(self):
        self.assertTrue(S.is_neon("#00FF41"))
        self.assertFalse(S.is_neon("#ffffff"))


class ClockMigration(unittest.TestCase):

    def test_minimal_entry_gets_all_defaults(self):
        c = S.migrate_clock({"tz": "Europe/London"})
        self.assertEqual(c["label"], "London")
        self.assertEqual(set(S.DEFAULT_CLOCK), set(c))

    def test_v0_single_colour_key_migrates(self):
        c = S.migrate_clock({"tz": "UTC", "color": "#2277ff"})
        self.assertEqual(c["text_color"], "#2277ff")
        self.assertEqual(c["card_color"], S.PAL_DARK["#2277ff"])

    def test_legacy_greys_are_replaced(self):
        for old in ("#bbbbbb", "#ddcc00"):
            self.assertEqual(S.migrate_clock({"tz": "UTC", "text_color": old})["text_color"],
                             "#ffffff")

    def test_underscored_zone_becomes_readable_label(self):
        self.assertEqual(S.migrate_clock({"tz": "America/New_York"})["label"], "New York")

    def test_unusable_entries_rejected(self):
        for bad in ({}, {"tz": ""}, {"tz": "Mars/Olympus"}, {"tz": 42}, [], "nope", None):
            self.assertIsNone(S.migrate_clock(bad), bad)

    def test_bad_values_fall_back_instead_of_raising(self):
        c = S.migrate_clock({"tz": "UTC", "x": "left", "y": None,
                             "scale": "big", "opacity": float("nan"),
                             "text_color": "not-a-colour"})
        self.assertEqual((c["x"], c["y"]), (100, 100))
        self.assertEqual(c["scale"], 1.0)
        self.assertEqual(c["opacity"], 0.97)
        self.assertEqual(c["text_color"], "#ffffff")

    def test_numeric_fields_are_clamped(self):
        c = S.migrate_clock({"tz": "UTC", "scale": 99, "opacity": 99})
        self.assertEqual(c["scale"], S.SCALE_MAX)
        self.assertEqual(c["opacity"], S.OPACITY_MAX)
        c = S.migrate_clock({"tz": "UTC", "scale": -5, "opacity": -5})
        self.assertEqual(c["scale"], S.SCALE_MIN)
        self.assertEqual(c["opacity"], S.OPACITY_MIN)

    def test_booleans_coerced(self):
        c = S.migrate_clock({"tz": "UTC", "hour24": 0, "show_seconds": "yes"})
        self.assertIs(c["hour24"], False)
        self.assertIs(c["show_seconds"], True)


class ConfigIO(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "cfg.json")

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_missing_file_gives_defaults(self):
        self.assertEqual(len(S.load_config(self.path)), len(S.DEFAULT_CONFIG))

    def test_corrupt_json_gives_defaults(self):
        self._write("{not json at all")
        self.assertEqual(len(S.load_config(self.path)), len(S.DEFAULT_CONFIG))

    def test_round_trip(self):
        cfgs = [S.migrate_clock({"tz": "Asia/Tokyo", "label": "Tokyo"})]
        self.assertTrue(S.save_config(cfgs, self.path))
        self.assertEqual(S.load_config(self.path)[0]["label"], "Tokyo")

    def test_legacy_bare_list_still_loads(self):
        self._write(json.dumps([{"tz": "Europe/Paris", "color": "#cc0000"}]))
        got = S.load_config(self.path)
        self.assertEqual(got[0]["tz"], "Europe/Paris")
        self.assertEqual(got[0]["text_color"], "#cc0000")

    def test_one_bad_entry_does_not_discard_the_good_ones(self):
        # This used to blow away the whole file via a bare assert.
        self._write(json.dumps([{"tz": "Mars/Olympus"}, {"tz": "Asia/Tokyo"}]))
        got = S.load_config(self.path)
        self.assertEqual([c["tz"] for c in got], ["Asia/Tokyo"])

    def test_all_bad_entries_fall_back_to_defaults(self):
        self._write(json.dumps([{"tz": "Mars/Olympus"}]))
        self.assertEqual(len(S.load_config(self.path)), len(S.DEFAULT_CONFIG))

    def test_non_ascii_labels_survive(self):
        cfgs = [S.migrate_clock({"tz": "Europe/Zurich", "label": "Zürich"}),
                S.migrate_clock({"tz": "Asia/Tokyo", "label": "東京"})]
        S.save_config(cfgs, self.path)
        self.assertEqual([c["label"] for c in S.load_config(self.path)],
                         ["Zürich", "東京"])

    def test_save_is_atomic(self):
        S.save_config([S.migrate_clock({"tz": "UTC"})], self.path)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_save_failure_is_reported_not_raised(self):
        self.assertFalse(S.save_config([], os.path.join(self.dir, "no", "such", "f.json")))

    def test_version_is_recorded(self):
        S.save_config([S.migrate_clock({"tz": "UTC"})], self.path)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["version"], S.CONFIG_VERSION)


class ScreenClamping(unittest.TestCase):
    SW, SH = 1920, 1080

    def test_on_screen_position_untouched(self):
        self.assertEqual(S.clamp_to_screen(300, 200, 600, 220, self.SW, self.SH),
                         (300, 200))

    def test_recovers_from_a_detached_monitor(self):
        # Restored at x=3000 on a screen that no longer exists
        x, y = S.clamp_to_screen(3000, 1500, 600, 220, self.SW, self.SH)
        self.assertLessEqual(x, self.SW)
        self.assertLessEqual(y, self.SH)

    def test_negative_coords_stay_reachable(self):
        x, y = S.clamp_to_screen(-5000, -900, 600, 220, self.SW, self.SH)
        self.assertGreaterEqual(x + 600, 0)   # some of it is on screen
        self.assertGreaterEqual(y, 0)         # title bar never above the top

    def test_returns_ints(self):
        x, y = S.clamp_to_screen(10.7, 20.2, 600, 220, self.SW, self.SH)
        self.assertIsInstance(x, int)
        self.assertIsInstance(y, int)


class Labels(unittest.TestCase):

    def _at(self, tz, **kw):
        return datetime.datetime(2026, 9, 10, 12, 0, tzinfo=zoneinfo.ZoneInfo(tz), **kw)

    def test_whole_hour_offsets(self):
        self.assertEqual(S.utc_offset_label(self._at("Asia/Tokyo")), "GMT+9")
        self.assertEqual(S.utc_offset_label(self._at("America/New_York")), "GMT-4")

    def test_half_hour_offset(self):
        self.assertEqual(S.utc_offset_label(self._at("Asia/Kolkata")), "GMT+5:30")

    def test_utc_has_no_sign(self):
        self.assertEqual(
            S.utc_offset_label(datetime.datetime(2026, 9, 10, tzinfo=datetime.timezone.utc)),
            "GMT")

    def test_naive_datetime_is_handled(self):
        self.assertEqual(S.utc_offset_label(datetime.datetime(2026, 9, 10)), "GMT")

    def test_day_badge(self):
        local = datetime.datetime(2026, 9, 10, 22, 0)
        self.assertEqual(S.day_offset_label(datetime.datetime(2026, 9, 11, 11, 0), local), "+1")
        self.assertEqual(S.day_offset_label(datetime.datetime(2026, 9, 9, 3, 0), local), "-1")
        self.assertEqual(S.day_offset_label(datetime.datetime(2026, 9, 10, 3, 0), local), "")


class PathDisplay(unittest.TestCase):

    def test_home_is_abbreviated(self):
        p = os.path.join(os.path.expanduser("~"), "Solari")
        self.assertTrue(S.pretty_path(p).startswith("~"))

    def test_long_paths_are_elided_but_keep_the_tail(self):
        out = S.pretty_path("/" + "/".join("verylongsegment" for _ in range(12)))
        self.assertLessEqual(len(out), 46)
        self.assertIn("...", out)
        self.assertTrue(out.endswith("segment"))


@unittest.skipUnless(S.HAS_PIL, "Pillow not installed")
class Rendering(unittest.TestCase):

    def test_card_has_requested_size_and_is_opaque(self):
        img = S.render_card(82, 104, 12, "#1e1e1e", None, S.THEME["well"], False)
        self.assertEqual(img.size, (82, 104))
        self.assertEqual(img.mode, "RGB")

    def test_card_is_lit_from_above(self):
        img = S.render_card(82, 104, 12, "#1e1e1e", None, S.THEME["well"], False)
        top = sum(img.getpixel((41, 6)))
        bottom = sum(img.getpixel((41, 98)))
        self.assertGreater(top, bottom, "card face should be brighter at the top")

    def test_corners_blend_to_the_colour_behind(self):
        behind = S.THEME["well"]
        img = S.render_card(82, 104, 12, "#1e1e1e", None, behind, False)
        self.assertEqual(img.getpixel((0, 0)), S.to_rgb(behind))

    def test_rolling_face_is_darker_through_the_middle(self):
        args = (82, 104, 12, "#1e1e1e", None, S.THEME["well"])
        still = S.render_card(*args, False).getpixel((41, 52))
        rolling = S.render_card(*args, True).getpixel((41, 52))
        self.assertLess(sum(rolling), sum(still))

    def test_render_is_cached(self):
        args = (82, 104, 12, "#123456", None, S.THEME["well"], False)
        S.render_card(*args)
        before = S.render_card.cache_info().hits
        S.render_card(*args)
        self.assertEqual(S.render_card.cache_info().hits, before + 1)

    def test_chassis_size(self):
        img = S.render_chassis(400, 200, 8, 16, (20, 50, 380, 150), None)
        self.assertEqual(img.size, (400, 200))

    def test_app_icon(self):
        self.assertEqual(S.make_app_icon(64).size, (64, 64))


if __name__ == "__main__":
    unittest.main(verbosity=2)
