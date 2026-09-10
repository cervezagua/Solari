# 🕐 Solari

Floating flip-clock widgets for your Windows desktop.
Each timezone lives in its own frameless, draggable, always-on-top window with a drum-roll animation.

Named after the iconic **Solari di Udine** split-flap boards found in airports and train stations worldwide.

---
<img width="400" height="400" alt="image" src="https://github.com/user-attachments/assets/1e06f069-d5f7-4fa8-803c-d73b855b49d4" />

## 📦 Files

| File | Purpose |
|---|---|
| `solari.py` | Main app source — single file, drop it anywhere |
| `test_solari.py` | Headless tests for the non-GUI logic |
| `BUILD.bat` | One-click EXE builder |
| `solari_config.json` | Auto-created on first run in `%APPDATA%\Solari\` |

---

## 🚀 Quick Start (no build needed)

1. Install Python 3.10+ from https://python.org
2. `pip install tzdata pystray Pillow`
3. Run: `python solari.py`

> **Pillow is what makes the cards look like cards.** Tk's canvas cannot
> antialias or draw gradients, so the dimensional card faces and window chassis
> are rendered with Pillow and cached. Without it the app still runs, with flat
> single-colour cards.

---

## 🔨 Build a standalone EXE

1. Install Python 3.10+ (check "Add to PATH")
2. Double-click **`BUILD.bat`**
3. Find `Solari.exe` in `dist/`

> Self-contained — no Python needed on the target machine.

---

## 🖥 Usage

- **Manager window** opens on launch — it lists every clock you own, with a live
  time preview and per-clock **✎ edit**, **Find**, and **✕ remove**
- **Drag** any clock window anywhere on screen — it snaps to screen edges and to
  other clocks. Hold **Shift** while dragging to place it freely
- **Drag ◢ corner** → resize the clock
- **✎** → timezone, label, colours, and display options
- **✕** → remove the clock (asks first)
- **Find** → pulls a clock back on screen if it ended up on a monitor you no
  longer have. **Bring all on screen** does the lot
- All settings auto-save to `%APPDATA%\Solari\solari_config.json`

### Per-clock options

| Option | What it does |
|---|---|
| 24-hour clock | Switch between 24h and 12h; 12h shows AM/PM in the footer |
| Show seconds | Hide the seconds pair for a narrower widget |
| Always on top | Let other windows cover this clock |
| Opacity | 35 %–100 % |
| Look | One-click presets, or pick text and card colours yourself |

Each clock's footer shows its **UTC offset** (`GMT+9`) and a **`+1` / `-1` badge**
when that timezone is on a different calendar day from yours.

---

## 🔁 Auto-start with Windows

Enable the **"Launch at Windows startup"** toggle in the manager window.

---

## 🌍 Timezones

Uses IANA timezone names, e.g. `America/New_York`, `Europe/London`, `Asia/Tokyo`.
The edit dialog has a searchable list of all available timezones.

---

## 🧪 Tests

```
python -m unittest test_solari -v
```

Runs headless — no display required. Covers the colour maths, config
load/migrate/save (including recovery from a corrupt or partly-invalid file),
off-screen clamping, UTC-offset and day-badge formatting, and the card renderer.

---

## 🛠 Notes on how it works

- **One timer for the whole app.** A single scheduler re-aims at the next true
  second boundary on every tick, so digits flip *on* the second and cannot
  drift. Animation runs on one shared ~60 fps frame loop that stops entirely
  when nothing is moving.
- **Nothing is redrawn per frame.** Canvas items are created once and animated
  with `coords()` / `itemconfigure()`. Card faces and the window chassis are
  rendered once per style and cached.
- **High-DPI aware on Windows**, so the app renders at native resolution instead
  of being bitmap-stretched by the OS. Clock-face text is pixel-sized to stay
  locked to the cards; UI text follows the display DPI.
- Runs on Linux and macOS too — it picks an installed font instead of assuming
  Segoe UI, and uses the platform's config directory. "Launch at startup" is
  Windows-only.
