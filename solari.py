"""
Solari - split-flap desktop clocks.

Each time zone lives in its own frameless, always-on-top window: a machined
housing with a recessed well and a row of flip cards that roll over to the
next digit.  A manager window lists every clock with a live preview.

Rendering
---------
Tk's canvas has no antialiasing, no gradients and no alpha, so nothing
dimensional can be drawn with it directly.  Every surface - housings, cards,
digits, chrome text, icons - is rendered with Pillow, cached, and placed on a
canvas as an image.  The canvas is used only as a compositor: animation moves
existing items and never creates or deletes them per frame.

Without Pillow the app still runs, with flat cards and plain canvas text.

Units
-----
Layout is written in device-independent pixels ("dp") and multiplied by the
display's scale factor, so a clock is the same physical size on a 100 % and a
200 % display.  Clock faces additionally carry their own user scale.
"""

import datetime
import functools
import json
import math
import os
import queue
import sys
import tempfile
import threading
import time
import traceback
import tkinter as tk
import tkinter.font
import zoneinfo

try:
    import winreg
    HAS_WINREG = True
except ImportError:
    HAS_WINREG = False

# Pillow and pystray are independent optional dependencies.
try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import pystray
    HAS_TRAY = HAS_PIL
except ImportError:
    HAS_TRAY = False

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
APP_NAME = "Solari"
STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
CONFIG_VERSION = 3


# ---------------------------------------------------------------------------
#  Display scale
#
#  DPI awareness has to be declared before any Tk window exists, or Windows
#  bitmap-stretches the whole app.  Once it is declared, Tk sees real device
#  pixels, so every pixel dimension has to be scaled by UI_SCALE by hand -
#  otherwise a 200 % display gets crisp but half-size windows.
# ---------------------------------------------------------------------------
UI_SCALE = 1.0


def enable_dpi_awareness():
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)     # per-monitor
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def init_ui_scale(root):
    """Measure the display once and align Tk's point size with it."""
    global UI_SCALE
    try:
        dpi = float(root.winfo_fpixels("1i"))
    except tk.TclError:
        dpi = 96.0
    if not dpi or dpi <= 0:
        dpi = 96.0
    # macOS reports 72 and scales internally; never shrink below 1.
    UI_SCALE = max(1.0, min(4.0, dpi / 96.0))
    try:
        root.tk.call("tk", "scaling", dpi / 72.0)
    except tk.TclError:
        pass
    return UI_SCALE


def dp(v):
    """Design pixels to device pixels."""
    return int(round(v * UI_SCALE))


# ---------------------------------------------------------------------------
#  Design tokens
# ---------------------------------------------------------------------------
THEME = {
    # application chrome
    "bg":          "#0e0f12",
    "panel":       "#141519",
    "surface":     "#1b1c21",
    "surface_hi":  "#24262c",
    "surface_lo":  "#0b0c0e",
    "field":       "#101114",
    "hairline":    "#25272d",
    "text":        "#eceef1",
    "text_dim":    "#a3a7af",
    "text_faint":  "#6b6f78",
    "accent":      "#f2a93b",
    "accent_hi":   "#ffbe5c",
    "accent_ink":  "#1b1205",
    "danger":      "#ef565b",
    "danger_hi":   "#ff6e73",
    "select":      "#2a2418",
    # clock
    "well":        "#08080a",
    "colon":       "#a2a6ae",
    "colon_dim":   "#4c4f57",
}

# Housing finishes. `ink`/`sub` are the chrome text colours on that housing;
# a clock's own colour appears only in its digits, dot and day badge.
FINISHES = {
    "graphite": {
        "name": "Graphite", "tone": "#17181c", "brushed": False,
        "ink": "#eceef1", "sub": "#9599a1", "chip": (255, 255, 255, 20),
        "icon": "#9ea2aa", "icon_hi": "#ffffff", "rim": 58, "lip": 20,
    },
    "aluminium": {
        "name": "Aluminium", "tone": "#c7cacf", "brushed": True,
        "ink": "#1c1e22", "sub": "#4f535b", "chip": (0, 0, 0, 24),
        "icon": "#4a4e55", "icon_hi": "#000000", "rim": 120, "lip": 70,
    },
}

TEXT_COLORS = [
    ("#f4f4f2", "Ivory"),
    ("#ffd84a", "Signal"),
    ("#ffb547", "Amber"),
    ("#ff6b5a", "Coral"),
    ("#5ce08a", "Mint"),
    ("#39ff6e", "Phosphor"),
    ("#7fd4ff", "Sky"),
    ("#4c8dff", "Azure"),
    ("#b388ff", "Lavender"),
    ("#c9ccd1", "Silver"),
    ("#1b1b1c", "Ink"),
]

CARD_COLORS = [
    ("#1e1f22", "Graphite"),
    ("#111215", "Onyx"),
    ("#2a2d33", "Slate"),
    ("#0d1626", "Navy"),
    ("#1d0f0d", "Oxblood"),
    ("#0d1a12", "Forest"),
    ("#1a1406", "Bronze"),
    ("#160f24", "Plum"),
    ("#f3f2ee", "Paper"),
    ("#e8e1d0", "Cream"),
]

# Name, text, card, finish
PRESETS = [
    ("Classic",   "#f4f4f2", "#1e1f22", "graphite"),
    ("Departure", "#ffd84a", "#111215", "graphite"),
    ("Midnight",  "#4c8dff", "#0d1626", "graphite"),
    ("Ember",     "#ff6b5a", "#1d0f0d", "graphite"),
    ("Phosphor",  "#39ff6e", "#0d1a12", "graphite"),
    ("Brass",     "#ffb547", "#1a1406", "graphite"),
    ("Paper",     "#1b1b1c", "#f3f2ee", "aluminium"),
    ("Studio",    "#f4f4f2", "#2a2d33", "aluminium"),
]

# Card tint suggested for a text colour, including the pre-redesign palette so
# clocks saved by older versions still get a sensible match.
PAL_DARK = {
    "#f4f4f2": "#1e1f22", "#ffd84a": "#111215", "#ffb547": "#1a1406",
    "#ff6b5a": "#1d0f0d", "#5ce08a": "#0d1a12", "#39ff6e": "#0d1a12",
    "#7fd4ff": "#0d1626", "#4c8dff": "#0d1626", "#b388ff": "#160f24",
    "#c9ccd1": "#111215", "#1b1b1c": "#f3f2ee",
    "#ffffff": "#1e1e1e", "#1e1e1e": "#111111", "#cc0000": "#1a0000",
    "#00aa00": "#001a00", "#00ff41": "#001a08", "#2277ff": "#001033",
    "#ff7700": "#1a0a00", "#8833cc": "#0d0020", "#ffd700": "#1a1400",
    "#aaaaaa": "#111111", "#000000": "#f3f2ee",
}

NEON = {"#00ff41", "#39ff6e"}
SECONDS_MODES = ("off", "small", "large")
FACES = {"modern": "Modern", "signage": "Signage"}

COLOR_CYCLE = ["#f4f4f2", "#4c8dff", "#ff6b5a", "#5ce08a", "#ffd84a", "#b388ff"]


# ---------------------------------------------------------------------------
#  Fonts
#
#  Tk fonts carry the app chrome's live text; TrueType files carry every
#  rendered sprite (digits, labels, pills).  Both resolve against what is
#  actually installed rather than assuming Windows.
# ---------------------------------------------------------------------------
UI_FAMILY = "Segoe UI"
_UI_PREFS = ("Segoe UI Variable Text", "Segoe UI", "Inter", "SF Pro Text",
             "Helvetica Neue", "Cantarell", "DejaVu Sans", "Liberation Sans",
             "Arial")


def resolve_fonts(root):
    global UI_FAMILY
    try:
        available = {f.lower(): f for f in tkinter.font.families(root)}
    except tk.TclError:
        return UI_FAMILY
    for fam in _UI_PREFS:
        if fam.lower() in available:
            UI_FAMILY = available[fam.lower()]
            break
    return UI_FAMILY


def ui_font(size, weight="normal"):
    """Point-sized, so it follows the display DPI."""
    return (UI_FAMILY, size, weight)


def ui_font_px(size, weight="normal"):
    """Pixel-sized (negative Tk size) for text locked to pixel geometry."""
    return (UI_FAMILY, -max(1, int(abs(size))), weight)


# TrueType chains: (filename, variation words).  Variation words select a
# named instance of a variable font, e.g. Bahnschrift "SemiBold".
_TTF_CHAINS = {
    "modern": [
        ("SegUIVar.ttf", ("display", "semibold")),
        ("InterDisplay-SemiBold.otf", None),
        ("seguisb.ttf", None),
        ("Inter-SemiBold.otf", None),
        ("SFNS.ttf", ("semibold",)),
        ("HelveticaNeue.ttc", None),
        ("DejaVuSans-Bold.ttf", None),
        ("LiberationSans-Bold.ttf", None),
        ("arialbd.ttf", None),
    ],
    "signage": [
        ("bahnschrift.ttf", ("semibold",)),
        ("D-DIN-Bold.otf", None),
        ("DINPro-Medium.otf", None),
        ("Barlow-SemiBold.ttf", None),
        ("InterDisplay-SemiBold.otf", None),
        ("seguisb.ttf", None),
        ("DejaVuSansCondensed-Bold.ttf", None),
        ("DejaVuSans-Bold.ttf", None),
        ("arialbd.ttf", None),
    ],
    "ui_bold": [
        ("SegUIVar.ttf", ("text", "semibold")),
        ("seguisb.ttf", None),
        ("Inter-SemiBold.otf", None),
        ("SFNS.ttf", ("semibold",)),
        ("DejaVuSans-Bold.ttf", None),
        ("LiberationSans-Bold.ttf", None),
        ("arialbd.ttf", None),
    ],
    "ui": [
        ("SegUIVar.ttf", ("text", "regular")),
        ("segoeui.ttf", None),
        ("Inter-Medium.otf", None),
        ("SFNS.ttf", None),
        ("DejaVuSans.ttf", None),
        ("LiberationSans-Regular.ttf", None),
        ("arial.ttf", None),
    ],
}


def _font_dirs():
    dirs = []
    if IS_WINDOWS:
        dirs.append(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    elif IS_MAC:
        dirs += ["/System/Library/Fonts", "/Library/Fonts",
                 os.path.expanduser("~/Library/Fonts")]
    else:
        dirs += ["/usr/share/fonts", "/usr/local/share/fonts",
                 os.path.expanduser("~/.local/share/fonts"),
                 os.path.expanduser("~/.fonts")]
    return [d for d in dirs if os.path.isdir(d)]


@functools.lru_cache(maxsize=1)
def _font_index():
    """filename (lower-case) -> full path, across every font directory."""
    index = {}
    for root_dir in _font_dirs():
        for base, _dirs, files in os.walk(root_dir):
            for name in files:
                if name.lower().endswith((".ttf", ".otf", ".ttc")):
                    index.setdefault(name.lower(), os.path.join(base, name))
    return index


def _apply_variation(font, words):
    """Select a named instance whose name contains every word in `words`."""
    try:
        names = font.get_variation_names()
    except (OSError, AttributeError):
        return False
    for raw in names:
        name = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
        if all(w in name.lower() for w in words):
            try:
                font.set_variation_by_name(raw)
                return True
            except (OSError, ValueError, AttributeError):
                return False
    return False


@functools.lru_cache(maxsize=8)
def resolve_ttf(role):
    """(path, variation words) for the first installed font in a role's chain."""
    index = _font_index()
    for fname, words in _TTF_CHAINS.get(role, ()):
        path = index.get(fname.lower())
        if path:
            try:
                f = ImageFont.truetype(path, 24)
            except (OSError, ValueError):
                continue
            if words and not _apply_variation(f, words):
                # A variable font without the instance we want: still usable at
                # its default weight, but prefer the next static font if any.
                continue
            return path, words
    return None, None


@functools.lru_cache(maxsize=64)
def ttf(role, px):
    """A sized FreeType font for a role, falling back to Pillow's own face."""
    px = max(4, int(px))
    path, words = resolve_ttf(role)
    if path:
        try:
            f = ImageFont.truetype(path, px)
            if words:
                _apply_variation(f, words)
            return f
        except (OSError, ValueError):
            pass
    try:
        return ImageFont.load_default(size=px)         # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def font_report():
    """Which file each role resolved to - printed by --fonts and in CI."""
    out = {}
    for role in _TTF_CHAINS:
        path, words = resolve_ttf(role) if HAS_PIL else (None, None)
        out[role] = (os.path.basename(path) if path else "Pillow default") + \
                    (f" [{' '.join(words)}]" if words else "")
    return out


# ---------------------------------------------------------------------------
#  Colour maths (memoised - these sit in hot paths)
# ---------------------------------------------------------------------------
QUANT = 32.0


def quantise(t):
    """Snap a 0..1 factor to 1/32 steps so colour caches actually hit."""
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


def ease_out_cubic(t):
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def is_neon(text_color):
    return text_color.lower() in NEON


def rgba(hex_c, a=255):
    return to_rgb(hex_c) + (a,)


@functools.lru_cache(maxsize=256)
def luminance(hex_c):
    """WCAG relative luminance."""
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = to_rgb(hex_c)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# ---------------------------------------------------------------------------
#  Config
#
#  %APPDATA%\Solari on Windows, so existing installs carry over.
# ---------------------------------------------------------------------------
def config_dir():
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif IS_MAC:
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def config_path():
    return os.path.join(config_dir(), "solari_config.json")


def pretty_path(p, limit=44):
    """Shorten a path for display without losing its tail."""
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


def asset_path(name):
    """A bundled file, both in-tree and inside a PyInstaller bundle."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(base, name)
    return p if os.path.exists(p) else None


def startup_command():
    """What the Run key should launch.

    A frozen build launches itself.  From source it must launch the
    interpreter: registering the .py path alone hands it to whatever .py is
    associated with - often an editor, which then opens at every login.
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --background'
    exe = sys.executable
    windowed = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if IS_WINDOWS and os.path.exists(windowed):
        exe = windowed
    return f'"{exe}" "{os.path.abspath(__file__)}" --background'


def read_startup():
    """The registered launch command, or None."""
    if not HAS_WINREG:
        return None
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0, winreg.KEY_READ)
        try:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
        finally:
            winreg.CloseKey(key)
        return value
    except OSError:
        return None


def get_startup():
    return read_startup() is not None


def set_startup(enable):
    if not HAS_WINREG:
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            if enable:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, startup_command())
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


def heal_startup():
    """Re-point an existing startup entry if the app has moved since."""
    current = read_startup()
    if current is not None and current != startup_command():
        set_startup(True)


DEFAULT_CLOCK = {
    "tz": "UTC",
    "label": "UTC",
    "x": 100,
    "y": 100,
    "text_color": "#f4f4f2",
    "card_color": "#1e1f22",
    "scale": 1.0,
    "hour24": True,
    "seconds": "small",
    "opacity": 0.98,
    "topmost": True,
    "finish": "graphite",
    "face": "modern",
}

DEFAULT_CONFIG = [
    dict(DEFAULT_CLOCK, tz="America/New_York", label="New York",
         x=60, y=80, text_color="#f4f4f2", card_color="#1e1f22"),
    dict(DEFAULT_CLOCK, tz="Europe/London", label="London",
         x=60, y=330, text_color="#ffd84a", card_color="#111215"),
    dict(DEFAULT_CLOCK, tz="Asia/Tokyo", label="Tokyo",
         x=60, y=580, text_color="#ff6b5a", card_color="#1d0f0d"),
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


def city_of(tz):
    return tz.split("/")[-1].replace("_", " ") if tz else ""


@functools.lru_cache(maxsize=1024)
def zone_or_none(tz):
    try:
        return zoneinfo.ZoneInfo(tz)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError, TypeError):
        return None


def migrate_clock(raw):
    """Normalise one clock from any past schema. None only if it has no zone.

    An unknown zone is kept, not dropped.  Dropping it meant that running
    without a tz database (Windows has none built in) silently discarded every
    clock - and the next save then overwrote the user's config with defaults.
    """
    if not isinstance(raw, dict):
        return None
    tz = raw.get("tz")
    if not isinstance(tz, str) or not tz.strip():
        return None
    c = dict(DEFAULT_CLOCK)
    c["tz"] = tz.strip()

    label = raw.get("label")
    c["label"] = label.strip() if isinstance(label, str) and label.strip() else city_of(c["tz"])

    text_color = _valid_hex(raw.get("text_color", raw.get("color")), "#f4f4f2")
    if text_color in ("#bbbbbb", "#ddcc00"):        # v0 greys
        text_color = "#f4f4f2"
    c["text_color"] = text_color
    c["card_color"] = _valid_hex(raw.get("card_color"), PAL_DARK.get(text_color, "#1e1f22"))

    c["x"] = int(_num(raw.get("x"), -60000, 60000, 100))
    c["y"] = int(_num(raw.get("y"), -60000, 60000, 100))
    c["scale"] = round(_num(raw.get("scale"), SCALE_MIN, SCALE_MAX, 1.0), 2)
    c["opacity"] = round(_num(raw.get("opacity"), OPACITY_MIN, OPACITY_MAX, 0.98), 2)
    c["hour24"] = bool(raw.get("hour24", True))
    c["topmost"] = bool(raw.get("topmost", True))

    seconds = raw.get("seconds")
    if seconds not in SECONDS_MODES:
        # v2 stored a boolean
        seconds = "small" if raw.get("show_seconds", True) else "off"
    c["seconds"] = seconds
    c["finish"] = raw.get("finish") if raw.get("finish") in FINISHES else "graphite"
    c["face"] = raw.get("face") if raw.get("face") in FACES else "modern"
    return c


def _backup_unreadable(path):
    """Keep a copy of a config we could not parse before anything replaces it."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = f"{path}.unreadable-{stamp}"
    try:
        os.replace(path, dest)
        return dest
    except OSError:
        return None


def load_config(path=None):
    """Read the config.

    - No file: the starter clocks.
    - A file that cannot be parsed: moved aside to *.unreadable-<time>, then the
      starter clocks - never silently overwritten.
    - A readable file is authoritative, including an empty list: a user who
      removed every clock should not get the starters back on next launch.
    """
    path = path or config_path()
    if not os.path.exists(path):
        return [dict(c) for c in DEFAULT_CONFIG]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError):
        _backup_unreadable(path)
        return [dict(c) for c in DEFAULT_CONFIG]

    if isinstance(data, dict):           # v2+ wrapper
        data = data.get("clocks")
    if not isinstance(data, list):
        _backup_unreadable(path)
        return [dict(c) for c in DEFAULT_CONFIG]
    return [m for m in (migrate_clock(c) for c in data) if m]


def save_config(configs, path=None, retries=6):
    """Atomic write, retried.

    os.replace raises PermissionError on Windows while an indexer, antivirus
    scanner or sync client briefly holds the target open, which is routine -
    one attempt is not enough.
    """
    path = path or config_path()
    payload = {"version": CONFIG_VERSION, "clocks": configs}
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        return False
    delay = 0.03
    for attempt in range(retries):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            if attempt == retries - 1:
                break
            time.sleep(delay)
            delay *= 2
        except OSError:
            break
    try:
        os.unlink(tmp)
    except OSError:
        pass
    return False


# ---------------------------------------------------------------------------
#  Single instance
#
#  Launch-at-startup plus a manual launch used to give two copies of every
#  clock, both writing the same config.  A second launch now hands over to the
#  running one (which brings its manager forward) and exits.
# ---------------------------------------------------------------------------
class InstanceLock:
    def __init__(self, directory):
        self.path = os.path.join(directory, "solari.lock")
        self.request = os.path.join(directory, "show.request")
        self._fh = None

    def acquire(self):
        try:
            fh = open(self.path, "a+")
        except OSError:
            return True              # cannot lock: run rather than refuse
        try:
            if IS_WINDOWS:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self):
        if self._fh is None:
            return
        try:
            if IS_WINDOWS:
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None

    def request_show(self):
        try:
            with open(self.request, "w"):
                pass
        except OSError:
            pass

    def take_show_request(self):
        if os.path.exists(self.request):
            try:
                os.unlink(self.request)
            except OSError:
                pass
            return True
        return False


# ---------------------------------------------------------------------------
#  Monitors
#
#  Tk only knows the primary screen.  Clamping to it moved every clock that
#  lived on a second monitor back onto the first at launch.
# ---------------------------------------------------------------------------
def get_monitors(root=None):
    """Work areas (l, t, r, b) of every monitor, primary first."""
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                            ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

            user32 = ctypes.windll.user32
            found = []
            proc_t = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HANDLE, wintypes.HDC,
                                        ctypes.POINTER(RECT), wintypes.LPARAM)

            def cb(hmon, _hdc, _rect, _data):
                mi = MONITORINFO()
                mi.cbSize = ctypes.sizeof(MONITORINFO)
                if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    r = mi.rcWork
                    found.append(((r.left, r.top, r.right, r.bottom), mi.dwFlags & 1))
                return 1

            callback = proc_t(cb)          # must outlive the call
            user32.EnumDisplayMonitors(None, None, callback, 0)
            if found:
                found.sort(key=lambda m: not m[1])     # primary first
                return [m[0] for m in found]
        except Exception:
            pass
    if root is not None:
        try:
            return [(0, 0, root.winfo_screenwidth(), root.winfo_screenheight())]
        except tk.TclError:
            pass
    return [(0, 0, 1920, 1080)]


def ensure_visible(x, y, w, h, monitors, grab=48):
    """Keep a window where it is if enough of its top strip is on a monitor;
    otherwise move it fully onto the nearest one."""
    strip = min(h, grab)
    for l, t, r, b in monitors:
        ox = min(x + w, r) - max(x, l)
        oy = min(y + strip, b) - max(y, t)
        if ox >= min(w, grab) and oy >= min(strip, grab // 2) and y >= t - strip // 2:
            return int(x), int(y)
    cx, cy = x + w / 2, y + h / 2

    def dist(m):
        l, t, r, b = m
        dx = max(l - cx, 0, cx - r)
        dy = max(t - cy, 0, cy - b)
        return dx * dx + dy * dy

    l, t, r, b = min(monitors, key=dist)
    nx = min(max(x, l), max(l, r - w))
    ny = min(max(y, t), max(t, b - h))
    return int(nx), int(ny)


def monitor_for(x, y, w, h, monitors):
    """The monitor holding the window's centre (or the nearest)."""
    cx, cy = x + w / 2, y + h / 2
    for m in monitors:
        if m[0] <= cx < m[2] and m[1] <= cy < m[3]:
            return m
    return min(monitors, key=lambda m: (max(m[0] - cx, 0, cx - m[2]) ** 2 +
                                        max(m[1] - cy, 0, cy - m[3]) ** 2))


def snap(x, y, w, h, monitors, peers, threshold):
    """Pull to the current monitor's work-area edges and to sibling clocks."""
    l, t, r, b = monitor_for(x, y, w, h, monitors)
    xs = [l, r - w]
    ys = [t, b - h]
    for px, py, pw, ph in peers:
        xs += [px, px + pw - w, px + pw, px - w]
        ys += [py, py + ph - h, py + ph, py - h]
    for c in xs:
        if abs(x - c) <= threshold:
            x = c
            break
    for c in ys:
        if abs(y - c) <= threshold:
            y = c
            break
    return int(x), int(y)


# Kept for callers that only know a single screen.
def clamp_to_screen(x, y, w, h, sw, sh, margin=24):
    return ensure_visible(x, y, w, h, [(0, 0, sw, sh)], grab=margin)


# ---------------------------------------------------------------------------
#  Rendering primitives
#
#  Every renderer takes `ss`, its supersampling factor.  Final renders use 3
#  for smooth curves; live resizing passes 1 to get a draft at a fraction of
#  the cost, then re-renders at full quality on release.
#
#  Highlights and shadows are always drawn on their own transparent layer and
#  alpha-composited.  ImageDraw *replaces* pixels, so drawing a translucent
#  line straight onto an RGBA image punches a hole in it instead of tinting it.
# ---------------------------------------------------------------------------
CARD_SS = 3


