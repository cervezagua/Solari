"""
Headless tests for Solari's pure logic.

Everything here runs without a display: no Tk root is created, so this works on
a build agent.  The GUI classes are exercised separately by actually running the
app (see README).
"""

import datetime
import functools
import json
import os
import tempfile
import time
import types
import tkinter as tk
import unittest
import zoneinfo

import solari as S


@functools.lru_cache(maxsize=1)
def _display_available():
    """GUI tests need a real display; the rest of this file does not."""
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except Exception:
        return False


needs_display = unittest.skipUnless(_display_available(), "no display available")


def shown_digit(card):
    """The digit a FlipCard is actually displaying, or None if it is blank."""
    for item in (card._txt_a, card._txt_b):
        if (card.itemcget(item, "state") or "normal") != "hidden":
            return card.itemcget(item, "text")
    return None


def settle(card):
    """Run a roll to completion through real intermediate frames.

    It has to walk the frames rather than jumping to the end: the outgoing text
    item is hidden partway through, and skipping straight to the completion
    branch would never exercise that - which is precisely the state the blank
    card bug lived in.
    """
    if not card._rolling:
        return
    step_s = S.FRAME_MS / 1000.0
    i = 0
    while card.step(card._t0 + i * step_s):
        i += 1
        if i > 500:
            raise AssertionError("roll never terminated")


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

    def test_surface_size_and_mode(self):
        img = S.render_surface(140, 48, 10, S.THEME["surface"], S.THEME["bg"])
        self.assertEqual(img.size, (140, 48))
        self.assertEqual(img.mode, "RGB")

    def test_raised_surface_is_lit_from_above(self):
        img = S.render_surface(120, 60, 10, "#2a2a30", S.THEME["bg"])
        self.assertGreater(sum(img.getpixel((60, 3))), sum(img.getpixel((60, 56))))

    def test_inset_surface_inverts_the_lighting(self):
        """A recess is shadowed at the top and catches light on its bottom lip -
        the opposite of a raised surface, from the same function."""
        img = S.render_surface(120, 60, 10, "#2a2a30", S.THEME["bg"], inset=True)
        self.assertLess(sum(img.getpixel((60, 3))), sum(img.getpixel((60, 56))))

    def test_strength_softens_the_shading(self):
        full = S.render_surface(120, 60, 10, "#2a2a30", S.THEME["bg"], strength=1.0)
        soft = S.render_surface(120, 60, 10, "#2a2a30", S.THEME["bg"], strength=0.3)
        spread = lambda im: sum(im.getpixel((60, 3))) - sum(im.getpixel((60, 56)))
        self.assertGreater(spread(full), spread(soft))

    def test_surface_corners_blend_to_the_colour_behind(self):
        behind = S.THEME["bg"]
        img = S.render_surface(120, 60, 12, "#2a2a30", behind)
        self.assertEqual(img.getpixel((0, 0)), S.to_rgb(behind))

    def test_surface_is_cached(self):
        args = (111, 41, 8, "#334455", S.THEME["bg"])
        S.render_surface(*args)
        before = S.render_surface.cache_info().hits
        S.render_surface(*args)
        self.assertEqual(S.render_surface.cache_info().hits, before + 1)

    def test_cards_and_buttons_share_one_renderer(self):
        """render_card must delegate, so the two can never drift apart."""
        behind = S.THEME["well"]
        card = S.render_card(82, 104, 12, "#1e1e1e", None, behind, False)
        surface = S.render_surface(82, 104, 12, "#1e1e1e", behind)
        self.assertEqual(card.tobytes(), surface.tobytes())

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
        img = S.render_chassis(400, 200, 0, 0, 12, (20, 50, 380, 150), None)
        self.assertEqual(img.size, (400, 200))

    def test_chassis_well_rounds_independently_of_the_panel(self):
        """A flush panel must not flatten the recess: the well radius used to
        be derived from the panel radius, so squaring one squared the other."""
        well = (20, 50, 380, 150)
        flat = S.render_chassis(400, 200, 0, 0, 0, well, None)
        curved = S.render_chassis(400, 200, 0, 0, 24, well, None)
        # Compare the whole corner box rather than one pixel: a single probe a
        # couple of px in looks identical either way once downsampled.
        differing = sum(
            1
            for x in range(20, 46)
            for y in range(50, 76)
            if flat.getpixel((x, y)) != curved.getpixel((x, y))
        )
        self.assertGreater(differing, 40,
                           "well radius is not honoured independently")

    def test_app_icon(self):
        self.assertEqual(S.make_app_icon(64).size, (64, 64))


