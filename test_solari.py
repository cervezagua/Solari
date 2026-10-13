"""
Tests for Solari.

The pure-logic tests run anywhere.  The GUI tests need a display (CI runs
them under Xvfb on Linux and on a real desktop on Windows) and skip
themselves without one.

Classes named Regression* each pin a bug that was reproduced before it was
fixed; the docstring says what used to happen.
"""

import datetime
import functools
import inspect
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
import zoneinfo
from unittest import mock

import solari as S


@functools.lru_cache(maxsize=1)
def _display_available():
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except Exception:
        return False


needs_display = unittest.skipUnless(_display_available(), "no display available")
needs_pil = unittest.skipUnless(S.HAS_PIL, "Pillow not installed")


def pump(widget, seconds=0.0, until=None):
    """Run the event loop for a while, or until a condition holds."""
    end = time.monotonic() + seconds
    while True:
        widget.update()
        if until is not None and until():
            return True
        if time.monotonic() >= end:
            return until is None
        time.sleep(0.01)


def temp_app(clocks=None, **kw):
    """An App on a scratch config, so tests never touch the real one."""
    d = tempfile.mkdtemp(prefix="solari-test-")
    path = os.path.join(d, "solari_config.json")
    if clocks is not None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": S.CONFIG_VERSION, "clocks": clocks}, f)
    with mock.patch.object(S, "config_dir", lambda: d):
        app = S.App(config_file=path, **kw)
    app._test_dir = d
    app.update()
    return app


def saved_clocks(app):
    with open(app.config_file, encoding="utf-8") as f:
        return json.load(f)["clocks"]


# ===========================================================================
#  Pure logic
# ===========================================================================
class ColourMaths(unittest.TestCase):

    def test_to_rgb_forms(self):
        self.assertEqual(S.to_rgb("#ffffff"), (255, 255, 255))
        self.assertEqual(S.to_rgb("#2277ff"), (0x22, 0x77, 0xFF))
        self.assertEqual(S.to_rgb("#fff"), (255, 255, 255))

    def test_to_hex_clamps(self):
        self.assertEqual(S.to_hex(300, -20, 128), "#ff0080")

    def test_darken_and_lighten_are_bounded(self):
        self.assertEqual(S.darken("#ffffff", 0.0), "#000000")
        self.assertEqual(S.lighten("#000000", 1.0), "#ffffff")
        self.assertNotEqual(S.lighten("#000000", 0.17), "#000000")

    def test_lerp(self):
        self.assertEqual(S.lerp("#000000", "#ffffff", 0.5), "#7f7f7f")

    def test_quantise_bounds_the_number_of_colours(self):
        self.assertEqual(len({S.quantise(i / 1000) for i in range(1001)}), int(S.QUANT) + 1)
        self.assertEqual(S.quantise(-3), 0.0)
        self.assertEqual(S.quantise(9), 1.0)

    def test_easing_curves_are_monotonic_and_clamped(self):
        for fn in (S.smoothstep, S.ease_out_cubic):
            vals = [fn(i / 20) for i in range(21)]
            self.assertTrue(all(b >= a for a, b in zip(vals, vals[1:])))
            self.assertEqual(fn(-1), 0.0)
            self.assertEqual(fn(2), 1.0)

    def test_contrast_ratio(self):
        self.assertAlmostEqual(S.contrast("#ffffff", "#000000"), 21.0, places=1)
        self.assertAlmostEqual(S.contrast("#777777", "#777777"), 1.0)

    def test_every_preset_is_legible(self):
        for name, tc, cc, fin in S.PRESETS:
            self.assertGreaterEqual(S.contrast(tc, cc), 4.5, name)
            self.assertIn(fin, S.FINISHES)


class ClockMigration(unittest.TestCase):

    def test_minimal_entry_gets_all_defaults(self):
        c = S.migrate_clock({"tz": "Europe/London"})
        self.assertEqual(c["label"], "London")
        self.assertEqual(set(S.DEFAULT_CLOCK), set(c))

    def test_v0_single_colour_key_migrates(self):
        c = S.migrate_clock({"tz": "UTC", "color": "#2277ff"})
        self.assertEqual(c["text_color"], "#2277ff")
        self.assertEqual(c["card_color"], S.PAL_DARK["#2277ff"])

    def test_v2_boolean_seconds_migrate(self):
        self.assertEqual(S.migrate_clock({"tz": "UTC", "show_seconds": True})["seconds"], "small")
        self.assertEqual(S.migrate_clock({"tz": "UTC", "show_seconds": False})["seconds"], "off")
        self.assertEqual(S.migrate_clock({"tz": "UTC", "seconds": "large"})["seconds"], "large")

    def test_underscored_zone_becomes_readable_label(self):
        self.assertEqual(S.migrate_clock({"tz": "America/New_York"})["label"], "New York")

    def test_entries_without_a_zone_are_rejected(self):
        for bad in ({}, {"tz": ""}, {"tz": "   "}, {"tz": 42}, [], "nope", None):
            self.assertIsNone(S.migrate_clock(bad), bad)

    def test_bad_values_fall_back_instead_of_raising(self):
        c = S.migrate_clock({"tz": "UTC", "x": "left", "y": None, "scale": "big",
                             "opacity": float("nan"), "text_color": "not-a-colour",
                             "finish": "chrome", "face": "comic"})
        self.assertEqual((c["x"], c["y"]), (100, 100))
        self.assertEqual(c["scale"], 1.0)
        self.assertEqual(c["opacity"], 0.98)
        self.assertEqual(c["text_color"], "#f4f4f2")
        self.assertEqual((c["finish"], c["face"]), ("graphite", "modern"))

    def test_numeric_fields_are_clamped(self):
        c = S.migrate_clock({"tz": "UTC", "scale": 99, "opacity": 99})
        self.assertEqual((c["scale"], c["opacity"]), (S.SCALE_MAX, S.OPACITY_MAX))
        c = S.migrate_clock({"tz": "UTC", "scale": -5, "opacity": -5})
        self.assertEqual((c["scale"], c["opacity"]), (S.SCALE_MIN, S.OPACITY_MIN))