def _column(h, stops):
    """Per-row colours for a multi-stop vertical gradient."""
    rgb = [to_rgb(c) for _, c in stops]
    last = len(stops) - 1
    out = []
    for y in range(h):
        t = y / max(1, h - 1)
        for i in range(last):
            p0, p1 = stops[i][0], stops[i + 1][0]
            if t <= p1 or i == last - 1:
                f = max(0.0, min(1.0, (t - p0) / ((p1 - p0) or 1.0)))
                a, b = rgb[i], rgb[i + 1]
                out.append((int(a[0] + (b[0] - a[0]) * f),
                            int(a[1] + (b[1] - a[1]) * f),
                            int(a[2] + (b[2] - a[2]) * f)))
                break
    return out


def _vgrad(w, h, stops):
    """Vertical gradient: one colour per row, stretched sideways in C.

    Drawing a line per row cost a Python call per pixel row; building a
    one-pixel column and stretching it is 3-5x faster at every size used.
    """
    col = Image.new("RGB", (1, max(1, h)))
    col.putdata(_column(max(1, h), stops))
    return col.resize((max(1, w), max(1, h)), Image.NEAREST)


def _vramp(w, h, a0, a1, p0=0.0, p1=1.0):
    """L-mode vertical ramp, a0 above p0 fading to a1 below p1."""
    h = max(1, h)
    vals = []
    span = (p1 - p0) or 1.0
    for y in range(h):
        t = y / max(1, h - 1)
        if t <= p0:
            v = a0
        elif t >= p1:
            v = a1
        else:
            v = a0 + (a1 - a0) * ((t - p0) / span)
        vals.append(int(v))
    col = Image.new("L", (1, h))
    col.putdata(vals)
    return col.resize((max(1, w), h), Image.NEAREST)


def _masked(layer, ramp):
    """Multiply a layer's alpha by a ramp."""
    a = layer.split()[3]
    layer.putalpha(Image.composite(ramp, Image.new("L", layer.size, 0), a))
    return layer


def _body(W, H, fill, inset, strength):
    if inset:
        return _vgrad(W, H, [
            (0.00, darken(fill, 1.0 - 0.34 * strength)),
            (0.55, fill),
            (1.00, lighten(fill, 0.10 * strength)),
        ])
    return _vgrad(W, H, [
        (0.00, lighten(fill, 0.17 * strength)),
        (0.50, fill),
        (0.88, darken(fill, 1.0 - 0.38 * strength)),
        (1.00, darken(fill, 1.0 - 0.20 * strength)),     # bounce light at the base
    ])