@needs_display
class FlipCardBehaviour(unittest.TestCase):
    """Regression cover for the animation's visible state.

    Asserting the digit's *text* is not enough: step() hides the outgoing text
    item once it has faded, so a completed roll that never restores it leaves
    the card blank while still reporting the right text.
    """

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        S.resolve_fonts(self.root)
        self.ticker = S.Ticker(self.root)
        self.card = S.FlipCard(self.root, self.ticker, "#ffffff", "#1e1e1e", 1.0)
        self.card.pack()
        self.root.update()

    def tearDown(self):
        self.ticker.stop()
        self.root.destroy()

    def test_digit_is_visible_before_any_roll(self):
        self.card.set_immediate("3")
        self.assertEqual(shown_digit(self.card), "3")

    def test_digit_is_still_visible_after_a_completed_roll(self):
        self.card.set_immediate("3")
        self.card.set("7")
        settle(self.card)
        self.assertEqual(shown_digit(self.card), "7",
                         "card went blank after the roll landed")

    def test_card_never_blanks_across_many_consecutive_rolls(self):
        self.card.set_immediate("0")
        for i in range(1, 13):
            d = str(i % 10)
            self.card.set(d)
            settle(self.card)
            self.assertEqual(shown_digit(self.card), d, f"blank after roll {i}")

    def test_exactly_one_item_is_visible_at_rest(self):
        self.card.set_immediate("5")
        self.card.set("6")
        settle(self.card)
        states = [(self.card.itemcget(i, "state") or "normal")
                  for i in (self.card._txt_a, self.card._txt_b)]
        self.assertEqual(states.count("hidden"), 1,
                         "at rest exactly one digit item should be showing")

    def test_mid_roll_shows_both_digits_clipped_apart(self):
        self.card.set_immediate("3")
        self.card.set("7")
        self.card.step(self.card._t0 + S.DRUM_DURATION * 0.5)
        ya = self.card.coords(self.card._txt_a)[1]
        yb = self.card.coords(self.card._txt_b)[1]
        self.assertLess(ya, yb, "outgoing digit should ride above the incoming one")


@needs_display
class RenderedWidgets(unittest.TestCase):
    """SurfaceButton / ToggleSwitch: the chrome shared with the manager."""

    def setUp(self):
        # Mapped, not withdrawn: an unmapped widget reports width 1 and does not
        # dispatch synthetic events, so click tests would pass vacuously.
        self.root = tk.Tk()
        self.root.geometry("340x220+40+40")
        S.resolve_fonts(self.root)
        self.root.update()
        self.fired = []

    def tearDown(self):
        self.root.destroy()

    def _button(self, **kw):
        b = S.make_button(self.root, "Press", lambda: self.fired.append(1), **kw)
        b.pack()
        self.root.update()
        return b

    def test_button_does_not_shadow_tk_internals(self):
        """tkinter.Misc keeps the widget's Tcl path in _w; shadowing it makes
        every later call address a widget that does not exist."""
        b = self._button()
        self.assertIsInstance(b._w, str)
        self.assertTrue(b._w.startswith("."))

    def test_click_fires_the_command(self):
        b = self._button()
        b.event_generate("<ButtonPress-1>", x=5, y=5)
        b.event_generate("<ButtonRelease-1>", x=5, y=5)
        self.root.update()
        self.assertEqual(self.fired, [1])

    def test_release_outside_does_not_fire(self):
        b = self._button()
        b.event_generate("<ButtonPress-1>", x=5, y=5)
        b.event_generate("<ButtonRelease-1>", x=-50, y=-50)
        self.root.update()
        self.assertEqual(self.fired, [])

    def test_disabled_button_does_not_fire(self):
        b = self._button()
        b.set_enabled(False)
        b.event_generate("<ButtonPress-1>", x=5, y=5)
        b.event_generate("<ButtonRelease-1>", x=5, y=5)
        self.root.update()
        self.assertEqual(self.fired, [])

    def test_button_repaints_when_stretched(self):
        b = S.make_button(self.root, "Wide", lambda: None)
        natural = b._cw
        b.pack(fill="x")
        self.root.update()
        self.assertGreater(b.winfo_width(), natural,
                           "fill='x' should have stretched the button")
        self.assertEqual(b._cw, b.winfo_width(),
                         "button did not re-render at its new width")

    def test_toggle_follows_its_variable(self):
        var = tk.BooleanVar(value=False)
        sw = S.ToggleSwitch(self.root, var)
        sw.pack()
        self.root.update()
        self.assertEqual(sw._pos, 0.0)
        var.set(True)
        for _ in range(40):
            self.root.update()
            time.sleep(0.006)
        self.assertEqual(sw._pos, 1.0)

    def test_toggle_click_flips_the_variable(self):
        var = tk.BooleanVar(value=False)
        sw = S.ToggleSwitch(self.root, var)
        sw.pack()
        self.root.update()
        sw.event_generate("<Button-1>", x=10, y=10)
        self.root.update()
        self.assertTrue(var.get())

    def test_toggle_destroyed_mid_slide_leaves_nothing_pending(self):
        """A queued frame must be cancelled on destroy: Tk deletes the command
        behind the widget, so the callback cannot guard itself."""
        var = tk.BooleanVar(value=False)
        sw = S.ToggleSwitch(self.root, var)
        sw.pack()
        self.root.update()
        var.set(True)
        self.root.update()
        self.assertIsNotNone(sw._job, "should be mid-slide")
        sw.destroy()
        self.root.update()
        self.assertIsNone(sw._job)
        # The trace must be gone too, or writes keep reaching a dead widget.
        var.set(False)
        self.root.update()

    def test_disabled_toggle_ignores_clicks(self):
        var = tk.BooleanVar(value=False)
        sw = S.ToggleSwitch(self.root, var)
        sw.pack()
        sw.set_enabled(False)
        self.root.update()
        sw.event_generate("<Button-1>", x=10, y=10)
        self.root.update()
        self.assertFalse(var.get())