class ConfigIO(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "cfg.json")

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_missing_file_gives_the_starter_clocks(self):
        self.assertEqual(len(S.load_config(self.path)), len(S.DEFAULT_CONFIG))

    def test_round_trip(self):
        cfgs = [S.migrate_clock({"tz": "Asia/Tokyo", "label": "Tokyo"})]
        self.assertTrue(S.save_config(cfgs, self.path))
        self.assertEqual(S.load_config(self.path)[0]["label"], "Tokyo")

    def test_legacy_bare_list_still_loads(self):
        self._write(json.dumps([{"tz": "Europe/Paris", "color": "#cc0000"}]))
        got = S.load_config(self.path)
        self.assertEqual((got[0]["tz"], got[0]["text_color"]), ("Europe/Paris", "#cc0000"))

    def test_one_bad_entry_does_not_discard_the_good_ones(self):
        self._write(json.dumps([{"label": "no zone"}, {"tz": "Asia/Tokyo"}]))
        self.assertEqual([c["tz"] for c in S.load_config(self.path)], ["Asia/Tokyo"])

    def test_non_ascii_labels_survive(self):
        cfgs = [S.migrate_clock({"tz": "Europe/Zurich", "label": "Zürich"}),
                S.migrate_clock({"tz": "Asia/Tokyo", "label": "東京"})]
        S.save_config(cfgs, self.path)
        self.assertEqual([c["label"] for c in S.load_config(self.path)], ["Zürich", "東京"])

    def test_save_is_atomic_and_versioned(self):
        S.save_config([S.migrate_clock({"tz": "UTC"})], self.path)
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["version"], S.CONFIG_VERSION)

    def test_save_failure_is_reported_not_raised(self):
        self.assertFalse(S.save_config([], os.path.join(self.dir, "no", "such", "f.json")))


class RegressionConfigLoss(unittest.TestCase):
    """Four ways the user's clocks used to be lost."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "cfg.json")

    def test_unknown_zone_is_kept_not_dropped(self):
        """Without a tz database (Windows has none built in) every clock was
        dropped on load - and the next save wrote the starter clocks over
        the user's file."""
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([{"tz": "Mars/Olympus", "label": "Base"}], f)
        got = S.load_config(self.path)
        self.assertEqual([(c["tz"], c["label"]) for c in got], [("Mars/Olympus", "Base")])

    def test_corrupt_file_is_backed_up_before_anything_replaces_it(self):
        """A config that failed to parse was silently overwritten."""
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        S.load_config(self.path)
        backups = [n for n in os.listdir(self.dir) if ".unreadable-" in n]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(self.dir, backups[0]), encoding="utf-8") as f:
            self.assertEqual(f.read(), "{not json")

    def test_removing_every_clock_sticks(self):
        """An empty list loaded as 'no config' and resurrected the starters."""
        S.save_config([], self.path)
        self.assertEqual(S.load_config(self.path), [])

    def test_save_retries_a_sharing_violation(self):
        """os.replace raises PermissionError while an indexer or antivirus
        holds the file; the save used to give up on the first one."""
        real = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) < 3:
                raise PermissionError(13, "in use")
            return real(src, dst)

        with mock.patch.object(S.os, "replace", flaky), mock.patch.object(S.time, "sleep"):
            self.assertTrue(S.save_config([S.migrate_clock({"tz": "UTC"})], self.path))
        self.assertEqual(len(calls), 3)
        self.assertEqual(S.load_config(self.path)[0]["tz"], "UTC")


class RegressionMultiMonitor(unittest.TestCase):
    """Clocks on a second monitor were moved onto the first at every launch."""

    MONS = [(0, 0, 1920, 1040), (1920, 0, 3840, 1040)]

    def test_clock_on_second_monitor_stays_put(self):
        self.assertEqual(S.ensure_visible(2500, 300, 520, 190, self.MONS), (2500, 300))

    def test_clock_on_a_detached_monitor_comes_back(self):
        x, y = S.ensure_visible(5000, 300, 520, 190, self.MONS)
        self.assertTrue(any(l <= x and x + 520 <= r for l, t, r, b in self.MONS))

    def test_mostly_offscreen_clock_is_pulled_in_fully(self):
        x, y = S.ensure_visible(-5000, -900, 520, 190, self.MONS)
        self.assertEqual((x, y), (0, 0))

    def test_snaps_to_its_own_monitors_edges(self):
        x, y = S.snap(1925, 4, 520, 190, self.MONS, [], 10)
        self.assertEqual((x, y), (1920, 0))

    def test_snaps_to_a_sibling_clock(self):
        x, y = S.snap(105, 297, 520, 190, self.MONS, [(100, 100, 520, 190)], 10)
        self.assertEqual((x, y), (100, 290))

    def test_returns_ints(self):
        for v in S.ensure_visible(10.7, 20.2, 520, 190, self.MONS):
            self.assertIsInstance(v, int)


