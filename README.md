# 🕐 Solari

[![CI](https://github.com/cervezagua/Solari/actions/workflows/ci.yml/badge.svg)](https://github.com/cervezagua/Solari/actions/workflows/ci.yml)

Floating flip-clock widgets for your Windows desktop.
Each time zone lives in its own frameless, draggable, always-on-top window: a
machined housing with a recessed well and a row of flip cards that roll over
to the next digit.

Named after the iconic **Solari di Udine** split-flap boards found in airports and train stations worldwide.

---
<img width="400" height="400" alt="image" src="https://github.com/user-attachments/assets/1e06f069-d5f7-4fa8-803c-d73b855b49d4" />

## 📦 Files

| File | Purpose |
|---|---|
| `solari.py` | The whole app - a single file, drop it anywhere |
| `test_solari.py` | Tests: pure logic everywhere, the GUI when a display is available |
| `BUILD.bat` | One-click EXE builder |
| `solari_config.json` | Auto-created on first run in `%APPDATA%\Solari\` |

---

## 🚀 Quick Start (no build needed)

1. Install Python 3.10+ from https://python.org
2. `pip install tzdata pystray Pillow`
3. Run: `python solari.py`

> **Pillow is what makes it look like hardware.** Tk's canvas cannot
> antialias, draw gradients or blend alpha, so every surface - housings, cards,
> digits, icons, buttons - is rendered with Pillow and cached. Without it the
> app still runs, with flat cards and plain text.

---

## 🔨 Build a standalone EXE

1. Install Python 3.10+ (check "Add to PATH")
2. Double-click **`BUILD.bat`**
3. Find `Solari.exe` in `dist/`

> Self-contained - no Python needed on the target machine.

---

## 🖥 Usage

### The clocks

- **Drag** a clock anywhere. It snaps to screen edges and to other clocks;
  hold **Shift** to place it freely. Clocks remember their monitor.
- **Hover** to reveal its controls: **✎ edit**, **✕ remove**, and the
  **resize grip** in the bottom-right corner (or **Ctrl + mouse wheel**).
- **Double-click** to edit, **right-click** for quick options (seconds,
  finish, 24-hour, always on top, duplicate, remove).
- Each footer shows the date, the **UTC offset** (`GMT+9`) and a **`+1` / `−1`**
  badge when that zone is on a different calendar day from yours.

### The manager

Opens on launch (at sign-in only the clocks appear - the tray icon, any
clock's menu, or launching Solari again brings it back). It lists every clock
with a **live flip-card thumbnail**, where it is and how far ahead or behind
you. Hover a row for **bring into view**, **edit** and **remove**; rows also
work from the keyboard (↑ ↓, Enter, Delete). **Ctrl+N** adds a clock.

**Gather clocks** lines every clock up on the main screen - handy after
unplugging a monitor.

### The editor

Every change shows on the real clock as you make it, with a live preview on
top; **Cancel** puts everything back.

| Option | What it does |
|---|---|
| Name | Anything you like - it is kept when you change the zone |
| Time zone | Search by city, country, former name, abbreviation (`PST`, `JST`) or offset (`+9`, `GMT-3:30`) |
| Style | Eight presets, or your own digit and card colours (low contrast is flagged) |
| Finish | Graphite or brushed Aluminium housing |
| Typeface | Modern, or Signage (Bahnschrift on Windows) |
| Seconds | Off, small, or full-size cards |
| Format | 24-hour, or 12-hour with AM/PM |
| Size, Opacity | 40 %-300 %, 35 %-100 % |
| Always on top | Let other windows cover this clock |

All settings save as you go to `%APPDATA%\Solari\solari_config.json`.

---

## 🔁 Launch at sign-in

Turn on **Launch at sign-in** in the manager. Solari then starts quietly with
just your clocks. Only one copy ever runs: launching it again simply brings
the manager forward.

---

## 🧪 Tests

```
python -m unittest test_solari -v
```

Covers the colour maths, config load/migrate/save, multi-monitor placement and
snapping, offset and day-badge labels, the layout and every renderer, zone
search, the single-instance lock - and, when a display is available, the flip
animation, clock windows, the editor, the manager and the tray hand-off.
Without a display the GUI tests skip themselves, so the same command works on
a build agent and on your desktop.

Every bug fixed in the redesign has a `Regression*` test that pins it, and one
test scans every widget class for names that shadow tkinter internals - the
cause of two earlier crashes.

Handy flags:

| Flag | Does |
|---|---|
| `--selftest` | Starts the app on a scratch config, exercises edit, duplicate, remove, new and gather, and exits with a status code |
| `--screenshot DIR` | Saves screenshots of the manager, editor and clocks (and a sheet of every preset) to `DIR` |
| `--fonts` | Prints which font file each text role resolved to |
| `--background` | Start with only the clocks showing (what sign-in uses) |

### CI

Every push runs the suite on Linux (under Xvfb) and on Windows with Python
3.10 and 3.13, then runs `BUILD.bat` on a Windows runner and launches the
resulting `Solari.exe --selftest`. A Windows job also takes screenshots on a
real Windows desktop and uploads them, along with the exe, as artifacts.

---

## 🛠 Notes on how it works

- **One shading model.** `render_surface` draws every raised and recessed
  surface - flip cards, manager rows, buttons, switches, sliders - lit from
  above with a specular edge, contact shadow and bevel, so the manager matches
  the clocks by construction.
- **One layout.** `clock_layout()` is the single source of truth for where
  everything on a clock goes. The live window, the editor preview and the
  manager thumbnails all use it, so they can never drift apart.
- **One timer.** A single scheduler re-aims at the next true second boundary
  on every tick, so digits flip *on* the second and cannot drift. Animation
  runs on one shared frame loop - ~60 fps while a card rolls, ~25 fps for the
  colon pulse, and stopped when nothing moves.
- **Nothing is created per frame.** Canvas items are made once and animated
  with `coords()` / `itemconfigure()`; renders are cached and shared between
  cards. Live resizing draws quick drafts and renders full quality on release.
- **High-DPI aware.** Windows is told the app handles DPI itself, and every
  size is written in device-independent pixels, so a clock is the same
  physical size and pin-sharp on a 100 % and a 200 % display.
- **Your clocks are never thrown away.** An unreadable config is kept as a
  backup, an unknown time zone is kept and flagged, removing every clock
  sticks, and saving retries while Windows holds the file.
- Runs on Linux and macOS too, with installed fonts and the platform's config
  directory. Launch at sign-in is Windows-only.