@needs_display
class ManagerBehaviour(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._orig = S.config_dir
        S.config_dir = lambda: self.dir
        self.root = tk.Tk()
        self.root.withdraw()
        S.resolve_fonts(self.root)

    def tearDown(self):
        S.config_dir = self._orig
        self.root.destroy()

    def _manager(self, n):
        app = types.SimpleNamespace(
            windows=[], startup_var=tk.BooleanVar(value=False),
            toggle_startup=lambda: None, add_clock=lambda: None,
            recall_all=lambda: None, quit_app=lambda: None,
            remove_clock=lambda w: None, recall=lambda w: None,
            edit_clock=lambda w: None, tray_active=lambda: False)
        ticker = S.Ticker(self.root)
        for i in range(n):
            cfg = S.migrate_clock({"tz": "UTC", "label": f"Zone {i}",
                                   "text_color": S.COLOR_CYCLE[i % 6]})
            app.windows.append(S.ClockWindow(self.root, cfg, ticker,
                                             lambda w: None, lambda w: None,
                                             lambda: []))
        mgr = S.ManagerWindow(self.root, app)
        mgr.refresh_list()
        self.root.update()
        return mgr

    def test_builds_a_row_per_clock(self):
        for n in (1, 3, 6):
            mgr = self._manager(n)
            self.assertEqual(len(mgr._rows), n)
            self.assertIn(str(n), mgr._count.cget("text"))
            mgr.destroy()

    def test_count_label_is_singular_for_one(self):
        mgr = self._manager(1)
        self.assertEqual(mgr._count.cget("text"), "1 clock")

    def test_row_controls_stay_inside_the_row(self):
        """The rightmost control must not run off the edge, including when the
        scrollbar appears and narrows every row."""
        mgr = self._manager(7)
        for row in mgr._rows:
            self.assertLessEqual(row.bbox(row._buttons[0])[2], row._cw)

    def test_rows_carry_their_clock_colour(self):
        mgr = self._manager(3)
        for row in mgr._rows:
            self.assertEqual(row.itemcget(row._stripe, "fill"),
                             row.win.cfg["text_color"])

    def test_tick_fills_in_every_row_time(self):
        mgr = self._manager(3)
        mgr.on_second(time.time())
        for row in mgr._rows:
            self.assertRegex(row.itemcget(row._clock, "text"),
                             r"^\d{2}:\d{2}:\d{2}")

    def test_twelve_hour_row_shows_the_meridiem(self):
        mgr = self._manager(1)
        mgr._rows[0].win.cfg["hour24"] = False
        mgr.on_second(time.time())
        self.assertRegex(mgr._rows[0].itemcget(mgr._rows[0]._clock, "text"),
                         r"(AM|PM)$")


@needs_display
class ClockWindowBehaviour(unittest.TestCase):

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        S.resolve_fonts(self.root)
        self.ticker = S.Ticker(self.root)
        self.cfg = S.migrate_clock({"tz": "UTC", "label": "Test"})
        self.win = S.ClockWindow(self.root, self.cfg, self.ticker,
                                 lambda w: None, lambda w: None, lambda: [])
        self.root.update()

    def tearDown(self):
        self.ticker.stop()
        self.root.destroy()

    def _settle_all(self):
        for card in self.win._cards.values():
            if card._rolling:
                settle(card)

    def test_every_digit_is_visible_on_first_paint(self):
        for key, card in self.win._cards.items():
            self.assertIsNotNone(shown_digit(card), f"{key} blank on first paint")

    def test_every_digit_stays_visible_across_simulated_seconds(self):
        """The reported bug: minutes and seconds blanked out as they rolled."""
        base = datetime.datetime(2026, 1, 1, 11, 58, 55)
        for offset in range(12):
            fake = base + datetime.timedelta(seconds=offset)
            self.win._last.clear()
            hh, mm, ss = fake.strftime("%H"), fake.strftime("%M"), fake.strftime("%S")
            digits = {"h1": hh[0], "h2": hh[1], "m1": mm[0], "m2": mm[1],
                      "s1": ss[0], "s2": ss[1]}
            for key, card in self.win._cards.items():
                card.set(digits[key])
            self._settle_all()
            for key, card in self.win._cards.items():
                self.assertEqual(shown_digit(card), digits[key],
                                 f"{key} wrong/blank at {fake:%H:%M:%S}")

    def test_both_digits_of_each_pair_render(self):
        self.win.on_second(time.time(), animate=True)
        self._settle_all()
        for pair in (("h1", "h2"), ("m1", "m2"), ("s1", "s2")):
            for key in pair:
                self.assertIsNotNone(shown_digit(self.win._cards[key]),
                                     f"{key} of pair {pair} is blank")

    def test_hiding_seconds_drops_to_four_cards(self):
        self.cfg["show_seconds"] = False
        self.win.refresh_style()
        self.assertEqual(len(self.win._cards), 4)
        for key, card in self.win._cards.items():
            self.assertIsNotNone(shown_digit(card), f"{key} blank after relayout")

    def test_twelve_hour_mode_keeps_two_hour_digits(self):
        self.cfg["hour24"] = False
        self.win.refresh_style()
        self.win.on_second(time.time(), animate=False)
        for key in ("h1", "h2"):
            self.assertIsNotNone(shown_digit(self.win._cards[key]))

    @unittest.skipUnless(S.HAS_PIL, "Pillow not installed")
    def test_chassis_fills_the_window_including_corners(self):
        """No outer mat, and a flush outer edge.

        The panel used to sit inset inside a black surround, which read as a
        border drawn around the widget; an overrideredirect Tk window has no
        transparency to blend into, so any inset is simply visible black. The
        corners then had to go square too - the resize grip in the bottom-right
        is square, so rounding only the other three looked broken.
        """
        for scale in (0.4, 1.0, 2.2):
            self.win._scale = scale
            L = self.win._layout()
            self.assertEqual(L["pad"], 0, f"outer mat came back at scale {scale}")
            self.assertEqual(L["r"], 0, f"panel is rounded again at scale {scale}")
            self.assertGreater(L["wr"], 0, "the recessed well should stay rounded")
            img = S.render_chassis(L["w"], L["h"], L["pad"], L["r"], L["wr"],
                                   L["well"], None)
            w, h = img.size
            probes = {
                "top": (w // 2, 0),
                "bottom": (w // 2, h - 1),
                "left": (0, h // 2),
                "right": (w - 1, h // 2),
                "top-left": (0, 0),
                "top-right": (w - 1, 0),
                "bottom-left": (0, h - 1),
                "bottom-right": (w - 1, h - 1),
            }
            for name, xy in probes.items():
                self.assertGreater(
                    sum(img.getpixel(xy)), 12,
                    f"{name} is black at scale {scale} - panel does not reach it")

    def test_digits_survive_a_rescale(self):
        self.win.on_second(time.time(), animate=True)
        self._settle_all()
        for scale in (0.4, 1.0, 2.5):
            self.win._scale = scale
            self.win._relayout()
            for key, card in self.win._cards.items():
                self.assertIsNotNone(shown_digit(card),
                                     f"{key} blank at scale {scale}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