class Labels(unittest.TestCase):

    def _at(self, tz):
        return datetime.datetime(2026, 9, 10, 12, 0, tzinfo=zoneinfo.ZoneInfo(tz))

    def test_offsets_use_a_real_minus_sign(self):
        self.assertEqual(S.utc_offset_label(self._at("Asia/Tokyo")), "GMT+9")
        self.assertEqual(S.utc_offset_label(self._at("America/New_York")), "GMT\u22124")
        self.assertEqual(S.utc_offset_label(self._at("Asia/Kolkata")), "GMT+5:30")
        self.assertEqual(S.utc_offset_label(datetime.datetime(2026, 9, 10)), "GMT")

    def test_day_badge(self):
        local = datetime.datetime(2026, 9, 10, 22, 0)
        self.assertEqual(S.day_offset_label(datetime.datetime(2026, 9, 11, 11), local), "+1")
        self.assertEqual(S.day_offset_label(datetime.datetime(2026, 9, 9, 3), local), "\u22121")
        self.assertEqual(S.day_offset_label(datetime.datetime(2026, 9, 10, 3), local), "")

    def test_relative_offset(self):
        local = self._at("Europe/London")
        self.assertEqual(S.relative_offset_label(self._at("Asia/Tokyo"), local), "8 h ahead")
        self.assertEqual(S.relative_offset_label(self._at("America/New_York"), local), "5 h behind")
        self.assertEqual(S.relative_offset_label(self._at("Asia/Kolkata"), local), "4:30 h ahead")
        self.assertEqual(S.relative_offset_label(self._at("Europe/Dublin"), local), "Same as local")

    def test_pretty_path(self):
        p = os.path.join(os.path.expanduser("~"), "Solari")
        self.assertTrue(S.pretty_path(p).startswith("~"))
        out = S.pretty_path("/" + "/".join("verylongsegment" for _ in range(12)))
        self.assertLessEqual(len(out), 46)
        self.assertTrue(out.endswith("segment"))


class Digits(unittest.TestCase):

    def _d(self, h, m, s, hour24):
        return S.clock_digits(datetime.datetime(2026, 1, 1, h, m, s), {"hour24": hour24})

    def test_minutes_and_seconds_always_show_two_digits(self):
        d = self._d(9, 5, 7, True)
        self.assertEqual((d["m1"], d["m2"], d["s1"], d["s2"]), ("0", "5", "0", "7"))

    def test_24_hour_keeps_the_leading_zero(self):
        d = self._d(9, 5, 0, True)
        self.assertEqual((d["h1"], d["h2"]), ("0", "9"))

    def test_12_hour_blanks_a_leading_zero(self):
        """Regression: 12-hour mode showed 09:05 where a clock reads 9:05."""
        d = self._d(21, 5, 0, False)
        self.assertEqual((d["h1"], d["h2"]), (" ", "9"))
        d = self._d(22, 5, 0, False)
        self.assertEqual((d["h1"], d["h2"]), ("1", "0"))

    def test_mini_time(self):
        now = datetime.datetime(2026, 1, 1, 9, 5)
        self.assertEqual(S.mini_time(now, {"hour24": True}), "09:05")
        self.assertEqual(S.mini_time(now, {"hour24": False}), " 9:05")


class Layout(unittest.TestCase):

    def _cfg(self, **kw):
        return dict(S.DEFAULT_CLOCK, **kw)

    def test_seconds_modes(self):
        for mode, cards in (("off", 4), ("small", 6), ("large", 6)):
            L = S.clock_layout(self._cfg(seconds=mode), 1.0)
            self.assertEqual(sum(1 for s in L["slots"] if s[0] == "card"), cards, mode)
        small = S.clock_layout(self._cfg(seconds="small"), 1.0)
        large = S.clock_layout(self._cfg(seconds="large"), 1.0)
        self.assertLess(small["sw"], large["sw"])

    def test_everything_sits_inside_the_housing(self):
        for mode in S.SECONDS_MODES:
            for s in (0.4, 1.0, 2.7):
                L = S.clock_layout(self._cfg(seconds=mode), s)
                x1, y1, x2, y2 = L["well"]
                self.assertTrue(0 < x1 < x2 < L["w"] and 0 < y1 < y2 < L["h"])
                for kind, key, x, y, w, h, rad in L["slots"]:
                    self.assertLessEqual(L["row_x"] + x + w, x2)
                    self.assertLessEqual(L["row_y"] + y + h, y2)
                self.assertLess(L["label_x"] + L["label_max"], L["edit_x"])

    def test_layout_scales_linearly(self):
        a = S.clock_layout(self._cfg(), 1.0)
        b = S.clock_layout(self._cfg(), 2.0)
        self.assertAlmostEqual(b["w"] / a["w"], 2.0, delta=0.03)

    def test_regression_display_scale_is_applied(self):
        """Geometry ignored the display's DPI, so on a 200 % screen every
        clock came out half its intended physical size."""
        cfg = self._cfg()
        with mock.patch.object(S, "UI_SCALE", 2.0):
            self.assertEqual(S.effective_scale(cfg), 2.0)
            L = S.clock_layout(cfg, S.effective_scale(cfg))
        self.assertEqual(L["cw"], S.clock_layout(cfg, 1.0)["cw"] * 2)
        with mock.patch.object(S, "UI_SCALE", 1.5):
            self.assertEqual(S.dp(10), 15)


