"""
Solari - Flip-Clock Desktop Widget
Drum-roll animation - Corner-drag resize - Author: Cervezagua

Rendering notes
---------------
Tk's canvas has no antialiasing and no gradient fill, so anything drawn with
canvas primitives is permanently faceted and flat.  Every dimensional surface
here (card faces, window chassis) is therefore rendered once with Pillow at a
supersampled size, downsampled with LANCZOS, and cached - then blitted as a
single image item.  That is both prettier and cheaper than redrawing polygons
every animation frame.  Without Pillow the app falls back to plain canvas
drawing and still runs.
"""

import datetime
import functools
import json
import os
import sys
import time
import tkinter as tk
import tkinter.font
import tkinter.messagebox
import zoneinfo

try:
    import winreg
    HAS_WINREG = True
except ImportError:
    HAS_WINREG = False

# Pillow and pystray are independent optional deps.  Importing them together
# means a machine with Pillow but no pystray gets neither.
try:
    from PIL import Image, ImageDraw, ImageFilter, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import pystray
    HAS_TRAY = HAS_PIL
except ImportError:
    HAS_TRAY = False

IS_WINDOWS = sys.platform.startswith("win")
APP_NAME = "Solari"
STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
CONFIG_VERSION = 2


# ---------------------------------------------------------------------------
#  High-DPI
#
#  Must run before any Tk window exists.  Without it Windows bitmap-stretches
#  the whole app on any scaled display, which no amount of drawing quality can
#  recover from.
# ---------------------------------------------------------------------------
def enable_dpi_awareness():
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        try:
            # 2 = per-monitor aware
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ---------------------------------------------------------------------------
#  Theme
#
#  Single source of truth.  Everything that used to be a scattered hex literal
#  lives here.
# ---------------------------------------------------------------------------
THEME = {
    "bg":          "#0b0b0d",   # dialog / manager background
    "chassis":     "#131316",   # clock widget body
    "well":        "#08080a",   # recessed area directly behind the flip cards
    "surface":     "#1a1a1f",   # raised control background
    "surface_hi":  "#232329",   # hover
    "text":        "#f2f2f5",
    "text_dim":    "#9a9aa4",
    "text_faint":  "#55555f",
    "accent":      "#3d7dd6",
    "accent_hi":   "#4f92ec",
    "danger":      "#e5484d",
    "divider":     "#26262c",
    "colon":       "#b4b4c4",   # peak of the per-second pulse
    "colon_dim":   "#4a4a58",   # rest - most of each second is spent here
}

# Colour swatches offered in the editor
PALETTE = [
    ("#ffffff", "White"),
    ("#1e1e1e", "Grey"),
    ("#cc0000", "Red"),
    ("#00aa00", "Green"),
    ("#00ff41", "Matrix"),
    ("#2277ff", "Blue"),
    ("#ff7700", "Orange"),
    ("#8833cc", "Purple"),
    ("#ffd700", "Gold"),
    ("#aaaaaa", "Silver"),
    ("#000000", "Black"),
]

# Default card tint paired with each text colour
PAL_DARK = {
    "#ffffff": "#1e1e1e",
    "#1e1e1e": "#111111",
    "#cc0000": "#1a0000",
    "#00aa00": "#001a00",
    "#00ff41": "#001a08",
    "#2277ff": "#001033",
    "#ff7700": "#1a0a00",
    "#8833cc": "#0d0020",
    "#ffd700": "#1a1400",
    "#aaaaaa": "#111111",
    "#000000": "#0a0a0a",
}

# Curated text + card pairings, so a look can be picked in one click
PRESETS = [
    ("Classic",  "#ffffff", "#1e1e1e"),
    ("Midnight", "#2277ff", "#001033"),
    ("Ember",    "#ff7700", "#1a0a00"),
    ("Signal",   "#cc0000", "#1a0000"),
    ("Matrix",   "#00ff41", "#001a08"),
    ("Gold",     "#ffd700", "#1a1400"),
    ("Amethyst", "#8833cc", "#0d0020"),
    ("Steel",    "#aaaaaa", "#111111"),
]

# Text colours that get the neon rim treatment
NEON = {"#00ff41"}

ALL_ZONES = sorted(zoneinfo.available_timezones())

COLOR_CYCLE = ["#ffffff", "#2277ff", "#cc0000", "#00aa00", "#ffd700", "#8833cc"]


# ---------------------------------------------------------------------------
#  Fonts
#
#  "Segoe UI" silently falls back to a default Tk font off Windows, which looks
#  wrong rather than merely different.  Resolve once against what's installed.
# ---------------------------------------------------------------------------
UI_FAMILY = "Segoe UI"
DIGIT_FAMILY = "Segoe UI"

_UI_PREFS = ("Segoe UI", "Inter", "SF Pro Text", "Helvetica Neue",
             "DejaVu Sans", "Liberation Sans", "Arial")
_DIGIT_PREFS = ("Segoe UI", "Inter", "SF Pro Display", "Helvetica Neue",
                "DejaVu Sans", "Liberation Sans", "Arial")


def resolve_fonts(root):
    """Pick the first installed family from each preference list."""
    global UI_FAMILY, DIGIT_FAMILY
    try:
        available = {f.lower() for f in tkinter.font.families(root)}
    except tk.TclError:
        return
    for pref, target in ((_UI_PREFS, "ui"), (_DIGIT_PREFS, "digit")):
        for fam in pref:
            if fam.lower() in available:
                if target == "ui":
                    UI_FAMILY = fam
                else:
                    DIGIT_FAMILY = fam
                break


def ui_font(size, weight="normal"):
    """Point-sized: follows the display DPI, for the manager and dialogs."""
    return (UI_FAMILY, size, weight)


def ui_font_px(size, weight="normal"):
    """Pixel-sized (negative Tk size): for chrome on the clock widget, whose
    geometry is in pixels and must not be re-scaled underneath it."""
    return (UI_FAMILY, -abs(size), weight)


def digit_font(size):
    return (DIGIT_FAMILY, -abs(size), "bold")


# ---------------------------------------------------------------------------
#  Colour maths
#
#  These sit in the animation hot path, so they memoise.  Callers quantise
#  continuous factors (see QUANT) before calling, otherwise every frame is a
#  cache miss.
# ---------------------------------------------------------------------------
QUANT = 32.0


def quantise(t):
    """Snap a 0..1 factor to 1/32 steps so the colour caches actually hit."""
    return round(max(0.0, min(1.0, t)) * QUANT) / QUANT


@functools.lru_cache(maxsize=512)
def to_rgb(hex_c):
    h = hex_c.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _clamp8(v):
    return 0 if v < 0 else (255 if v > 255 else int(v))


def to_hex(r, g, b):
    return f"#{_clamp8(r):02x}{_clamp8(g):02x}{_clamp8(b):02x}"


@functools.lru_cache(maxsize=1024)
def darken(hex_c, f):
    r, g, b = to_rgb(hex_c)
    return to_hex(r * f, g * f, b * f)


@functools.lru_cache(maxsize=1024)
def lighten(hex_c, f):
    """Scale toward white rather than multiplying, so near-black still lifts."""
    r, g, b = to_rgb(hex_c)
    return to_hex(r + (255 - r) * f, g + (255 - g) * f, b + (255 - b) * f)


@functools.lru_cache(maxsize=2048)
def lerp(a, b, t):
    ra, ga, ba = to_rgb(a)
    rb, gb, bb = to_rgb(b)
    return to_hex(ra + (rb - ra) * t, ga + (gb - ga) * t, ba + (bb - ba) * t)