@functools.lru_cache(maxsize=160)
def render_surface(w, h, radius, fill, behind, glow=None, inset=False,
                   strength=1.0, ss=CARD_SS, ring=None):
    """A shaded surface as an opaque RGB image, corners pre-blended on `behind`.

    The one shading model shared by flip cards, manager rows and buttons, so
    they match by construction.  Lit from above: body gradient with bounce
    light, a specular edge fading down the shoulders, a contact shadow rising
    from the base and a faint inner bevel.  `inset` inverts the light for a
    recess or a pressed button; `glow` adds a coloured inner rim; `ring` draws
    a crisp keyboard-focus outline.
    """
    w, h = max(1, int(w)), max(1, int(h))
    ss = max(1, int(ss))
    W, H, R = w * ss, h * ss, max(0, int(radius * ss))
    R = min(R, W // 2, H // 2)
    body = _body(W, H, fill, inset, strength)

    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, H - 1], radius=R, fill=255)
    surf = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    surf.paste(body, (0, 0), mask)

    lw = max(1, int(ss * 1.2))
    edge = [lw // 2, lw // 2, W - 1 - lw // 2, H - 1 - lw // 2]

    def edge_layer(colour, a0, a1, p0, p1):
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle(edge, radius=R, outline=colour, width=lw)
        return _masked(layer, _vramp(W, H, a0, a1, p0, p1))

    hi, lo = int(132 * strength), int(150 * strength)
    if inset:
        surf = Image.alpha_composite(surf, edge_layer((0, 0, 0, 255), lo, 0, 0.0, 0.5))
        surf = Image.alpha_composite(surf, edge_layer((255, 255, 255, 255), 0, hi, 0.6, 1.0))
    else:
        surf = Image.alpha_composite(surf, edge_layer((255, 255, 255, 255), hi, 0, 0.0, 0.42))
        surf = Image.alpha_composite(surf, edge_layer((0, 0, 0, 255), 0, lo, 0.55, 1.0))
        bevel = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        pad = lw * 2
        if W > pad * 2 + 2 and H > pad * 2 + 2:
            ImageDraw.Draw(bevel).rounded_rectangle(
                [pad, pad, W - 1 - pad, H - 1 - pad], radius=max(0, R - pad),
                outline=(255, 255, 255, 255), width=max(1, lw // 2))
            surf = Image.alpha_composite(
                surf, _masked(bevel, _vramp(W, H, int(46 * strength), 0, 0.0, 0.30)))

    if glow:
        halo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(halo).rounded_rectangle(
            [lw, lw, W - 1 - lw, H - 1 - lw], radius=max(0, R - lw),
            outline=rgba(glow, 210), width=lw)
        halo = halo.filter(ImageFilter.GaussianBlur(ss * 1.6))
        halo.putalpha(Image.composite(halo.split()[3], Image.new("L", (W, H), 0), mask))
        surf = Image.alpha_composite(surf, halo)

    if ring:
        rl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        rw = max(2, int(ss * 2))
        ImageDraw.Draw(rl).rounded_rectangle(
            [rw // 2, rw // 2, W - 1 - rw // 2, H - 1 - rw // 2],
            radius=max(0, R - rw // 2), outline=rgba(ring), width=rw)
        surf = Image.alpha_composite(surf, rl)

    out = Image.new("RGB", (W, H), behind)
    out.paste(surf, (0, 0), surf)
    return out if ss == 1 else out.resize((w, h), Image.LANCZOS)


@functools.lru_cache(maxsize=48)
def render_card(w, h, radius, card_color, rim_color, behind, rolling, ss=CARD_SS):
    """A flip-card face: the shared surface, plus the drum shade while rolling."""
    base = render_surface(w, h, radius, card_color, behind, glow=rim_color, ss=ss)
    if not rolling:
        return base
    shade = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(shade)
    band = max(1, int(h * 0.62))
    top = (h - band) // 2
    for i in range(band):
        t = i / max(1, band - 1)
        d.line([(0, top + i), (w, top + i)], fill=int(86 * (1.0 - abs(2.0 * t - 1.0)) ** 0.8))
    return Image.composite(Image.new("RGB", (w, h), "#000000"), base, shade)


@functools.lru_cache(maxsize=4)
def _brush_noise(w, h):
    """Deterministic brushed-metal streaks (seeded, so renders are repeatable)."""
    import random
    cw = max(8, w // 12)
    noise = Image.frombytes("L", (cw, h), random.Random(1931).randbytes(cw * h))
    return noise.resize((w, h), Image.BILINEAR).filter(ImageFilter.GaussianBlur(0.6))


@functools.lru_cache(maxsize=12)
def render_housing(w, h, finish, well, well_radius, ss=CARD_SS):
    """The clock body: flush edges, lit from above, with a recessed well.

    The well is filled with exactly THEME['well'] because card corners are
    pre-blended against that colour.  Only the well has curves, so only the
    well is supersampled - the body renders at 1x.
    """
    spec = FINISHES.get(finish, FINISHES["graphite"])
    tone = spec["tone"]
    if spec["brushed"]:
        base = _vgrad(w, h, [(0.0, lighten(tone, 0.22)), (0.5, tone), (1.0, darken(tone, 0.80))])
        base = Image.blend(base, Image.merge("RGB", [_brush_noise(w, h)] * 3), 0.07)
    else:
        base = _vgrad(w, h, [(0.0, lighten(tone, 0.10)), (0.42, tone), (1.0, darken(tone, 0.66))])
    img = base.convert("RGBA")

    trim = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    td = ImageDraw.Draw(trim)
    td.line([(0, 0), (w - 1, 0)], fill=(255, 255, 255, spec["rim"]))
    td.line([(0, 1), (0, h - 1)], fill=(255, 255, 255, spec["rim"] // 4))
    td.line([(w - 1, 1), (w - 1, h - 1)], fill=(0, 0, 0, 70))
    td.line([(0, h - 1), (w - 1, h - 1)], fill=(0, 0, 0, 110))

    x1, y1, x2, y2 = well
    if x2 > x1 and y2 > y1:
        r = max(2, int(well_radius))
        # shadow cast into the recess from its top edge, light on its bottom lip
        td.line([(x1 + r, y1 - 1), (x2 - r, y1 - 1)], fill=(0, 0, 0, 70))
        td.line([(x1 + r, y2), (x2 - r, y2)], fill=(255, 255, 255, spec["lip"]))
    img.alpha_composite(trim)

    if x2 > x1 and y2 > y1:
        ww, wh = x2 - x1, y2 - y1
        s = max(1, int(ss))
        wl = Image.new("RGBA", (ww * s, wh * s), (0, 0, 0, 0))
        ImageDraw.Draw(wl).rounded_rectangle(
            [0, 0, ww * s - 1, wh * s - 1], radius=max(1, int(well_radius * s)),
            fill=rgba(THEME["well"]))
        if s > 1:
            wl = wl.resize((ww, wh), Image.LANCZOS)
        img.alpha_composite(wl, (x1, y1))
    return img.convert("RGB")


# -- sprites -----------------------------------------------------------------
@functools.lru_cache(maxsize=96)
def _digit_metrics(face, px):
    """Bounding box of '0' - the reference every digit is aligned to."""
    f = ttf(face, px)
    return f.getbbox("0")


@functools.lru_cache(maxsize=512)
def digit_sprite(ch, w, h, color, face="modern", blur=False, ss=CARD_SS):
    """One digit, transparent, sized to its card, with a soft contact shadow.

    Rendered rather than drawn as canvas text: Tk text cannot carry a shadow,
    a neon glow or motion blur, and its metrics differ by platform.  Returns
    None for a blank flap.
    """
    if not ch or not ch.strip():
        return None
    ss = max(1, int(ss))
    W, H = w * ss, h * ss
    ref = 100
    l0, t0, r0, b0 = _digit_metrics(face, ref)
    cap = max(1, b0 - t0)
    px = max(6, int(ref * (H * 0.66) / cap))
    f = ttf(face, px)
    l, t, r, b = f.getbbox(ch)
    rl, rt, rr, rb = f.getbbox("0")
    x = (W - (r - l)) / 2 - l
    y = (H - (rb - rt)) / 2 - rt
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    if is_neon(color):
        halo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(halo).text((x, y), ch, font=f, fill=rgba(color, 150))
        img = Image.alpha_composite(img, halo.filter(ImageFilter.GaussianBlur(ss * 3)))

    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    off = max(1, int(H * 0.018))
    ImageDraw.Draw(shadow).text((x, y + off), ch, font=f, fill=(0, 0, 0, 160))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(max(1, H * 0.014))))
    ImageDraw.Draw(img).text((x, y), ch, font=f, fill=rgba(color))

    if ss > 1:
        img = img.resize((w, h), Image.LANCZOS)
    if blur:
        # Vertical motion blur for mid-roll frames: smear along the travel axis.
        smear = img.resize((w, max(1, h // 7)), Image.BOX).resize((w, h), Image.BILINEAR)
        img = Image.blend(img, smear, 0.55)
    return img


@functools.lru_cache(maxsize=256)
def text_sprite(text, role, px, color, tracking=0.0, ss=3):
    """Text as an RGBA image.  Tracking (letter-spacing) is something Tk text
    cannot do, and is what makes small uppercase labels read as refined."""
    if not text:
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    ss = max(1, int(ss))
    f = ttf(role, px * ss)
    asc, desc = f.getmetrics()
    if tracking:
        advances = [f.getlength(c) for c in text]
        extra = tracking * px * ss
        width = int(sum(advances) + extra * (len(text) - 1)) + ss * 2
    else:
        width = int(f.getlength(text)) + ss * 2
    img = Image.new("RGBA", (max(1, width), asc + desc), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill = rgba(color) if isinstance(color, str) else color
    if tracking:
        x = 0.0
        for c, a in zip(text, advances):
            d.text((x, 0), c, font=f, fill=fill)
            x += a + extra
    else:
        d.text((0, 0), text, font=f, fill=fill)
    if ss > 1:
        img = img.resize((max(1, img.width // ss), max(1, img.height // ss)), Image.LANCZOS)
    return img


def fit_text(text, role, px, tracking, max_w):
    """Trim with an ellipsis until the rendered width fits."""
    if text_sprite(text, role, px, "#ffffff", tracking).width <= max_w:
        return text
    for n in range(len(text) - 1, 0, -1):
        cand = text[:n].rstrip() + "…"
        if text_sprite(cand, role, px, "#ffffff", tracking).width <= max_w:
            return cand
    return "…"


@functools.lru_cache(maxsize=128)
def pill_sprite(text, px, fg, bg, ss=3):
    """A small rounded label - GMT offset, AM/PM, day badge."""
    t = text_sprite(text, "ui_bold", px, fg, 0.04)
    padx, pady = max(4, int(px * 0.7)), max(1, int(px * 0.22))
    w, h = t.width + padx * 2, t.height + pady * 2
    p = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    fill = rgba(bg) if isinstance(bg, str) else bg
    ImageDraw.Draw(p).rounded_rectangle([0, 0, w * ss - 1, h * ss - 1],
                                        radius=h * ss // 2, fill=fill)
    p = p.resize((w, h), Image.LANCZOS)
    p.alpha_composite(t, ((w - t.width) // 2, (h - t.height) // 2))
    return p


@functools.lru_cache(maxsize=64)
def colon_sprite(w, h, color, ss=3):
    """Two round dots - consistent across fonts, unlike a ':' glyph."""
    W, H = max(1, w) * ss, max(1, h) * ss
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = max(1.5 * ss, H * 0.032)
    for cy in (H * 0.37, H * 0.63):
        d.ellipse([W / 2 - r, cy - r, W / 2 + r, cy + r], fill=rgba(color))
    return img.resize((max(1, w), max(1, h)), Image.LANCZOS)


# Icons on a 24-unit grid, stroked like a modern outline icon set.
_ICONS = {
    "close": [[(6.5, 6.5), (17.5, 17.5)], [(17.5, 6.5), (6.5, 17.5)]],
    "plus": [[(12, 5), (12, 19)], [(5, 12), (19, 12)]],
    "edit": [[(15.5, 4.5), (19.5, 8.5), (8.5, 19.5), (3.8, 20.2), (4.5, 15.5), (15.5, 4.5)],
             [(13.5, 6.5), (17.5, 10.5)]],
    "locate": [[(12, 2.5), (12, 6)], [(12, 18), (12, 21.5)],
               [(2.5, 12), (6, 12)], [(18, 12), (21.5, 12)]],
    "trash": [[(4, 7), (20, 7)], [(9, 7), (9.5, 4), (14.5, 4), (15, 7)],
              [(6, 7), (7, 20), (17, 20), (18, 7)], [(10, 11), (10, 16)], [(14, 11), (14, 16)]],
    "search": [[(15.5, 15.5), (20.5, 20.5)]],
    "check": [[(5, 12.5), (10, 17.5), (19, 7)]],
    "chevron": [[(6, 9.5), (12, 15.5), (18, 9.5)]],
    "alert": [[(12, 7), (12, 13.5)]],
    "copy": [[(9, 9), (19, 9), (19, 19), (9, 19), (9, 9)],
             [(15, 5), (5, 5), (5, 15)]],
    "more": [],
    "grip": [],
    "sun": [],
}
_ICON_CIRCLES = {
    "locate": [((12, 12), 6.5, False), ((12, 12), 1.6, True)],
    "search": [((10.5, 10.5), 6.5, False)],
    "alert": [((12, 12), 9.5, False), ((12, 17.2), 1.3, True)],
    "more": [((5.5, 12), 1.7, True), ((12, 12), 1.7, True), ((18.5, 12), 1.7, True)],
    "grip": [((19, 19), 1.5, True), ((14, 19), 1.5, True), ((19, 14), 1.5, True),
             ((9, 19), 1.5, True), ((14, 14), 1.5, True), ((19, 9), 1.5, True)],
}


@functools.lru_cache(maxsize=256)
def icon_sprite(name, size, color, ss=4, weight=1.8):
    """A vector icon - crisp at any DPI and independent of font glyph coverage."""
    S = max(1, int(size)) * ss
    k = S / 24.0
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill = rgba(color) if isinstance(color, str) else color
    width = max(1, int(weight * k))
    r = width / 2
    for poly in _ICONS.get(name, []):
        pts = [(x * k, y * k) for x, y in poly]
        d.line(pts, fill=fill, width=width, joint="curve")
        for x, y in (pts[0], pts[-1]):          # round caps
            d.ellipse([x - r, y - r, x + r, y + r], fill=fill)
    for (cx, cy), rad, solid in _ICON_CIRCLES.get(name, []):
        box = [(cx - rad) * k, (cy - rad) * k, (cx + rad) * k, (cy + rad) * k]
        if solid:
            d.ellipse(box, fill=fill)
        else:
            d.ellipse(box, outline=fill, width=width)
    return img.resize((max(1, int(size)), max(1, int(size))), Image.LANCZOS)


@functools.lru_cache(maxsize=32)
def dot_sprite(size, color, ss=4):
    S = max(1, size) * ss
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse([0, 0, S - 1, S - 1], fill=rgba(color))
    return img.resize((max(1, size), max(1, size)), Image.LANCZOS)


def make_app_icon(size=64):
    """The Solari mark: a flip card, split by its hinge, showing a clock face."""
    if not HAS_PIL:
        return None
    s = 4
    S_ = size * s
    img = Image.new("RGBA", (S_, S_), (0, 0, 0, 0))
    r = int(S_ * 0.22)
    face = Image.new("RGBA", (S_, S_), (0, 0, 0, 0))
    body = _vgrad(S_, S_, [(0, "#34363d"), (0.5, "#1f2025"), (1, "#141518")]).convert("RGBA")
    mask = Image.new("L", (S_, S_), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S_ - 1, S_ - 1], radius=r, fill=255)
    face.paste(body, (0, 0), mask)
    img = Image.alpha_composite(img, face)
    d = ImageDraw.Draw(img)
    amber = rgba(THEME["accent"])
    m = int(S_ * 0.21)
    d.ellipse([m, m, S_ - m, S_ - m], outline=amber, width=int(s * 2.8))
    cx = cy = S_ // 2
    d.line([cx, cy, cx, cy - int(S_ * 0.19)], fill=amber, width=int(s * 2.8))
    d.line([cx, cy, cx + int(S_ * 0.15), cy + int(S_ * 0.09)], fill=amber, width=int(s * 2.8))
    hinge = Image.new("RGBA", (S_, S_), (0, 0, 0, 0))
    ImageDraw.Draw(hinge).line([(0, S_ // 2), (S_, S_ // 2)], fill=(0, 0, 0, 150), width=s * 2)
    img = Image.alpha_composite(img, hinge)
    return img.resize((size, size), Image.LANCZOS)


def photo(img, master):
    """PhotoImage bound to a specific widget's interpreter.

    Without `master`, ImageTk binds to whichever Tk root was created first,
    and a second root then cannot display the image at all.
    """
    return ImageTk.PhotoImage(img, master=master)


# ---------------------------------------------------------------------------
#  Ticker
#
#  One second-aligned timer and one animation loop for the whole app.  The
#  loop runs at ~60 fps only while a digit is actually rolling; slower effects
#  (the colon pulse) ask for ~25 fps, and when nothing moves it stops.
# ---------------------------------------------------------------------------
FRAME_MS = 16
SLOW_FRAME_MS = 40
DRUM_DURATION = 0.21


class Ticker:
    def __init__(self, root):
        self._root = root
        self._clocks = []
        self._anim = set()
        self._tick_job = None
        self._anim_job = None
        self._running = False

    def subscribe(self, target):
        if target not in self._clocks:
            self._clocks.append(target)

    def unsubscribe(self, target):
        if target in self._clocks:
            self._clocks.remove(target)
        self._anim.discard(target)

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
        for target in list(self._clocks):
            try:
                target.on_second(now)
            except tk.TclError:
                self.unsubscribe(target)
            except Exception:
                # One failing subscriber must not stop the timer every clock
                # shares - report it and carry on.
                self._root.report_callback_exception(*sys.exc_info())
        # Re-aim at the next true second boundary every time, so the clock
        # cannot drift however long the tick itself took.
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
            except Exception:
                self._anim.discard(target)
                self._root.report_callback_exception(*sys.exc_info())
        if self._anim:
            fast = any(getattr(t, "fast", True) for t in self._anim)
            self._anim_job = self._root.after(FRAME_MS if fast else SLOW_FRAME_MS, self._frame)


class SpriteBank:
    """PhotoImages for one window, keyed by what they show.

    Every card in a clock shares the same digits at the same size, so they
    share these images instead of each holding ten of their own.
    """

    def __init__(self, master):
        self.master = master
        self._cache = {}

    def get(self, key, factory):
        p = self._cache.get(key)
        if p is None:
            img = factory()
            if img is None:
                return None
            p = photo(img, self.master)
            self._cache[key] = p
        return p

    def clear(self):
        self._cache.clear()


# ---------------------------------------------------------------------------
#  FlipCard
#
#  One canvas sized exactly to its card, so a digit rolling past the top edge
#  is clipped for free.  Three items, created once: the face and two digit
#  sprites.  A roll only moves the sprites; nothing is created per frame.
# ---------------------------------------------------------------------------
class FlipCard(tk.Canvas):
    fast = True

    def __init__(self, parent, clock, w, h, radius):
        self.clock = clock
        self.W, self.H, self.R = int(w), int(h), int(radius)
        super().__init__(parent, width=self.W, height=self.H, bg=THEME["well"],
                         highlightthickness=0, bd=0, takefocus=0)
        self._cur = " "
        self._nxt = " "
        self._rolling = False
        self._t0 = 0.0
        self._faces = {}
        self._shown = [None, None]       # strong refs to the displayed sprites
        self._build()

    # -- construction ------------------------------------------------------
    def _build(self):
        cx, cy = self.W // 2, self.H // 2
        if HAS_PIL:
            self._bg = self.create_image(0, 0, anchor="nw", image=self._face(False))
            self._a = self.create_image(cx, cy, anchor="center", state="hidden")
            self._b = self.create_image(cx, cy, anchor="center", state="hidden")
        else:
            self._bg = self._flat_face()
            font = ui_font_px(int(self.H * 0.66), "bold")
            self._a = self.create_text(cx, cy, text="", font=font,
                                       fill=self.clock.text_color, anchor="center")
            self._b = self.create_text(cx, cy, text="", font=font,
                                       fill=self.clock.text_color, anchor="center",
                                       state="hidden")

    def _flat_face(self):
        r, W, H = self.R, self.W, self.H
        pts = [r, 0, W - r, 0, W, 0, W, r, W, H - r, W, H,
               W - r, H, r, H, 0, H, 0, H - r, 0, r, 0, 0]
        return self.create_polygon(pts, smooth=True, fill=self.clock.card_color, outline="")

    def _face(self, rolling):
        key = (rolling, self.W, self.H, self.clock.card_color, self.clock.draft)
        p = self._faces.get(key)
        if p is None:
            rim = self.clock.text_color if is_neon(self.clock.text_color) else None
            img = render_card(self.W, self.H, self.R, self.clock.card_color, rim,
                              THEME["well"], rolling, 1 if self.clock.draft else CARD_SS)
            p = photo(img, self)
            if len(self._faces) > 6:
                self._faces.clear()
            self._faces[key] = p
        return p

    def _sprite(self, ch, blur=False):
        c = self.clock
        ss = 1 if c.draft else CARD_SS
        key = ("d", ch, self.W, self.H, c.text_color, c.face, blur, ss)
        return c.sprites.get(key, lambda: digit_sprite(ch, self.W, self.H, c.text_color,
                                                       c.face, blur, ss))

    def _show(self, slot, item, ch, blur=False):
        if not HAS_PIL:
            self.itemconfigure(item, text=ch.strip(), state="normal" if ch.strip() else "hidden")
            return
        img = self._sprite(ch, blur) if ch.strip() else None
        self._shown[slot] = img
        if img is None:
            # Detach the image as well as hiding the item: Tk re-resolves an
            # item's image on every configure, so an item still naming an
            # image that has since been freed makes the next one raise.
            self.itemconfigure(item, image="", state="hidden")
        else:
            self.itemconfigure(item, image=img, state="normal")

    # -- public ------------------------------------------------------------
    def set(self, ch):
        """Roll to `ch`.  One arriving mid-roll retargets the incoming digit."""
        if ch == self._cur and not self._rolling:
            return
        self._nxt = ch
        if self._rolling:
            self._show(1, self._b, ch, blur=True)
            return
        self._rolling = True
        self._t0 = time.perf_counter()
        if HAS_PIL:
            self.itemconfigure(self._bg, image=self._face(True))
        # Prepared but hidden until step() has somewhere to put it; revealing
        # it here stacked it on the outgoing digit for a frame.
        self._show(1, self._b, ch)
        self.itemconfigure(self._b, state="hidden")
        self.clock.ticker.animate(self)

    def set_immediate(self, ch):
        self._cur = self._nxt = ch
        self._rolling = False
        if HAS_PIL:
            self.itemconfigure(self._bg, image=self._face(False))
        self.coords(self._a, self.W // 2, self.H // 2)
        self._show(0, self._a, ch)               # also restores state="normal"
        if HAS_PIL:
            self.itemconfigure(self._b, image="", state="hidden")
        else:
            self.itemconfigure(self._b, state="hidden")
        self._shown[1] = None

    def restyle(self):
        self._faces.clear()
        if HAS_PIL:
            self.itemconfigure(self._bg, image=self._face(self._rolling))
        else:
            self.itemconfigure(self._bg, fill=self.clock.card_color)
            for item in (self._a, self._b):
                self.itemconfigure(item, fill=self.clock.text_color)
        self.set_immediate(self._nxt if self._rolling else self._cur)

    def resize(self, w, h, radius):
        w, h = int(w), int(h)
        if (w, h, radius) == (self.W, self.H, self.R):
            self.restyle()
            return
        self.W, self.H, self.R = w, h, int(radius)
        self.configure(width=w, height=h)
        if not HAS_PIL:
            self.delete(self._bg)
            self._bg = self._flat_face()
            self.tag_lower(self._bg)
            font = ui_font_px(int(h * 0.66), "bold")
            for item in (self._a, self._b):
                self.itemconfigure(item, font=font)
        self.restyle()

    # -- animation ---------------------------------------------------------
    def step(self, now):
        t = (now - self._t0) / DRUM_DURATION
        if t >= 1.0:
            self.set_immediate(self._nxt)
            return False
        e = smoothstep(t)
        cx, cy, H = self.W // 2, self.H // 2, self.H
        blur = 0.2 < e < 0.8                     # motion blur at peak speed
        if self._shown[0] is not None or not HAS_PIL:
            if blur != getattr(self, "_blurred", False) and self._cur.strip():
                self._show(0, self._a, self._cur, blur)
            self.coords(self._a, cx, cy - int(H * e))
        if self._nxt.strip():
            if blur != getattr(self, "_blurred", False) or \
                    self.itemcget(self._b, "state") == "hidden":
                self._show(1, self._b, self._nxt, blur)
            self.coords(self._b, cx, cy + int(H * (1.0 - e)))
        self._blurred = blur
        return True


# ---------------------------------------------------------------------------
#  Clock layout - the one source of truth for where everything goes
#
#  Used by the live ClockWindow and by compose_clock(), the pure-Pillow
#  renderer behind the editor preview, the manager thumbnails and CI
#  screenshots - so those can never drift from the real clock.
# ---------------------------------------------------------------------------
def effective_scale(cfg):
    return float(cfg.get("scale", 1.0)) * UI_SCALE


def clock_layout(cfg, s):
    r = lambda v, lo=1: max(lo, int(round(v * s)))
    L = {"s": s}
    L["mx"] = r(16, 6)
    L["hdr"] = r(34, 18)
    L["ftr"] = r(32, 16)
    L["wp"] = r(12, 4)
    L["cw"], L["ch"] = r(78, 18), r(102, 24)
    L["radius"] = r(11, 3)
    L["gap"] = r(6, 2)
    L["colw"] = r(18, 6)
    mode = cfg.get("seconds", "small")
    if mode == "small":
        L["sw"], L["sh"] = r(78 * 0.6, 12), r(102 * 0.6, 16)
        L["sradius"] = r(8, 2)
    else:
        L["sw"], L["sh"], L["sradius"] = L["cw"], L["ch"], L["radius"]

    slots = []
    x = 0

    def card(key, w, h, rad):
        nonlocal x
        slots.append(("card", key, x, L["ch"] - h, w, h, rad))
        x += w + L["gap"]

    def colon(key):
        nonlocal x
        slots.append(("colon", key, x, 0, L["colw"], L["ch"], 0))
        x += L["colw"] + L["gap"]

    card("h1", L["cw"], L["ch"], L["radius"])
    card("h2", L["cw"], L["ch"], L["radius"])
    colon("c1")
    card("m1", L["cw"], L["ch"], L["radius"])
    card("m2", L["cw"], L["ch"], L["radius"])
    if mode == "large":
        colon("c2")
        card("s1", L["sw"], L["sh"], L["sradius"])
        card("s2", L["sw"], L["sh"], L["sradius"])
    elif mode == "small":
        x += r(6, 2)
        card("s1", L["sw"], L["sh"], L["sradius"])
        card("s2", L["sw"], L["sh"], L["sradius"])
    L["slots"] = slots
    L["row_w"] = x - L["gap"]

    wx1, wy1 = L["mx"], L["hdr"]
    wx2 = wx1 + L["row_w"] + 2 * L["wp"]
    wy2 = wy1 + L["ch"] + 2 * L["wp"]
    L["well"] = (wx1, wy1, wx2, wy2)
    L["well_r"] = r(13, 3)
    L["row_x"], L["row_y"] = wx1 + L["wp"], wy1 + L["wp"]
    L["w"], L["h"] = wx2 + L["mx"], wy2 + L["ftr"]

    L["dot"] = r(7, 4)
    L["label_px"] = r(10.5, 7)
    L["foot_px"] = r(9, 7)
    L["icon"] = r(15, 10)
    L["head_cy"] = L["hdr"] // 2 + r(1)
    L["foot_cy"] = wy2 + L["ftr"] // 2
    L["close_x"] = L["w"] - L["mx"] - L["icon"]
    L["edit_x"] = L["close_x"] - L["icon"] - r(10, 4)
    L["label_x"] = L["mx"] + L["dot"] + r(7, 3)
    L["label_max"] = L["edit_x"] - L["label_x"] - r(8, 4)
    L["grip"] = r(13, 9)
    return L


def clock_digits(now, cfg):
    """{slot key: character} for a moment.  12-hour mode blanks a leading
    zero the way a real flap board does ( 9:41, not 09:41)."""
    hour24 = cfg.get("hour24", True)
    hh = now.strftime("%H" if hour24 else "%I")
    if not hour24 and hh[0] == "0":
        hh = " " + hh[1]
    mm, ss = now.strftime("%M"), now.strftime("%S")
    return {"h1": hh[0], "h2": hh[1], "m1": mm[0], "m2": mm[1], "s1": ss[0], "s2": ss[1]}


def utc_offset_label(dt):
    """'GMT+9', 'GMT−3:30', 'GMT' - with a real minus sign."""
    off = dt.utcoffset()
    if off is None:
        return "GMT"
    total = int(off.total_seconds()) // 60
    sign = "−" if total < 0 else "+"
    hh, mm = divmod(abs(total), 60)
    if not hh and not mm:
        return "GMT"
    return f"GMT{sign}{hh}" + (f":{mm:02d}" if mm else "")


def day_offset_label(dt, local_now=None):
    """'+1' / '−1' when that zone is on a different calendar day."""
    local = local_now or datetime.datetime.now()
    delta = (dt.date() - local.date()).days
    if delta > 0:
        return "+1"
    if delta < 0:
        return "−1"
    return ""


def footer_texts(now, cfg):
    return {
        "date": now.strftime("%a %d %b").upper(),
        "ampm": "" if cfg.get("hour24", True) else now.strftime("%p"),
        "gmt": utc_offset_label(now),
        "badge": day_offset_label(now),
    }


def clock_now(cfg, ts=None):
    zone = zone_or_none(cfg.get("tz", "UTC")) or datetime.timezone.utc
    if ts is None:
        return datetime.datetime.now(zone)
    return datetime.datetime.fromtimestamp(ts, zone)


def compose_clock(cfg, now=None, scale=None, hover=False, ss=CARD_SS):
    """The whole clock as one RGB image - the same layout as the live window."""
    s = effective_scale(cfg) if scale is None else scale
    now = now or clock_now(cfg)
    L = clock_layout(cfg, s)
    spec = FINISHES.get(cfg.get("finish"), FINISHES["graphite"])
    img = render_housing(L["w"], L["h"], cfg.get("finish", "graphite"),
                         L["well"], L["well_r"], ss).convert("RGBA")
    tc, cc, face = cfg["text_color"], cfg["card_color"], cfg.get("face", "modern")
    rim = tc if is_neon(tc) else None
    digits = clock_digits(now, cfg)
    for kind, key, x, y, w, h, rad in L["slots"]:
        px, py = L["row_x"] + x, L["row_y"] + y
        if kind == "card":
            img.paste(render_card(w, h, rad, cc, rim, THEME["well"], False, ss), (px, py))
            spr = digit_sprite(digits[key], w, h, tc, face, False, ss)
            if spr is not None:
                img.alpha_composite(spr, (px, py))
        else:
            img.alpha_composite(colon_sprite(w, h, THEME["colon"]), (px, py))
    d = L["dot"]
    img.alpha_composite(dot_sprite(d, tc), (L["mx"], L["head_cy"] - d // 2))
    label = fit_text(cfg.get("label", "").upper(), "ui_bold", L["label_px"], 0.12, L["label_max"])
    lab = text_sprite(label, "ui_bold", L["label_px"], spec["ink"], 0.12)
    img.alpha_composite(lab, (L["label_x"], L["head_cy"] - lab.height // 2))
    for i, sprite in enumerate(_footer_sprites(now, cfg, L, spec)):
        img.alpha_composite(sprite[0], sprite[1])
    if hover:
        ic = L["icon"]
        img.alpha_composite(icon_sprite("edit", ic, spec["icon"]), (L["edit_x"], L["head_cy"] - ic // 2))
        img.alpha_composite(icon_sprite("close", ic, spec["icon"]), (L["close_x"], L["head_cy"] - ic // 2))
        g = L["grip"]
        img.alpha_composite(icon_sprite("grip", g, spec["sub"]), (L["w"] - g - 2, L["h"] - g - 2))
    return img.convert("RGB")


def _footer_sprites(now, cfg, L, spec):
    """[(sprite, (x, y)), ...] for the footer, right-aligned chips."""
    t = footer_texts(now, cfg)
    px = L["foot_px"]
    out = []
    date = text_sprite(t["date"], "ui_bold", px, spec["sub"], 0.10)
    out.append((date, (L["mx"] + 1, L["foot_cy"] - date.height // 2)))
    right = L["w"] - L["mx"]
    tc = cfg["text_color"]
    gap = max(3, int(round(6 * L["s"])))
    chips = []
    if t["badge"]:
        chips.append(pill_sprite(t["badge"], px, tc, rgba(tc, 40)))
    chips.append(pill_sprite(t["gmt"], px, spec["sub"], spec["chip"]))
    if t["ampm"]:
        chips.append(pill_sprite(t["ampm"], px, spec["sub"], spec["chip"]))
    for chip in chips:
        right -= chip.width
        out.append((chip, (right, L["foot_cy"] - chip.height // 2)))
        right -= gap
    return out


# ---------------------------------------------------------------------------
#  ClockWindow
#
#  One canvas acting as a compositor: the housing image, chrome sprites, and
#  the flip cards as embedded child canvases (nested so they clip their own
#  digits).  Relayout moves and re-images items; it never rebuilds the window.
# ---------------------------------------------------------------------------
SNAP_DP = 10
CHROME_HIDE_MS = 700
RESIZE_FRAME_MS = 16


class ClockWindow(tk.Toplevel):

    def __init__(self, master, app, cfg):
        super().__init__(master)
        self.app = app
        self.cfg = cfg
        self.ticker = app.ticker
        self.sprites = SpriteBank(self)
        self.draft = False
        self._scale = float(cfg.get("scale", 1.0))
        self._drag = None
        self._resize = None
        self._pending_scale = None
        self._resize_job = None
        self._hide_job = None
        self._chrome = False
        self._cards = {}
        self._colons = {}
        self._card_wins = {}
        self._last = {}
        self._footer = {}
        self._footer_items = []
        self._colon_t0 = None
        self._colon_level = None
        self._housing = None
        self._label_photo = None
        self._dot_photo = None
        self._flash_job = None
        self._L = None

        self.overrideredirect(True)
        self.configure(bg="#000000")
        self.wm_attributes("-topmost", bool(cfg.get("topmost", True)))
        self._apply_opacity()

        self.cv = tk.Canvas(self, highlightthickness=0, bd=0, bg=THEME["well"],
                            takefocus=0)
        self.cv.pack(fill="both", expand=True)
        self._build()
        self._place_initial()
        self._wire_events()
        self.ticker.subscribe(self)
        self.on_second(time.time(), animate=False)

    # -- style accessors (read by FlipCard) --------------------------------
    @property
    def text_color(self):
        return self.cfg.get("text_color", "#f4f4f2")

    @property
    def card_color(self):
        return self.cfg.get("card_color", "#1e1f22")

    @property
    def face(self):
        return self.cfg.get("face", "modern")

    @property
    def spec(self):
        return FINISHES.get(self.cfg.get("finish"), FINISHES["graphite"])

    def _apply_opacity(self):
        try:
            self.wm_attributes("-alpha", float(self.cfg.get("opacity", 0.98)))
        except tk.TclError:
            pass

    def zone(self):
        return zone_or_none(self.cfg.get("tz", "UTC")) or datetime.timezone.utc

    def zone_valid(self):
        return zone_or_none(self.cfg.get("tz", "UTC")) is not None

    # -- construction ------------------------------------------------------
    def _build(self):
        cv = self.cv
        self._bg = cv.create_image(0, 0, anchor="nw")
        self._dot = cv.create_image(0, 0, anchor="w")
        self._label = cv.create_image(0, 0, anchor="w")
        self._edit = cv.create_image(0, 0, anchor="nw", state="hidden",
                                     tags=("nodrag", "btn", "btn_edit"))
        self._close = cv.create_image(0, 0, anchor="nw", state="hidden",
                                      tags=("nodrag", "btn", "btn_close"))
        self._grip = cv.create_image(0, 0, anchor="nw", state="hidden",
                                     tags=("nodrag", "grip"))
        if not HAS_PIL:
            cv.itemconfigure(self._label, state="hidden")
            self._label_txt = cv.create_text(0, 0, anchor="w", text="",
                                             fill=self.spec["ink"])
        self._relayout()

    def _sync_slots(self, L):
        """Create or drop only the cards and colons whose presence changed."""
        want_cards = {k for kind, k, *_ in L["slots"] if kind == "card"}
        want_colons = {k for kind, k, *_ in L["slots"] if kind == "colon"}
        for key in list(self._cards):
            if key not in want_cards:
                self.cv.delete(self._card_wins.pop(key))
                self._cards.pop(key).destroy()
                self._last.pop(key, None)
        for key in list(self._colons):
            if key not in want_colons:
                self.cv.delete(self._colons.pop(key))
        for kind, key, x, y, w, h, rad in L["slots"]:
            if kind == "card" and key not in self._cards:
                card = FlipCard(self.cv, self, w, h, rad)
                self._bind_drag(card)
                self._cards[key] = card
                self._card_wins[key] = self.cv.create_window(0, 0, anchor="nw", window=card)
            elif kind == "colon" and key not in self._colons:
                self._colons[key] = self.cv.create_image(0, 0, anchor="nw")

    def layout(self):
        return clock_layout(self.cfg, self._scale * UI_SCALE)

    def _relayout(self, draft=False):
        self.draft = draft
        L = self.layout()
        self._L = L
        cv = self.cv
        self._sync_slots(L)
        self.geometry(f"{L['w']}x{L['h']}")
        cv.configure(width=L["w"], height=L["h"])
        ss = 1 if draft else CARD_SS
        spec = self.spec

        if HAS_PIL:
            self._housing = photo(render_housing(L["w"], L["h"], self.cfg.get("finish", "graphite"),
                                                 L["well"], L["well_r"], ss), self)
            cv.itemconfigure(self._bg, image=self._housing)
            self._dot_photo = photo(dot_sprite(L["dot"], self.text_color), self)
            cv.itemconfigure(self._dot, image=self._dot_photo)
            cv.coords(self._dot, L["mx"], L["head_cy"])
            self._paint_label(L)
            ic = L["icon"]
            for item, name in ((self._edit, "edit"), (self._close, "close")):
                cv.itemconfigure(item, image=self.sprites.get(
                    ("icon", name, ic, spec["icon"]), lambda n=name: icon_sprite(n, ic, spec["icon"])))
            cv.coords(self._edit, L["edit_x"], L["head_cy"] - ic // 2)
            cv.coords(self._close, L["close_x"], L["head_cy"] - ic // 2)
            g = L["grip"]
            cv.itemconfigure(self._grip, image=self.sprites.get(
                ("grip", g, spec["sub"]), lambda: icon_sprite("grip", g, spec["sub"])))
            cv.coords(self._grip, L["w"] - g - 2, L["h"] - g - 2)
        else:
            cv.configure(bg=FINISHES.get(self.cfg.get("finish"), FINISHES["graphite"])["tone"])
            cv.coords(self._label_txt, L["label_x"], L["head_cy"])
            cv.itemconfigure(self._label_txt, text=self.cfg.get("label", "").upper(),
                             font=ui_font_px(L["label_px"], "bold"), fill=spec["ink"])

        for kind, key, x, y, w, h, rad in L["slots"]:
            px, py = L["row_x"] + x, L["row_y"] + y
            if kind == "card":
                self._cards[key].resize(w, h, rad)
                cv.coords(self._card_wins[key], px, py)
            else:
                cv.coords(self._colons[key], px, py)
        self._colon_level = None
        self._paint_colons(1.0)
        self._footer = {}
        self._last = {}
        self.on_second(time.time(), animate=False)
        for item in (self._edit, self._close, self._grip):
            cv.tag_raise(item)

    def _paint_label(self, L):
        label = fit_text(self.cfg.get("label", "").upper(), "ui_bold",
                         L["label_px"], 0.12, L["label_max"])
        if not self.zone_valid():
            label = fit_text(label + "  ·  UNKNOWN ZONE", "ui_bold",
                             L["label_px"], 0.12, L["label_max"])
        self._label_photo = photo(text_sprite(label, "ui_bold", L["label_px"],
                                              self.spec["ink"], 0.12), self)
        self.cv.itemconfigure(self._label, image=self._label_photo)
        self.cv.coords(self._label, L["label_x"], L["head_cy"])

    def _paint_colons(self, level):
        """Colon brightness, quantised so the image only changes ~6 times."""
        q = round(max(0.0, min(1.0, level)) * 5) / 5
        if q == self._colon_level or not self._L:
            return
        self._colon_level = q
        if not HAS_PIL:
            return
        col = lerp(THEME["colon_dim"], THEME["colon"], q)
        L = self._L
        img = self.sprites.get(("colon", L["colw"], L["ch"], col),
                               lambda: colon_sprite(L["colw"], L["ch"], col))
        for item in self._colons.values():
            self.cv.itemconfigure(item, image=img)

    # -- time --------------------------------------------------------------
    def on_second(self, ts, animate=True):
        now = clock_now(self.cfg, ts)
        for key, ch in clock_digits(now, self.cfg).items():
            card = self._cards.get(key)
            if card is not None and self._last.get(key) != ch:
                card.set(ch) if animate else card.set_immediate(ch)
                self._last[key] = ch
        self._refresh_footer(now)
        if animate and self._colons:
            self._colon_t0 = time.perf_counter()
            self.ticker.animate(self)

    fast = False         # the colon pulse is fine at ~25 fps

    def step(self, now):
        if self._colon_t0 is None:
            return False
        t = (now - self._colon_t0) / 0.6
        if t >= 1.0:
            self._colon_t0 = None
            self._paint_colons(1.0)
            return False
        self._paint_colons(1.0 - 0.5 * math.sin(math.pi * t))
        return True

    def _refresh_footer(self, now):
        texts = footer_texts(now, self.cfg)
        if texts == self._footer or not self._L:
            return
        self._footer = texts
        for item in self._footer_items:
            self.cv.delete(item)
        self._footer_items = []
        if not HAS_PIL:
            item = self.cv.create_text(self._L["mx"], self._L["foot_cy"], anchor="w",
                                       text=f"{texts['date']}   {texts['gmt']}",
                                       fill=self.spec["sub"],
                                       font=ui_font_px(self._L["foot_px"]))
            self._footer_items.append(item)
            return
        self._footer_photos = []
        for sprite, (x, y) in _footer_sprites(now, self.cfg, self._L, self.spec):
            p = photo(sprite, self)
            self._footer_photos.append(p)
            self._footer_items.append(self.cv.create_image(x, y, anchor="nw", image=p))

    # -- hover chrome ------------------------------------------------------
    def _set_chrome(self, on):
        if on == self._chrome:
            return
        self._chrome = on
        state = "normal" if on else "hidden"
        for item in (self._edit, self._close, self._grip):
            self.cv.itemconfigure(item, state=state)

    def _pointer_inside(self):
        try:
            px, py = self.winfo_pointerxy()
            x, y = self.winfo_rootx(), self.winfo_rooty()
            return x <= px < x + self.winfo_width() and y <= py < y + self.winfo_height()
        except tk.TclError:
            return False

    def _on_enter(self, _=None):
        if self._hide_job:
            self.after_cancel(self._hide_job)
            self._hide_job = None
        self._set_chrome(True)

    def _on_leave(self, _=None):
        if self._hide_job:
            self.after_cancel(self._hide_job)
        self._hide_job = self.after(CHROME_HIDE_MS, self._maybe_hide)

    def _maybe_hide(self):
        self._hide_job = None
        if self._drag or self._resize or self._pointer_inside():
            self._hide_job = self.after(CHROME_HIDE_MS, self._maybe_hide)
            return
        self._set_chrome(False)

    def _hover_button(self, tag, on):
        if not HAS_PIL or not self._L:
            return
        spec, ic = self.spec, self._L["icon"]
        colour = (THEME["danger"] if tag == "btn_close" else spec["icon_hi"]) if on else spec["icon"]
        name = "close" if tag == "btn_close" else "edit"
        img = self.sprites.get(("icon", name, ic, colour), lambda: icon_sprite(name, ic, colour))
        self.cv.itemconfigure(tag, image=img)
        self.cv.configure(cursor="hand2" if on else "")

    # -- binding -----------------------------------------------------------
    def _wire_events(self):
        cv = self.cv
        self.bind("<Enter>", self._on_enter, add="+")
        self.bind("<Leave>", self._on_leave, add="+")
        self._bind_drag(cv)
        cv.tag_bind("btn_edit", "<ButtonRelease-1>", lambda e: self.app.edit_clock(self))
        cv.tag_bind("btn_close", "<ButtonRelease-1>", lambda e: self.app.remove_clock(self))
        for tag in ("btn_edit", "btn_close"):
            cv.tag_bind(tag, "<Enter>", lambda e, t=tag: self._hover_button(t, True))
            cv.tag_bind(tag, "<Leave>", lambda e, t=tag: self._hover_button(t, False))
        cv.tag_bind("grip", "<ButtonPress-1>", self._rs_start)
        cv.tag_bind("grip", "<B1-Motion>", self._rs_move)
        cv.tag_bind("grip", "<ButtonRelease-1>", self._rs_end)
        cv.tag_bind("grip", "<Enter>", lambda e: cv.configure(cursor="size_nw_se"))
        cv.tag_bind("grip", "<Leave>", lambda e: cv.configure(cursor=""))

    def _bind_drag(self, widget):
        """Bound exactly once per widget - rebinding with add='+' stacks copies."""
        widget.bind("<ButtonPress-1>", self._drag_start, add="+")
        widget.bind("<B1-Motion>", self._drag_move, add="+")
        widget.bind("<ButtonRelease-1>", self._drag_end, add="+")
        widget.bind("<Double-Button-1>", lambda e: self.app.edit_clock(self), add="+")
        widget.bind("<Button-3>", self._context_menu, add="+")
        if IS_MAC:
            widget.bind("<Button-2>", self._context_menu, add="+")
        widget.bind("<Control-MouseWheel>", self._wheel_scale, add="+")
        widget.bind("<Control-Button-4>", self._wheel_scale, add="+")
        widget.bind("<Control-Button-5>", self._wheel_scale, add="+")

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
        if not (e.state & 0x0001):                  # hold Shift to place freely
            peers = []
            for p in getattr(self.app, "windows", []):
                if p is self:
                    continue
                try:
                    peers.append((p.winfo_x(), p.winfo_y(), p.winfo_width(), p.winfo_height()))
                except tk.TclError:
                    pass
            x, y = snap(x, y, self.winfo_width(), self.winfo_height(),
                        self.app.monitors(), peers, dp(SNAP_DP))
        self.geometry(f"+{x}+{y}")
        self._drag_pos = (x, y)

    def _drag_end(self, e):
        if self._drag:
            # The position the drag set: winfo_x() lags until the next idle.
            pos = getattr(self, "_drag_pos", None)
            if pos is not None:
                self.cfg["x"], self.cfg["y"] = pos
                self._drag_pos = None
            # Saved now, not at quit: a shutdown or crash used to lose every
            # move made during the session.
            self.app.schedule_save()
        self._drag = None

    def _place_initial(self):
        self.update_idletasks()
        x, y = ensure_visible(self.cfg.get("x", 100), self.cfg.get("y", 100),
                              self.winfo_reqwidth(), self.winfo_reqheight(),
                              self.app.monitors())
        self.cfg["x"], self.cfg["y"] = x, y
        self.geometry(f"+{x}+{y}")

    def recall(self, x=None, y=None):
        """Bring a clock back into view.

        lift() and -topmost re-assert an overrideredirect window's cached
        geometry, which replayed the old position over the move - so the raise
        happens a pass later.
        """
        if x is None or y is None:
            l, t, _, _ = self.app.monitors()[0]
            x, y = l + dp(40), t + dp(40)
        self.update_idletasks()
        self.geometry(f"+{x}+{y}")
        self.update_idletasks()
        if (self.winfo_x(), self.winfo_y()) != (x, y):
            self.geometry(f"{self.winfo_width()}x{self.winfo_height()}+{x}+{y}")
        self.cfg["x"], self.cfg["y"] = x, y
        self.after(0, self._raise_self)

    def _raise_self(self):
        try:
            self.wm_attributes("-topmost", bool(self.cfg.get("topmost", True)))
            self.lift()
        except tk.TclError:
            pass

    def flash(self):
        """Pulse the opacity twice, so a recalled clock is easy to spot."""
        base = float(self.cfg.get("opacity", 0.98))
        seq = (0.35, base, 0.35, base)
        if self._flash_job:
            self.after_cancel(self._flash_job)

        def step(i):
            self._flash_job = None
            try:
                self.wm_attributes("-alpha", seq[i])
            except tk.TclError:
                return
            if i + 1 < len(seq):
                self._flash_job = self.after(120, lambda: step(i + 1))
        step(0)

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
        self._pending_scale = round(max(SCALE_MIN, min(SCALE_MAX,
                                                       s0 + (e.x_root - rx0) * s0 / w0)), 2)
        # Coalesce: motion events arrive far faster than a frame, and each
        # relayout re-renders the housing - at most one per frame, in draft.
        if self._resize_job is None:
            self._resize_job = self.after(RESIZE_FRAME_MS, self._apply_resize)

    def _apply_resize(self):
        self._resize_job = None
        if self._pending_scale is None or not self._resize:
            return
        if abs(self._pending_scale - self._scale) >= 0.01:
            self._scale = self._pending_scale
            wx, wy = self._resize[0], self._resize[1]
            self._relayout(draft=True)
            self.geometry(f"+{wx}+{wy}")

    def _rs_end(self, e):
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
            self._resize_job = None
        if self._resize:
            if self._pending_scale is not None:
                self._scale = self._pending_scale
            wx, wy = self._resize[0], self._resize[1]
            self._resize = None
            self._relayout(draft=False)           # full quality on release
            self.geometry(f"+{wx}+{wy}")
            self.cfg["scale"] = self._scale
            self.cfg["x"], self.cfg["y"] = self.winfo_x(), self.winfo_y()
            self.app.schedule_save()
        self._pending_scale = None

    def _wheel_scale(self, e):
        up = getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0
        new = round(max(SCALE_MIN, min(SCALE_MAX, self._scale * (1.06 if up else 1 / 1.06))), 2)
        if new != self._scale:
            x, y = self.winfo_x(), self.winfo_y()
            self._scale = new
            self.cfg["scale"] = new
            self._relayout()
            self.geometry(f"+{x}+{y}")
            self.app.schedule_save()

    # -- context menu ------------------------------------------------------
    def _context_menu(self, e):
        m = tk.Menu(self, tearoff=0, bg=THEME["surface"], fg=THEME["text"],
                    activebackground=THEME["surface_hi"], activeforeground=THEME["text"],
                    bd=0, relief="flat", font=ui_font(10))
        m.add_command(label="Edit…", command=lambda: self.app.edit_clock(self))
        m.add_command(label="All clocks…", command=self.app.show_manager)
        m.add_separator()
        secs = tk.StringVar(m, self.cfg.get("seconds", "small"))
        sub = tk.Menu(m, tearoff=0, bg=THEME["surface"], fg=THEME["text"],
                      activebackground=THEME["surface_hi"], activeforeground=THEME["text"])
        for mode, name in (("off", "Off"), ("small", "Small"), ("large", "Large")):
            sub.add_radiobutton(label=name, value=mode, variable=secs,
                                command=lambda v=mode: self._set_option("seconds", v))
        m.add_cascade(label="Seconds", menu=sub)
        fin = tk.StringVar(m, self.cfg.get("finish", "graphite"))
        sub2 = tk.Menu(m, tearoff=0, bg=THEME["surface"], fg=THEME["text"],
                       activebackground=THEME["surface_hi"], activeforeground=THEME["text"])
        for key, spec in FINISHES.items():
            sub2.add_radiobutton(label=spec["name"], value=key, variable=fin,
                                 command=lambda v=key: self._set_option("finish", v))
        m.add_cascade(label="Finish", menu=sub2)
        h24 = tk.BooleanVar(m, self.cfg.get("hour24", True))
        m.add_checkbutton(label="24-hour clock", variable=h24,
                          command=lambda: self._set_option("hour24", h24.get()))
        top = tk.BooleanVar(m, self.cfg.get("topmost", True))
        m.add_checkbutton(label="Always on top", variable=top,
                          command=lambda: self._set_option("topmost", top.get()))
        m.add_separator()
        m.add_command(label="Duplicate", command=lambda: self.app.duplicate_clock(self))
        m.add_command(label="Remove…", command=lambda: self.app.remove_clock(self))
        self._menu_vars = (secs, fin, h24, top)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _set_option(self, key, value):
        self.cfg[key] = value
        self.refresh_style()
        self.app.schedule_save()
        self.app.clock_changed(self)

    # -- style -------------------------------------------------------------
    def refresh_style(self):
        self._apply_opacity()
        self.wm_attributes("-topmost", bool(self.cfg.get("topmost", True)))
        self._scale = float(self.cfg.get("scale", self._scale))
        self.sprites.clear()
        x, y = self.winfo_x(), self.winfo_y()
        self._relayout()                 # restyles every card as it goes
        self.geometry(f"+{x}+{y}")

    def destroy(self):
        for job in (self._resize_job, self._hide_job, self._flash_job):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self.ticker.unsubscribe(self)
        for card in self._cards.values():
            self.ticker.unsubscribe(card)
        super().destroy()


# ---------------------------------------------------------------------------
#  Widget kit
#
#  Tk's stock widgets are flat colour blocks that cannot carry a gradient, a
#  specular edge or a focus ring, which is why the app chrome never looked
#  related to the clocks.  These are canvases with rendered faces, drawn by
#  the same render_surface as the flip cards.  All are keyboard-operable and
#  sized in dp.
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=4096)
def _measure(font, text):
    """Text width in pixels.  Memoised: the zone list and manager rows
    re-measure on every hover, and each Font() is a Tk round trip."""
    return tkinter.font.Font(font=font).measure(text)


def _linespace(font):
    return tkinter.font.Font(font=font).metrics("linespace")


class TipBubble:
    """The tooltip bubble, shared by widget tooltips and canvas-item hints."""

    def __init__(self, owner, delay=550):
        self.owner, self.delay = owner, delay
        self._job = None
        self._tip = None

    def schedule(self, text, where):
        """Show `text` after the delay; `where()` gives the root x (centre), y."""
        self.hide()
        try:
            self._job = self.owner.after(self.delay, lambda: self._show(text, where))
        except tk.TclError:
            pass

    def _show(self, text, where):
        self._job = None
        try:
            x, y = where()
            tip = tk.Toplevel(self.owner)
            tip.overrideredirect(True)
            tip.wm_attributes("-topmost", True)
            frame = tk.Frame(tip, bg=THEME["hairline"], padx=1, pady=1)
            frame.pack()
            tk.Label(frame, text=text, bg=THEME["surface_hi"], fg=THEME["text"],
                     font=ui_font(9), padx=dp(8), pady=dp(4)).pack()
            tip.update_idletasks()
            tip.geometry(f"+{x - tip.winfo_reqwidth() // 2}+{y}")
            self._tip = tip
        except tk.TclError:
            pass

    def hide(self, _=None):
        if self._job:
            try:
                self.owner.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


class Tooltip:
    """A hint shown after a pause over a widget."""

    def __init__(self, widget, text, delay=600):
        self.widget, self.text = widget, text
        self.bubble = TipBubble(widget, delay)
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.bubble.hide, add="+")
        widget.bind("<ButtonPress>", self.bubble.hide, add="+")
        widget.bind("<Destroy>", self.bubble.hide, add="+")

    def _schedule(self, _=None):
        w = self.widget
        self.bubble.schedule(self.text, lambda: (w.winfo_rootx() + w.winfo_width() // 2,
                                                 w.winfo_rooty() + w.winfo_height() + dp(6)))


# fill, ink, hover fill, hover ink (THEME keys or literal colours; None = none)
_BTN_STYLES = {
    "primary":   ("accent", "accent_ink", "accent_hi", "accent_ink"),
    "secondary": ("surface", "text", "surface_hi", "text"),
    "ghost":     (None, "text_dim", "surface_hi", "text"),
    "danger":    ("surface", "danger", "danger", "#ffffff"),
    "quiet":     (None, "text_faint", None, "text"),
    "ghost_danger": (None, "text_dim", "surface_hi", "danger_hi"),
}


def _tok(v):
    return None if v is None else THEME.get(v, v)


class SurfaceButton(tk.Canvas):

    def __init__(self, parent, text, command, kind="secondary", icon=None,
                 width=None, height=36, font=None, behind=None, radius=9,
                 takefocus=True, tooltip=None, padx=16):
        self._fill, self._ink, self._hfill, self._hink = (_tok(v) for v in _BTN_STYLES[kind])
        self._kind = kind
        self._behind = behind or THEME["bg"]
        self._radius = dp(radius)
        self._command = command
        self._font = font or ui_font(10, "bold")
        self._icon = icon
        self._enabled = True
        self._hover = self._pressed = self._focus = False
        self._photos = {}
        self._text = text
        isz = dp(16)
        tw = _measure(self._font, text) if text else 0
        w = width if width is not None else tw + dp(padx) * 2 + (isz + (dp(8) if text else 0) if icon else 0)
        h = dp(height)
        super().__init__(parent, width=w, height=h, bd=0, highlightthickness=0,
                         bg=self._behind, takefocus=takefocus, cursor="hand2")
        self._cw, self._ch = w, h          # not _w: Tk stores the widget path there
        self._img = self.create_image(0, 0, anchor="nw")
        self._ico = self.create_image(0, 0, anchor="center")
        self._txt = self.create_text(0, 0, text=text, font=self._font, anchor="center")
        self._layout()
        self._paint()
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", lambda e: self._set(pressed=True))
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<FocusIn>", lambda e: self._set(focus=True))
        self.bind("<FocusOut>", lambda e: self._set(focus=False))
        self.bind("<Return>", lambda e: self.invoke())
        self.bind("<space>", lambda e: self.invoke())
        self.bind("<Configure>", self._on_configure)
        if tooltip:
            Tooltip(self, tooltip)

    def _layout(self):
        cx, cy = self._cw // 2, self._ch // 2
        if self._icon and self._text:
            isz = dp(16)
            tw = _measure(self._font, self._text)
            total = isz + dp(8) + tw
            self.coords(self._ico, cx - total // 2 + isz // 2, cy)
            self.coords(self._txt, cx - total // 2 + isz + dp(8) + tw // 2, cy)
        else:
            self.coords(self._ico, cx, cy)
            self.coords(self._txt, cx, cy)

    def _set(self, **kw):
        changed = False
        for k, v in kw.items():
            if getattr(self, "_" + k) != v:
                setattr(self, "_" + k, v)
                changed = True
        if changed:
            self._paint()

    def _paint(self):
        hover = self._hover and self._enabled
        fill = self._hfill if hover and self._hfill else self._fill
        if self._pressed and hover and fill is None:
            fill = THEME["surface"]
        ink = self._hink if hover else self._ink
        if not self._enabled:
            ink = THEME["text_faint"]
        ring = THEME["accent"] if self._focus else None
        if HAS_PIL and (fill or ring):
            base = fill or self._behind
            key = (base, self._pressed and hover, ring, self._cw, self._ch)
            p = self._photos.get(key)
            if p is None:
                strength = 0.55 if self._kind in ("ghost", "quiet") else 0.8
                img = render_surface(self._cw, self._ch, self._radius, base, self._behind,
                                     inset=self._pressed and hover, strength=strength, ring=ring)
                p = photo(img, self)
                if len(self._photos) > 12:
                    self._photos.clear()
                self._photos[key] = p
            self.itemconfigure(self._img, image=p, state="normal")
        elif not HAS_PIL and fill:
            self.configure(bg=fill)
            self.itemconfigure(self._img, image="", state="hidden")
        else:
            self.configure(bg=self._behind)
            self.itemconfigure(self._img, image="", state="hidden")
        self.itemconfigure(self._txt, fill=ink)
        if self._icon and HAS_PIL:
            key = ("icon", self._icon, ink)
            p = self._photos.get(key)
            if p is None:
                p = photo(icon_sprite(self._icon, dp(16), ink), self)
                self._photos[key] = p
            self.itemconfigure(self._ico, image=p)

    def _on_configure(self, e):
        if (e.width, e.height) == (self._cw, self._ch):
            return
        self._cw, self._ch = e.width, e.height
        self._photos = {k: v for k, v in self._photos.items() if k[0] == "icon"}
        self._layout()
        self._paint()

    def _release(self, e):
        inside = 0 <= e.x < self._cw and 0 <= e.y < self._ch
        self._set(pressed=False, hover=inside)
        if inside:
            self.invoke()

    def invoke(self):
        if self._enabled and self._command:
            self._command()

    def set_text(self, text):
        self._text = text
        self.itemconfigure(self._txt, text=text)
        self._layout()

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "")
        self._paint()


def make_button(parent, text, command, kind="secondary", **kw):
    return SurfaceButton(parent, text, command, kind=kind, **kw)


class IconButton(SurfaceButton):
    """A square, icon-only button: invisible until hovered, like a toolbar."""

    def __init__(self, parent, icon, command, tooltip=None, size=30, behind=None,
                 danger=False):
        super().__init__(parent, "", command, kind="ghost", icon=icon, width=dp(size),
                         height=size, behind=behind, radius=8, tooltip=tooltip)
        if danger:
            self._hink = THEME["danger"]
        self._ink = THEME["text_faint"]
        self._paint()


class ToggleSwitch(tk.Canvas):
    """An on/off switch: inset track, raised knob, a short eased slide."""

    DURATION = 0.14

    def __init__(self, parent, variable, command=None, behind=None, **kw):
        self.var = variable
        self._command = command
        self._behind = behind or THEME["surface"]
        self._enabled = True
        self._focus = False
        self._pos = 1.0 if variable.get() else 0.0
        self._target = self._pos
        self._t0 = None
        self._start = self._pos
        self._job = None
        self._photos = {}
        self.W, self.H = dp(40), dp(22)
        super().__init__(parent, width=self.W, height=self.H, bd=0,
                         highlightthickness=0, bg=self._behind, takefocus=True,
                         cursor="hand2", **kw)
        self._track = self.create_image(0, 0, anchor="nw")
        self._knob = self.create_image(0, 0, anchor="nw")
        self.bind("<Button-1>", self._clicked)
        self.bind("<space>", self._clicked)
        self.bind("<Return>", self._clicked)
        self.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.bind("<FocusOut>", lambda e: self._set_focus(False))
        self._trace = variable.trace_add("write", lambda *_: self._sync())
        self.bind("<Destroy>", self._teardown)
        self._draw()

    def _teardown(self, _=None):
        """Drop the pending frame and the variable trace.  winfo_exists()
        inside the callback is not enough: destroying the widget deletes the
        Tcl command behind it, so a queued `after` fires against nothing."""
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        if self._trace is not None:
            try:
                self.var.trace_remove("write", self._trace)
            except (tk.TclError, ValueError):
                pass
            self._trace = None

    def _photo(self, key, img_fn):
        p = self._photos.get(key)
        if p is None:
            p = photo(img_fn(), self)
            if len(self._photos) > 20:
                self._photos.clear()
            self._photos[key] = p
        return p

    def _set_focus(self, on):
        self._focus = on
        self._draw()

    def _draw(self):
        if not HAS_PIL:
            self.configure(bg=THEME["accent"] if self.var.get() else THEME["surface_hi"])
            return
        on = THEME["accent"] if self._enabled else THEME["surface_hi"]
        fill = lerp(THEME["field"], on, quantise(self._pos))
        ring = THEME["accent_hi"] if self._focus else None
        self.itemconfigure(self._track, image=self._photo(
            ("t", fill, ring), lambda: render_surface(self.W, self.H, self.H // 2, fill,
                                                      self._behind, inset=True,
                                                      strength=0.6, ring=ring)))
        d = self.H - dp(6)
        knob = THEME["text"] if self._enabled else THEME["text_faint"]
        self.itemconfigure(self._knob, image=self._photo(
            ("k", knob, fill), lambda: render_surface(d, d, d // 2, knob, fill, strength=0.5)))
        self.coords(self._knob, dp(3) + int((self.W - d - dp(6)) * self._pos), dp(3))

    def _clicked(self, _=None):
        if not self._enabled:
            return "break"
        self.var.set(not self.var.get())
        if self._command:
            self._command()
        return "break"

    def _sync(self):
        self._target = 1.0 if self.var.get() else 0.0
        if abs(self._target - self._pos) < 0.01:
            return
        self._t0 = time.perf_counter()
        self._start = self._pos
        if self._job is None:
            self._animate()

    def _animate(self):
        self._job = None
        if not self.winfo_exists():
            return
        t = (time.perf_counter() - self._t0) / self.DURATION
        if t >= 1.0:
            self._pos = self._target
            self._draw()
            return
        self._pos = self._start + (self._target - self._start) * ease_out_cubic(t)
        self._draw()
        self._job = self.after(FRAME_MS, self._animate)

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "", takefocus=bool(enabled))
        self._photos.clear()
        self._draw()


class Segmented(tk.Canvas):
    """A segmented control: one choice from a few, all visible at once."""

    def __init__(self, parent, options, variable, command=None, behind=None,
                 height=30, seg_min=0):
        self.options = list(options)              # [(value, label), ...]
        self.var = variable
        self._command = command
        self._behind = behind or THEME["bg"]
        self._focus = False
        self._photos = {}
        font = ui_font(9, "bold")
        self._font = font
        seg = max(max(_measure(font, lbl) for _, lbl in self.options) + dp(22), dp(seg_min))
        self.seg_w = seg
        self.H = dp(height)
        self.W = seg * len(self.options) + dp(4)
        super().__init__(parent, width=self.W, height=self.H, bd=0,
                         highlightthickness=0, bg=self._behind, takefocus=True,
                         cursor="hand2")
        self._track = self.create_image(0, 0, anchor="nw")
        self._thumb = self.create_image(0, 0, anchor="nw")
        self._labels = [self.create_text(dp(2) + seg * i + seg // 2, self.H // 2,
                                         text=lbl, font=font, anchor="center")
                        for i, (_, lbl) in enumerate(self.options)]
        self.bind("<Button-1>", self._click)
        self.bind("<Left>", lambda e: self._move(-1))
        self.bind("<Right>", lambda e: self._move(1))
        self.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.bind("<FocusOut>", lambda e: self._set_focus(False))
        self._trace = variable.trace_add("write", lambda *_: self._draw())
        self.bind("<Destroy>", self._teardown)
        self._draw()

    def _teardown(self, _=None):
        if self._trace is not None:
            try:
                self.var.trace_remove("write", self._trace)
            except (tk.TclError, ValueError):
                pass
            self._trace = None

    def _set_focus(self, on):
        self._focus = on
        self._draw()

    def _index(self):
        vals = [v for v, _ in self.options]
        return vals.index(self.var.get()) if self.var.get() in vals else 0

    def _draw(self):
        i = self._index()
        if HAS_PIL:
            ring = THEME["accent"] if self._focus else None
            key = ("track", ring)
            if key not in self._photos:
                self._photos[key] = photo(render_surface(self.W, self.H, dp(8), THEME["field"],
                                                         self._behind, inset=True,
                                                         strength=0.5, ring=ring), self)
            self.itemconfigure(self._track, image=self._photos[key])
            if "thumb" not in self._photos:
                self._photos["thumb"] = photo(render_surface(
                    self.seg_w, self.H - dp(4), dp(6), THEME["surface_hi"], THEME["field"],
                    strength=0.7), self)
            self.itemconfigure(self._thumb, image=self._photos["thumb"])
            self.coords(self._thumb, dp(2) + self.seg_w * i, dp(2))
        for j, item in enumerate(self._labels):
            self.itemconfigure(item, fill=THEME["text"] if j == i else THEME["text_dim"])

    def _set_index(self, i):
        i = max(0, min(len(self.options) - 1, i))
        value = self.options[i][0]
        if value != self.var.get():
            self.var.set(value)
            if self._command:
                self._command()

    def _click(self, e):
        self.focus_set()
        self._set_index(int((e.x - dp(2)) // self.seg_w))

    def _move(self, d):
        self._set_index(self._index() + d)
        return "break"


class Slider(tk.Canvas):
    """A horizontal slider with an accent fill and a raised knob."""

    def __init__(self, parent, variable, lo, hi, command=None, width=200,
                 behind=None, fmt=None):
        self.var, self.lo, self.hi = variable, lo, hi
        self._command = command
        self._behind = behind or THEME["bg"]
        self._fmt = fmt or (lambda v: f"{v:.2f}")
        self._photos = {}
        self._focus = False
        self.W, self.H = dp(width), dp(26)
        super().__init__(parent, width=self.W, height=self.H, bd=0,
                         highlightthickness=0, bg=self._behind, takefocus=True,
                         cursor="hand2")
        self.track_w = self.W - dp(52)
        self._track = self.create_image(dp(8), self.H // 2, anchor="w")
        self._fill = self.create_image(dp(8), self.H // 2, anchor="w")
        self._knob = self.create_image(0, self.H // 2, anchor="center")
        self._label = self.create_text(self.W, self.H // 2, anchor="e",
                                       font=ui_font(9), fill=THEME["text_dim"])
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._press)
        self.bind("<Left>", lambda e: self._nudge(-1))
        self.bind("<Right>", lambda e: self._nudge(1))
        self.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.bind("<FocusOut>", lambda e: self._set_focus(False))
        self._trace = variable.trace_add("write", lambda *_: self._draw())
        self.bind("<Destroy>", self._teardown)
        self._draw()

    def _teardown(self, _=None):
        if self._trace is not None:
            try:
                self.var.trace_remove("write", self._trace)
            except (tk.TclError, ValueError):
                pass
            self._trace = None

    def _set_focus(self, on):
        self._focus = on
        self._draw()

    def _frac(self):
        try:
            v = float(self.var.get())
        except (tk.TclError, ValueError):
            v = self.lo
        return (min(self.hi, max(self.lo, v)) - self.lo) / ((self.hi - self.lo) or 1)

    def _draw(self):
        f = self._frac()
        tw, th = self.track_w - dp(16), dp(4)
        if HAS_PIL:
            if "track" not in self._photos:
                self._photos["track"] = photo(render_surface(tw, th, th // 2, THEME["field"],
                                                             self._behind, inset=True,
                                                             strength=0.4, ss=4), self)
            self.itemconfigure(self._track, image=self._photos["track"])
            fw = max(th, int(tw * f))
            key = ("fill", fw)
            if key not in self._photos:
                if len(self._photos) > 40:
                    self._photos = {k: v for k, v in self._photos.items() if k in ("track",)}
                self._photos[key] = photo(render_surface(fw, th, th // 2, THEME["accent"],
                                                         self._behind, strength=0.3, ss=4), self)
            self.itemconfigure(self._fill, image=self._photos[key])
            kd = dp(16)
            kkey = ("knob", self._focus)
            if kkey not in self._photos:
                self._photos[kkey] = photo(render_surface(
                    kd, kd, kd // 2, THEME["text"], self._behind, strength=0.5,
                    ring=THEME["accent"] if self._focus else None), self)
            self.itemconfigure(self._knob, image=self._photos[kkey])
        self.coords(self._knob, dp(8) + int(tw * f), self.H // 2)
        try:
            self.itemconfigure(self._label, text=self._fmt(float(self.var.get())))
        except (tk.TclError, ValueError):
            pass

    def _set(self, v):
        v = round(min(self.hi, max(self.lo, v)), 2)
        if v != self.var.get():
            self.var.set(v)
            if self._command:
                self._command()

    def _press(self, e):
        self.focus_set()
        tw = self.track_w - dp(16)
        self._set(self.lo + (self.hi - self.lo) * (e.x - dp(8)) / max(1, tw))

    def _nudge(self, d):
        self._set(float(self.var.get()) + d * (self.hi - self.lo) / 40)
        return "break"


class SwatchRow(tk.Canvas):
    """A row of colour dots; the chosen one wears a ring."""

    def __init__(self, parent, colors, variable, command=None, behind=None, size=22):
        self.colors = list(colors)                 # [(hex, name)]
        self.var = variable
        self._command = command
        self._behind = behind or THEME["bg"]
        self.S, self.G = dp(size), dp(7)
        self._photos = {}
        W = len(self.colors) * (self.S + self.G) - self.G + dp(4)
        super().__init__(parent, width=W, height=self.S + dp(4), bd=0,
                         highlightthickness=0, bg=self._behind, takefocus=True,
                         cursor="hand2")
        self._items = []
        for i, (c, name) in enumerate(self.colors):
            item = self.create_image(dp(2) + i * (self.S + self.G), dp(2), anchor="nw")
            self._items.append(item)
        self.bind("<Button-1>", self._click)
        self.bind("<Left>", lambda e: self._move(-1))
        self.bind("<Right>", lambda e: self._move(1))
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._tip_hide())
        self._tip = None
        self._trace = variable.trace_add("write", lambda *_: self._draw())
        self.bind("<Destroy>", self._teardown)
        self._draw()

    def _teardown(self, _=None):
        self._tip_hide()
        if self._trace is not None:
            try:
                self.var.trace_remove("write", self._trace)
            except (tk.TclError, ValueError):
                pass
            self._trace = None

    @staticmethod
    @functools.lru_cache(maxsize=128)
    def _dot(color, size, selected, behind):
        ss = 4
        S = size * ss
        img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        inset = int(S * 0.20) if selected else int(S * 0.06)
        if selected:
            ring = THEME["text"] if luminance(behind) < 0.4 else "#1c1e22"
            d.ellipse([0, 0, S - 1, S - 1], outline=rgba(ring), width=int(ss * 1.6))
        d.ellipse([inset, inset, S - 1 - inset, S - 1 - inset], fill=rgba(color))
        # A hairline keeps dark swatches visible on a dark background.
        d.ellipse([inset, inset, S - 1 - inset, S - 1 - inset],
                  outline=(255, 255, 255, 38), width=ss)
        return img.resize((size, size), Image.LANCZOS)

    def _draw(self):
        cur = (self.var.get() or "").lower()
        for (c, _), item in zip(self.colors, self._items):
            sel = c.lower() == cur
            key = (c, sel)
            if key not in self._photos and HAS_PIL:
                self._photos[key] = photo(self._dot(c, self.S, sel, self._behind), self)
            if HAS_PIL:
                self.itemconfigure(item, image=self._photos[key])

    def _hit(self, x):
        i = int((x - dp(2)) // (self.S + self.G))
        return i if 0 <= i < len(self.colors) else None

    def _click(self, e):
        self.focus_set()
        i = self._hit(e.x)
        if i is not None:
            self._choose(i)

    def _choose(self, i):
        self.var.set(self.colors[i][0])
        if self._command:
            self._command()

    def _move(self, d):
        vals = [c.lower() for c, _ in self.colors]
        cur = (self.var.get() or "").lower()
        i = vals.index(cur) if cur in vals else -1
        self._choose(max(0, min(len(vals) - 1, i + d)))
        return "break"

    def _motion(self, e):
        i = self._hit(e.x)
        name = self.colors[i][1] if i is not None else None
        if name != getattr(self, "_tip_name", None):
            self._tip_hide()
            self._tip_name = name
            if name:
                try:
                    tip = tk.Toplevel(self)
                    tip.overrideredirect(True)
                    tip.wm_attributes("-topmost", True)
                    tk.Label(tip, text=name, bg=THEME["surface_hi"], fg=THEME["text"],
                             font=ui_font(8), padx=dp(6), pady=dp(2)).pack()
                    tip.geometry(f"+{e.x_root + dp(10)}+{e.y_root + dp(16)}")
                    self._tip = tip
                except tk.TclError:
                    pass

    def _tip_hide(self):
        self._tip_name = None
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


class TextField(tk.Frame):
    """A flat input with a hairline border that lights up in the accent."""

    def __init__(self, parent, variable=None, icon=None, behind=None, width=24,
                 placeholder=None):
        behind = behind or THEME["bg"]
        super().__init__(parent, bg=THEME["hairline"], padx=1, pady=1)
        inner = tk.Frame(self, bg=THEME["field"])
        inner.pack(fill="both", expand=True)
        self._icon_photo = None
        if icon and HAS_PIL:
            self._icon_photo = photo(icon_sprite(icon, dp(15), THEME["text_faint"]), self)
            tk.Label(inner, image=self._icon_photo, bg=THEME["field"]).pack(
                side="left", padx=(dp(9), 0))
        self.var = variable or tk.StringVar(self)
        self.entry = tk.Entry(inner, textvariable=self.var, bg=THEME["field"],
                              fg=THEME["text"], insertbackground=THEME["accent"],
                              relief="flat", bd=0, highlightthickness=0,
                              font=ui_font(10), width=width,
                              selectbackground=THEME["select"],
                              selectforeground=THEME["text"])
        self.entry.pack(side="left", fill="x", expand=True, padx=dp(9), pady=dp(7))
        self.entry.bind("<FocusIn>", lambda e: self.configure(bg=THEME["accent"]), add="+")
        self.entry.bind("<FocusOut>", lambda e: self.configure(bg=THEME["hairline"]), add="+")
        self._hint = None
        if placeholder:
            # Tk entries have no placeholder; a label laid over the entry
            # stands in, and clicks fall through to the entry beneath it.
            self._hint = tk.Label(inner, text=placeholder, bg=THEME["field"],
                                  fg=THEME["text_faint"], font=ui_font(10), bd=0,
                                  cursor="xterm")
            self._hint.bind("<Button-1>", lambda e: self.entry.focus_set())
            trace = self.var.trace_add("write", lambda *_: self._sync_hint())
            self.bind("<Destroy>", lambda e: self._untrace(trace), add="+")
            self.entry.bind("<FocusIn>", lambda e: self._sync_hint(), add="+")
            self.entry.bind("<FocusOut>", lambda e: self._sync_hint(), add="+")
            self.after_idle(self._sync_hint)

    def _untrace(self, trace):
        try:
            self.var.trace_remove("write", trace)
        except (tk.TclError, ValueError):
            pass

    def _sync_hint(self):
        if self._hint is None:
            return
        try:
            if self.var.get():
                self._hint.place_forget()
            else:
                self._hint.place(in_=self.entry, x=0, rely=0.5, anchor="w")
        except tk.TclError:
            pass


class ThinScrollbar(tk.Canvas):
    """A slim overlay-style scrollbar: a rounded thumb, no arrows or trough.
    Speaks the same set()/command protocol as tk.Scrollbar."""

    def __init__(self, parent, command, bg=None, width=8):
        super().__init__(parent, width=dp(width), bd=0, highlightthickness=0,
                         bg=bg or THEME["bg"], takefocus=0)
        self._command = command
        self._lo, self._hi = 0.0, 1.0
        self._drag = None
        self._thumb = self.create_line(0, 0, 0, 0, fill=THEME["surface_hi"],
                                       width=dp(width) - dp(2), capstyle="round")
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", lambda e: self.itemconfigure(self._thumb, fill=THEME["text_faint"]))
        self.bind("<Leave>", lambda e: self.itemconfigure(self._thumb, fill=THEME["surface_hi"]))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._move)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))

    def set(self, lo, hi):
        self._lo, self._hi = float(lo), float(hi)
        self._draw()

    def _draw(self):
        h = max(1, self.winfo_height())
        x = self.winfo_width() // 2
        cap = (dp(8) - dp(2)) // 2 + 1
        y1 = cap + (h - 2 * cap) * self._lo
        y2 = cap + (h - 2 * cap) * self._hi
        self.coords(self._thumb, x, y1, x, max(y1 + 1, y2))

    def _press(self, e):
        h = max(1, self.winfo_height())
        frac = e.y / h
        if not (self._lo <= frac <= self._hi):
            # click in the track: centre the thumb there
            span = self._hi - self._lo
            self._command("moveto", max(0.0, frac - span / 2))
        self._drag = (e.y, self._lo)

    def _move(self, e):
        if self._drag is None:
            return
        y0, lo0 = self._drag
        self._command("moveto", lo0 + (e.y - y0) / max(1, self.winfo_height()))


class ScrollFrame(tk.Frame):
    """A vertically scrollable container.

    The mouse wheel is captured only while the pointer is over it.  Binding it
    application-wide meant that scrolling anywhere - the timezone list in the
    editor, say - also scrolled the manager's clock list.
    """

    def __init__(self, parent, height=250, bg=None, width=None, **kw):
        bg = bg or THEME["bg"]
        super().__init__(parent, bg=bg, **kw)
        self._cv = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0,
                             height=height, takefocus=0, width=width or 1)
        self._sb = ThinScrollbar(self, self._cv.yview, bg=bg)
        self._cv.configure(yscrollcommand=self._on_scroll)
        self._cv.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self._cv, bg=bg)
        self._win = self._cv.create_window(0, 0, anchor="nw", window=self.body)
        self.body.bind("<Configure>", self._resize)
        self._cv.bind("<Configure>",
                      lambda e: self._cv.itemconfigure(self._win, width=e.width))
        self.bind("<Enter>", self._capture)
        self.bind("<Leave>", self._release)
        self._captured = False

    def _capture(self, _=None):
        if self._captured:
            return
        self._captured = True
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind_all(seq, self._wheel)

    def _release(self, _=None):
        try:
            px, py = self.winfo_pointerxy()
            if (self.winfo_rootx() <= px < self.winfo_rootx() + self.winfo_width() and
                    self.winfo_rooty() <= py < self.winfo_rooty() + self.winfo_height()):
                return                      # moved onto a child, still inside
        except tk.TclError:
            pass
        self._captured = False
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.unbind_all(seq)

    def _on_scroll(self, lo, hi):
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            self._sb.pack_forget()
        else:
            self._sb.pack(side="right", fill="y", padx=(dp(4), 0))
        self._sb.set(lo, hi)

    def _resize(self, _):
        self._cv.configure(scrollregion=self._cv.bbox("all"))

    def _wheel(self, e):
        delta = -1 if getattr(e, "num", 0) == 5 or getattr(e, "delta", 0) < 0 else 1
        self._cv.yview_scroll(-delta, "units")

    def set_height(self, h):
        self._cv.configure(height=max(1, int(h)))

    def scroll_to(self, widget):
        """Scroll just enough to show `widget` whole."""
        try:
            self.update_idletasks()
            total = max(1, self.body.winfo_height())
            view = self._cv.winfo_height()
            top = self._cv.canvasy(0)
            y, h = widget.winfo_y(), widget.winfo_height()
            if y < top:
                self._cv.yview_moveto(y / total)
            elif y + h > top + view:
                self._cv.yview_moveto((y + h - view) / total)
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
#  Time zones
#
#  Built lazily - enumerating the tz database touches hundreds of files and
#  used to run at import, slowing every launch for a list only the editor uses.
# ---------------------------------------------------------------------------
_REGIONS = ("Africa", "America", "Antarctica", "Arctic", "Asia", "Atlantic",
            "Australia", "Europe", "Indian", "Pacific")


class ZoneEntry(tuple):
    __slots__ = ()

    def __new__(cls, key, city, region):
        return tuple.__new__(cls, (key, city, region))

    key = property(lambda self: self[0])
    city = property(lambda self: self[1])
    region = property(lambda self: self[2])


def _read_tab(name):
    """A table from the tz database: the tzdata package first (it is what
    Windows has), then the system copy."""
    try:
        import importlib.resources
        return (importlib.resources.files("tzdata") / "zoneinfo" / name).read_text(encoding="utf-8")
    except Exception:
        pass
    for base in ("/usr/share/zoneinfo", "/usr/lib/zoneinfo", "/usr/share/lib/zoneinfo"):
        try:
            with open(os.path.join(base, name), encoding="utf-8") as f:
                return f.read()
        except OSError:
            continue
    return ""


@functools.lru_cache(maxsize=1)
def _zone_countries():
    """{zone key: country name} from zone.tab + iso3166.tab - one canonical
    zone per region, so legacy aliases (Asia/Calcutta beside Asia/Kolkata)
    do not show up as duplicates."""
    countries = {}
    for ln in _read_tab("iso3166.tab").splitlines():
        if ln and not ln.startswith("#") and "\t" in ln:
            code, name = ln.split("\t", 1)
            countries[code.strip()] = name.strip()
    out = {}
    for ln in _read_tab("zone.tab").splitlines():
        if ln and not ln.startswith("#"):
            cols = ln.split("\t")
            if len(cols) >= 3:
                out[cols[2].strip()] = countries.get(cols[0].strip(), cols[0].strip())
    return out


def _entry(key, country=None):
    if key == "UTC":
        return ZoneEntry("UTC", "UTC", "Coordinated Universal Time")
    parts = key.split("/")
    city = parts[-1].replace("_", " ")
    region = country or " \u00b7 ".join(p.replace("_", " ") for p in parts[:-1]) or key
    return ZoneEntry(key, city, region)


# Major cities that share another city's zone.  The tz database names one
# city per zone, so "San Francisco", "Beijing" or "Mumbai" were unfindable.
CITY_ZONES = [
    ("San Francisco", "United States", "America/Los_Angeles"),
    ("Seattle", "United States", "America/Los_Angeles"),
    ("San Diego", "United States", "America/Los_Angeles"),
    ("Las Vegas", "United States", "America/Los_Angeles"),
    ("Portland", "United States", "America/Los_Angeles"),
    ("Salt Lake City", "United States", "America/Denver"),
    ("Dallas", "United States", "America/Chicago"),
    ("Houston", "United States", "America/Chicago"),
    ("Austin", "United States", "America/Chicago"),
    ("Minneapolis", "United States", "America/Chicago"),
    ("Boston", "United States", "America/New_York"),
    ("Washington", "United States", "America/New_York"),
    ("Philadelphia", "United States", "America/New_York"),
    ("Atlanta", "United States", "America/New_York"),
    ("Miami", "United States", "America/New_York"),
    ("Montreal", "Canada", "America/Toronto"),
    ("Ottawa", "Canada", "America/Toronto"),
    ("Calgary", "Canada", "America/Edmonton"),
    ("Rio de Janeiro", "Brazil", "America/Sao_Paulo"),
    ("Brasilia", "Brazil", "America/Sao_Paulo"),
    ("Manchester", "United Kingdom", "Europe/London"),
    ("Edinburgh", "United Kingdom", "Europe/London"),
    ("Munich", "Germany", "Europe/Berlin"),
    ("Frankfurt", "Germany", "Europe/Berlin"),
    ("Hamburg", "Germany", "Europe/Berlin"),
    ("Barcelona", "Spain", "Europe/Madrid"),
    ("Milan", "Italy", "Europe/Rome"),
    ("Geneva", "Switzerland", "Europe/Zurich"),
    ("Lyon", "France", "Europe/Paris"),
    ("Rotterdam", "Netherlands", "Europe/Amsterdam"),
    ("St Petersburg", "Russia", "Europe/Moscow"),
    ("Tel Aviv", "Israel", "Asia/Jerusalem"),
    ("Abu Dhabi", "United Arab Emirates", "Asia/Dubai"),
    ("Mumbai", "India", "Asia/Kolkata"),
    ("New Delhi", "India", "Asia/Kolkata"),
    ("Bengaluru", "India", "Asia/Kolkata"),
    ("Chennai", "India", "Asia/Kolkata"),
    ("Hyderabad", "India", "Asia/Kolkata"),
    ("Hanoi", "Vietnam", "Asia/Ho_Chi_Minh"),
    ("Beijing", "China", "Asia/Shanghai"),
    ("Shenzhen", "China", "Asia/Shanghai"),
    ("Guangzhou", "China", "Asia/Shanghai"),
    ("Osaka", "Japan", "Asia/Tokyo"),
    ("Kyoto", "Japan", "Asia/Tokyo"),
    ("Busan", "South Korea", "Asia/Seoul"),
    ("Canberra", "Australia", "Australia/Sydney"),
    ("Wellington", "New Zealand", "Pacific/Auckland"),
    ("Cape Town", "South Africa", "Africa/Johannesburg"),
]

# Former names, nicknames and zone abbreviations -> zone, for search only.
SEARCH_ALIASES = {
    "kiev": "Europe/Kyiv", "calcutta": "Asia/Kolkata", "bombay": "Asia/Kolkata",
    "saigon": "Asia/Ho_Chi_Minh", "peking": "Asia/Shanghai", "rangoon": "Asia/Yangon",
    "nyc": "America/New_York", "sf": "America/Los_Angeles", "la": "America/Los_Angeles",
    "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles", "pt": "America/Los_Angeles",
    "mst": "America/Denver", "mdt": "America/Denver", "cst": "America/Chicago",
    "cdt": "America/Chicago", "est": "America/New_York", "edt": "America/New_York",
    "et": "America/New_York", "bst": "Europe/London", "gmt": "Europe/London",
    "cet": "Europe/Berlin", "cest": "Europe/Berlin", "eet": "Europe/Athens",
    "ist": "Asia/Kolkata", "jst": "Asia/Tokyo", "kst": "Asia/Seoul",
    "hkt": "Asia/Hong_Kong", "sgt": "Asia/Singapore", "aest": "Australia/Sydney",
    "aedt": "Australia/Sydney", "nzst": "Pacific/Auckland", "nzdt": "Pacific/Auckland",
}


@functools.lru_cache(maxsize=1)
def zone_catalog():
    """Canonical city zones, sorted by city, with their country."""
    table = _zone_countries()
    try:
        available = zoneinfo.available_timezones()
    except Exception:
        available = set()
    if table:
        keys = {k for k in table if not available or k in available}
    else:
        keys = {z for z in available if z.split("/")[0] in _REGIONS and "/" in z}
    keys.add("UTC")
    entries = [_entry(k, table.get(k)) for k in keys]
    entries += [ZoneEntry(z, city, country) for city, country, z in CITY_ZONES
                if not available or z in available]
    return sorted(entries, key=lambda e: (e.city.lower(), e.region))


def entry_for(key, city=None):
    """The row for a zone - preferring one whose city matches `city`, so a
    clock labelled San Francisco reopens on San Francisco, not Los Angeles."""
    fallback = None
    for e in zone_catalog():
        if e.key == key:
            if city and e.city.lower() == city.lower():
                return e
            if fallback is None or e.city == city_of(key):
                fallback = e
    return fallback or _entry(key)


def all_zone_keys():
    return [e.key for e in zone_catalog()]


@functools.lru_cache(maxsize=2048)
def _offset_aliases(key, hour_bucket):
    z = zone_or_none(key)
    if z is None:
        return ()
    label = utc_offset_label(datetime.datetime.now(z)).lower().replace("−", "-")
    tail = label[3:] or "+0"
    return (label, "utc" + tail, tail)


def _looks_like_offset(tok):
    t = tok.replace("−", "-")
    return (t[:1] in "+-" and t[1:2].isdigit()) or t.startswith(("gmt+", "gmt-", "utc+", "utc-"))


def search_zones(query, catalog=None):
    """Zones matching every word of the query, best first.

    Matches city, region and key with spaces and underscores treated alike
    ("new york" finds America/New_York), and UTC offsets ("+9", "gmt-3:30").
    """
    catalog = zone_catalog() if catalog is None else catalog
    q = query.strip().lower().replace("_", " ")
    if not q:
        return list(catalog)
    tokens = q.split()
    bucket = int(time.time() // 3600)
    ranked = []
    alias = SEARCH_ALIASES.get(q)
    for e in catalog:
        if alias and e.key == alias and e.city == city_of(alias):
            ranked.append((-1, e))
            continue
        blob = f"{e.city} {e.region} {e.key}".lower().replace("_", " ")
        ok = True
        for tok in tokens:
            if tok in blob:
                continue
            if _looks_like_offset(tok):
                t = tok.replace("−", "-")
                if any(a.startswith(t) for a in _offset_aliases(e.key, bucket)):
                    continue
            ok = False
            break
        if ok:
            city, region = e.city.lower(), e.region.lower()
            if city == q or region == q:
                rank = 0
            elif city.startswith(q):
                rank = 1
            elif region.startswith(q):
                rank = 2
            elif q in city:
                rank = 3
            else:
                rank = 4
            ranked.append((rank, e))
    ranked.sort(key=lambda r: (r[0], r[1].city.lower()))
    return [e for _, e in ranked]


class ZoneList(tk.Canvas):
    """A virtualised list of zones: only the visible rows exist.

    Each row shows the city, its region, the UTC offset and the time there now
    - a raw 'Area/City' string list told the user none of that.
    """

    def __init__(self, parent, on_pick, rows=7, width=300, behind=None):
        self._behind = behind or THEME["bg"]
        self.on_pick = on_pick
        self.rows = rows
        self.RH = dp(36)
        self.W = dp(width)
        super().__init__(parent, width=self.W, height=self.RH * rows, bd=0,
                         highlightthickness=0, bg=THEME["field"], takefocus=True)
        self.entries = []
        self.top = 0
        self.sel = None
        self.hover = None
        self._photos = {}
        self._pool = []
        for i in range(rows):
            y = i * self.RH
            bg = self.create_image(dp(4), y + dp(2), anchor="nw", state="hidden")
            bar = self.create_rectangle(dp(4), y + dp(9), dp(6), y + self.RH - dp(9),
                                        fill=THEME["accent"], outline="", state="hidden")
            city = self.create_text(dp(16), y + self.RH // 2, anchor="w",
                                    font=ui_font(10, "bold"), fill=THEME["text"])
            region = self.create_text(dp(16), y + self.RH // 2, anchor="w",
                                      font=ui_font(9), fill=THEME["text_faint"])
            right = self.create_text(self.W - dp(14), y + self.RH // 2, anchor="e",
                                     font=ui_font(9), fill=THEME["text_dim"])
            self._pool.append((bg, bar, city, region, right))
        self._thumb = self.create_rectangle(0, 0, 0, 0, fill=THEME["surface_hi"],
                                            outline="", state="hidden")
        self._empty = self.create_text(self.W // 2, self.RH * rows // 2, anchor="center",
                                       text="No matching time zones", font=ui_font(10),
                                       fill=THEME["text_faint"], state="hidden")
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._set_hover(None))
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind(seq, self._wheel)
        self.bind("<Up>", lambda e: self._step(-1))
        self.bind("<Down>", lambda e: self._step(1))
        self.bind("<Prior>", lambda e: self._step(-rows))
        self.bind("<Next>", lambda e: self._step(rows))
        self.bind("<Home>", lambda e: self._jump(0))
        self.bind("<End>", lambda e: self._jump(len(self.entries) - 1))

    def _row_photo(self, kind):
        if kind not in self._photos and HAS_PIL:
            fill = THEME["select"] if kind == "sel" else THEME["surface"]
            self._photos[kind] = photo(render_surface(
                self.W - dp(8), self.RH - dp(4), dp(7), fill, THEME["field"],
                strength=0.35), self)
        return self._photos.get(kind)

    def set_entries(self, entries, select_key=None, select_entry=None):
        self.entries = list(entries)
        self.sel = None
        if select_entry is not None and select_entry in self.entries:
            self.sel = self.entries.index(select_entry)
        elif select_key:
            for i, e in enumerate(self.entries):
                if e.key == select_key:
                    self.sel = i
                    break
        self.top = 0
        if self.sel is not None:
            self.top = max(0, min(self.sel - self.rows // 2, len(self.entries) - self.rows))
        self.render()

    def selected(self):
        return self.entries[self.sel] if self.sel is not None and self.sel < len(self.entries) else None

    def render(self):
        n = len(self.entries)
        self.itemconfigure(self._empty, state="normal" if n == 0 else "hidden")
        for i, (bg, bar, city, region, right) in enumerate(self._pool):
            idx = self.top + i
            if idx >= n:
                for item in (bg, bar, city, region, right):
                    self.itemconfigure(item, state="hidden")
                continue
            e = self.entries[idx]
            self.itemconfigure(city, text=e.city, state="normal")
            cw = _measure(ui_font(10, "bold"), e.city)
            self.coords(region, dp(16) + cw + dp(8), i * self.RH + self.RH // 2)
            self.itemconfigure(region, text=e.region, state="normal")
            z = zone_or_none(e.key)
            if z is not None:
                now = datetime.datetime.now(z)
                off = utc_offset_label(now)
                self.itemconfigure(right, text=f"{off}   {now:%H:%M}", state="normal")
            else:
                self.itemconfigure(right, text="", state="hidden")
            kind = "sel" if idx == self.sel else ("hover" if idx == self.hover else None)
            img = self._row_photo(kind) if kind else None
            if img is not None:
                self.itemconfigure(bg, image=img, state="normal")
            else:
                self.itemconfigure(bg, state="hidden")
            self.itemconfigure(bar, state="normal" if idx == self.sel else "hidden")
        if n > self.rows:
            h = self.RH * self.rows
            th = max(dp(24), int(h * self.rows / n))
            ty = int((h - th) * self.top / max(1, n - self.rows))
            self.coords(self._thumb, self.W - dp(5), ty + dp(2), self.W - dp(2), ty + th - dp(2))
            self.itemconfigure(self._thumb, state="normal")
        else:
            self.itemconfigure(self._thumb, state="hidden")

    def _index_at(self, y):
        idx = self.top + int(y // self.RH)
        return idx if 0 <= idx < len(self.entries) else None

    def _set_hover(self, idx):
        if idx != self.hover:
            self.hover = idx
            self.render()

    def _motion(self, e):
        self._set_hover(self._index_at(e.y))

    def _click(self, e):
        self.focus_set()
        idx = self._index_at(e.y)
        if idx is not None:
            self.select(idx)

    def select(self, idx, notify=True):
        if not self.entries:
            return
        idx = max(0, min(len(self.entries) - 1, idx))
        self.sel = idx
        if idx < self.top:
            self.top = idx
        elif idx >= self.top + self.rows:
            self.top = idx - self.rows + 1
        self.render()
        if notify:
            self.on_pick(self.entries[idx])

    def _step(self, d):
        self.select((self.sel if self.sel is not None else -1) + d)
        return "break"

    def _jump(self, idx):
        self.select(idx)
        return "break"

    def _wheel(self, e):
        d = -1 if getattr(e, "num", 0) == 5 or getattr(e, "delta", 0) < 0 else 1
        self.top = max(0, min(len(self.entries) - self.rows, self.top - d * 2))
        self.top = max(0, self.top)
        self.render()
        return "break"


# ---------------------------------------------------------------------------
#  Dialogs
# ---------------------------------------------------------------------------
class Dialog(tk.Toplevel):
    """A dialog that is always visible, modal unless it says otherwise.

    Two bugs lived here: dialogs were made transient to the manager, so with
    the manager hidden in the tray the dialog was hidden too - while holding
    the input grab, which froze the app.  And they were not topmost, so they
    opened underneath the always-on-top clocks.
    """

    modal = True

    def __init__(self, app, title, anchor=None):
        super().__init__(app)
        self.withdraw()
        self.app = app
        self._anchor = anchor
        self.title(title)
        self.configure(bg=THEME["bg"])
        self.resizable(False, False)
        apply_window_icon(self)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda e: self.cancel())

    def present(self):
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        a = self._anchor
        mons = self.app.monitors()
        try:
            if a is None or not a.winfo_viewable():
                raise tk.TclError
            ax, ay, aw, ah = a.winfo_rootx(), a.winfo_rooty(), a.winfo_width(), a.winfo_height()
            if a.overrideredirect():
                # A clock: open on its monitor, not on top of it.
                l, t, r, b = monitor_for(ax, ay, aw, ah, mons)
                cx, cy = (l + r) // 2, (t + b) // 2
            else:
                cx, cy = ax + aw // 2, ay + ah // 2
        except tk.TclError:
            l, t, r, b = mons[0]
            cx, cy = (l + r) // 2, (t + b) // 2
        x, y = ensure_visible(cx - w // 2, cy - h // 2, w, h, mons)
        mon = monitor_for(x, y, w, h, mons)
        x = min(max(x, mon[0]), max(mon[0], mon[2] - w))
        y = min(max(y, mon[1]), max(mon[1], mon[3] - h))
        self.geometry(f"+{x}+{y}")
        self.wm_attributes("-topmost", True)
        try:
            # Only a modal dialog is owned by its anchor.  An owned window is
            # hidden whenever its owner is minimised, which would take a
            # modeless editor away with the manager.
            if self.modal and a is not None and isinstance(a, tk.Toplevel) \
                    and a.winfo_viewable() and not a.overrideredirect():
                self.transient(a)
        except tk.TclError:
            pass
        self.deiconify()
        self.lift()
        self.focus_force()
        if self.modal:
            self._grab(12)

    def _grab(self, tries):
        """grab_set fails with 'window not viewable' until the WM maps it."""
        try:
            self.grab_set()
        except tk.TclError:
            if tries > 0:
                self.after(40, lambda: self._grab(tries - 1))

    def cancel(self):
        self.destroy()


def section_label(parent, text, bg=None):
    """Small tracked uppercase heading, rendered - Tk cannot letter-space."""
    bg = bg or THEME["bg"]
    if HAS_PIL:
        img = text_sprite(text.upper(), "ui_bold", dp(9), THEME["text_faint"], 0.14)
        lbl = tk.Label(parent, bg=bg, bd=0)
        lbl._photo = photo(img, lbl)
        lbl.configure(image=lbl._photo)
        return lbl
    return tk.Label(parent, text=text.upper(), bg=bg, fg=THEME["text_faint"],
                    font=ui_font(8, "bold"))


_BTN_STYLES["danger_solid"] = ("danger", "#ffffff", "danger_hi", "#ffffff")


class ConfirmDialog(Dialog):
    """A themed, topmost confirmation - tk.messagebox could open behind the
    always-on-top clocks and looked nothing like the app."""

    def __init__(self, app, title, message, confirm="Remove", danger=True, anchor=None,
                 cancel="Cancel", icon=None):
        super().__init__(app, title, anchor)
        icon = icon or ("trash" if danger else "check")
        self.result = False
        body = tk.Frame(self, bg=THEME["bg"], padx=dp(24), pady=dp(22))
        body.pack(fill="both", expand=True)
        top = tk.Frame(body, bg=THEME["bg"])
        top.pack(fill="x")
        if HAS_PIL:
            badge = tk.Label(top, bg=THEME["bg"])
            img = Image.new("RGBA", (dp(40), dp(40)), (0, 0, 0, 0))
            circ = Image.new("RGBA", (dp(40) * 4, dp(40) * 4), (0, 0, 0, 0))
            ImageDraw.Draw(circ).ellipse([0, 0, dp(40) * 4 - 1, dp(40) * 4 - 1],
                                         fill=rgba(THEME["danger"] if danger else THEME["accent"], 40))
            img.alpha_composite(circ.resize((dp(40), dp(40)), Image.LANCZOS))
            img.alpha_composite(icon_sprite(icon, dp(20),
                                            THEME["danger"] if danger else THEME["accent"]),
                                (dp(10), dp(10)))
            badge._photo = photo(img, badge)
            badge.configure(image=badge._photo)
            badge.pack(side="left", anchor="n", padx=(0, dp(14)))
        text = tk.Frame(top, bg=THEME["bg"])
        text.pack(side="left", fill="x")
        tk.Label(text, text=title, bg=THEME["bg"], fg=THEME["text"],
                 font=ui_font(12, "bold"), anchor="w").pack(anchor="w")
        tk.Label(text, text=message, bg=THEME["bg"], fg=THEME["text_dim"],
                 font=ui_font(10), justify="left", wraplength=dp(300),
                 anchor="w").pack(anchor="w", pady=(dp(4), 0))
        btns = tk.Frame(body, bg=THEME["bg"])
        btns.pack(fill="x", pady=(dp(22), 0))
        ok = make_button(btns, confirm, self._ok, kind="danger_solid" if danger else "primary",
                         height=34)
        ok.pack(side="right")
        if cancel:
            make_button(btns, cancel, self.cancel, kind="ghost", height=34).pack(
                side="right", padx=(0, dp(8)))
        self.bind("<Return>", lambda e: self._ok())
        self._default = ok

    def _ok(self):
        self.result = True
        self.destroy()

    @classmethod
    def ask(cls, app, title, message, confirm="Remove", danger=True, anchor=None,
            cancel="Cancel", icon=None):
        dlg = cls(app, title, message, confirm, danger, anchor, cancel, icon)
        dlg.present()
        dlg._default.focus_set()
        try:
            dlg.wait_window()
        except tk.TclError:
            pass
        return dlg.result


# ---------------------------------------------------------------------------
#  Window icon
# ---------------------------------------------------------------------------
_ICON_PHOTOS = {}


def apply_window_icon(win):
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
        # One PhotoImage per interpreter: an image belongs to the Tk that made it.
        key = str(win.tk)
        p = _ICON_PHOTOS.get(key)
        if p is None:
            p = _ICON_PHOTOS[key] = photo(make_app_icon(64), win._root())
        win.iconphoto(False, p)
    except tk.TclError:
        pass


# ---------------------------------------------------------------------------
#  Manager
#
#  A list of every clock with a live thumbnail - rendered with the same cards
#  as the clocks themselves - plus the few settings the app has.  Rows draw
#  their controls as canvas items, so a long list costs one widget per clock.
# ---------------------------------------------------------------------------
MGR_W = 460          # dp, window width
ROW_H = 70           # dp
THUMB_H = 38         # dp
LIST_ROWS = 5        # rows visible before the list scrolls


@functools.lru_cache(maxsize=96)
def mini_clock(hhmm, text_color, card_color, face, h, behind, ss=CARD_SS):
    """'14:02' as four tiny flip cards in a recessed well.

    The manager's live thumbnail.  The time is part of the cache key, so a
    row re-renders at most once a minute.
    """
    h = max(14, int(h))
    pad = max(2, round(h * 0.12))
    ch = h - pad * 2
    cw = max(6, round(ch * 0.74))
    gap = max(1, round(h * 0.045))
    colw = max(4, round(ch * 0.26))
    w = pad * 2 + cw * 4 + colw + gap * 4
    img = render_surface(w, h, max(3, round(h * 0.2)), THEME["well"], behind,
                         inset=True, strength=0.9, ss=ss).convert("RGBA")
    rim = text_color if is_neon(text_color) else None
    rad = max(2, round(ch * 0.13))
    x = pad
    for c in hhmm:
        if c == ":":
            img.alpha_composite(colon_sprite(colw, ch, THEME["colon"]), (x, pad))
            x += colw + gap
            continue
        img.paste(render_card(cw, ch, rad, card_color, rim, THEME["well"], False, ss), (x, pad))
        spr = digit_sprite(c, cw, ch, text_color, face, False, ss)
        if spr is not None:
            img.alpha_composite(spr, (x, pad))
        x += cw + gap
    return img.convert("RGB")


def mini_time(now, cfg):
    d = clock_digits(now, cfg)
    return f"{d['h1']}{d['h2']}:{d['m1']}{d['m2']}"


def relative_offset_label(dt, local=None):
    """'7 h ahead', '3:30 h behind', 'Same as local'."""
    local = local or datetime.datetime.now().astimezone()
    off, loc = dt.utcoffset(), local.utcoffset()
    if off is None or loc is None:
        return ""
    diff = int((off - loc).total_seconds()) // 60
    if diff == 0:
        return "Same as local"
    hh, mm = divmod(abs(diff), 60)
    span = f"{hh}:{mm:02d} h" if mm else f"{hh} h"
    return f"{span} ahead" if diff > 0 else f"{span} behind"


@functools.lru_cache(maxsize=512)
def _elide(font, text, max_w):
    """Trim with an ellipsis to fit a Tk font."""
    if _measure(font, text) <= max_w:
        return text
    lo, hi = 0, len(text)              # longest prefix that fits, by bisection
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _measure(font, text[:mid].rstrip() + "…") <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…" if lo else "…"


@functools.lru_cache(maxsize=32)
def render_panel(w, h, radius, fill, behind, ss=3):
    """A flat container with a hairline stroke and a soft top highlight.

    Settings and empty states sit in these: quieter than the raised surfaces,
    and flat, so ordinary widgets placed on them match the fill exactly.
    """
    w, h = max(1, int(w)), max(1, int(h))
    W, H, R = w * ss, h * ss, max(0, int(radius * ss))
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=R, fill=rgba(THEME["hairline"]))
    d.rounded_rectangle([ss, ss, W - 1 - ss, H - 1 - ss], radius=max(0, R - ss), fill=rgba(fill))
    hl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(hl).rounded_rectangle([ss, ss, W - 1 - ss, H - 1 - ss], radius=max(0, R - ss),
                                         outline=(255, 255, 255, 255), width=ss)
    img = Image.alpha_composite(img, _masked(hl, _vramp(W, H, 26, 0, 0.0, 0.3)))
    out = Image.new("RGB", (W, H), behind)
    out.paste(img, (0, 0), img)
    return out.resize((w, h), Image.LANCZOS)


class Panel(tk.Canvas):
    """A rendered panel that sizes itself to the frame placed inside it."""

    def __init__(self, parent, width, behind=None, fill=None, pad=14, radius=12):
        self._behind = behind or THEME["bg"]
        self.fill = fill or THEME["panel"]
        self._pad, self._radius = dp(pad), dp(radius)
        self.PW = width
        super().__init__(parent, width=width, height=dp(40), bd=0, highlightthickness=0,
                         bg=self._behind, takefocus=0)
        self._img = self.create_image(0, 0, anchor="nw")
        self._photo = None
        self.body = tk.Frame(self, bg=self.fill)
        self._win = self.create_window(self._pad, self._pad, anchor="nw", window=self.body,
                                       width=width - 2 * self._pad)
        self.body.bind("<Configure>", self._fit)

    def _fit(self, _=None):
        h = self.body.winfo_reqheight() + 2 * self._pad
        if int(self.cget("height")) != h or self._photo is None:
            self.configure(height=h)
            if HAS_PIL:
                self._photo = photo(render_panel(self.PW, h, self._radius, self.fill,
                                                 self._behind), self)
                self.itemconfigure(self._img, image=self._photo)
            else:
                self.configure(bg=self.fill)


class ClockRow(tk.Canvas):
    """One clock: live thumbnail, name, where and when it is, and actions."""

    ACTIONS = (("locate", "Bring into view", False),
               ("edit", "Edit", False),
               ("trash", "Remove", True))

    def __init__(self, parent, mgr, win, width):
        self.mgr, self.win, self.app = mgr, win, mgr.app
        self.RW, self.RH = int(width), dp(ROW_H)
        super().__init__(parent, width=self.RW, height=self.RH, bd=0, highlightthickness=0,
                         bg=THEME["bg"], takefocus=1)
        self._hover = self._has_focus = False
        self._hot = None
        self._photos = {}
        self._thumb_photo = None
        self._thumb_key = None
        self._meta_key = None
        self.tip = TipBubble(self)
        self._bg = self.create_image(0, 0, anchor="nw")
        self._thumb = self.create_image(dp(14), self.RH // 2, anchor="w")
        self._title = self.create_text(0, 0, anchor="sw", font=ui_font(11, "bold"),
                                       fill=THEME["text"])
        self._meta = self.create_text(0, 0, anchor="nw", font=ui_font(9),
                                      fill=THEME["text_faint"])
        self._acts = {}
        for name, tip, danger in self.ACTIONS:
            tag = "act_" + name
            pill = self.create_image(0, 0, anchor="center", tags=(tag,))
            ico = self.create_image(0, 0, anchor="center", tags=(tag,))
            self._acts[name] = (tag, pill, ico, tip, danger)
            self.tag_bind(tag, "<Enter>", lambda e, n=name: self._set_hot(n))
            self.tag_bind(tag, "<Leave>", lambda e: self._set_hot(None))
            self.tag_bind(tag, "<ButtonRelease-1>", lambda e, n=name: self._act(n))
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Double-Button-1>", lambda e: None if self._hot else self._act("edit"))
        self.bind("<Button-1>", lambda e: self.focus_set())
        self.bind("<Button-3>", self._menu)
        if IS_MAC:
            self.bind("<Button-2>", self._menu)
        self.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.bind("<FocusOut>", lambda e: self._set_focus(False))
        self.bind("<Return>", lambda e: self._act("edit"))
        self.bind("<Delete>", lambda e: self._act("trash"))
        self.bind("<Up>", lambda e: self.mgr.focus_row(self, -1))
        self.bind("<Down>", lambda e: self.mgr.focus_row(self, 1))
        self.bind("<Configure>", self._on_configure)
        self.bind("<Destroy>", self.tip.hide, add="+")
        self._layout()
        self.restyle()

    # -- geometry ----------------------------------------------------------
    def _action_x(self, i):
        return self.RW - dp(12) - dp(16) - i * dp(34)

    def _layout(self):
        cy = self.RH // 2
        for i, name in enumerate(reversed([a[0] for a in self.ACTIONS])):
            tag, pill, ico, *_ = self._acts[name]
            x = self._action_x(i)
            self.coords(pill, x, cy)
            self.coords(ico, x, cy)
        thumb_w = self._thumb_photo.width() if self._thumb_photo else dp(THUMB_H) * 3
        self._tx = dp(14) + thumb_w + dp(16)
        self.coords(self._title, self._tx, cy + dp(1))
        self.coords(self._meta, self._tx, cy + dp(4))
        # The actions show on hover and focus; the rest of the time the text
        # has the whole row.
        self._text_wide = self.RW - dp(18) - self._tx
        self._text_narrow = self._action_x(len(self.ACTIONS) - 1) - dp(22) - self._tx
        self._fit_text()

    def _fit_text(self):
        if not hasattr(self, "_title_text"):
            return
        room = self._text_narrow if self._hover or self._has_focus else self._text_wide
        self.itemconfigure(self._title, text=_elide(ui_font(11, "bold"), self._title_text, room))
        self.itemconfigure(self._meta, text=_elide(ui_font(9), self._meta_text, room))

    def _on_configure(self, e):
        if e.width == self.RW:
            return
        self.RW = e.width
        self._photos = {k: v for k, v in self._photos.items() if k[0] != "bg"}
        self._layout()
        self.restyle()

    # -- painting ----------------------------------------------------------
    def _paint(self):
        if not HAS_PIL:
            self.configure(bg=THEME["surface_hi"] if self._hover else THEME["surface"])
        else:
            fill = THEME["surface_hi"] if self._hover else THEME["surface"]
            ring = THEME["accent"] if self._has_focus else None
            key = ("bg", fill, ring, self.RW)
            p = self._photos.get(key)
            if p is None:
                p = self._photos[key] = photo(render_surface(
                    self.RW, self.RH, dp(12), fill, THEME["bg"], strength=0.55, ring=ring), self)
            self.itemconfigure(self._bg, image=p)
        shown = self._hover or self._has_focus
        for name, (tag, pill, ico, tip, danger) in self._acts.items():
            hot = name == self._hot
            colour = (THEME["danger_hi"] if danger else THEME["text"]) if hot else THEME["text_dim"]
            if HAS_PIL and shown:
                self.itemconfigure(ico, image=self._icon(name, colour), state="normal")
                self.itemconfigure(pill, image=self._pill(hot), state="normal")
            else:
                self.itemconfigure(ico, state="hidden")
                self.itemconfigure(pill, state="hidden")
        self._fit_text()

    def _icon(self, name, colour):
        key = ("icon", name, colour)
        p = self._photos.get(key)
        if p is None:
            p = self._photos[key] = photo(icon_sprite(name, dp(17), colour), self)
        return p

    def _pill(self, hot):
        key = ("pill", hot)
        p = self._photos.get(key)
        if p is None:
            S, ss = dp(30), 4
            img = Image.new("RGBA", (S * ss, S * ss), (0, 0, 0, 0))
            if hot:
                ImageDraw.Draw(img).rounded_rectangle([0, 0, S * ss - 1, S * ss - 1],
                                                      radius=dp(8) * ss, fill=(255, 255, 255, 22))
            else:
                # fully transparent, but it still gives the icon a 30 dp hit area
                img.putpixel((0, 0), (0, 0, 0, 1))
            p = self._photos[key] = photo(img.resize((S, S), Image.LANCZOS), self)
        return p

    def restyle(self):
        """Re-read the clock's settings (name, colours, zone)."""
        self._thumb_key = self._meta_key = None
        self.tick(force=True)
        self._paint()

    def tick(self, force=False):
        cfg = self.win.cfg
        now = clock_now(cfg)
        key = (mini_time(now, cfg), cfg["text_color"], cfg["card_color"], cfg.get("face"))
        if key != self._thumb_key and HAS_PIL:
            self._thumb_key = key
            old_w = self._thumb_photo.width() if self._thumb_photo else None
            self._thumb_photo = photo(mini_clock(key[0], key[1], key[2], key[3] or "modern",
                                                 dp(THUMB_H), THEME["surface"]), self)
            self.itemconfigure(self._thumb, image=self._thumb_photo)
            if old_w != self._thumb_photo.width():
                self._layout()
        elif not HAS_PIL and key != self._thumb_key:
            self._thumb_key = key
            self.itemconfigure(self._thumb, state="hidden")
            if not hasattr(self, "_thumb_txt"):
                self._thumb_txt = self.create_text(dp(14), self.RH // 2, anchor="w",
                                                   font=ui_font(16, "bold"))
            self.itemconfigure(self._thumb_txt, text=key[0], fill=cfg["text_color"])
        meta_key = (now.strftime("%Y%m%d%H%M"), cfg.get("label"), cfg.get("tz"), self.RW)
        if meta_key != self._meta_key or force:
            self._meta_key = meta_key
            entry = entry_for(cfg["tz"], cfg.get("label"))
            if zone_or_none(cfg["tz"]) is None:
                meta = f"{cfg['tz']}  ·  zone not found on this system"
                colour = THEME["danger"]
            else:
                bits = [entry.region if entry.city.lower() == cfg.get("label", "").lower()
                        else f"{entry.city}, {entry.region}", utc_offset_label(now),
                        relative_offset_label(now)]
                day = day_offset_label(now)
                if day:
                    bits.append("Tomorrow" if day == "+1" else "Yesterday")
                meta = "  ·  ".join(b for b in bits if b)
                colour = THEME["text_faint"]
            self._title_text = cfg.get("label") or entry.city
            self._meta_text = meta
            self.itemconfigure(self._meta, fill=colour)
            self._fit_text()

    # -- interaction -------------------------------------------------------
    def _set_hover(self, on):
        if on != self._hover:
            self._hover = on
            if not on:
                self._hot = None
            self._paint()

    def _set_focus(self, on):
        self._has_focus = on
        self._paint()
        if on:
            self.mgr.list.scroll_to(self)

    def _set_hot(self, name):
        if name == self._hot:
            return
        self._hot = name
        self.configure(cursor="hand2" if name else "")
        self._paint()
        if name:
            tag, pill, *_ = self._acts[name]
            tip = self._acts[name][3]
            self.tip.schedule(tip, lambda: (self.winfo_rootx() + int(self.coords(pill)[0]),
                                            self.winfo_rooty() + self.RH // 2 + dp(20)))
        else:
            self.tip.hide()

    def _act(self, name):
        self.tip.hide()
        if name == "locate":
            self.app.recall(self.win)
        elif name == "edit":
            self.app.edit_clock(self.win)
        elif name == "trash":
            self.app.remove_clock(self.win)
        return "break"

    def _menu(self, e):
        try:
            self.win._context_menu(e)
        except tk.TclError:
            pass


class ManagerWindow(tk.Toplevel):
    """Every clock, live, plus the app's few settings."""

    def __init__(self, app):
        super().__init__(app)
        self.withdraw()
        self.app = app
        self.rows = []
        self._photos = {}
        self._placed = False
        self._sub_key = None
        self.title(APP_NAME)
        self.configure(bg=THEME["bg"])
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.hide)
        apply_window_icon(self)
        self._build()
        self.bind("<Control-n>", lambda e: self.app.new_clock())
        self.bind("<Control-q>", lambda e: self.app.quit_app())

    # -- construction ------------------------------------------------------
    def _build(self):
        bg = THEME["bg"]
        self.pad = pad = dp(24)
        self.inner = inner = dp(MGR_W) - pad * 2
        outer = tk.Frame(self, bg=bg, padx=pad, pady=dp(22))
        outer.pack(fill="both", expand=True)

        # header: mark, title, live subtitle, primary action
        hh = dp(48)
        head = tk.Canvas(outer, width=inner, height=hh, bg=bg, bd=0, highlightthickness=0,
                         takefocus=0)
        head.pack(fill="x")
        tx = 0
        if HAS_PIL:
            self._photos["mark"] = photo(make_app_icon(dp(42)), self)
            head.create_image(0, hh // 2, anchor="w", image=self._photos["mark"])
            tx = dp(42) + dp(14)
            self._photos["title"] = photo(text_sprite("Solari", "ui_bold", dp(19),
                                                      THEME["text"], 0.01), self)
            head.create_image(tx, hh // 2 + dp(1), anchor="sw", image=self._photos["title"])
        else:
            head.create_text(tx, hh // 2 + dp(1), anchor="sw", text="Solari",
                             fill=THEME["text"], font=ui_font(16, "bold"))
        self._subtitle = head.create_text(tx, hh // 2 + dp(4), anchor="nw", text="",
                                          fill=THEME["text_faint"], font=ui_font(9))
        self.new_btn = SurfaceButton(head, "New clock", self.app.new_clock, kind="primary",
                                     icon="plus", height=36, behind=bg, padx=14,
                                     tooltip="Add a clock  (Ctrl+N)")
        head.create_window(inner, hh // 2, anchor="e", window=self.new_btn)
        self._head = head

        # clock list
        cap = tk.Frame(outer, bg=bg)
        cap.pack(fill="x", pady=(dp(22), dp(8)))
        section_label(cap, "Your clocks").pack(side="left")
        self._hint = tk.Label(cap, text="Double-click a clock to edit it", bg=bg,
                              fg=THEME["text_faint"], font=ui_font(8))
        self._hint.pack(side="right")
        host = tk.Frame(outer, bg=bg)
        host.pack(fill="x")
        self.list = ScrollFrame(host, height=dp(ROW_H), bg=bg, width=inner)
        self.empty = self._build_empty(host)

        # preferences
        cap2 = tk.Frame(outer, bg=bg)
        cap2.pack(fill="x", pady=(dp(22), dp(8)))
        section_label(cap2, "Preferences").pack(side="left")
        self.prefs = Panel(outer, inner)
        self.prefs.pack(fill="x")
        self._build_prefs(self.prefs.body)

        # footer
        foot = tk.Frame(outer, bg=bg)
        foot.pack(fill="x", pady=(dp(20), 0))
        left = tk.Frame(foot, bg=bg)
        left.pack(side="left", fill="x", expand=True)
        self.path_lbl = tk.Label(left, text="", bg=bg, fg=THEME["text_faint"],
                                 font=ui_font(8), anchor="w", cursor="hand2")
        self.path_lbl.pack(anchor="w")
        self.path_lbl.bind("<Button-1>", lambda e: self.app.open_config_folder())
        tk.Label(left, text="by Cervezagua", bg=bg, fg=THEME["text_faint"],
                 font=ui_font(8, "italic"), anchor="w").pack(anchor="w", pady=(dp(2), 0))
        SurfaceButton(foot, "Quit Solari", self.app.quit_app, kind="ghost_danger",
                      height=32, behind=bg, padx=12, tooltip="Close every clock  (Ctrl+Q)").pack(
            side="right")
        self.set_status(None)

    def _build_empty(self, parent):
        w, h = self.inner, dp(188)
        cv = tk.Canvas(parent, width=w, height=h, bg=THEME["bg"], bd=0, highlightthickness=0,
                       takefocus=0)
        if HAS_PIL:
            self._photos["empty"] = photo(render_panel(w, h, dp(14), THEME["panel"],
                                                       THEME["bg"]), cv)
            cv.create_image(0, 0, anchor="nw", image=self._photos["empty"])
            self._photos["empty_art"] = photo(mini_clock("--:--", THEME["text_faint"],
                                                         "#1e1f22", "modern", dp(38),
                                                         THEME["panel"]), cv)
            cv.create_image(w // 2, dp(42), anchor="center", image=self._photos["empty_art"])
        cv.create_text(w // 2, dp(84), text="No clocks yet", fill=THEME["text"],
                       font=ui_font(12, "bold"))
        cv.create_text(w // 2, dp(108), text="Pick a city and its time floats on your desktop.",
                       fill=THEME["text_dim"], font=ui_font(9))
        btn = SurfaceButton(cv, "Add a clock", self.app.new_clock, kind="secondary",
                            icon="plus", height=34, behind=THEME["panel"])
        cv.create_window(w // 2, dp(150), anchor="center", window=btn)
        return cv

    def _pref_row(self, parent, title, sub, control_fn, first=False):
        fill = self.prefs.fill
        if not first:
            tk.Frame(parent, bg=THEME["hairline"], height=1).pack(fill="x", pady=dp(10))
        row = tk.Frame(parent, bg=fill)
        row.pack(fill="x")
        text = tk.Frame(row, bg=fill)
        text.pack(side="left", fill="x", expand=True)
        tk.Label(text, text=title, bg=fill, fg=THEME["text"], font=ui_font(10, "bold"),
                 anchor="w").pack(anchor="w")
        sub_lbl = tk.Label(text, text=sub, bg=fill, fg=THEME["text_faint"], font=ui_font(9),
                           anchor="w")
        sub_lbl.pack(anchor="w")
        control = control_fn(row)
        control.pack(side="right", padx=(dp(12), 0))
        return control, sub_lbl

    def _build_prefs(self, body):
        fill = self.prefs.fill
        if HAS_WINREG:
            sub = "Start Solari quietly when you sign in"
        else:
            sub = "Available on Windows"
        self.startup_switch, _ = self._pref_row(
            body, "Launch at sign-in", sub,
            lambda p: ToggleSwitch(p, self.app.startup_var, command=self.app.toggle_startup,
                                   behind=fill), first=True)
        if not HAS_WINREG:
            self.startup_switch.set_enabled(False)
        self._pref_row(body, "Gather clocks", "Line every clock up on the main screen",
                       lambda p: SurfaceButton(p, "Gather", self.app.recall_all,
                                               kind="secondary", height=30, behind=fill,
                                               padx=14))

    # -- state -------------------------------------------------------------
    def set_status(self, message):
        """The footer line: where settings live, or a save problem."""
        if message:
            self.path_lbl.configure(text=message, fg=THEME["danger"])
        else:
            self.path_lbl.configure(text="Settings  ·  " + pretty_path(config_dir(), 40),
                                    fg=THEME["text_faint"])

    def refresh_list(self):
        for row in self.rows:
            row.destroy()
        self.rows = []
        n = len(self.app.windows)
        if n:
            self.empty.pack_forget()
            self.list.pack(fill="x")
            gap = dp(6)
            width = self.inner - (dp(14) if n > LIST_ROWS else 0)
            for i, win in enumerate(self.app.windows):
                row = ClockRow(self.list.body, self, win, width)
                row.pack(fill="x", pady=(0, gap if i < n - 1 else 0))
                self.rows.append(row)
            shown = min(n, LIST_ROWS)
            self.list.set_height(shown * dp(ROW_H) + (shown - 1) * gap)
            self._hint.configure(text="Double-click a clock to edit it")
        else:
            self.list.pack_forget()
            self.empty.pack(fill="x")
            self._hint.configure(text="")
        self._sub_key = None
        self.on_second(time.time())

    def update_row(self, win):
        for row in self.rows:
            if row.win is win:
                row.restyle()

    def focus_row(self, row, d):
        i = self.rows.index(row) + d if row in self.rows else 0
        if 0 <= i < len(self.rows):
            self.rows[i].focus_set()
        return "break"

    def on_second(self, ts):
        """The live thumbnails ride the shared tick."""
        if not self.winfo_viewable():
            return
        for row in list(self.rows):
            try:
                row.tick()
            except tk.TclError:
                pass
        now = datetime.datetime.now()
        n = len(self.app.windows)
        key = (n, now.strftime("%H%M"))
        if key != self._sub_key:
            self._sub_key = key
            count = "No clocks" if n == 0 else f"{n} clock" + ("" if n == 1 else "s")
            self._head.itemconfigure(self._subtitle,
                                     text=f"{count}  ·  {now.strftime('%a %d %b, %H:%M')} here")

    # -- visibility --------------------------------------------------------
    def show(self):
        if not self._placed:
            self.update_idletasks()
            l, t, r, b = self.app.monitors()[0]
            w, h = self.winfo_reqwidth(), self.winfo_reqheight()
            self.geometry(f"+{l + (r - l - w) // 2}+{t + max(0, (b - t - h) // 3)}")
            self._placed = True
        self.deiconify()
        self.lift()
        try:
            self.focus_force()
        except tk.TclError:
            pass
        self.on_second(time.time())

    def hide(self):
        """Closing the manager never stops the clocks.  It goes to the tray,
        or - with no tray to come back from - to the taskbar."""
        if self.app.tray_active():
            self.withdraw()
        else:
            self.iconify()


# ---------------------------------------------------------------------------
#  Clock editor
#
#  Edits apply to the real clock as you make them, with a live preview on a
#  stage at the top; Cancel restores everything.  One editor at a time.
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=64)
def preset_tile(text_color, card_color, finish, size, selected, behind, ss=4):
    """A preset swatch: a little housing with one card showing a digit."""
    spec = FINISHES.get(finish, FINISHES["graphite"])
    ring = dp(3) if selected else 0
    S = size + ring * 2
    out = Image.new("RGBA", (S * ss, S * ss), (0, 0, 0, 0))
    if selected:
        ImageDraw.Draw(out).rounded_rectangle([0, 0, S * ss - 1, S * ss - 1],
                                              radius=int(size * 0.3 * ss + ring * ss),
                                              outline=rgba(THEME["accent"]), width=int(1.8 * ss))
    out = out.resize((S, S), Image.LANCZOS)
    tone = spec["tone"]
    body = render_surface(size, size, max(3, int(size * 0.26)), tone, behind, strength=0.8)
    out.paste(body, (ring, ring))
    cw, ch = int(size * 0.54), int(size * 0.68)
    card = render_card(cw, ch, max(2, int(size * 0.09)), card_color,
                       text_color if is_neon(text_color) else None, tone, False)
    cx, cy = ring + (size - cw) // 2, ring + (size - ch) // 2
    out.paste(card, (cx, cy))
    spr = digit_sprite("8", cw, ch, text_color, "modern", False)
    if spr is not None:
        out.alpha_composite(spr, (cx, cy))
    return out


class PresetStrip(tk.Canvas):
    """The style presets as a row of tiles; picking one sets three options."""

    def __init__(self, parent, on_pick, behind=None, size=38):
        self._behind = behind or THEME["bg"]
        self.on_pick = on_pick
        self.S, self.G = dp(size), dp(7)
        self.ring = dp(3)
        n = len(PRESETS)
        W = n * (self.S + self.G) - self.G + self.ring * 2
        H = self.S + self.ring * 2
        super().__init__(parent, width=W, height=H, bd=0, highlightthickness=0,
                         bg=self._behind, takefocus=0, cursor="hand2")
        self._photos = {}
        self._tiles = []
        self.current = None
        self.hover = None
        self.on_name = None              # called with the name to show
        for i, (name, tc, cc, fin) in enumerate(PRESETS):
            tile = self.create_image(i * (self.S + self.G), 0, anchor="nw")
            self._tiles.append(tile)
            self.tag_bind(tile, "<Button-1>", lambda e, k=i: self._pick(k))
            self.tag_bind(tile, "<Enter>", lambda e, k=i: self._set_hover(k))
            self.tag_bind(tile, "<Leave>", lambda e: self._set_hover(None))
        self.draw()

    def draw(self, current=None):
        self.current = current
        for i, ((name, tc, cc, fin), tile) in enumerate(zip(PRESETS, self._tiles)):
            sel = i == current
            if HAS_PIL:
                key = (i, sel)
                if key not in self._photos:
                    self._photos[key] = photo(preset_tile(tc, cc, fin, self.S, sel,
                                                          self._behind), self)
                self.itemconfigure(tile, image=self._photos[key])
        self._announce()

    def _set_hover(self, i):
        self.hover = i
        self._announce()

    def _announce(self):
        i = self.hover if self.hover is not None else self.current
        if self.on_name:
            self.on_name(PRESETS[i][0] if i is not None else "Custom")

    def _pick(self, i):
        self.on_pick(PRESETS[i])


def preset_index(cfg):
    for i, (_, tc, cc, fin) in enumerate(PRESETS):
        if (tc, cc, fin) == (cfg.get("text_color"), cfg.get("card_color"), cfg.get("finish")):
            return i
    return None


@functools.lru_cache(maxsize=8)
def render_stage(w, h, behind):
    """The preview backdrop: a softly lit, recessed desk."""
    base = render_surface(w, h, dp(14), "#16171b", behind, inset=True, strength=0.7)
    glow = Image.new("L", (w, h), 0)
    ImageDraw.Draw(glow).ellipse([w * 0.15, -h * 0.6, w * 0.85, h * 0.9], fill=40)
    glow = glow.filter(ImageFilter.GaussianBlur(max(4, h // 5)))
    lit = Image.new("RGB", (w, h), "#ffffff")
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([1, 1, w - 2, h - 2], radius=dp(14), fill=255)
    glow = Image.composite(glow, Image.new("L", (w, h), 0), mask)
    return Image.composite(lit, base, glow.point(lambda v: v // 3))


@functools.lru_cache(maxsize=8)
def drop_shadow(w, h, blur, alpha=150):
    pad = blur * 3
    img = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([pad, pad, pad + w, pad + h], fill=(0, 0, 0, alpha))
    return img.filter(ImageFilter.GaussianBlur(blur)), pad


class ClockEditor(Dialog):
    """Modeless on purpose: the clock stays draggable while it is edited."""

    modal = False
    COL_L = 300
    COL_R = 356
    GUTTER = 32

    def __init__(self, app, win, is_new=False):
        anchor = app.mgr if app.mgr.winfo_viewable() else win
        super().__init__(app, "New clock" if is_new else "Edit clock", anchor)
        self.win = win
        self.cfg = win.cfg                       # edited in place, applied live
        self.is_new = is_new
        self.original = dict(win.cfg)
        self._apply_job = None
        self._preview_job = None
        self._search_job = None
        self._loading = False
        self._preview_photo = None
        cfg = self.cfg
        self.entry = entry_for(cfg["tz"], cfg.get("label"))
        # A name the user typed is theirs; only an untouched name follows the
        # zone.  The old dialog overwrote custom names the moment it opened.
        self._label_auto = cfg.get("label", "") in ("", self.entry.city, city_of(cfg["tz"]))

        self.v_label = tk.StringVar(self, cfg.get("label", ""))
        self.v_search = tk.StringVar(self, "")
        self.v_text = tk.StringVar(self, cfg["text_color"])
        self.v_card = tk.StringVar(self, cfg["card_color"])
        self.v_finish = tk.StringVar(self, cfg.get("finish", "graphite"))
        self.v_face = tk.StringVar(self, cfg.get("face", "modern"))
        self.v_seconds = tk.StringVar(self, cfg.get("seconds", "small"))
        self.v_format = tk.StringVar(self, "24" if cfg.get("hour24", True) else "12")
        self.v_top = tk.BooleanVar(self, bool(cfg.get("topmost", True)))
        self.v_scale = tk.DoubleVar(self, float(cfg.get("scale", 1.0)))
        self.v_opacity = tk.DoubleVar(self, float(cfg.get("opacity", 0.98)))

        self._build()
        self.zones.set_entries(zone_catalog(), select_key=cfg["tz"], select_entry=self.entry)
        self._sync_presets()
        self._sync_contrast()
        self._render_preview()

        for var, fn in ((self.v_label, self._on_label), (self.v_search, self._on_search)):
            var.trace_add("write", lambda *_, f=fn: f())
        for var in (self.v_text, self.v_card, self.v_finish, self.v_face, self.v_seconds,
                    self.v_format, self.v_top, self.v_scale, self.v_opacity):
            var.trace_add("write", lambda *_: self._on_style())
        self.bind("<Return>", self._on_return)
        self.app.ticker.subscribe(self)
        self.present()
        (self.search.entry if is_new else self.save_btn).focus_set()

    # -- construction ------------------------------------------------------
    def _build(self):
        bg = THEME["bg"]
        pad = dp(24)
        body = tk.Frame(self, bg=bg, padx=pad, pady=dp(20))
        body.pack(fill="both", expand=True)
        total = dp(self.COL_L) + dp(self.GUTTER) + dp(self.COL_R)

        # stage
        self.SW, self.SH = total, dp(184)
        self.stage = tk.Canvas(body, width=self.SW, height=self.SH, bg=bg, bd=0,
                               highlightthickness=0, takefocus=0)
        self.stage.pack(fill="x")
        self._stage_img = self.stage.create_image(0, 0, anchor="nw")
        if not HAS_PIL:
            self.stage.configure(bg=THEME["panel"])
            self._stage_txt = self.stage.create_text(self.SW // 2, self.SH // 2,
                                                     font=ui_font(28, "bold"))

        cols = tk.Frame(body, bg=bg)
        cols.pack(fill="x", pady=(dp(20), 0))
        left = tk.Frame(cols, bg=bg, width=dp(self.COL_L))
        left.pack(side="left", fill="y", anchor="n")
        right = tk.Frame(cols, bg=bg, width=dp(self.COL_R))
        right.pack(side="right", fill="y", anchor="n")

        # -- left: name and place
        section_label(left, "Name").pack(anchor="w")
        self.name = TextField(left, self.v_label, width=30)
        self.name.pack(fill="x", pady=(dp(6), dp(16)))
        zl = tk.Frame(left, bg=bg)
        zl.pack(fill="x")
        section_label(zl, "Time zone").pack(side="left")
        self.zone_lbl = tk.Label(zl, text="", bg=bg, fg=THEME["text_faint"], font=ui_font(8))
        self.zone_lbl.pack(side="right")
        self.search = TextField(left, self.v_search, icon="search", width=30,
                                placeholder="Search a city, country or GMT+9")
        self.search.pack(fill="x", pady=(dp(6), dp(6)))
        self.search.entry.bind("<Down>", self._focus_list)
        self.zones = ZoneList(left, self._on_zone, rows=6, width=self.COL_L, behind=bg)
        self.zones.pack(fill="x")
        self._sync_zone_label()
        top = tk.Frame(left, bg=bg)
        top.pack(fill="x", pady=(dp(16), 0))
        tk.Label(top, text="Always on top", bg=bg, fg=THEME["text"], font=ui_font(10),
                 anchor="w").pack(side="left")
        ToggleSwitch(top, self.v_top, behind=bg).pack(side="right")

        # -- right: appearance
        sl = tk.Frame(right, bg=bg)
        sl.pack(fill="x")
        section_label(sl, "Style").pack(side="left")
        self.preset_lbl = tk.Label(sl, text="", bg=bg, fg=THEME["text_dim"], font=ui_font(8))
        self.preset_lbl.pack(side="right")
        self.presets = PresetStrip(right, self._on_preset, behind=bg, size=37)
        self.presets.on_name = lambda name: self.preset_lbl.configure(text=name)
        self.presets.pack(anchor="w", pady=(dp(6), dp(12)))

        dl = tk.Frame(right, bg=bg)
        dl.pack(fill="x")
        section_label(dl, "Digits").pack(side="left")
        self.contrast_lbl = tk.Label(dl, text="", bg=bg, fg=THEME["danger"], font=ui_font(8))
        self.contrast_lbl.pack(side="right")
        SwatchRow(right, TEXT_COLORS, self.v_text, behind=bg, size=24).pack(
            anchor="w", pady=(dp(4), dp(10)))
        section_label(right, "Cards").pack(anchor="w")
        SwatchRow(right, CARD_COLORS, self.v_card, behind=bg, size=24).pack(
            anchor="w", pady=(dp(4), dp(12)))

        grid = tk.Frame(right, bg=bg)
        grid.pack(fill="x")
        half_dp = (self.COL_R - 16) / 2
        half = dp(half_dp)

        def cell(r, c, title, make):
            f = tk.Frame(grid, bg=bg, width=half)
            f.grid(row=r, column=c, sticky="nw", padx=(0, dp(16)) if c == 0 else 0,
                   pady=(0, dp(12)))
            section_label(f, title).pack(anchor="w")
            w = make(f)
            w.pack(anchor="w", pady=(dp(5), 0))
            return w

        seg = lambda f, opts, var: Segmented(f, opts, var, behind=bg, height=30,
                                             seg_min=(half_dp - 4) / len(opts))
        cell(0, 0, "Finish", lambda f: seg(f, [(k, v["name"]) for k, v in FINISHES.items()],
                                           self.v_finish))
        cell(0, 1, "Typeface", lambda f: seg(f, list(FACES.items()), self.v_face))
        cell(1, 0, "Seconds", lambda f: seg(f, [("off", "Off"), ("small", "Small"),
                                                ("large", "Large")], self.v_seconds))
        cell(1, 1, "Format", lambda f: seg(f, [("24", "24-hour"), ("12", "12-hour")],
                                           self.v_format))
        cell(2, 0, "Size", lambda f: Slider(f, self.v_scale, SCALE_MIN, SCALE_MAX, width=half_dp,
                                            behind=bg, fmt=lambda v: f"{v * 100:.0f}%"))
        cell(2, 1, "Opacity", lambda f: Slider(f, self.v_opacity, OPACITY_MIN, OPACITY_MAX,
                                               width=half_dp, behind=bg,
                                               fmt=lambda v: f"{v * 100:.0f}%"))

        # footer
        tk.Frame(body, bg=THEME["hairline"], height=1).pack(fill="x", pady=(dp(18), dp(16)))
        foot = tk.Frame(body, bg=bg)
        foot.pack(fill="x")
        self.save_btn = SurfaceButton(foot, "Add clock" if self.is_new else "Done", self.save,
                                      kind="primary", height=36, behind=bg, padx=20)
        self.save_btn.pack(side="right")
        SurfaceButton(foot, "Cancel", self.cancel, kind="ghost", height=36, behind=bg).pack(
            side="right", padx=(0, dp(8)))
        if not self.is_new:
            SurfaceButton(foot, "Remove clock", self._remove, kind="ghost_danger", icon="trash",
                          height=36, behind=bg, padx=12).pack(side="left")
            SurfaceButton(foot, "Duplicate", self._duplicate, kind="ghost", icon="copy",
                          height=36, behind=bg, padx=12).pack(side="left", padx=(dp(4), 0))
        tk.Label(foot, text="Changes show on the clock as you make them",
                 bg=bg, fg=THEME["text_faint"], font=ui_font(8)).pack(side="right",
                                                                      padx=(0, dp(16)))

    # -- reactions ---------------------------------------------------------
    def _on_label(self):
        if self._loading:
            return
        text = self.v_label.get()
        self._label_auto = text.strip() in ("", self.entry.city)
        self.cfg["label"] = text.strip() or self.entry.city
        self._schedule_apply()

    def _on_search(self):
        if self._search_job:
            self.after_cancel(self._search_job)
        self._search_job = self.after(70, self._run_search)

    def _run_search(self):
        if self._search_job:
            try:
                self.after_cancel(self._search_job)
            except tk.TclError:
                pass
            self._search_job = None
        q = self.v_search.get()
        results = search_zones(q)
        self.zones.set_entries(results, select_key=None if q.strip() else self.cfg["tz"],
                               select_entry=self.entry)

    def _on_zone(self, entry):
        self.entry = entry
        self.cfg["tz"] = entry.key
        if self._label_auto:
            self._loading = True
            self.v_label.set(entry.city)
            self._loading = False
            self.cfg["label"] = entry.city
        self._sync_zone_label()
        self._schedule_apply()

    def _on_preset(self, preset):
        _, tc, cc, fin = preset
        self._loading = True
        self.v_text.set(tc)
        self.v_card.set(cc)
        self.v_finish.set(fin)
        self._loading = False
        self._on_style()

    def _on_style(self):
        if self._loading:
            return
        c = self.cfg
        c["text_color"] = self.v_text.get()
        c["card_color"] = self.v_card.get()
        c["finish"] = self.v_finish.get()
        c["face"] = self.v_face.get()
        c["seconds"] = self.v_seconds.get()
        c["hour24"] = self.v_format.get() == "24"
        c["topmost"] = bool(self.v_top.get())
        try:
            c["scale"] = round(float(self.v_scale.get()), 2)
            c["opacity"] = round(float(self.v_opacity.get()), 2)
        except (tk.TclError, ValueError):
            pass
        self._sync_presets()
        self._sync_contrast()
        self._schedule_apply()

    def _sync_presets(self):
        self.presets.draw(preset_index(self.cfg))

    def _sync_contrast(self):
        ratio = contrast(self.cfg["text_color"], self.cfg["card_color"])
        self.contrast_lbl.configure(text="" if ratio >= 3.0 else f"Low contrast  {ratio:.1f}:1")

    def _sync_zone_label(self):
        z = zone_or_none(self.entry.key)
        if z is None:
            self.zone_lbl.configure(text=f"{self.entry.key} · not found", fg=THEME["danger"])
        else:
            now = datetime.datetime.now(z)
            self.zone_lbl.configure(text=f"{self.entry.key.replace('_', ' ')}  ·  "
                                         f"{utc_offset_label(now)}", fg=THEME["text_faint"])

    def _focus_list(self, _=None):
        self.zones.focus_set()
        if self.zones.entries:
            self.zones.select(self.zones.sel if self.zones.sel is not None else 0)
        return "break"

    def _on_return(self, e):
        """Return in the search box picks the top hit; in the name field or
        the list it confirms.  Toggles and buttons handle their own Return."""
        if e.widget is self.search.entry:
            if self.zones.entries:
                self.zones.select(self.zones.sel if self.zones.sel is not None else 0)
            return "break"
        if isinstance(e.widget, (tk.Entry, ZoneList)) or e.widget is self:
            self.save()
            return "break"
        return None

    # -- live apply --------------------------------------------------------
    def _schedule_apply(self):
        """Preview right away; the real clock re-renders a beat later, so
        dragging a slider does not queue a full re-render per motion event."""
        if self._preview_job is None:
            self._preview_job = self.after(16, self._render_preview)
        if self._apply_job is not None:
            self.after_cancel(self._apply_job)
        self._apply_job = self.after(140, self._apply)

    def _apply(self):
        self._apply_job = None
        try:
            self.win.refresh_style()
        except tk.TclError:
            return
        self.app.clock_changed(self.win, save=False, source=self)

    def on_second(self, ts):
        self._render_preview()

    def _render_preview(self):
        self._preview_job = None
        cfg = dict(self.cfg)
        if not HAS_PIL:
            now = clock_now(cfg)
            d = clock_digits(now, cfg)
            self.stage.itemconfigure(self._stage_txt, text=f"{d['h1']}{d['h2']}:{d['m1']}{d['m2']}",
                                     fill=cfg["text_color"])
            return
        L1 = clock_layout(cfg, 1.0)
        margin = dp(24)
        s = min((self.SW - 2 * margin) / L1["w"], (self.SH - 2 * margin) / L1["h"], UI_SCALE)
        img = compose_clock(cfg, scale=s, ss=2)
        stage = render_stage(self.SW, self.SH, THEME["bg"]).copy()
        blur = max(4, dp(9))
        shadow, spad = drop_shadow(img.width, img.height, blur)
        x, y = (self.SW - img.width) // 2, (self.SH - img.height) // 2
        stage.paste(shadow, (x - spad, y - spad + dp(7)), shadow)
        stage.paste(img, (x, y))
        self._preview_photo = photo(stage, self)
        self.stage.itemconfigure(self._stage_img, image=self._preview_photo)

    def reload(self):
        """The clock was changed elsewhere (its context menu)."""
        c = self.cfg
        self._loading = True
        try:
            self.v_text.set(c["text_color"])
            self.v_card.set(c["card_color"])
            self.v_finish.set(c.get("finish", "graphite"))
            self.v_face.set(c.get("face", "modern"))
            self.v_seconds.set(c.get("seconds", "small"))
            self.v_format.set("24" if c.get("hour24", True) else "12")
            self.v_top.set(bool(c.get("topmost", True)))
            self.v_scale.set(float(c.get("scale", 1.0)))
            self.v_opacity.set(float(c.get("opacity", 0.98)))
        finally:
            self._loading = False
        self._sync_presets()
        self._sync_contrast()
        self._render_preview()

    # -- outcomes ----------------------------------------------------------
    def _close(self):
        for job in (self._apply_job, self._preview_job, self._search_job):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self._apply_job = self._preview_job = self._search_job = None
        self.app.ticker.unsubscribe(self)
        if self.app.editor is self:
            self.app.editor = None
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    def save(self):
        if not self.cfg.get("label", "").strip():
            self.cfg["label"] = self.entry.city
        self._close()
        try:
            self.win.refresh_style()
        except tk.TclError:
            pass
        self.app.clock_changed(self.win, source=self)

    def cancel(self):
        self._close()
        if self.is_new:
            self.app.discard_clock(self.win)
            return
        # Restore in place: app.configs holds this same dict.
        self.cfg.clear()
        self.cfg.update(self.original)
        try:
            self.win.refresh_style()
        except tk.TclError:
            pass
        self.app.clock_changed(self.win, save=False, source=self)

    def _remove(self):
        self.app.remove_clock(self.win, anchor=self)

    def _duplicate(self):
        win = self.win
        self.save()
        self.app.duplicate_clock(win)


# ---------------------------------------------------------------------------
#  App
# ---------------------------------------------------------------------------
SAVE_DELAY_MS = 400
POLL_MS = 250


class App(tk.Tk):

    def __init__(self, config_file=None, lock=None, background=False):
        super().__init__()
        self.withdraw()
        self.title(APP_NAME)
        init_ui_scale(self)
        resolve_fonts(self)
        apply_window_icon(self)
        self.errors = []
        self.lock = lock
        self.config_file = config_file or config_path()
        self.ticker = Ticker(self)
        self.configs = load_config(self.config_file)
        self.windows = []
        self.editor = None
        self._save_job = None
        self._monitors = None
        self._monitors_t = 0.0
        self._tray = None
        self._tray_lock = threading.Lock()
        self._tray_q = queue.Queue()
        self._poll_n = 0
        self._quitting = False
        self.startup_var = tk.BooleanVar(self, value=get_startup())
        heal_startup()

        self.mgr = ManagerWindow(self)
        for cfg in self.configs:
            self._spawn(cfg)
        self.ticker.subscribe(self.mgr)
        self.ticker.start()
        self.mgr.refresh_list()

        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        # Windows asks before logging off or shutting down; save then too.
        for w in (self, self.mgr):
            w.protocol("WM_SAVE_YOURSELF", self.save_now)
        if HAS_TRAY:
            self._start_tray()
        self._poll_job = self.after(POLL_MS, self._poll)
        # At sign-in only the clocks appear.  The manager is one click away:
        # the tray, any clock's menu, or simply launching Solari again.
        if not (background and self.windows):
            self.mgr.show()

    # -- errors ------------------------------------------------------------
    def report_callback_exception(self, exc, val, tb):
        """Log instead of dying silently: under pythonw there is no console."""
        text = "".join(traceback.format_exception(exc, val, tb))
        self.errors.append(text)
        if sys.stderr is not None:
            try:
                sys.stderr.write(text)
            except (OSError, ValueError):
                pass
        try:
            log = os.path.join(config_dir(), "solari_error.log")
            if os.path.exists(log) and os.path.getsize(log) > 256_000:
                os.replace(log, log + ".old")
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{text}\n")
        except OSError:
            pass

    # -- environment -------------------------------------------------------
    def monitors(self):
        """Work areas, cached briefly: dragging asks on every motion event."""
        now = time.monotonic()
        if self._monitors is None or now - self._monitors_t > 2.0:
            self._monitors = get_monitors(self)
            self._monitors_t = now
        return self._monitors

    def open_config_folder(self):
        d = config_dir()
        try:
            if IS_WINDOWS:
                os.startfile(d)
            elif IS_MAC:
                import subprocess
                subprocess.Popen(["open", d])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", d])
        except (OSError, AttributeError):
            pass

    # -- persistence -------------------------------------------------------
    def schedule_save(self, delay=SAVE_DELAY_MS):
        """Debounced: a burst of changes (a drag, a slider) is one write."""
        if self._save_job is not None:
            self.after_cancel(self._save_job)
        self._save_job = self.after(delay, self.save_now)

    def save_now(self):
        if self._save_job is not None:
            try:
                self.after_cancel(self._save_job)
            except tk.TclError:
                pass
            self._save_job = None
        ok = save_config(self.configs, self.config_file)
        if ok:
            self.mgr.set_status(None)
        else:
            self.mgr.set_status("Couldn't save settings - retrying")
            if not self._quitting:
                self.schedule_save(5000)
        return ok

    # -- clocks ------------------------------------------------------------
    def _spawn(self, cfg):
        win = ClockWindow(self, self, cfg)
        self.windows.append(win)
        return win

    def new_clock(self):
        if self.editor is not None and self.editor.is_new:
            self.editor.lift()
            return None
        n = len(self.configs)
        tc = COLOR_CYCLE[n % len(COLOR_CYCLE)]
        l, t, r, b = self.monitors()[0]
        cfg = dict(DEFAULT_CLOCK, tz="UTC", label="UTC",
                   x=l + dp(60) + (n % 8) * dp(28), y=t + dp(60) + (n % 8) * dp(28),
                   text_color=tc, card_color=PAL_DARK.get(tc, "#1e1f22"))
        self.configs.append(cfg)
        win = self._spawn(cfg)
        self.mgr.refresh_list()
        self.edit_clock(win, is_new=True)
        return win

    def duplicate_clock(self, win):
        cfg = dict(win.cfg)
        cfg["x"] = win.winfo_x() + dp(28)
        cfg["y"] = win.winfo_y() + dp(28)
        i = self.configs.index(win.cfg) + 1 if win.cfg in self.configs else len(self.configs)
        self.configs.insert(i, cfg)
        new = ClockWindow(self, self, cfg)
        self.windows.insert(self.windows.index(win) + 1 if win in self.windows else len(self.windows), new)
        self.mgr.refresh_list()
        self.schedule_save()
        return new

    def remove_clock(self, win, confirm=True, anchor=None):
        if win not in self.windows:
            return False
        if confirm:
            if anchor is None:
                anchor = self.mgr if self.mgr.winfo_viewable() else win
            name = win.cfg.get("label") or city_of(win.cfg.get("tz", ""))
            if not ConfirmDialog.ask(self, "Remove this clock?",
                                     f"“{name}” will be removed from your desktop.",
                                     confirm="Remove", anchor=anchor):
                return False
        if self.editor is not None and self.editor.win is win:
            self.editor._close()
        self._drop(win)
        self.save_now()
        return True

    def discard_clock(self, win):
        """Silently drop a clock that was never confirmed (a cancelled New)."""
        if win in self.windows:
            self._drop(win)
            self.save_now()

    def _drop(self, win):
        # By identity: list.remove() compares dicts by value, so two clocks
        # with identical settings could drop the wrong entry.
        self.configs = [c for c in self.configs if c is not win.cfg]
        if win in self.windows:
            self.windows.remove(win)
        try:
            win.destroy()
        except tk.TclError:
            pass
        self.mgr.refresh_list()

    def edit_clock(self, win, is_new=False):
        if win not in self.windows:
            return None
        ed = self.editor
        if ed is not None:
            if ed.win is win:
                ed.deiconify()
                ed.lift()
                ed.focus_force()
                return ed
            ed.save()                       # one at a time: keep what was done
        self.editor = ClockEditor(self, win, is_new=is_new)
        return self.editor

    def clock_changed(self, win, save=True, source=None):
        """A clock's settings changed - from its editor, or from its own menu
        while an editor is open on it, which then has to catch up."""
        self.mgr.update_row(win)
        ed = self.editor
        if ed is not None and ed.win is win and source is not ed:
            ed.reload()
        if save:
            self.schedule_save()

    def recall(self, win):
        """Raise a clock and pulse it; move it onto the main screen only if it
        is not already fully visible somewhere."""
        try:
            x, y, w, h = win.winfo_x(), win.winfo_y(), win.winfo_width(), win.winfo_height()
        except tk.TclError:
            return
        visible = any(l <= x and t <= y and x + w <= r and y + h <= b
                      for l, t, r, b in self.monitors())
        if visible:
            win.recall(x, y)
        else:
            win.recall()
        win.flash()
        self.schedule_save()

    def recall_all(self):
        """Line every clock up in columns at the top-left of the main screen."""
        l, t, r, b = self.monitors()[0]
        gap = dp(14)
        x, y, col_w = l + dp(32), t + dp(32), 0
        for win in self.windows:
            try:
                win.update_idletasks()
                w, h = win.winfo_width(), win.winfo_height()
            except tk.TclError:
                continue
            if y + h > b - dp(16) and y > t + dp(32):
                x += col_w + gap
                y, col_w = t + dp(32), 0
            win.recall(x, y)
            y += h + gap
            col_w = max(col_w, w)
        self.schedule_save()

    def toggle_startup(self):
        want = bool(self.startup_var.get())
        if set_startup(want):
            return
        self.startup_var.set(not want)
        ConfirmDialog.ask(self, "Couldn't change sign-in launch",
                          "Windows did not accept the change to the startup entry."
                          if HAS_WINREG else "Launching at sign-in is only available on Windows.",
                          confirm="OK", danger=False, cancel=None, icon="alert",
                          anchor=self.mgr)

    def show_manager(self):
        self.mgr.show()

    # -- tray --------------------------------------------------------------
    def tray_active(self):
        return self._tray is not None

    def _start_tray(self):
        """pystray runs its own loop on a worker thread.  Tk is not thread-safe
        - not even after() - so menu clicks only post to a queue, which the
        Tk thread drains in _poll()."""
        post = self._tray_q.put

        def run():
            try:
                menu = pystray.Menu(
                    pystray.MenuItem("Show Solari", lambda: post("show"), default=True),
                    pystray.MenuItem("New clock", lambda: post("new")),
                    pystray.MenuItem("Gather clocks", lambda: post("gather")),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Quit", lambda: post("quit")),
                )
                # Created on this thread: on Windows the icon's message loop
                # must run on the thread that made its window.
                icon = pystray.Icon(APP_NAME, make_app_icon(64), APP_NAME, menu)
                with self._tray_lock:
                    if self._quitting:
                        return
                    self._tray = icon
                icon.run()
            except Exception:
                try:
                    with open(os.path.join(config_dir(), "tray_error.log"), "w",
                              encoding="utf-8") as f:
                        traceback.print_exc(file=f)
                except OSError:
                    pass
                self._tray = None
                post("tray_failed")

        threading.Thread(target=run, name="solari-tray", daemon=True).start()

    def _poll(self):
        """Drain tray commands; every second, check for a second launch."""
        if self._quitting:
            return
        try:
            self._drain_tray()
        except Exception:
            self.report_callback_exception(*sys.exc_info())
        if self._quitting:
            return
        self._poll_n += 1
        if self.lock is not None and self._poll_n % 4 == 0 and self.lock.take_show_request():
            self.show_manager()
        self._poll_job = self.after(POLL_MS, self._poll)

    def _drain_tray(self):
        try:
            while True:
                cmd = self._tray_q.get_nowait()
                if cmd == "show":
                    self.show_manager()
                elif cmd == "new":
                    self.show_manager()
                    self.new_clock()
                elif cmd == "gather":
                    self.recall_all()
                elif cmd == "quit":
                    self.quit_app()
                    return
                elif cmd == "tray_failed":
                    self.show_manager()      # do not strand the user
        except queue.Empty:
            pass

    # -- shutdown ----------------------------------------------------------
    def quit_app(self):
        if self._quitting:
            return
        self._quitting = True
        if self.editor is not None:
            try:
                self.editor._close()       # its changes are already in the config
            except tk.TclError:
                pass
        self.save_now()
        self.ticker.stop()
        # Stop the tray first: its thread must not outlive Tk.  The lock
        # closes the window where the thread has made its icon but not yet
        # published it, which would leave an orphan icon in the tray.
        with self._tray_lock:
            tray, self._tray = self._tray, None
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
        for win in list(self.windows):
            try:
                win.destroy()
            except tk.TclError:
                pass
        self.windows.clear()
        if self.lock is not None:
            self.lock.release()
        # Cancel every timer still pending in this interpreter.  Tk shares one
        # event loop per thread, so a timer outliving its app would fire into
        # deleted commands the next time any Tk root runs.
        try:
            for job in self.tk.splitlist(self.tk.call("after", "info")):
                self.after_cancel(job)
        except tk.TclError:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------
def _grab(win, path):
    """Screenshot one window from the real screen."""
    from PIL import ImageGrab
    win.update()
    x, y = win.winfo_rootx(), win.winfo_rooty()
    w, h = win.winfo_width(), win.winfo_height()
    kw = {} if IS_WINDOWS or IS_MAC else {"xdisplay": None}
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h), **kw)
    img.save(path)
    return img.size


def run_screenshots(out_dir):
    """Render every surface for review.  CI runs this on a real Windows
    desktop and uploads the result, so the UI can be checked without a
    Windows machine."""
    os.makedirs(out_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="solari-shots-")
    report = []
    # 1. pure renders: every preset, both seconds sizes, 12-hour
    if HAS_PIL:
        now = datetime.datetime(2026, 3, 14, 9, 41, 27, tzinfo=datetime.timezone.utc)
        tiles = []
        for i, (name, tc, cc, fin) in enumerate(PRESETS):
            cfg = dict(DEFAULT_CLOCK, label=name, text_color=tc, card_color=cc, finish=fin,
                       seconds=SECONDS_MODES[i % 3], hour24=i % 2 == 0,
                       face="signage" if i % 4 == 3 else "modern")
            tiles.append(compose_clock(cfg, now=now, scale=1.0))
        W = max(t.width for t in tiles) * 2 + 60
        H = sum(t.height for t in tiles[::2]) + 20 * (len(tiles) // 2) + 40
        sheet = Image.new("RGB", (W, H), THEME["bg"])
        y = 20
        for i in range(0, len(tiles), 2):
            sheet.paste(tiles[i], (20, y))
            if i + 1 < len(tiles):
                sheet.paste(tiles[i + 1], (40 + tiles[i].width, y))
            y += max(t.height for t in tiles[i:i + 2]) + 20
        sheet.save(os.path.join(out_dir, "presets.png"))
        report.append("presets.png")

    # 2. the live app
    app = App(config_file=os.path.join(tmp, "solari_config.json"))
    steps = []

    def shot(name, win):
        # Clocks are always on top and CI's desktop is small: lift the
        # window being shot above them for the moment it takes.
        try:
            win.wm_attributes("-topmost", True)
            win.lift()
        except tk.TclError:
            pass
        try:
            size = _grab(win, os.path.join(out_dir, name))
            report.append(f"{name} {size[0]}x{size[1]}")
        except Exception as e:
            report.append(f"{name} failed: {e}")

    def desktop(name):
        try:
            from PIL import ImageGrab
            kw = {"all_screens": True} if IS_WINDOWS else ({} if IS_MAC else {"xdisplay": None})
            ImageGrab.grab(**kw).save(os.path.join(out_dir, name))
            report.append(name)
        except Exception as e:
            report.append(f"{name} failed: {e}")

    def run(i=0):
        if i < len(steps):
            delay, fn = steps[i]
            fn()
            app.after(delay, lambda: run(i + 1))

    steps += [
        (900, lambda: None),
        (300, lambda: shot("manager.png", app.mgr)),
        (300, lambda: [shot(f"clock_{k}.png", w) for k, w in enumerate(app.windows)]),
        (1200, lambda: app.edit_clock(app.windows[0])),
        (300, lambda: shot("editor.png", app.editor)),
        (300, lambda: desktop("desktop.png")),
        (300, lambda: app.editor.cancel()),
        (800, lambda: app.new_clock()),
        (300, lambda: (app.editor.v_search.set("san fran"))),
        (500, lambda: shot("editor_new.png", app.editor)),
        (300, lambda: app.editor.cancel()),
        (600, lambda: [app._drop(w) for w in list(app.windows)]),
        (300, lambda: shot("manager_empty.png", app.mgr)),
        (100, app.quit_app),
    ]
    app.after(10, run)
    app.mainloop()
    with open(os.path.join(out_dir, "report.txt"), "w", encoding="utf-8") as f:
        f.write(f"UI scale {UI_SCALE:.2f}\nfonts {font_report()}\n" + "\n".join(report) + "\n")
        if app.errors:
            f.write("\nERRORS\n" + "\n".join(app.errors))
    print("\n".join(report))
    return 1 if app.errors else 0


def run_selftest():
    """Start the whole app on a scratch config, exercise every main path,
    and quit.  CI runs this against the frozen exe too, where a missing
    hidden import only shows up at runtime."""
    tmp = tempfile.mkdtemp(prefix="solari-selftest-")
    path = os.path.join(tmp, "solari_config.json")
    app = App(config_file=path)
    log = []

    def exercise():
        w0 = app.windows[0]
        ed = app.edit_clock(w0)
        ed.v_text.set("#39ff6e")
        ed.v_seconds.set("large")
        ed.v_search.set("tokyo")
        ed._run_search()
        ed.save()
        log.append(f"edit -> {w0.cfg['text_color']} {w0.cfg['seconds']}")
        dup = app.duplicate_clock(w0)
        log.append(f"duplicate -> {len(app.windows)} clocks")
        app.remove_clock(dup, confirm=False)
        new = app.new_clock()
        app.editor.cancel()
        log.append(f"new+cancel -> {len(app.windows)} clocks, kept={new in app.windows}")
        app.recall_all()
        app.mgr.hide()
        app.show_manager()

    app.after(600, exercise)
    app.after(2200, app.quit_app)
    app.mainloop()
    with open(path, encoding="utf-8") as f:
        saved = json.load(f)["clocks"]
    ok = not app.errors and len(saved) == len(DEFAULT_CONFIG)
    print("\n".join(log))
    print(f"selftest {'OK' if ok else 'FAILED'} - {len(saved)} clocks saved, "
          f"Pillow={HAS_PIL}, tray={HAS_TRAY}, scale={UI_SCALE:.2f}, fonts={font_report()}")
    if app.errors:
        print("\n".join(app.errors))
    return 0 if ok else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--fonts" in argv:
        for role, name in font_report().items():
            print(f"{role:8} {name}")
        return 0
    enable_dpi_awareness()
    if "--selftest" in argv:
        return run_selftest()
    if "--screenshot" in argv:
        i = argv.index("--screenshot")
        out = argv[i + 1] if i + 1 < len(argv) else "screenshots"
        return run_screenshots(out)
    lock = InstanceLock(config_dir())
    if not lock.acquire():
        # Already running: bring its manager forward instead of doubling up.
        lock.request_show()
        return 0
    app = App(lock=lock, background="--background" in argv)
    try:
        app.mainloop()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