@needs_pil
class Rendering(unittest.TestCase):

    def test_surface_size_mode_and_light(self):
        img = S.render_surface(120, 60, 10, "#2a2a30", S.THEME["bg"])
        self.assertEqual((img.size, img.mode), ((120, 60), "RGB"))
        self.assertGreater(sum(img.getpixel((60, 3))), sum(img.getpixel((60, 56))))

    def test_inset_surface_inverts_the_lighting(self):
        img = S.render_surface(120, 60, 10, "#2a2a30", S.THEME["bg"], inset=True)
        self.assertLess(sum(img.getpixel((60, 3))), sum(img.getpixel((60, 56))))

    def test_corners_blend_to_the_colour_behind(self):
        img = S.render_surface(80, 40, 12, "#ff0000", "#00ff00")
        self.assertEqual(img.getpixel((0, 0)), (0, 255, 0))

    def test_draft_renders_match_in_size(self):
        a = S.render_surface(90, 50, 8, "#333333", "#000000", ss=3)
        b = S.render_surface(90, 50, 8, "#333333", "#000000", ss=1)
        self.assertEqual(a.size, b.size)

    def test_rolling_card_is_darker_through_the_middle(self):
        rest = S.render_card(60, 80, 8, "#2a2a2a", None, "#000000", False)
        roll = S.render_card(60, 80, 8, "#2a2a2a", None, "#000000", True)
        self.assertLess(sum(roll.getpixel((30, 40))), sum(rest.getpixel((30, 40))))

    def test_housing_has_flush_square_corners(self):
        """The resize grip sits in a square corner, so the housing is square
        too: its corner pixels are the housing, not the desktop."""
        L = S.clock_layout(dict(S.DEFAULT_CLOCK), 1.0)
        for finish in S.FINISHES:
            img = S.render_housing(L["w"], L["h"], finish, L["well"], L["well_r"])
            self.assertEqual(img.size, (L["w"], L["h"]))
            body = img.getpixel((L["w"] // 2, 6))
            corner = img.getpixel((1, 1))
            self.assertLess(sum(abs(a - b) for a, b in zip(body, corner)), 60, finish)

    def test_well_is_exactly_the_colour_cards_blend_against(self):
        L = S.clock_layout(dict(S.DEFAULT_CLOCK), 1.0)
        img = S.render_housing(L["w"], L["h"], "aluminium", L["well"], L["well_r"])
        x1, y1, x2, y2 = L["well"]
        self.assertEqual(img.getpixel(((x1 + x2) // 2, (y1 + y2) // 2)), S.to_rgb(S.THEME["well"]))

    def test_digit_sprites(self):
        self.assertIsNone(S.digit_sprite(" ", 40, 60, "#ffffff"))
        img = S.digit_sprite("8", 40, 60, "#ffffff")
        self.assertEqual((img.size, img.mode), ((40, 60), "RGBA"))
        self.assertGreater(img.getbbox()[3] - img.getbbox()[1], 30)
        blurred = S.digit_sprite("8", 40, 60, "#ffffff", blur=True)
        self.assertNotEqual(img.tobytes(), blurred.tobytes())

    def test_tracking_widens_text(self):
        plain = S.text_sprite("LONDON", "ui_bold", 11, "#ffffff")
        tracked = S.text_sprite("LONDON", "ui_bold", 11, "#ffffff", 0.2)
        self.assertGreater(tracked.width, plain.width)

    def test_fit_text_ellipsises(self):
        out = S.fit_text("A VERY LONG CITY NAME INDEED", "ui_bold", 11, 0.1, 60)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(S.text_sprite(out, "ui_bold", 11, "#fff", 0.1).width, 60)

    def test_icons_render_at_size(self):
        for name in ("close", "edit", "locate", "trash", "search", "plus", "alert", "copy"):
            img = S.icon_sprite(name, 18, "#ffffff")
            self.assertEqual(img.size, (18, 18))
            self.assertIsNotNone(img.getbbox(), name)

    def test_compose_matches_the_live_layout(self):
        for mode in S.SECONDS_MODES:
            cfg = dict(S.DEFAULT_CLOCK, seconds=mode)
            L = S.clock_layout(cfg, 1.0)
            self.assertEqual(S.compose_clock(cfg, scale=1.0).size, (L["w"], L["h"]))

    def test_mini_clock_and_preset_tiles(self):
        img = S.mini_clock("09:41", "#ffffff", "#1e1f22", "modern", 38, S.THEME["surface"])
        self.assertEqual(img.height, 38)
        self.assertGreater(img.width, img.height * 2)
        self.assertEqual(S.mini_clock(" 9:41", "#ffffff", "#1e1f22", "modern", 38,
                                      S.THEME["surface"]).size, img.size)
        tile = S.preset_tile("#ffffff", "#1e1f22", "graphite", 37, True, S.THEME["bg"])
        self.assertGreater(tile.width, 37)

    def test_fonts_resolve_for_every_role(self):
        self.assertEqual(set(S.font_report()), set(S._TTF_CHAINS))
        for role in S._TTF_CHAINS:
            self.assertIsNotNone(S.ttf(role, 20).getbbox("0"))

    def test_app_icon(self):
        self.assertEqual(S.make_app_icon(48).size, (48, 48))


class RegressionZoneSearch(unittest.TestCase):
    """The zone picker could not find most of the places people look for."""

    def _top(self, q):
        hits = S.search_zones(q)
        return hits[0] if hits else None

    def test_spaces_match_underscores(self):
        """'new york' found nothing: the list held America/New_York."""
        self.assertEqual(self._top("new york").key, "America/New_York")

    def test_major_cities_without_their_own_zone(self):
        for q, key in (("san francisco", "America/Los_Angeles"), ("beijing", "Asia/Shanghai"),
                       ("mumbai", "Asia/Kolkata"), ("frankfurt", "Europe/Berlin")):
            self.assertEqual(self._top(q).key, key, q)

    def test_former_names_and_abbreviations(self):
        for q, key in (("kiev", "Europe/Kyiv"), ("calcutta", "Asia/Kolkata"),
                       ("pst", "America/Los_Angeles"), ("jst", "Asia/Tokyo")):
            hit = self._top(q)
            self.assertIsNotNone(hit, q)
            self.assertEqual(hit.key, key, q)

    def test_country_names(self):
        top = S.search_zones("india")[:3]
        self.assertTrue(all(e.key == "Asia/Kolkata" for e in top))
        self.assertIn("Asia/Tokyo", {e.key for e in S.search_zones("japan")})

    def test_utc_offsets(self):
        self.assertIn("Asia/Tokyo", {e.key for e in S.search_zones("+9")})
        self.assertIn("Asia/Kolkata", {e.key for e in S.search_zones("gmt+5:30")})

    def test_empty_query_lists_everything(self):
        self.assertEqual(len(S.search_zones("")), len(S.zone_catalog()))
        self.assertGreater(len(S.zone_catalog()), 300)

    def test_reopening_keeps_the_chosen_city(self):
        """A clock set to San Francisco reopened on Los Angeles."""
        self.assertEqual(S.entry_for("America/Los_Angeles", "San Francisco").city, "San Francisco")
        self.assertEqual(S.entry_for("America/Los_Angeles", "Office").city, "Los Angeles")

    def test_catalog_is_not_built_at_import(self):
        """Enumerating the tz database at import slowed every launch."""
        out = subprocess.run(
            [sys.executable, "-c",
             "import solari; print(solari.zone_catalog.cache_info().currsize)"],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(S.__file__)),
            timeout=60)
        self.assertEqual(out.stdout.strip(), "0", out.stderr)


class RegressionStartupEntry(unittest.TestCase):

    def test_source_launch_registers_the_interpreter(self):
        """Run from source, the Run key held the bare .py path - which opens
        in whatever .py is associated with, often an editor, at every login."""
        cmd = S.startup_command()
        self.assertTrue(cmd.startswith(f'"{os.path.dirname(sys.executable)}'), cmd)
        self.assertIn(os.path.abspath(S.__file__), cmd)
        self.assertTrue(cmd.endswith("--background"))

    def test_frozen_launch_registers_the_exe(self):
        with mock.patch.object(S.sys, "frozen", True, create=True), \
                mock.patch.object(S.sys, "executable", r"C:\Apps\Solari.exe"):
            self.assertEqual(S.startup_command(), '"C:\\Apps\\Solari.exe" --background')


class RegressionSingleInstance(unittest.TestCase):
    """Launch-at-login plus a manual launch gave two copies of every clock."""

    def test_second_lock_is_refused_until_released(self):
        d = tempfile.mkdtemp()
        a, b = S.InstanceLock(d), S.InstanceLock(d)
        self.assertTrue(a.acquire())
        try:
            self.assertFalse(b.acquire())
        finally:
            a.release()
        self.assertTrue(b.acquire())
        b.release()

    def test_second_launch_hands_over_and_exits(self):
        d = tempfile.mkdtemp()
        held = S.InstanceLock(d)
        self.assertTrue(held.acquire())
        try:
            with mock.patch.object(S, "config_dir", lambda: d), \
                    mock.patch.object(S, "App", side_effect=AssertionError("started a 2nd app")):
                self.assertEqual(S.main([]), 0)
            self.assertTrue(held.take_show_request())
            self.assertFalse(held.take_show_request())
        finally:
            held.release()


# ===========================================================================
#  GUI
# ===========================================================================
TK_INSTANCE_ATTRS = {"_w", "_name", "tk", "master", "children", "widgetName",
                     "_tclCommands", "_last_child_ids"}
INTENTIONAL_OVERRIDES = {"destroy", "report_callback_exception"}


def _tk_names(cls):
    names = set()
    for base in cls.__mro__:
        if base.__module__.startswith("tkinter"):
            names |= set(vars(base))
    return names


class RegressionTkNameCollisions(unittest.TestCase):
    """Two crashes came from shadowing tkinter internals: a ClockWindow
    method named _bind (tkinter's own bind() calls self._bind), and a button
    storing its width in self._w (Tk's widget path)."""

    def test_no_solari_widget_class_redefines_a_tkinter_name(self):
        for name, cls in inspect.getmembers(S, inspect.isclass):
            if not issubclass(cls, tk.Misc) or cls.__module__ != S.__name__:
                continue
            own = set()
            for base in cls.__mro__:
                if base.__module__ == S.__name__:
                    own |= {k for k in vars(base) if not k.startswith("__")}
            clash = (own & _tk_names(cls)) - INTENTIONAL_OVERRIDES
            self.assertFalse(clash, f"{name} shadows tkinter: {sorted(clash)}")

    @needs_display
    def test_no_live_widget_overwrites_a_tkinter_instance_attribute(self):
        app = temp_app()
        try:
            app.edit_clock(app.windows[0])
            app.update()
            seen = 0
            stack = [app]
            while stack:
                w = stack.pop()
                stack.extend(w.winfo_children())
                seen += 1
                tk_methods = _tk_names(type(w))
                for key, value in vars(w).items():
                    if key == "_w":
                        self.assertTrue(isinstance(value, str) and value.startswith("."),
                                        f"{type(w).__name__}._w = {value!r}")
                    elif key not in TK_INSTANCE_ATTRS:
                        self.assertNotIn(key, tk_methods,
                                         f"{type(w).__name__}.{key} shadows a tkinter method")
            self.assertGreater(seen, 50)
        finally:
            app.quit_app()


class _Clock:
    """The minimum a FlipCard needs from its clock."""

    def __init__(self, root):
        self.text_color, self.card_color, self.face = "#ffffff", "#1e1f22", "modern"
        self.draft = False
        self.sprites = S.SpriteBank(root)
        self.ticker = S.Ticker(root)


@needs_display
class FlipCardBehaviour(unittest.TestCase):

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.card = S.FlipCard(self.root, _Clock(self.root), 60, 80, 8)

    def tearDown(self):
        self.root.destroy()

    def _visible(self):
        c = self.card
        return [i for i in (c._a, c._b) if c.itemcget(i, "state") != "hidden"]

    def _settle(self):
        c = self.card
        i = 0
        while c._rolling and c.step(c._t0 + i * S.FRAME_MS / 1000):
            i += 1
            self.assertLess(i, 500)

    def test_digit_visible_before_and_after_rolls(self):
        self.card.set_immediate("4")
        self.assertEqual(len(self._visible()), 1)
        for ch in "5678901234":
            self.card.set(ch)
            self._settle()
            self.assertEqual(self.card._cur, ch)
            self.assertEqual(self._visible(), [self.card._a], ch)

    def test_mid_roll_shows_both_digits_apart(self):
        self.card.set_immediate("1")
        self.card.set("2")
        self.card.step(self.card._t0 + S.DRUM_DURATION / 2)
        self.assertEqual(len(self._visible()), 2)
        ya = self.card.coords(self.card._a)[1]
        yb = self.card.coords(self.card._b)[1]
        self.assertLess(ya, yb)

    def test_blank_flap_hides_the_digit(self):
        self.card.set_immediate(" ")
        self.assertEqual(self._visible(), [])

    def test_retarget_mid_roll_lands_on_the_last_digit(self):
        self.card.set_immediate("1")
        self.card.set("2")
        self.card.set("3")
        self._settle()
        self.assertEqual(self.card._cur, "3")


@needs_display
class ClockWindowBehaviour(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = temp_app([dict(S.DEFAULT_CLOCK, tz="Asia/Tokyo", label="Tokyo", x=200, y=150)])
        cls.win = cls.app.windows[0]

    @classmethod
    def tearDownClass(cls):
        cls.app.quit_app()

    def setUp(self):
        self.win.cfg.update(seconds="small", hour24=True, scale=1.0)
        self.win.refresh_style()
        self.app.update()

    def test_window_matches_its_layout(self):
        L = self.win._L
        self.assertEqual((self.win.winfo_width(), self.win.winfo_height()), (L["w"], L["h"]))

    def test_cards_follow_the_seconds_mode(self):
        for mode, n in (("off", 4), ("large", 6), ("small", 6)):
            self.win.cfg["seconds"] = mode
            self.win.refresh_style()
            self.assertEqual(len(self.win._cards), n, mode)

    def test_regression_restyle_after_a_roll(self):
        """A live style change freed the sprite a finished roll had left on
        the hidden item, and the next configure raised 'image doesn't exist'."""
        w = self.win
        for card in w._cards.values():
            card.set_immediate("1")
            card.set("2")
            card.step(card._t0 + S.DRUM_DURATION * 2)
        for colour in ("#ff6b5a", "#5ce08a", "#f4f4f2"):
            w.cfg["text_color"] = colour
            w.refresh_style()
            self.app.update()
        self.assertEqual(self.app.errors, [])

    def test_cards_show_the_time(self):
        now = S.clock_now(self.win.cfg)
        self.win.on_second(time.time(), animate=False)
        want = S.clock_digits(now, self.win.cfg)
        for key, card in self.win._cards.items():
            self.assertEqual(card._cur, want[key], key)

    def test_digits_survive_a_rescale(self):
        self.win._scale = 1.7
        self.win._relayout()
        shown = [c.itemcget(c._a, "state") for c in self.win._cards.values()]
        self.assertTrue(all(s == "normal" for s in shown))
        self.win._scale = 1.0
        self.win._relayout()

    def test_live_resize_renders_drafts_then_final(self):
        w = self.win
        w._rs_start(mock.Mock(x_root=500))
        w._rs_move(mock.Mock(x_root=650))
        self.assertTrue(pump(w, 2, until=lambda: w.draft))
        w._rs_end(mock.Mock(x_root=650))
        self.assertFalse(w.draft)
        self.assertGreater(w.cfg["scale"], 1.0)

    def test_regression_drag_is_saved_without_quitting(self):
        """A moved clock was saved only at quit, so a shutdown or crash
        lost every move made in the session."""
        w = self.win
        x0, y0 = w.winfo_x(), w.winfo_y()
        ev = lambda x, y: mock.Mock(x_root=x, y_root=y, state=0x0001, widget=w.cv)
        w._drag_start(ev(x0 + 10, y0 + 10))
        w._drag_move(ev(x0 + 90, y0 + 50))
        w._drag_end(ev(x0 + 90, y0 + 50))
        self.assertTrue(pump(w, 3, until=lambda: os.path.exists(self.app.config_file) and
                             saved_clocks(self.app)[0]["x"] == x0 + 80))

    def test_unknown_zone_is_flagged_not_hidden(self):
        app = temp_app([dict(S.DEFAULT_CLOCK, tz="Mars/Olympus", label="Base")])
        try:
            self.assertEqual(len(app.windows), 1)
            self.assertFalse(app.windows[0].zone_valid())
        finally:
            app.quit_app()


@needs_display
class EditorBehaviour(unittest.TestCase):

    def setUp(self):
        self.app = temp_app([
            dict(S.DEFAULT_CLOCK, tz="Asia/Tokyo", label="Tokyo office", x=200, y=150),
            dict(S.DEFAULT_CLOCK, tz="Europe/London", label="London", x=200, y=400)])
        self.tokyo, self.london = self.app.windows

    def tearDown(self):
        self.app.quit_app()

    def test_regression_custom_name_survives_opening(self):
        """Opening the editor replaced a custom name with the zone's city."""
        ed = self.app.edit_clock(self.tokyo)
        self.app.update()
        self.assertEqual(ed.v_label.get(), "Tokyo office")
        ed.zones.select(0)                   # picking a zone keeps a custom name
        self.assertEqual(ed.v_label.get(), "Tokyo office")

    def test_untouched_name_follows_the_zone(self):
        ed = self.app.edit_clock(self.london)
        ed.v_search.set("osaka")
        ed._run_search()
        ed.zones.select(0)
        self.assertEqual(ed.v_label.get(), "Osaka")
        self.assertEqual(self.london.cfg["tz"], "Asia/Tokyo")

    def test_regression_zone_list_opens_on_the_full_list(self):
        """The list opened filtered to the current zone - one entry."""
        ed = self.app.edit_clock(self.tokyo)
        self.assertEqual(len(ed.zones.entries), len(S.zone_catalog()))
        self.assertEqual(ed.zones.selected().key, "Asia/Tokyo")

    def test_regression_dialogs_are_topmost(self):
        """Dialogs opened underneath the always-on-top clocks."""
        ed = self.app.edit_clock(self.tokyo)
        self.app.update()
        self.assertTrue(int(ed.wm_attributes("-topmost")))

    def test_regression_editor_works_with_the_manager_hidden(self):
        """With the manager hidden to the tray, the dialog was transient to a
        withdrawn window: invisible, yet holding the grab - a frozen app."""
        self.app.mgr.withdraw()
        self.app.update()
        ed = self.app.edit_clock(self.tokyo)
        self.assertTrue(pump(ed, 2, until=ed.winfo_viewable))
        self.assertNotEqual(ed.wm_transient(), str(self.app.mgr))

    def test_changes_apply_live_and_cancel_restores(self):
        before = dict(self.tokyo.cfg)
        ed = self.app.edit_clock(self.tokyo)
        ed.v_text.set("#39ff6e")
        ed.v_finish.set("aluminium")
        pump(ed, 0.4)
        self.assertEqual(self.tokyo.cfg["text_color"], "#39ff6e")
        ed.cancel()
        self.assertEqual(self.tokyo.cfg, before)
        self.assertIs(self.app.configs[0], self.tokyo.cfg)

    def test_presets_set_three_options(self):
        ed = self.app.edit_clock(self.tokyo)
        name, tc, cc, fin = S.PRESETS[6]
        ed.presets._pick(6)
        self.assertEqual((self.tokyo.cfg["text_color"], self.tokyo.cfg["card_color"],
                          self.tokyo.cfg["finish"]), (tc, cc, fin))
        self.assertEqual(ed.preset_lbl.cget("text"), name)

    def test_cancelling_a_new_clock_removes_it(self):
        n = len(self.app.windows)
        self.app.new_clock()
        self.assertEqual(len(self.app.windows), n + 1)
        self.app.editor.cancel()
        self.assertEqual(len(self.app.windows), n)
        self.assertEqual(len(saved_clocks(self.app)), n)

    def test_one_editor_at_a_time(self):
        a = self.app.edit_clock(self.tokyo)
        b = self.app.edit_clock(self.london)
        self.assertFalse(a.winfo_exists())
        self.assertIs(self.app.editor, b)
        self.assertIs(self.app.edit_clock(self.london), b)

    def test_menu_changes_reach_an_open_editor(self):
        ed = self.app.edit_clock(self.tokyo)
        self.tokyo._set_option("seconds", "large")
        self.assertEqual(ed.v_seconds.get(), "large")

    def test_low_contrast_is_flagged(self):
        ed = self.app.edit_clock(self.tokyo)
        ed.v_text.set("#2a2d33")
        ed.v_card.set("#2a2d33")
        self.assertIn("contrast", ed.contrast_lbl.cget("text").lower())


@needs_display
class AppBehaviour(unittest.TestCase):

    def setUp(self):
        self.app = temp_app()

    def tearDown(self):
        self.app.quit_app()

    def test_manager_lists_every_clock(self):
        self.assertEqual(len(self.app.mgr.rows), len(self.app.windows))
        self.app.mgr.on_second(time.time())
        for row in self.app.mgr.rows:
            self.assertTrue(row.itemcget(row._title, "text"))

    def test_empty_state(self):
        for w in list(self.app.windows):
            self.app.remove_clock(w, confirm=False)
        self.app.update()
        self.assertEqual(self.app.mgr.rows, [])
        self.assertTrue(self.app.mgr.empty.winfo_ismapped())
        self.assertEqual(saved_clocks(self.app), [])

    def test_remove_matches_by_identity(self):
        """list.remove() compares dicts by value, so removing one of two
        identical clocks could drop the other's settings."""
        first = self.app.windows[0]
        twin = self.app.duplicate_clock(first)
        twin.cfg.update(x=first.cfg["x"], y=first.cfg["y"])
        self.assertEqual(twin.cfg, first.cfg)
        self.app.remove_clock(twin, confirm=False)
        self.assertFalse(any(c is twin.cfg for c in self.app.configs))
        self.assertTrue(any(c is first.cfg for c in self.app.configs))

    def test_gather_lines_clocks_up_on_the_main_screen(self):
        for w in self.app.windows:
            w.recall(9000, 9000)
        self.app.recall_all()
        self.app.update()
        l, t, r, b = self.app.monitors()[0]
        for w in self.app.windows:
            self.assertTrue(l <= w.winfo_x() < r and t <= w.winfo_y() < b)

    def test_regression_wheel_is_scoped_to_the_list(self):
        """The manager bound the mouse wheel application-wide, so scrolling
        anywhere - the editor's zone list, say - scrolled the manager."""
        sf = self.app.mgr.list
        self.assertEqual(self.app.tk.call("bind", "all", "<MouseWheel>"), "")
        sf._capture()
        self.assertNotEqual(self.app.tk.call("bind", "all", "<MouseWheel>"), "")
        with mock.patch.object(sf, "winfo_pointerxy", return_value=(-9999, -9999)):
            sf._release()
        self.assertEqual(self.app.tk.call("bind", "all", "<MouseWheel>"), "")

    def test_regression_tray_never_calls_tk_from_its_thread(self):
        """Tray menu callbacks called self.after() from pystray's thread;
        Tk is not thread-safe."""
        captured = {}
        main = threading.current_thread()
        tk_calls_off_main = []

        class Item:
            def __init__(self, text, action, default=False):
                captured[text] = action

        class FakePystray:
            MenuItem = Item

            class Menu:
                SEPARATOR = None

                def __init__(self, *items):
                    pass

            class Icon:
                def __init__(self, *a):
                    pass

                def run(self):
                    pass

                def stop(self):
                    pass

        real_after = self.app.after

        def watched_after(*a, **k):
            if threading.current_thread() is not main:
                tk_calls_off_main.append(a)
            return real_after(*a, **k)

        with mock.patch.object(S, "pystray", FakePystray, create=True), \
                mock.patch.object(self.app, "after", watched_after), \
                mock.patch.object(self.app, "recall_all") as gather:
            self.app._start_tray()
            pump(self.app, 2, until=lambda: "Gather clocks" in captured)
            t = threading.Thread(target=captured["Gather clocks"])
            t.start()
            t.join()
            self.assertTrue(pump(self.app, 2, until=lambda: gather.called))
        self.assertEqual(tk_calls_off_main, [])

    def test_second_launch_request_shows_the_manager(self):
        self.app.lock = S.InstanceLock(self.app._test_dir)
        self.app.mgr.withdraw()
        self.app.update()
        self.app.lock.request_show()
        self.assertTrue(pump(self.app, 3, until=self.app.mgr.winfo_viewable))

    def test_a_failing_subscriber_does_not_stop_the_clocks(self):
        class Broken:
            def on_second(self, ts):
                raise ValueError("boom")

        self.app.ticker._clocks.insert(0, Broken())       # first in line
        card = self.app.windows[0]._cards["s2"]
        before = card._cur
        with mock.patch.object(S.sys, "stderr", None), \
                mock.patch.object(S, "config_dir", lambda: self.app._test_dir):
            self.assertTrue(pump(self.app, 3, until=lambda: card._cur != before))
        self.assertTrue(any("boom" in e for e in self.app.errors))
        self.app.errors.clear()

    def test_saves_on_session_end(self):
        self.assertTrue(self.app.protocol("WM_SAVE_YOURSELF"))

    def test_callback_errors_are_logged_not_lost(self):
        log = os.path.join(self.app._test_dir, "solari_error.log")
        with mock.patch.object(S, "config_dir", lambda: self.app._test_dir), \
                mock.patch.object(S.sys, "stderr", None):        # as under pythonw
            self.app.after(0, lambda: 1 / 0)
            pump(self.app, 0.3)
        self.assertTrue(any("ZeroDivisionError" in e for e in self.app.errors))
        with open(log, encoding="utf-8") as f:
            self.assertIn("ZeroDivisionError", f.read())
        self.app.errors.clear()

    def test_regression_images_bind_to_their_own_interpreter(self):
        """PhotoImages without a master bound to the first Tk root, so a
        second root could not display any of them."""
        other = tk.Tk()
        try:
            other.withdraw()
            S.SurfaceButton(other, "Hello", lambda: None).pack()
            S.ToggleSwitch(other, tk.BooleanVar(other, True)).pack()
            other.update()
        finally:
            other.destroy()

    def test_background_launch_keeps_the_manager_out_of_the_way(self):
        app = temp_app(background=True)
        try:
            self.assertFalse(app.mgr.winfo_viewable())
        finally:
            app.quit_app()


@needs_display
class Widgets(unittest.TestCase):

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.destroy()

    def test_button_click_and_disabled(self):
        hits = []
        b = S.SurfaceButton(self.root, "Go", lambda: hits.append(1))
        b.invoke()
        b._release(mock.Mock(x=-5, y=-5))         # released outside: no click
        b.set_enabled(False)
        b.invoke()
        self.assertEqual(hits, [1])

    def test_toggle_follows_and_flips_its_variable(self):
        v = tk.BooleanVar(self.root, False)
        t = S.ToggleSwitch(self.root, v)
        t._clicked()
        self.assertTrue(v.get())
        t.set_enabled(False)
        t._clicked()
        self.assertTrue(v.get())

    def test_toggle_destroyed_mid_slide_leaves_nothing_pending(self):
        v = tk.BooleanVar(self.root, False)
        t = S.ToggleSwitch(self.root, v)
        v.set(True)
        t.destroy()
        pump(self.root, 0.3)                     # a stray frame would raise here

    def test_segmented_and_keyboard(self):
        v = tk.StringVar(self.root, "a")
        seg = S.Segmented(self.root, [("a", "A"), ("b", "B"), ("c", "C")], v)
        seg._move(1)
        self.assertEqual(v.get(), "b")
        seg._move(5)
        self.assertEqual(v.get(), "c")

    def test_slider_clamps(self):
        v = tk.DoubleVar(self.root, 0.5)
        s = S.Slider(self.root, v, 0.4, 1.0)
        s._set(7)
        self.assertEqual(v.get(), 1.0)
        s._nudge(-1)
        self.assertLess(v.get(), 1.0)

    def test_zone_list_virtualises(self):
        picked = []
        zl = S.ZoneList(self.root, picked.append, rows=5)
        zl.set_entries(S.zone_catalog(), select_key="Asia/Tokyo")
        self.assertEqual(len(zl._pool), 5)
        zl._step(1)
        self.assertEqual(len(picked), 1)
        zl.set_entries([])
        self.assertEqual(zl.itemcget(zl._empty, "state"), "normal")

    def test_text_field_placeholder(self):
        f = S.TextField(self.root, placeholder="Search")
        f.pack()
        self.root.update()
        f.var.set("x")
        self.root.update()
        self.assertFalse(f._hint.winfo_ismapped())


if __name__ == "__main__":
    unittest.main()