def smoothstep(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def is_neon(text_color):
    return text_color.lower() in NEON


# ---------------------------------------------------------------------------
#  Config
#
#  Windows keeps %APPDATA%\Solari so existing installs carry over untouched.
# ---------------------------------------------------------------------------
def config_dir():
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def config_path():
    return os.path.join(config_dir(), "solari_config.json")


def pretty_path(p, limit=44):
    """Shorten a config path for display without losing the tail."""
    appdata = os.environ.get("APPDATA")
    if IS_WINDOWS and appdata and p.lower().startswith(appdata.lower()):
        p = "%APPDATA%" + p[len(appdata):]
    else:
        home = os.path.expanduser("~")
        if home and p.startswith(home):
            p = "~" + p[len(home):]
    if len(p) > limit:
        p = p[:limit // 2 - 2] + "..." + p[-(limit // 2 - 1):]
    return p


def exe_path():
    return sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)


def asset_path(name):
    """Locate a bundled file both in-tree and inside a PyInstaller bundle."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(base, name)
    return p if os.path.exists(p) else None


def set_startup(enable):
    if not HAS_WINREG:
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            if enable:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe_path()}"')
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
        return True
    except OSError:
        return False


def get_startup():
    if not HAS_WINREG:
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, APP_NAME)
        finally:
            winreg.CloseKey(key)
        return True
    except OSError:
        return False


DEFAULT_CLOCK = {
    "tz": "UTC",
    "label": "UTC",
    "x": 100,
    "y": 100,
    "text_color": "#ffffff",
    "card_color": "#1e1e1e",
    "scale": 1.0,
    "hour24": True,
    "show_seconds": True,
    "opacity": 0.97,
    "topmost": True,
}

DEFAULT_CONFIG = [
    dict(DEFAULT_CLOCK, tz="America/New_York", label="New York",
         x=60, y=80, text_color="#ffffff", card_color="#1e1e1e"),
    dict(DEFAULT_CLOCK, tz="Europe/London", label="London",
         x=500, y=80, text_color="#2277ff", card_color="#001033"),
    dict(DEFAULT_CLOCK, tz="Asia/Tokyo", label="Tokyo",
         x=940, y=80, text_color="#cc0000", card_color="#1a0000"),
]

SCALE_MIN = 0.4
SCALE_MAX = 3.0
OPACITY_MIN = 0.35
OPACITY_MAX = 1.0

_HEX_OK = set("0123456789abcdefABCDEF")


def _valid_hex(v, fallback):
    if isinstance(v, str) and len(v) == 7 and v[0] == "#" and all(c in _HEX_OK for c in v[1:]):
        return v.lower()
    return fallback


def _num(v, lo, hi, fallback):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return fallback
    if n != n:  # NaN
        return fallback
    return max(lo, min(hi, n))


def migrate_clock(raw):
    """Normalise one clock entry from any past schema. Returns None if unusable."""
    if not isinstance(raw, dict):
        return None
    c = dict(DEFAULT_CLOCK)

    tz = raw.get("tz")
    if not isinstance(tz, str) or not tz:
        return None
    try:
        zoneinfo.ZoneInfo(tz)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return None
    c["tz"] = tz

    label = raw.get("label")
    c["label"] = label.strip() if isinstance(label, str) and label.strip() else tz.split("/")[-1].replace("_", " ")

    # v0 used a single "color" key
    text_color = raw.get("text_color", raw.get("color", "#ffffff"))
    text_color = _valid_hex(text_color, "#ffffff")
    # v0/v1 greys that read as muddy against the new chassis
    if text_color in ("#bbbbbb", "#ddcc00"):
        text_color = "#ffffff"
    c["text_color"] = text_color
    c["card_color"] = _valid_hex(raw.get("card_color"), PAL_DARK.get(text_color, "#1e1e1e"))

    c["x"] = int(_num(raw.get("x"), -30000, 30000, 100))
    c["y"] = int(_num(raw.get("y"), -30000, 30000, 100))
    c["scale"] = round(_num(raw.get("scale"), SCALE_MIN, SCALE_MAX, 1.0), 2)
    c["opacity"] = round(_num(raw.get("opacity"), OPACITY_MIN, OPACITY_MAX, 0.97), 2)
    c["hour24"] = bool(raw.get("hour24", True))
    c["show_seconds"] = bool(raw.get("show_seconds", True))
    c["topmost"] = bool(raw.get("topmost", True))
    return c


def load_config(path=None):
    """Read config, dropping only the entries that are unusable.

    A single bad entry used to discard the whole file (and the bare `assert`
    that guarded it vanished under `python -O`).
    """
    path = path or config_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return [dict(c) for c in DEFAULT_CONFIG]

    if isinstance(data, dict):           # v2 wrapper form
        data = data.get("clocks", [])
    if not isinstance(data, list):
        return [dict(c) for c in DEFAULT_CONFIG]

    clocks = [m for m in (migrate_clock(c) for c in data) if m]
    return clocks or [dict(c) for c in DEFAULT_CONFIG]


def save_config(configs, path=None):
    """Write atomically - a crash mid-write used to truncate the config."""
    path = path or config_path()
    payload = {"version": CONFIG_VERSION, "clocks": configs}
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def clamp_to_screen(x, y, w, h, sw, sh, margin=24):
    """Keep at least `margin` px of the widget reachable on screen.

    A clock last positioned on a monitor that is no longer attached would
    otherwise restore off-screen with no way to get it back.
    """
    x = min(x, sw - margin)
    x = max(x, margin - w)
    y = min(y, sh - margin)
    y = max(y, 0)
    return int(x), int(y)


# ---------------------------------------------------------------------------
#  Card rendering
#
#  Card faces are pre-blended against the colour they sit on rather than kept
#  transparent, so their antialiased corners composite exactly and the widget
#  can stay a plain opaque image item.  The area behind the cards is therefore
#  a flat well colour - the chassis gradient lives outside it.
# ---------------------------------------------------------------------------
CARD_SS = 3          # supersample factor before LANCZOS downsample
BASE_W = 82          # card geometry at scale 1.0
BASE_H = 104
BASE_FS = 78         # digit height in px
BASE_R = 12          # corner radius


def _lerp_raw(a, b, t):
    """Uncached lerp for bulk gradient work, so it can't evict the hot cache."""
    ra, ga, ba = to_rgb(a)
    rb, gb, bb = to_rgb(b)
    return to_hex(ra + (rb - ra) * t, ga + (gb - ga) * t, ba + (bb - ba) * t)


def _vgrad(w, h, stops):
    """Vertical gradient RGB image. stops = [(pos 0..1, hex), ...] ascending."""
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    last = len(stops) - 1
    for y in range(h):
        t = y / max(1, h - 1)
        for i in range(last):
            p0, c0 = stops[i]
            p1, c1 = stops[i + 1]
            if t <= p1 or i == last - 1:
                span = (p1 - p0) or 1.0
                d.line([(0, y), (w, y)], fill=_lerp_raw(c0, c1, max(0.0, min(1.0, (t - p0) / span))))
                break
    return img


def _vramp(w, h, a0, a1, p0=0.0, p1=1.0):
    """L-mode vertical alpha ramp, a0 at p0 fading to a1 at p1."""
    img = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        if t <= p0:
            v = a0
        elif t >= p1:
            v = a1
        else:
            v = a0 + (a1 - a0) * ((t - p0) / ((p1 - p0) or 1.0))
        d.line([(0, y), (w, y)], fill=int(v))
    return img


@functools.lru_cache(maxsize=96)
def render_card(w, h, radius, card_color, rim_color, behind, rolling):
    """One card face as an RGB PIL image, already blended onto `behind`.

    Shading model: lit from above.  A three-stop body gradient with a touch of
    bounce light at the base, a specular highlight that fades down the top
    corners, a matching shadow rising from the bottom, and a faint inner bevel.
    `rim_color` adds a neon inner glow; `rolling` bakes in the drum-interior
    shade used while a digit is scrolling.
    """
    s = CARD_SS
    W, H, R = w * s, h * s, max(1, radius * s)

    body = _vgrad(W, H, [
        (0.00, lighten(card_color, 0.17)),
        (0.50, card_color),
        (0.88, darken(card_color, 0.62)),
        (1.00, darken(card_color, 0.80)),   # bounce light off the base
    ])
    if rolling:
        # Recessed drum interior: darken the middle band the digit travels through
        shade = _vramp(W, H, 0, 0, 0.0, 0.0)
        d = ImageDraw.Draw(shade)
        band = int(H * 0.62)
        top = (H - band) // 2
        for i in range(band):
            t = i / max(1, band - 1)
            # smooth bell, strongest at the centre of the travel
            v = int(86 * (1.0 - abs(2.0 * t - 1.0)) ** 0.8)
            d.line([(0, top + i), (W, top + i)], fill=v)
        body = Image.composite(Image.new("RGB", (W, H), "#000000"), body, shade)

    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, H - 1], radius=R, fill=255)

    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    card.paste(body, (0, 0), mask)

    lw = max(1, int(s * 1.2))

    # Specular top edge - fades away down the shoulders
    spec = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(spec).rounded_rectangle(
        [lw // 2, lw // 2, W - 1 - lw // 2, H - 1 - lw // 2],
        radius=R, outline=(255, 255, 255, 255), width=lw)
    spec.putalpha(Image.composite(_vramp(W, H, 132, 0, 0.0, 0.42), Image.new("L", (W, H), 0), spec.split()[3]))
    card = Image.alpha_composite(card, spec)

    # Contact shadow rising from the base
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [lw // 2, lw // 2, W - 1 - lw // 2, H - 1 - lw // 2],
        radius=R, outline=(0, 0, 0, 255), width=lw)
    shadow.putalpha(Image.composite(_vramp(W, H, 0, 150, 0.55, 1.0), Image.new("L", (W, H), 0), shadow.split()[3]))
    card = Image.alpha_composite(card, shadow)

    # Inner bevel - a second, inset edge that sells thickness
    bevel = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    inset = lw * 2
    ImageDraw.Draw(bevel).rounded_rectangle(
        [inset, inset, W - 1 - inset, H - 1 - inset],
        radius=max(1, R - inset), outline=(255, 255, 255, 255), width=max(1, lw // 2))
    bevel.putalpha(Image.composite(_vramp(W, H, 46, 0, 0.0, 0.30), Image.new("L", (W, H), 0), bevel.split()[3]))
    card = Image.alpha_composite(card, bevel)

    # Neon inner rim
    if rim_color:
        rr, gg, bb = to_rgb(rim_color)
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(glow).rounded_rectangle(
            [lw, lw, W - 1 - lw, H - 1 - lw],
            radius=max(1, R - lw), outline=(rr, gg, bb, 210), width=lw)
        glow = glow.filter(ImageFilter.GaussianBlur(s * 1.6))
        glow.putalpha(Image.composite(glow.split()[3], Image.new("L", (W, H), 0), mask))
        card = Image.alpha_composite(card, glow)

    out = Image.new("RGB", (W, H), behind)
    out.paste(card, (0, 0), card)
    return out.resize((w, h), Image.LANCZOS)


# ---------------------------------------------------------------------------
#  Ticker
#
#  One second-aligned timer and one animation loop for the whole app.  Each
#  clock used to run its own 250 ms poll plus a 500 ms blink, so N clocks meant
#  6N timers a second and digits flipped up to 250 ms after the real second.
# ---------------------------------------------------------------------------
FRAME_MS = 16               # ~60 fps while something is actually moving
DRUM_DURATION = 0.19        # seconds, wall-clock


class Ticker:
    def __init__(self, root):
        self._root = root
        self._clocks = []
        self._anim = set()
        self._tick_job = None
        self._anim_job = None
        self._running = False

    def subscribe(self, clock):
        if clock not in self._clocks:
            self._clocks.append(clock)

    def unsubscribe(self, clock):
        if clock in self._clocks:
            self._clocks.remove(clock)
        self._anim.discard(clock)

    def start(self):
        if self._running:
            return
        self._running = True
        self._tick()

    def stop(self):
        self._running = False
        for job in (self._tick_job, self._anim_job):
            if job:
                try:
                    self._root.after_cancel(job)
                except tk.TclError:
                    pass
        self._tick_job = self._anim_job = None
        self._clocks.clear()
        self._anim.clear()

    def _tick(self):
        self._tick_job = None
        if not self._running:
            return
        now = time.time()
        for clock in list(self._clocks):
            try:
                clock.on_second(now)
            except tk.TclError:
                self.unsubscribe(clock)
        # Re-aim at the next true second boundary every time, so the clock
        # cannot drift no matter how long the tick itself took.
        delay = int((1.0 - (time.time() % 1.0)) * 1000) + 3
        self._tick_job = self._root.after(max(20, min(1000, delay)), self._tick)

    def animate(self, target):
        """Register anything exposing step(now) -> keep_going."""
        self._anim.add(target)
        if self._anim_job is None and self._running:
            self._anim_job = self._root.after(FRAME_MS, self._frame)

    def _frame(self):
        self._anim_job = None
        if not self._running:
            return
        now = time.perf_counter()
        for target in list(self._anim):
            try:
                if not target.step(now):
                    self._anim.discard(target)
            except tk.TclError:
                self._anim.discard(target)
        if self._anim:
            self._anim_job = self._root.after(FRAME_MS, self._frame)


# ---------------------------------------------------------------------------
#  FlipCard
#
#  A single canvas sized exactly to the card, so a digit scrolling past the top
#  edge is clipped by the canvas for free.  (The old two-half-canvas split was
#  left over from a real flip design and doubled every draw once the divider
#  was dropped.)  Items are created once and animated with coords() and
#  itemconfigure() - nothing is deleted or recreated per frame.
# ---------------------------------------------------------------------------
class FlipCard(tk.Canvas):

    def __init__(self, parent, ticker, text_color, card_color, scale=1.0,
                 behind=None):
        self._behind = behind or THEME["well"]
        self.text_color = text_color
        self.card_color = card_color
        self._apply_scale(scale)
        super().__init__(parent, width=self.W, height=self.H,
                         bg=self._behind, highlightthickness=0, bd=0,
                         takefocus=0)
        self.ticker = ticker
        self._cur = "0"
        self._nxt = "0"
        self._rolling = False
        self._t0 = 0.0
        self._photo_idle = None     # strong refs: an unreferenced PhotoImage is
        self._photo_roll = None     # collected and the card silently blanks
        self._bg = None
        self._build()

    # -- geometry ----------------------------------------------------------
    def _apply_scale(self, scale):
        self.scale = scale
        self.W = max(20, int(BASE_W * scale))
        self.H = max(24, int(BASE_H * scale))
        self.R = max(3, int(BASE_R * scale))
        self.FS = max(8, int(BASE_FS * scale))

    # -- construction ------------------------------------------------------
    def _build(self):
        self._render_faces()
        if HAS_PIL:
            self._bg = self.create_image(0, 0, anchor="nw", image=self._photo_idle)
        else:
            self._bg = self._fallback_face()
        cy = self.H // 2
        font = digit_font(self.FS)
        self._txt_a = self.create_text(self.W // 2, cy, text=self._cur,
                                       font=font, fill=self.text_color,
                                       anchor="center")
        self._txt_b = self.create_text(self.W // 2, cy, text=self._nxt,
                                       font=font, fill=self.text_color,
                                       anchor="center", state="hidden")

    def _render_faces(self):
        if not HAS_PIL:
            return
        rim = self.text_color if is_neon(self.text_color) else None
        idle = render_card(self.W, self.H, self.R, self.card_color, rim,
                           self._behind, False)
        roll = render_card(self.W, self.H, self.R, self.card_color, rim,
                           self._behind, True)
        self._photo_idle = ImageTk.PhotoImage(idle)
        self._photo_roll = ImageTk.PhotoImage(roll)

    def _fallback_face(self):
        """Plain rounded polygon when Pillow is missing - flat, but it runs."""
        r, W, H = self.R, self.W, self.H
        pts = [r, 0, W - r, 0, W, 0, W, r, W, H - r, W, H,
               W - r, H, r, H, 0, H, 0, H - r, 0, r, 0, 0]
        return self.create_polygon(pts, smooth=True, fill=self.card_color,
                                   outline="")

    def _set_face(self, rolling):
        if HAS_PIL and self._bg is not None:
            self.itemconfigure(self._bg,
                               image=self._photo_roll if rolling else self._photo_idle)

    # -- public ------------------------------------------------------------
    def set(self, digit):
        """Roll to `digit`. A digit arriving mid-roll queues for the next one."""
        if digit == self._cur and not self._rolling:
            return
        self._nxt = digit
        if self._rolling:
            # Queued mid-roll: retarget the incoming item so the roll lands on
            # the newest digit instead of animating a stale one in first.
            self.itemconfigure(self._txt_b, text=digit)
            return
        self._rolling = True
        self._t0 = time.perf_counter()
        self._set_face(True)
        # Text only - it stays hidden until step() has somewhere to put it.
        # Revealing it here left it stacked on the outgoing digit at centre for
        # one frame, which flashed a doubled digit on every flip.
        self.itemconfigure(self._txt_b, text=digit)
        self.ticker.animate(self)

    def set_immediate(self, digit):
        self._cur = self._nxt = digit
        self._rolling = False
        self._set_face(False)
        self.itemconfigure(self._txt_a, text=digit, fill=self.text_color)
        self.coords(self._txt_a, self.W // 2, self.H // 2)
        self.itemconfigure(self._txt_b, state="hidden")

    def update_style(self, text_color, card_color):
        self.text_color = text_color
        self.card_color = card_color
        self._render_faces()
        if HAS_PIL:
            self._set_face(self._rolling)
        elif self._bg is not None:
            self.itemconfigure(self._bg, fill=card_color)
        if not self._rolling:
            self.itemconfigure(self._txt_a, fill=text_color)

    def rescale(self, scale):
        """Resize in place - no widget teardown, so live drag resize is cheap."""
        if abs(scale - self.scale) < 0.005:
            return
        self._apply_scale(scale)
        self.configure(width=self.W, height=self.H)
        self._render_faces()
        if HAS_PIL:
            self._set_face(self._rolling)
        else:
            self.delete(self._bg)
            self._bg = self._fallback_face()
            self.tag_lower(self._bg)
        font = digit_font(self.FS)
        for item in (self._txt_a, self._txt_b):
            self.itemconfigure(item, font=font)
        if not self._rolling:
            self.coords(self._txt_a, self.W // 2, self.H // 2)

    # -- animation ---------------------------------------------------------
    def step(self, now):
        """One frame. Returns False when the roll is finished."""
        t = (now - self._t0) / DRUM_DURATION
        if t >= 1.0:
            self.set_immediate(self._nxt)
            return False

        e = smoothstep(t)
        cx, cy, H = self.W // 2, self.H // 2, self.H
        # Outgoing digit rides up and out; incoming rises into place.
        a_alpha = max(0.0, 1.0 - t * 1.4)
        b_alpha = max(0.0, min(1.0, (t - 0.3) / 0.7))

        if a_alpha > 0.02:
            self.coords(self._txt_a, cx, cy - int(H * e))
            self.itemconfigure(self._txt_a, state="normal",
                               fill=darken(self.text_color, quantise(a_alpha)))
        else:
            self.itemconfigure(self._txt_a, state="hidden")

        if b_alpha > 0.02:
            self.coords(self._txt_b, cx, cy + int(H * (1.0 - e)))
            self.itemconfigure(self._txt_b, state="normal",
                               fill=darken(self.text_color, quantise(b_alpha)))
        else:
            self.itemconfigure(self._txt_b, state="hidden")
        return True


# ---------------------------------------------------------------------------
#  Chassis rendering
#
#  The clock widget used to be a stack of flat frames, which read as a black
#  rectangle with parts glued on.  It is now one raised panel with a recessed
#  well cut into it, rendered as a single image.
#
#  The window itself stays rectangular.  Colour-key transparency is the only
#  way Tk can round a window, and it cannot blend edges - the antialiased
#  corner pixels survive as a jagged halo, which looks worse than a bezel.  So
#  the area outside the panel is pure black and reads as shadow.
# ---------------------------------------------------------------------------
CHASSIS_SS = 2


@functools.lru_cache(maxsize=48)
def render_chassis(w, h, pad, radius, well, accent):
    """Full widget background. `well` is the recessed rect in unscaled coords."""
    s = CHASSIS_SS
    W, H = w * s, h * s
    P, R = pad * s, max(2, radius * s)
    base = THEME["chassis"]

    img = Image.new("RGB", (W, H), "#000000")

    panel = _vgrad(W - 2 * P, H - 2 * P, [
        (0.00, lighten(base, 0.10)),
        (0.42, base),
        (1.00, darken(base, 0.66)),
    ])
    pmask = Image.new("L", (W - 2 * P, H - 2 * P), 0)
    ImageDraw.Draw(pmask).rounded_rectangle(
        [0, 0, W - 2 * P - 1, H - 2 * P - 1], radius=R, fill=255)
    img.paste(panel, (P, P), pmask)

    lw = max(1, int(s))
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # Rim light along the panel edge, brightest on top - separates the panel
    # from the black around it.
    rim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(rim).rounded_rectangle(
        [P, P, W - P - 1, H - P - 1], radius=R,
        outline=(255, 255, 255, 255), width=lw)
    rim.putalpha(Image.composite(_vramp(W, H, 74, 12, 0.0, 0.75),
                                 Image.new("L", (W, H), 0), rim.split()[3]))
    layer = Image.alpha_composite(layer, rim)

    # Recessed well: flat fill, because card corners pre-blend against exactly
    # this colour, plus an inner shadow confined to its padding ring.
    x1, y1, x2, y2 = (v * s for v in well)
    wr = max(2, int(radius * s * 0.6))
    d.rounded_rectangle([x1, y1, x2, y2], radius=wr, fill=THEME["well"])

    depth = max(1, int(s * 2))
    for i in range(depth * 3):
        a = int(120 * (1.0 - i / (depth * 3)))
        d.rounded_rectangle([x1 + i, y1 + i, x2 - i, y2 - i], radius=max(1, wr - i),
                            outline=(0, 0, 0, a), width=1)
    # Light catching the bottom lip of the recess
    d.arc([x1, y1, x2, y2], start=20, end=160, fill=(255, 255, 255, 26), width=lw)

    img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")

    # Faint accent bloom above the well, so the clock's colour tints its housing
    if accent:
        rr, gg, bb = to_rgb(accent)
        bloom = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(bloom).rounded_rectangle(
            [x1 - s * 3, y1 - s * 3, x2 + s * 3, y2 + s * 3],
            radius=wr, outline=(rr, gg, bb, 105), width=max(1, s * 2))
        bloom = bloom.filter(ImageFilter.GaussianBlur(s * 3.5))
        img = Image.alpha_composite(img.convert("RGBA"), bloom).convert("RGB")

    return img.resize((w, h), Image.LANCZOS)


# ---------------------------------------------------------------------------
#  ClockWindow
#
#  One canvas for the whole widget.  Every piece of chrome is a canvas item on
#  the chassis image, and the flip cards are embedded child canvases (nested so
#  they clip their own digits).  Relayout moves items rather than destroying
#  them, which is what makes live drag-resize cheap.
# ---------------------------------------------------------------------------
SNAP_PX = 9


class ClockWindow(tk.Toplevel):

    def __init__(self, master, cfg, ticker, on_close, on_edit, peers):
        super().__init__(master)
        self.cfg = cfg
        self.ticker = ticker
        self.on_close = on_close
        self.on_edit = on_edit
        self._peers = peers          # () -> other ClockWindows, for snapping

        self._scale = float(cfg.get("scale", 1.0))
        self._drag = None
        self._resize = None
        self._zone = None
        self._zone_key = None
        self._cards = {}
        self._colons = []
        self._last = {}
        self._photo = None           # strong ref to the chassis image
        self._colon_t0 = None
        self._shown = {}             # last text pushed to each label item

        self.overrideredirect(True)
        self.configure(bg="#000000")
        self.wm_attributes("-topmost", bool(cfg.get("topmost", True)))
        self._apply_opacity()

        self.cv = tk.Canvas(self, highlightthickness=0, bd=0, bg="#000000",
                            takefocus=0)
        self.cv.pack(fill="both", expand=True)
        self._build()
        self._place_initial()
        self._bind()
        self.ticker.subscribe(self)
        self.on_second(time.time(), animate=False)

    # -- config helpers ----------------------------------------------------
    def _tc(self):
        return self.cfg.get("text_color", "#ffffff")

    def _cc(self):
        return self.cfg.get("card_color", "#1e1e1e")

    def _apply_opacity(self):
        try:
            self.wm_attributes("-alpha", float(self.cfg.get("opacity", 0.97)))
        except tk.TclError:
            pass

    def zone(self):
        """Cached ZoneInfo - it used to be reconstructed on every tick."""
        key = self.cfg.get("tz", "UTC")
        if key != self._zone_key:
            try:
                self._zone = zoneinfo.ZoneInfo(key)
            except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
                self._zone = datetime.timezone.utc
            self._zone_key = key
        return self._zone

    # -- layout ------------------------------------------------------------
    def _card_keys(self):
        keys = ["h1", "h2", "m1", "m2"]
        if self.cfg.get("show_seconds", True):
            keys += ["s1", "s2"]
        return keys

    def _layout(self):
        s = self._scale
        L = {
            "pad": max(5, int(8 * s)),
            "r": max(8, int(16 * s)),
            "mx": max(8, int(14 * s)),
            "my": max(6, int(10 * s)),
            "hdr": max(16, int(26 * s)),
            "ftr": max(14, int(22 * s)),
            "gap": max(4, int(8 * s)),
            "wp": max(4, int(10 * s)),
            "cw": max(20, int(BASE_W * s)),
            "ch": max(24, int(BASE_H * s)),
            "cgap": max(2, int(5 * s)),
            "colw": max(7, int(16 * s)),
            "fs_lbl": max(9, int(13 * s)),
            "fs_ftr": max(9, int(14 * s)),
            "fs_col": max(14, int(52 * s)),
            "handle": max(9, int(13 * s)),
        }
        seq = ["h1", "h2", ":", "m1", "m2"]
        if self.cfg.get("show_seconds", True):
            seq += [":", "s1", "s2"]
        x, slots = 0, []
        for i, k in enumerate(seq):
            if i:
                x += L["cgap"]
            w = L["colw"] if k == ":" else L["cw"]
            slots.append((k, x, w))
            x += w
        L["slots"] = slots
        L["row_w"] = x

        well_w = x + 2 * L["wp"]
        well_h = L["ch"] + 2 * L["wp"]
        panel_w = well_w + 2 * L["mx"]

        # Header needs room for dot + label + two buttons
        self._f_lbl.configure(size=-L["fs_lbl"])
        hdr_need = int(L["mx"] * 2 + 14 * s + self._f_lbl.measure(self.cfg.get("label", "")) + 54 * s)
        panel_w = max(panel_w, hdr_need)

        panel_h = L["my"] * 2 + L["hdr"] + L["gap"] + well_h + L["gap"] + L["ftr"]
        L["panel_w"], L["panel_h"] = panel_w, panel_h
        L["w"] = panel_w + 2 * L["pad"]
        L["h"] = panel_h + 2 * L["pad"]

        wx = L["pad"] + (panel_w - well_w) // 2
        wy = L["pad"] + L["my"] + L["hdr"] + L["gap"]
        L["well"] = (wx, wy, wx + well_w, wy + well_h)
        L["row_x"] = wx + L["wp"]
        L["row_y"] = wy + L["wp"]
        return L

    # -- construction ------------------------------------------------------
    def _build(self):
        self._f_lbl = tkinter.font.Font(family=UI_FAMILY, size=-10, weight="bold")
        L = self._layout()
        cv = self.cv

        self._bg = cv.create_image(0, 0, anchor="nw")
        self._dot = cv.create_oval(0, 0, 0, 0, outline="", tags=("chrome",))
        self._lbl = cv.create_text(0, 0, anchor="w", text="", tags=("chrome",))
        self._btn_edit = cv.create_text(0, 0, anchor="center", text="✎",
                                        tags=("chrome", "nodrag", "btn_edit"))
        self._btn_close = cv.create_text(0, 0, anchor="center", text="✕",
                                         tags=("chrome", "nodrag", "btn_close"))
        self._date = cv.create_text(0, 0, anchor="w", text="", tags=("chrome",))
        self._chip = cv.create_text(0, 0, anchor="e", text="", tags=("chrome",))
        self._badge = cv.create_text(0, 0, anchor="e", text="", tags=("chrome",))
        self._handle = cv.create_polygon(0, 0, 0, 0, 0, 0, outline="",
                                         tags=("nodrag", "handle"))
        self._sync_cards(L)
        self._relayout(L)

    def _sync_cards(self, L):
        """Create or drop only the cards whose presence actually changed."""
        want = self._card_keys()
        for key in list(self._cards):
            if key not in want:
                self._cards.pop(key).destroy()
                self._last.pop(key, None)
        for key in want:
            if key not in self._cards:
                card = FlipCard(self.cv, self.ticker, self._tc(), self._cc(),
                                self._scale)
                self._bind_card(card)
                self._cards[key] = card
        n_colons = sum(1 for k, _, _ in L["slots"] if k == ":")
        while len(self._colons) < n_colons:
            self._colons.append(self.cv.create_text(0, 0, anchor="center",
                                                    text=":", tags=("chrome",)))
        while len(self._colons) > n_colons:
            self.cv.delete(self._colons.pop())

    def _relayout(self, L=None):
        L = L or self._layout()
        cv, s = self.cv, self._scale
        tc = self._tc()

        self.geometry(f"{L['w']}x{L['h']}")
        cv.configure(width=L["w"], height=L["h"])

        if HAS_PIL:
            accent = tc if tc.lower() not in ("#ffffff", "#aaaaaa") else None
            self._photo = ImageTk.PhotoImage(
                render_chassis(L["w"], L["h"], L["pad"], L["r"], L["well"], accent))
            cv.itemconfigure(self._bg, image=self._photo)
        else:
            cv.itemconfigure(self._bg, state="hidden")
            cv.configure(bg=THEME["chassis"])

        hx = L["pad"] + L["mx"]
        hy = L["pad"] + L["my"] + L["hdr"] // 2
        dr = max(2, int(4 * s))
        cv.coords(self._dot, hx, hy - dr, hx + 2 * dr, hy + dr)
        cv.itemconfigure(self._dot, fill=tc)
        cv.coords(self._lbl, hx + 3 * dr, hy)
        cv.itemconfigure(self._lbl, text=self.cfg.get("label", ""), fill=tc,
                         font=ui_font_px(L["fs_lbl"], "bold"))

        bx = L["pad"] + L["panel_w"] - L["mx"]
        bs = max(9, int(12 * s))
        cv.coords(self._btn_close, bx - bs // 2, hy)
        cv.coords(self._btn_edit, bx - bs * 2, hy)
        for item in (self._btn_close, self._btn_edit):
            cv.itemconfigure(item, fill=THEME["text_faint"], font=ui_font_px(bs))

        ci = 0
        for key, ox, w in L["slots"]:
            x = L["row_x"] + ox
            if key == ":":
                cv.coords(self._colons[ci], x + w // 2, L["row_y"] + L["ch"] // 2)
                cv.itemconfigure(self._colons[ci], font=digit_font(L["fs_col"]),
                                 fill=THEME["colon"])
                ci += 1
            else:
                card = self._cards[key]
                card.rescale(s)
                if getattr(card, "_win", None) is None:
                    card._win = cv.create_window(x, L["row_y"], anchor="nw",
                                                 window=card)
                else:
                    cv.coords(card._win, x, L["row_y"])

        fy = L["pad"] + L["panel_h"] - L["my"] - L["ftr"] // 2
        cv.coords(self._date, L["pad"] + L["mx"], fy)
        cv.itemconfigure(self._date, fill=tc, font=ui_font_px(L["fs_ftr"], "bold"))
        cv.coords(self._badge, bx, fy)
        cv.itemconfigure(self._badge, font=ui_font_px(max(7, int(9 * s)), "bold"))
        cv.coords(self._chip, bx, fy)
        cv.itemconfigure(self._chip, fill=THEME["text_dim"],
                         font=ui_font_px(L["fs_ftr"]))

        hp = L["handle"]
        x2, y2 = L["w"] - L["pad"] - 2, L["h"] - L["pad"] - 2
        cv.coords(self._handle, x2, y2 - hp, x2, y2, x2 - hp, y2)
        cv.itemconfigure(self._handle, fill=lighten(THEME["chassis"], 0.16))
        cv.tag_raise(self._handle)
        self._L = L
        self._refresh_footer()

    # -- binding -----------------------------------------------------------
    def _bind(self):
        cv = self.cv
        cv.bind("<ButtonPress-1>", self._drag_start, add="+")
        cv.bind("<B1-Motion>", self._drag_move, add="+")
        cv.bind("<ButtonRelease-1>", self._drag_end, add="+")
        cv.tag_bind("btn_edit", "<Button-1>", lambda e: self.on_edit(self))
        cv.tag_bind("btn_close", "<Button-1>", lambda e: self.on_close(self))
        for tag, hover in (("btn_edit", self._tc), ("btn_close", lambda: THEME["danger"])):
            cv.tag_bind(tag, "<Enter>",
                        lambda e, t=tag, h=hover: cv.itemconfigure(t, fill=h()))
            cv.tag_bind(tag, "<Leave>",
                        lambda e, t=tag: cv.itemconfigure(t, fill=THEME["text_faint"]))
        cv.tag_bind("handle", "<ButtonPress-1>", self._rs_start)
        cv.tag_bind("handle", "<B1-Motion>", self._rs_move)
        cv.tag_bind("handle", "<ButtonRelease-1>", self._rs_end)
        cv.tag_bind("handle", "<Enter>",
                    lambda e: cv.configure(cursor="size_nw_se"))
        cv.tag_bind("handle", "<Leave>", lambda e: cv.configure(cursor=""))

    def _bind_card(self, card):
        """Cards are separate widgets, so events do not reach the chassis.

        Bound exactly once at creation - the old recursive rebinder re-applied
        `add="+"` handlers on every rebuild and stacked duplicates.
        """
        card.bind("<ButtonPress-1>", self._drag_start, add="+")
        card.bind("<B1-Motion>", self._drag_move, add="+")
        card.bind("<ButtonRelease-1>", self._drag_end, add="+")

    def _on_nodrag(self, event):
        if event.widget is not self.cv:
            return False
        cur = self.cv.find_withtag("current")
        return bool(cur) and "nodrag" in self.cv.gettags(cur[0])

    # -- move / snap -------------------------------------------------------
    def _drag_start(self, e):
        if self._on_nodrag(e):
            return
        self._drag = (e.x_root - self.winfo_x(), e.y_root - self.winfo_y())

    def _drag_move(self, e):
        if not self._drag:
            return
        x, y = e.x_root - self._drag[0], e.y_root - self._drag[1]
        if not (e.state & 0x0001):        # hold Shift to place freely
            x, y = self._snap(x, y)
        self.geometry(f"+{x}+{y}")

    def _drag_end(self, e):
        if self._drag:
            self.cfg["x"], self.cfg["y"] = self.winfo_x(), self.winfo_y()
        self._drag = None

    def _snap(self, x, y):
        """Pull to screen edges and to the edges of sibling clocks."""
        w, h = self.winfo_width(), self.winfo_height()
        xs = [0, self.winfo_screenwidth() - w]
        ys = [0, self.winfo_screenheight() - h]
        for p in self._peers():
            if p is self:
                continue
            try:
                if not p.winfo_exists():
                    continue
                px, py = p.winfo_x(), p.winfo_y()
                pw, ph = p.winfo_width(), p.winfo_height()
            except tk.TclError:
                continue
            xs += [px, px + pw - w, px + pw, px - w]
            ys += [py, py + ph - h, py + ph, py - h]
        for c in xs:
            if abs(x - c) <= SNAP_PX:
                x = c
                break
        for c in ys:
            if abs(y - c) <= SNAP_PX:
                y = c
                break
        return x, y

    def _place_initial(self):
        self.update_idletasks()
        x, y = clamp_to_screen(self.cfg.get("x", 100), self.cfg.get("y", 100),
                               self.winfo_reqwidth(), self.winfo_reqheight(),
                               self.winfo_screenwidth(), self.winfo_screenheight())
        self.cfg["x"], self.cfg["y"] = x, y
        self.geometry(f"+{x}+{y}")

    def recall(self, x=40, y=40):
        """Bring a clock that ended up off-screen back into view.

        Order matters: on an overrideredirect window, setting -topmost after a
        move re-asserts the previous geometry and silently discards it, so the
        position has to be applied last.
        """
        self.update_idletasks()
        self.geometry(f"+{x}+{y}")
        self.update_idletasks()
        if (self.winfo_x(), self.winfo_y()) != (x, y):
            # Some window managers ignore a position-only request; restate it
            # with the size included.
            self.geometry(f"{self.winfo_width()}x{self.winfo_height()}+{x}+{y}")
        self.cfg["x"], self.cfg["y"] = x, y
        # Raise in a later pass.  lift() and -topmost both re-assert this
        # window's cached geometry on an overrideredirect window, which would
        # replay the old position straight over the move we just made.
        self.after(0, self._raise_self)

    def _raise_self(self):
        try:
            self.wm_attributes("-topmost", bool(self.cfg.get("topmost", True)))
            self.lift()
        except tk.TclError:
            pass

    # -- resize ------------------------------------------------------------
    def _rs_start(self, e):
        self._drag = None
        self.update_idletasks()
        self._resize = (self.winfo_x(), self.winfo_y(), self._scale,
                        max(1, self.winfo_width()), e.x_root)

    def _rs_move(self, e):
        if not self._resize:
            return
        wx, wy, s0, w0, rx0 = self._resize
        new = round(max(SCALE_MIN, min(SCALE_MAX, s0 + (e.x_root - rx0) * s0 / w0)), 2)
        if abs(new - self._scale) >= 0.02:
            self._scale = new
            self._relayout()          # in place: no widgets destroyed
            self.geometry(f"+{wx}+{wy}")

    def _rs_end(self, e):
        if self._resize:
            self.cfg["scale"] = self._scale
            self.cfg["x"], self.cfg["y"] = self.winfo_x(), self.winfo_y()
        self._resize = None

    # -- time --------------------------------------------------------------
    def on_second(self, _ts, animate=True):
        now = datetime.datetime.now(self.zone())
        hh = now.strftime("%H" if self.cfg.get("hour24", True) else "%I")
        mm, ss = now.strftime("%M"), now.strftime("%S")
        digits = {"h1": hh[0], "h2": hh[1], "m1": mm[0], "m2": mm[1],
                  "s1": ss[0], "s2": ss[1]}
        for key, card in self._cards.items():
            d = digits[key]
            if self._last.get(key) != d:
                card.set(d) if animate else card.set_immediate(d)
                self._last[key] = d
        self._refresh_footer(now)
        if animate and self._colons:
            self._colon_t0 = time.perf_counter()
            self.ticker.animate(self)

    def _set_text(self, item, text):
        """Only touch the canvas when the string actually changed."""
        if self._shown.get(item) != text:
            self.cv.itemconfigure(item, text=text)
            self._shown[item] = text

    def _refresh_footer(self, now=None):
        now = now or datetime.datetime.now(self.zone())
        self._set_text(self._date, now.strftime("%a  %b %d  %Y"))

        chip = utc_offset_label(now)
        if not self.cfg.get("hour24", True):
            chip = f"{now.strftime('%p')}  ·  {chip}"
        badge = day_offset_label(now)
        self._set_text(self._badge, badge)
        self.cv.itemconfigure(self._badge, fill=self._tc())

        bx = self._L["pad"] + self._L["panel_w"] - self._L["mx"]
        pad = self._f_lbl.measure("  " + badge) if badge else 0
        self.cv.coords(self._chip, bx - pad, self.cv.coords(self._chip)[1])
        self._set_text(self._chip, chip)

    def step(self, now):
        """Colon fade, pulsed once per second off the shared frame loop."""
        if self._colon_t0 is None:
            return False
        t = (now - self._colon_t0) / 0.55
        if t >= 1.0:
            self._colon_t0 = None
            col = THEME["colon_dim"]
            done = True
        else:
            col = lerp(THEME["colon_dim"], THEME["colon"],
                       quantise(1.0 - smoothstep(t)))
            done = False
        for c in self._colons:
            self.cv.itemconfigure(c, fill=col)
        return not done

    # -- style -------------------------------------------------------------
    def refresh_style(self):
        self._apply_opacity()
        self.wm_attributes("-topmost", bool(self.cfg.get("topmost", True)))
        self._scale = float(self.cfg.get("scale", self._scale))
        self._zone_key = None
        L = self._layout()
        self._sync_cards(L)
        for card in self._cards.values():
            card.update_style(self._tc(), self._cc())
        self._last.clear()
        self._shown.clear()
        self._relayout(L)
        self.on_second(time.time(), animate=False)

    def destroy(self):
        self.ticker.unsubscribe(self)
        super().destroy()


def utc_offset_label(dt):
    """'GMT+9', 'GMT-3:30', 'GMT' - what a world clock is actually for."""
    off = dt.utcoffset()
    if off is None:
        return "GMT"
    total = int(off.total_seconds()) // 60
    sign = "-" if total < 0 else "+"
    hh, mm = divmod(abs(total), 60)
    if not hh and not mm:
        return "GMT"
    return f"GMT{sign}{hh}" + (f":{mm:02d}" if mm else "")


def day_offset_label(dt, local_now=None):
    """'+1' / '-1' when that timezone is on a different calendar day."""
    local = local_now or datetime.datetime.now()
    delta = (dt.date() - local.date()).days
    if delta > 0:
        return "+1"
    if delta < 0:
        return "-1"
    return ""


# ---------------------------------------------------------------------------
#  Shared widgets
# ---------------------------------------------------------------------------
_BTN_STYLES = {
    "primary": ("accent", "#ffffff", "accent_hi", "#ffffff"),
    "ghost": ("surface", "text_dim", "surface_hi", "text"),
    "danger": ("surface", "text_dim", "surface_hi", "danger"),
    "flat": ("bg", "text_faint", "surface", "text"),
}


def make_button(parent, text, command, kind="ghost", **kw):
    # Entries are THEME keys, or literal colours that pass straight through.
    bg, fg, hbg, hfg = (THEME.get(v, v) for v in _BTN_STYLES[kind])
    b = tk.Button(parent, text=text, command=command, relief="flat", bd=0,
                  cursor="hand2", highlightthickness=0, bg=bg, fg=fg,
                  activebackground=hbg, activeforeground=hfg, **kw)
    b.bind("<Enter>", lambda e: b.configure(bg=hbg, fg=hfg))
    b.bind("<Leave>", lambda e: b.configure(bg=bg, fg=fg))
    return b


def entry_style():
    return dict(bg=THEME["surface"], fg=THEME["text"],
                insertbackground=THEME["text"], relief="flat",
                highlightthickness=1, highlightbackground=THEME["divider"],
                highlightcolor=THEME["accent"], font=ui_font(11))


class ScrollFrame(tk.Frame):
    """Vertically scrollable container. `body` is the frame to fill."""

    def __init__(self, parent, height=250, **kw):
        super().__init__(parent, bg=THEME["bg"], **kw)
        self._cv = tk.Canvas(self, bg=THEME["bg"], highlightthickness=0, bd=0,
                             height=height, takefocus=0)
        self._sb = tk.Scrollbar(self, orient="vertical", command=self._cv.yview,
                                relief="flat", bd=0, width=10,
                                bg=THEME["surface"], troughcolor=THEME["bg"],
                                activebackground=THEME["surface_hi"])
        self._cv.configure(yscrollcommand=self._on_scroll)
        self._cv.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self._cv, bg=THEME["bg"])
        self._win = self._cv.create_window(0, 0, anchor="nw", window=self.body)
        self.body.bind("<Configure>", self._resize)
        self._cv.bind("<Configure>",
                      lambda e: self._cv.itemconfigure(self._win, width=e.width))
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self._cv.bind_all(seq, self._wheel, add="+")

    def _on_scroll(self, lo, hi):
        # Only show the scrollbar when there is something to scroll to
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            self._sb.pack_forget()
        else:
            self._sb.pack(side="right", fill="y")
        self._sb.set(lo, hi)

    def _resize(self, _):
        self._cv.configure(scrollregion=self._cv.bbox("all"))

    def _wheel(self, e):
        try:
            if not self.winfo_ismapped():
                return
        except tk.TclError:
            return
        delta = -1 if getattr(e, "num", 0) == 5 or getattr(e, "delta", 0) < 0 else 1
        self._cv.yview_scroll(-delta, "units")


class SwatchPicker(tk.Frame):
    COLS = 6

    def __init__(self, parent, variable, **kw):
        super().__init__(parent, bg=THEME["bg"], **kw)
        self.var = variable
        self._tip = None
        self._holders = {}
        for i, (color, name) in enumerate(PALETTE):
            # A Radiobutton with indicatoron=False paints `selectcolor` over its
            # background when chosen, so the white swatch used to turn black the
            # moment it was selected.  Keep the swatch its own colour and show
            # selection with the surrounding holder instead.
            holder = tk.Frame(self, bg=THEME["divider"], padx=2, pady=2)
            holder.grid(row=i // self.COLS, column=i % self.COLS, padx=3, pady=3)
            rb = tk.Radiobutton(holder, variable=variable, value=color,
                                bg=color, activebackground=color,
                                selectcolor=color, indicatoron=False,
                                relief="flat", bd=0, width=3, height=1,
                                cursor="hand2", highlightthickness=0)
            rb.pack()
            self._holders[color] = holder
            self._tooltip(rb, name)
        variable.trace_add("write", self._sync)
        self._sync()

    def _sync(self, *_):
        cur = self.var.get()
        for color, holder in self._holders.items():
            try:
                holder.configure(bg=THEME["accent"] if color == cur else THEME["divider"])
            except tk.TclError:
                pass

    def _tooltip(self, widget, text):
        def show(e):
            hide(e)
            top = self.winfo_toplevel()
            self._tip = tk.Label(top, text=text, bg=THEME["surface_hi"],
                                 fg=THEME["text"], font=ui_font(8),
                                 padx=5, pady=2)
            self._tip.place(x=e.x_root - top.winfo_rootx() + 12,
                            y=e.y_root - top.winfo_rooty() + 14)

        def hide(_):
            if self._tip is not None:
                try:
                    self._tip.destroy()
                except tk.TclError:
                    pass
                self._tip = None

        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)
        widget.bind("<Destroy>", hide)


# ---------------------------------------------------------------------------
#  EditDialog
# ---------------------------------------------------------------------------
class EditDialog(tk.Toplevel):

    def __init__(self, master, cfg, on_save):
        super().__init__(master)
        self.cfg = cfg
        self.on_save = on_save
        self._user_edited_label = False
        self._syncing = False
        self.title("Edit Clock")
        self.configure(bg=THEME["bg"])
        self.resizable(False, False)
        self.transient(master)
        apply_window_icon(self)
        self._build()
        self.bind("<Return>", lambda e: self._save())
        self.bind("<Escape>", lambda e: self.destroy())
        self.update_idletasks()
        self._centre_on(master)
        self.grab_set()
        self.e_lbl.focus_set()

    def _centre_on(self, master):
        try:
            mx, my = master.winfo_rootx(), master.winfo_rooty()
            mw, mh = master.winfo_width(), master.winfo_height()
            if mw <= 1:
                raise tk.TclError
        except tk.TclError:
            mx = my = 0
            mw, mh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = mx + (mw - self.winfo_width()) // 2
        y = my + (mh - self.winfo_height()) // 2
        x, y = clamp_to_screen(x, y, self.winfo_width(), self.winfo_height(),
                               self.winfo_screenwidth(), self.winfo_screenheight())
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _row_label(self, text, row, sticky="w"):
        tk.Label(self, text=text, bg=THEME["bg"], fg=THEME["text_dim"],
                 font=ui_font(11)).grid(row=row, column=0, sticky=sticky,
                                        padx=16, pady=8)

    def _build(self):
        P = dict(padx=16, pady=8)

        self._row_label("Label", 0)
        self.e_lbl = tk.Entry(self, width=26, **entry_style())
        self.e_lbl.insert(0, self.cfg.get("label", ""))
        self.e_lbl.grid(row=0, column=1, sticky="ew", **P)
        self.e_lbl.bind("<Key>", lambda e: setattr(self, "_user_edited_label", True))

        self._row_label("Timezone", 1, "nw")
        tzf = tk.Frame(self, bg=THEME["bg"])
        tzf.grid(row=1, column=1, sticky="ew", **P)
        self.sv = tk.StringVar()
        tk.Entry(tzf, textvariable=self.sv, width=32, **entry_style()).pack(fill="x")
        self.sv.trace_add("write", self._on_tz_change)

        wrap = tk.Frame(tzf, bg=THEME["surface"])
        wrap.pack(fill="both", expand=True, pady=(3, 0))
        sb = tk.Scrollbar(wrap, orient="vertical", relief="flat", bd=0, width=10,
                          bg=THEME["surface"], troughcolor=THEME["bg"],
                          activebackground=THEME["surface_hi"])
        sb.pack(side="right", fill="y")
        self.lb = tk.Listbox(wrap, bg=THEME["surface"], fg=THEME["text_dim"],
                             selectbackground=THEME["accent"],
                             selectforeground="#ffffff", font=ui_font(10),
                             relief="flat", height=7, yscrollcommand=sb.set,
                             activestyle="none", bd=0, highlightthickness=0)
        self.lb.pack(fill="both", expand=True)
        sb.config(command=self.lb.yview)
        self._populate(ALL_ZONES)
        self.lb.bind("<<ListboxSelect>>", self._on_lb_select)
        self.sv.set(self.cfg.get("tz", "UTC"))

        self._row_label("Look", 2, "nw")
        pf = tk.Frame(self, bg=THEME["bg"])
        pf.grid(row=2, column=1, sticky="w", **P)
        for i, (name, tc, cc) in enumerate(PRESETS):
            make_button(pf, name, lambda t=tc, c=cc: self._apply_preset(t, c),
                        kind="ghost", font=ui_font(9), padx=8, pady=4
                        ).grid(row=i // 4, column=i % 4, padx=2, pady=2, sticky="ew")

        self._row_label("Text Colour", 3, "nw")
        self.tcv = tk.StringVar(value=self.cfg.get("text_color", "#ffffff"))
        SwatchPicker(self, self.tcv).grid(row=3, column=1, sticky="w", **P)

        self._row_label("Card Colour", 4, "nw")
        self.ccv = tk.StringVar(value=self.cfg.get("card_color", "#1e1e1e"))
        SwatchPicker(self, self.ccv).grid(row=4, column=1, sticky="w", **P)
        make_button(self, "Match card to text colour",
                    lambda: self.ccv.set(PAL_DARK.get(self.tcv.get(), "#1e1e1e")),
                    kind="flat", font=ui_font(9), padx=8, pady=3
                    ).grid(row=5, column=1, sticky="w", padx=16, pady=(0, 6))

        self._row_label("Display", 6, "nw")
        of = tk.Frame(self, bg=THEME["bg"])
        of.grid(row=6, column=1, sticky="w", **P)
        self.v24 = tk.BooleanVar(value=self.cfg.get("hour24", True))
        self.vsec = tk.BooleanVar(value=self.cfg.get("show_seconds", True))
        self.vtop = tk.BooleanVar(value=self.cfg.get("topmost", True))
        for i, (txt, var) in enumerate((("24-hour clock", self.v24),
                                        ("Show seconds", self.vsec),
                                        ("Always on top", self.vtop))):
            tk.Checkbutton(of, text=txt, variable=var, bg=THEME["bg"],
                           fg=THEME["text_dim"], selectcolor=THEME["surface"],
                           activebackground=THEME["bg"],
                           activeforeground=THEME["text"], font=ui_font(10),
                           relief="flat", cursor="hand2", highlightthickness=0,
                           anchor="w").grid(row=i, column=0, sticky="w")

        self._row_label("Opacity", 7, "w")
        self.vop = tk.DoubleVar(value=self.cfg.get("opacity", 0.97))
        tk.Scale(self, variable=self.vop, from_=OPACITY_MIN, to=OPACITY_MAX,
                 resolution=0.01, orient="horizontal", bg=THEME["bg"],
                 fg=THEME["text_dim"], troughcolor=THEME["surface"],
                 activebackground=THEME["accent"], highlightthickness=0,
                 relief="flat", bd=0, sliderrelief="flat", showvalue=True,
                 font=ui_font(8), length=200
                 ).grid(row=7, column=1, sticky="w", padx=16, pady=(0, 4))

        bf = tk.Frame(self, bg=THEME["bg"])
        bf.grid(row=8, column=0, columnspan=2, pady=14)
        make_button(bf, "  Save  ", self._save, kind="primary",
                    font=ui_font(10, "bold"), padx=18, pady=7).pack(side="left", padx=6)
        make_button(bf, "Cancel", self.destroy, kind="flat",
                    font=ui_font(10), padx=18, pady=7).pack(side="left", padx=6)

        self.columnconfigure(1, weight=1)

    def _apply_preset(self, tc, cc):
        self.tcv.set(tc)
        self.ccv.set(cc)

    def _populate(self, zones):
        """One batched insert - this used to add ~600 items one at a time on
        every keystroke."""
        self.lb.delete(0, "end")
        if zones:
            self.lb.insert("end", *zones)

    def _on_tz_change(self, *_):
        if self._syncing:
            return
        q = self.sv.get().lower()
        self._populate([z for z in ALL_ZONES if q in z.lower()])
        self._autofill_label(self.sv.get())

    def _autofill_label(self, tz):
        if self._user_edited_label:
            return
        city = tz.split("/")[-1].replace("_", " ")
        self.e_lbl.delete(0, "end")
        self.e_lbl.insert(0, city)

    def _on_lb_select(self, _):
        sel = self.lb.curselection()
        if not sel:
            return
        tz = self.lb.get(sel[0])
        # Writing the entry used to re-run the filter and rebuild the list out
        # from under the click, losing the selection.
        self._syncing = True
        try:
            self.sv.set(tz)
        finally:
            self._syncing = False
        self._autofill_label(tz)

    def _save(self):
        tz = self.sv.get().strip()
        try:
            zoneinfo.ZoneInfo(tz)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
            tk.messagebox.showerror("Invalid Timezone",
                                    f"'{tz}' is not a known timezone.\n"
                                    "Pick one from the list.", parent=self)
            return
        self.cfg.update({
            "tz": tz,
            "label": self.e_lbl.get().strip() or tz.split("/")[-1].replace("_", " "),
            "text_color": self.tcv.get(),
            "card_color": self.ccv.get(),
            "hour24": bool(self.v24.get()),
            "show_seconds": bool(self.vsec.get()),
            "topmost": bool(self.vtop.get()),
            "opacity": round(float(self.vop.get()), 2),
        })
        self.on_save()
        self.destroy()


# ---------------------------------------------------------------------------
#  App mark
# ---------------------------------------------------------------------------
_ICON_PHOTO = None


def make_app_icon(size=64):
    """The Solari mark: a flip card with a clock face on it."""
    if not HAS_PIL:
        return None
    s = 4
    S_ = size * s
    img = Image.new("RGBA", (S_, S_), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(S_ * 0.22)
    d.rounded_rectangle([0, 0, S_ - 1, S_ - 1], radius=r, fill=(30, 30, 34, 255))
    d.rounded_rectangle([0, 0, S_ - 1, int(S_ * 0.5)], radius=r,
                        fill=(46, 46, 52, 255))
    d.rounded_rectangle([0, 0, S_ - 1, S_ - 1], radius=r,
                        outline=(255, 255, 255, 40), width=max(1, s))
    m = int(S_ * 0.20)
    d.ellipse([m, m, S_ - m, S_ - m], outline=(255, 255, 255, 255), width=int(s * 2.6))
    cx = cy = S_ // 2
    d.line([cx, cy, cx, cy - int(S_ * 0.20)], fill=(255, 255, 255, 255), width=int(s * 2.6))
    d.line([cx, cy, cx + int(S_ * 0.16), cy + int(S_ * 0.11)],
           fill=(255, 255, 255, 255), width=int(s * 2.6))
    return img.resize((size, size), Image.LANCZOS)


def apply_window_icon(win):
    global _ICON_PHOTO
    ico = asset_path("solari.ico")
    if ico and IS_WINDOWS:
        try:
            win.iconbitmap(ico)
            return
        except tk.TclError:
            pass
    if not HAS_PIL:
        return
    try:
        if _ICON_PHOTO is None:
            _ICON_PHOTO = ImageTk.PhotoImage(make_app_icon(64))
        win.iconphoto(False, _ICON_PHOTO)
    except tk.TclError:
        pass


# ---------------------------------------------------------------------------
#  ManagerWindow
#
#  Now lists the clocks it manages: without this there was no way to reach a
#  clock that had drifted off-screen, or to see what you owned.
# ---------------------------------------------------------------------------
class ManagerWindow(tk.Toplevel):

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self._rows = []
        self.title(APP_NAME)
        self.configure(bg=THEME["bg"])
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._hide)
        apply_window_icon(self)
        self._build()

    def _hide(self):
        # Without a tray icon there would be no way back, so quitting is safer
        if HAS_TRAY and self.app.tray_active():
            self.withdraw()
        else:
            self.app.quit_app()

    def show(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _build(self):
        head = tk.Frame(self, bg=THEME["bg"], pady=22, padx=30)
        head.pack(fill="x")
        if HAS_PIL:
            self._mark = ImageTk.PhotoImage(make_app_icon(46))
            tk.Label(head, image=self._mark, bg=THEME["bg"]).pack()
        tk.Label(head, text="Solari", bg=THEME["bg"], fg=THEME["text"],
                 font=ui_font(21, "bold")).pack(pady=(9, 2))
        tk.Label(head, text="Floating flip-clock widgets for your desktop",
                 bg=THEME["bg"], fg=THEME["text_faint"],
                 font=ui_font(10)).pack()

        tk.Frame(self, bg=THEME["divider"], height=1).pack(fill="x")

        cap = tk.Frame(self, bg=THEME["bg"], padx=24, pady=8)
        cap.pack(fill="x")
        tk.Label(cap, text="CLOCKS", bg=THEME["bg"], fg=THEME["text_faint"],
                 font=ui_font(9, "bold")).pack(side="left")
        self._count = tk.Label(cap, text="", bg=THEME["bg"],
                               fg=THEME["text_faint"], font=ui_font(9))
        self._count.pack(side="right")

        self.list = ScrollFrame(self, height=176)
        self.list.pack(fill="x", padx=24)

        body = tk.Frame(self, bg=THEME["bg"], padx=24, pady=12)
        body.pack(fill="x")
        make_button(body, "＋   Add Clock", self.app.add_clock, kind="primary",
                    font=ui_font(12, "bold"), pady=11).pack(fill="x", pady=(0, 8))
        make_button(body, "Bring all on screen", self.app.recall_all,
                    kind="ghost", font=ui_font(10), pady=7).pack(fill="x")

        st = tk.Frame(body, bg=THEME["surface"])
        st.pack(fill="x", pady=(10, 8))
        tk.Label(st, text="  Launch at Windows startup", bg=THEME["surface"],
                 fg=THEME["text_dim"] if HAS_WINREG else THEME["text_faint"],
                 font=ui_font(10)).pack(side="left", pady=8)
        cb = tk.Checkbutton(st, variable=self.app.startup_var,
                            command=self.app.toggle_startup,
                            bg=THEME["surface"], selectcolor=THEME["accent"],
                            activebackground=THEME["surface"], relief="flat",
                            cursor="hand2", highlightthickness=0)
        cb.pack(side="right", padx=10)
        if not HAS_WINREG:
            cb.configure(state="disabled", cursor="")

        make_button(body, "Quit", self.app.quit_app, kind="danger",
                    font=ui_font(10), pady=8).pack(fill="x")

        tk.Frame(self, bg=THEME["divider"], height=1).pack(fill="x")
        foot = tk.Frame(self, bg=THEME["bg"], pady=10)
        foot.pack(fill="x")
        tk.Label(foot, text=pretty_path(config_dir()), bg=THEME["bg"],
                 fg=THEME["text_faint"],
                 font=ui_font(8)).pack()
        tk.Label(foot, text="by  Cervezagua", bg=THEME["bg"],
                 fg=THEME["text_faint"], font=ui_font(9, "italic")).pack(pady=(4, 0))

        self.minsize(392, 0)
        self.update_idletasks()

    # -- clock list --------------------------------------------------------
    def refresh_list(self):
        for child in self.list.body.winfo_children():
            child.destroy()
        self._rows = []
        for win in self.app.windows:
            self._rows.append(self._make_row(win))
        n = len(self.app.windows)
        self._count.configure(text=f"{n} clock" + ("" if n == 1 else "s"))
        self.on_second(time.time())

    def _make_row(self, win):
        tc = win.cfg.get("text_color", "#ffffff")
        row = tk.Frame(self.list.body, bg=THEME["surface"])
        row.pack(fill="x", pady=2)

        tk.Label(row, text="●", bg=THEME["surface"], fg=tc,
                 font=ui_font(9)).pack(side="left", padx=(10, 4))
        tk.Label(row, text=win.cfg.get("label", ""), bg=THEME["surface"],
                 fg=THEME["text"], font=ui_font(10, "bold"), anchor="w"
                 ).pack(side="left")

        make_button(row, "✕", lambda w=win: self.app.remove_clock(w),
                    kind="danger", font=ui_font(10), padx=8, pady=4
                    ).pack(side="right", padx=(2, 8))
        make_button(row, "Find", lambda w=win: self.app.recall(w),
                    kind="ghost", font=ui_font(9), padx=8, pady=4
                    ).pack(side="right", padx=2)
        make_button(row, "✎", lambda w=win: self.app.edit_clock(w),
                    kind="ghost", font=ui_font(10), padx=8, pady=4
                    ).pack(side="right", padx=2)

        clock = tk.Label(row, text="", bg=THEME["surface"], fg=THEME["text_dim"],
                         font=ui_font(10))
        clock.pack(side="right", padx=8)
        return (win, clock)

    def on_second(self, _ts):
        """Live preview in each row - the Manager rides the same shared tick."""
        for win, lbl in self._rows:
            try:
                now = datetime.datetime.now(win.zone())
                fmt = "%H:%M:%S" if win.cfg.get("hour24", True) else "%I:%M:%S %p"
                lbl.configure(text=now.strftime(fmt))
            except tk.TclError:
                pass


# ---------------------------------------------------------------------------
#  App
# ---------------------------------------------------------------------------
class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.withdraw()
        self.title(APP_NAME)
        resolve_fonts(self)
        self._apply_tk_scaling()
        apply_window_icon(self)

        self.ticker = Ticker(self)
        self.configs = load_config()
        self.windows = []
        self._tray = None
        self.startup_var = tk.BooleanVar(value=get_startup())

        self.mgr = ManagerWindow(self, self)
        for cfg in self.configs:
            self._spawn(cfg)
        self.ticker.subscribe(self.mgr)
        self.ticker.start()
        self.mgr.refresh_list()

        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        if HAS_TRAY:
            self._start_tray()

    def _apply_tk_scaling(self):
        """Let point-sized UI text follow the display DPI.

        Clock-face text is specified in pixels (negative Tk sizes) so it stays
        locked to the pixel-sized cards and is unaffected by this.
        """
        try:
            dpi = self.winfo_fpixels("1i")
            if dpi and dpi > 0:
                self.tk.call("tk", "scaling", dpi / 72.0)
        except tk.TclError:
            pass

    # -- clocks ------------------------------------------------------------
    def _spawn(self, cfg):
        win = ClockWindow(self, cfg, self.ticker, on_close=self.remove_clock,
                          on_edit=self.edit_clock, peers=lambda: self.windows)
        self.windows.append(win)
        return win

    def add_clock(self):
        n = len(self.configs)
        tc = COLOR_CYCLE[n % len(COLOR_CYCLE)]
        cfg = dict(DEFAULT_CLOCK, tz="UTC", label="UTC",
                   x=150 + n * 40, y=150 + n * 30,
                   text_color=tc, card_color=PAL_DARK.get(tc, "#1e1e1e"))
        self.configs.append(cfg)
        win = self._spawn(cfg)
        self.mgr.refresh_list()
        self.save()
        self.edit_clock(win)

    def remove_clock(self, win):
        if not tk.messagebox.askyesno(
                "Remove clock",
                f"Remove “{win.cfg.get('label', '')}”?",
                parent=self.mgr if self.mgr.winfo_ismapped() else None):
            return
        # Match on identity: list.remove() compares by value, so two clocks with
        # identical settings could drop the wrong entry.
        self.configs = [c for c in self.configs if c is not win.cfg]
        if win in self.windows:
            self.windows.remove(win)
        win.destroy()
        self.mgr.refresh_list()
        self.save()

    def edit_clock(self, win):
        def saved():
            win.refresh_style()
            self.mgr.refresh_list()
            self.save()
        EditDialog(self.mgr, win.cfg, saved)

    def recall(self, win):
        win.recall()
        self.save()

    def recall_all(self):
        for i, win in enumerate(self.windows):
            win.recall(40 + i * 30, 40 + i * 26)
        self.save()

    def save(self):
        save_config(self.configs)

    # -- startup -----------------------------------------------------------
    def toggle_startup(self):
        if not set_startup(self.startup_var.get()):
            self.startup_var.set(not self.startup_var.get())
            tk.messagebox.showwarning(
                "Startup",
                "Could not update the Windows startup entry."
                if HAS_WINREG else
                "Launch at startup is only available on Windows.",
                parent=self.mgr)

    # -- tray --------------------------------------------------------------
    def tray_active(self):
        return self._tray is not None

    def _start_tray(self):
        import threading
        import traceback

        def run():
            try:
                menu = pystray.Menu(
                    pystray.MenuItem("Show Manager",
                                     lambda: self.after(0, self.mgr.show), default=True),
                    pystray.MenuItem("Add Clock", lambda: self.after(0, self.add_clock)),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Quit", lambda: self.after(0, self.quit_app)),
                )
                icon = pystray.Icon(APP_NAME, make_app_icon(64), APP_NAME, menu)
                self._tray = icon
                icon.run(setup=lambda ic: setattr(ic, "visible", True))
            except Exception:
                with open(os.path.join(config_dir(), "tray_error.log"),
                          "w", encoding="utf-8") as f:
                    traceback.print_exc(file=f)
                self._tray = None
                self.after(0, self.mgr.deiconify)   # don't strand the user

        threading.Thread(target=run, daemon=True).start()

    # -- shutdown ----------------------------------------------------------
    def quit_app(self):
        self.save()
        self.ticker.stop()
        # Stop the tray first: its thread must not touch Tk after destroy()
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
            self._tray = None
        for win in list(self.windows):
            try:
                win.destroy()
            except tk.TclError:
                pass
        self.windows.clear()
        try:
            self.destroy()
        except tk.TclError:
            pass


def main():
    enable_dpi_awareness()
    App().mainloop()


if __name__ == "__main__":
    main()
