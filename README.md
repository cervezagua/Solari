# 🕐 Solari

Floating flip-clock widgets for your Windows desktop.  
Each timezone lives in its own frameless, draggable, always-on-top window with a drum-roll animation.

Named after the iconic **Solari di Udine** split-flap boards found in airports and train stations worldwide.

---
<img width="400" height="400" alt="image" src="https://github.com/user-attachments/assets/1e06f069-d5f7-4fa8-803c-d73b855b49d4" />

## 📦 Files

| File | Purpose |
|---|---|
| `solari.py` | Main app source |
| `BUILD.bat` | One-click EXE builder |
| `solari_config.json` | Auto-created on first run in `%APPDATA%\Solari\` |

---

## 🚀 Quick Start (no build needed)

1. Install Python 3.10+ from https://python.org
2. `pip install tzdata pystray Pillow`
3. Run: `python solari.py`

---

## 🔨 Build a standalone EXE

1. Install Python 3.10+ (check "Add to PATH")
2. Double-click **`BUILD.bat`**
3. Find `Solari.exe` in `dist/`

> Self-contained — no Python needed on the target machine.

---

## 🖥 Usage

- **Manager window** opens on launch — add, configure, quit
- **Drag** any clock window anywhere on screen
- **✎** → edit timezone, label, colours
- **✕** → remove clock
- **Drag ◢ corner** → resize the clock
- All settings auto-saved to `%APPDATA%\Solari\solari_config.json`

---

## 🔁 Auto-start with Windows

Enable the **"Launch at Windows startup"** toggle in the manager window.

---

## 🌍 Timezones

Uses IANA timezone names, e.g. `America/New_York`, `Europe/London`, `Asia/Tokyo`.  
The edit dialog has a searchable list of all available timezones.
