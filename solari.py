"""
Solari — Flip-Clock Desktop Widget
Drum-roll animation · Corner-drag resize · Author: Cervezagua
"""

import tkinter as tk
import tkinter.messagebox
import datetime, zoneinfo, json, os, sys

try:
    import winreg
    HAS_WINREG = True
except ImportError:
    HAS_WINREG = False

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

# ─────────────────────────────────────────────────────────────────────────────
#  Config  →  %APPDATA%\Solari\
# ─────────────────────────────────────────────────────────────────────────────
APP_NAME    = "Solari"
STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

def config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d

def config_path():
    return os.path.join(config_dir(), "solari_config.json")

def exe_path():
    return sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)

def set_startup(enable):
    if not HAS_WINREG: return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0, winreg.KEY_SET_VALUE)
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe_path()}"')
        else:
            try: winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError: pass
        winreg.CloseKey(key)
        return True
    except Exception: return False

def get_startup():
    if not HAS_WINREG: return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except Exception: return False

# ─────────────────────────────────────────────────────────────────────────────
#  Colour palette
#  Fix #2: "Grey" = the dark card bg (#1e1e1e) that appears behind white text —
#  exactly the shade requested. Not a mid-grey.
# ─────────────────────────────────────────────────────────────────────────────
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

# Default dark card tint paired with each text colour
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

ALL_ZONES = sorted(zoneinfo.available_timezones())

DEFAULT_CONFIG = [
    {"tz":"America/New_York","label":"New York","x":60, "y":80,"text_color":"#ffffff","card_color":"#1e1e1e","scale":1.0},
    {"tz":"Europe/London",   "label":"London",  "x":500,"y":80,"text_color":"#2277ff","card_color":"#001033","scale":1.0},
    {"tz":"Asia/Tokyo",      "label":"Tokyo",   "x":940,"y":80,"text_color":"#cc0000","card_color":"#1a0000","scale":1.0},
]

# ─────────────────────────────────────────────────────────────────────────────
#  Card geometry (base at scale = 1.0)
# ─────────────────────────────────────────────────────────────────────────────
BASE_W  = 82
BASE_H  = 104   # full card height — no divider gap
BASE_FS = 60    # font pt
BASE_R  = 10    # corner radius

NEON = {"#00ff41"}

def _darken(hex_c, f):
    h = hex_c.lstrip("#")
    r, g, b = int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
    return f"#{int(r*f):02x}{int(g*f):02x}{int(b*f):02x}"

def _lerp(a, b, t):
    ah, bh = a.lstrip("#"), b.lstrip("#")
    ra,ga,ba = int(ah[0:2],16), int(ah[2:4],16), int(ah[4:6],16)
    rb,gb,bb = int(bh[0:2],16), int(bh[2:4],16), int(bh[4:6],16)
    return f"#{int(ra+(rb-ra)*t):02x}{int(ga+(gb-ga)*t):02x}{int(ba+(bb-ba)*t):02x}"

def _smoothstep(t):
    return t * t * (3 - 2 * t)


# ─────────────────────────────────────────────────────────────────────────────
#  FlipCard  —  DRUM ROLL  (slot machine scroll upward)
#
#  Fix #3: NO divider. The card is one unbroken rounded rectangle.
#  Two stacked Canvas widgets (top-half / bottom-half) give natural clipping.
#  Drum roll: old digit scrolls off top, new digit enters from bottom.
# ─────────────────────────────────────────────────────────────────────────────
DRUM_STEPS = 12
DRUM_MS    = 14

class FlipCard(tk.Frame):

    def __init__(self, parent, text_color="#ffffff", card_color="#1e1e1e",
                 scale=1.0, **kw):
        super().__init__(parent, bg="#000000", **kw)
        self.text_color = text_color
        self.card_color = card_color
        self._cur  = "0"
        self._nxt  = "0"
        self._busy = False
        self._apply_scale(scale)
        self._build_canvases()
        self._draw_static(self._cur)

    def _apply_scale(self, scale):
        self.scale = scale
        self.W   = max(20, int(BASE_W  * scale))
        self.H   = max(24, int(BASE_H  * scale))
        self.MID = self.H // 2
        self.R   = max(3,  int(BASE_R  * scale))
        self.FS  = max(8,  int(BASE_FS * scale))

    def _build_canvases(self):
        for w in self.winfo_children():
            w.destroy()
        # Two half-height canvases — no gap frame between them
        self._top = tk.Canvas(self, width=self.W, height=self.MID,
                               bg="#000000", highlightthickness=0)
        self._top.pack(side="top")
        self._bot = tk.Canvas(self, width=self.W, height=self.MID,
                               bg="#000000", highlightthickness=0)
        self._bot.pack(side="top")

    # ── rounded rect ─────────────────────────────────────────────────────────
    def _rr(self, cv, x1, y1, x2, y2, **kw):
        r = min(self.R, (x2-x1)//2, (y2-y1)//2)
        pts = [
            x1+r,y1, x2-r,y1, x2,y1,   x2,y1+r,
            x2,y2-r, x2,y2,   x2-r,y2, x1+r,y2,
            x1,y2,   x1,y2-r, x1,y1+r, x1,y1,
        ]
        return cv.create_polygon(pts, smooth=True, **kw)

    # ── draw one half-canvas ─────────────────────────────────────────────────
    def _draw_half(self, cv, is_top, strips):
        """
        strips: list of (digit, y_offset, alpha)
          y_offset: vertical pixel shift of the digit centre from its natural position
          alpha:    brightness 0..1
        """
        cv.delete("all")
        W, MID, H = self.W, self.MID, self.H
        cc   = self.card_color
        neon = self.text_color.lower() in NEON
        FONT = ("Segoe UI", self.FS, "bold")

        # Card background — full card drawn, canvas clips to its half
        if is_top:
            self._rr(cv, 0, 0, W, H, fill=cc, outline="")
        else:
            self._rr(cv, 0, -MID, W, MID, fill=cc, outline="")

        for digit, y_off, alpha in strips:
            if alpha < 0.02:
                continue
            tc     = _darken(self.text_color, max(0.0, alpha))
            bg_mix = _lerp(cc, "#000000", max(0.0, 1.0 - alpha))

            # Canvas y of the digit centre
            cy = (MID + y_off) if is_top else (0 + y_off)

            # Scroll band — darker strip behind the moving digit
            if alpha < 0.92:
                band_h = max(4, int(H * 0.45))
                by1 = max(0, cy - band_h // 2)
                by2 = min(MID, cy + band_h // 2)
                if by2 > by1:
                    cv.create_rectangle(0, by1, W, by2, fill=bg_mix, outline="")

            if neon and alpha > 0.25:
                for off in (3, 2, 1):
                    cv.create_text(W//2, cy, text=digit, font=FONT,
                                   fill=_darken(self.text_color, off*0.15*alpha),
                                   anchor="center")
            cv.create_text(W//2, cy, text=digit, font=FONT, fill=tc, anchor="center")

        if neon:
            gc = _darken(self.text_color, 0.55)
            if is_top:
                self._rr(cv, 0, 0, W, H, fill="", outline=gc, width=2)
            else:
                self._rr(cv, 0, -MID, W, MID, fill="", outline=gc, width=2)

    def _draw_static(self, d):
        self._draw_half(self._top, True,  [(d, 0, 1.0)])
        self._draw_half(self._bot, False, [(d, 0, 1.0)])

    # ── public ────────────────────────────────────────────────────────────────
    def set(self, digit):
        if digit == self._cur: return
        if self._busy:
            self._nxt = digit; return
        self._nxt  = digit
        self._busy = True
        self._drum(0)

    def update_colors(self, tc, cc):
        self.text_color = tc
        self.card_color = cc
        if not self._busy:
            self._draw_static(self._cur)

    def rebuild(self, scale):
        old = self._cur
        self._apply_scale(scale)
        self._build_canvases()
        self._cur = old
        self._draw_static(self._cur)

    # ── drum roll ─────────────────────────────────────────────────────────────
    def _drum(self, step):
        t   = _smoothstep(step / DRUM_STEPS)
        H   = self.H

        old_off   = int(-H * t)
        nxt_off   = int( H * (1.0 - t))
        old_alpha = max(0.0, 1.0 - t * 1.4)
        nxt_alpha = max(0.0, (t - 0.3) / 0.7)

        strips = []
        if old_alpha > 0.02: strips.append((self._cur, old_off, old_alpha))
        if nxt_alpha > 0.02: strips.append((self._nxt, nxt_off, nxt_alpha))

        self._draw_half(self._top, True,  strips)
        self._draw_half(self._bot, False, strips)

        if step < DRUM_STEPS:
            self.after(DRUM_MS, lambda: self._drum(step + 1) if self.winfo_exists() else None)
        else:
            self._cur  = self._nxt
            self._busy = False
            if self.winfo_exists():
                self._draw_static(self._cur)


# ─────────────────────────────────────────────────────────────────────────────
#  ClockWindow
# ─────────────────────────────────────────────────────────────────────────────
SCALE_MIN  = 0.4
SCALE_MAX  = 3.0
HANDLE_PX  = 14   # corner handle size

class ClockWindow(tk.Toplevel):
    def __init__(self, master, cfg, on_close, on_edit):
        super().__init__(master)
        self.cfg      = cfg
        self.on_close = on_close
        self.on_edit  = on_edit
        self._drag    = None   # (off_x, off_y) for window move
        self._resize  = None   # (root_x, root_y, scale0, win_x, win_y) for resize
        self._prev    = {}
        self._scale   = float(cfg.get("scale", 1.0))

        self.overrideredirect(True)
        self.wm_attributes("-topmost", True)
        self.wm_attributes("-alpha", 0.97)
        self.configure(bg="#000000")

        self._build_ui()
        self.geometry(f"+{cfg.get('x',100)}+{cfg.get('y',100)}")
        self._bind_drag(self)
        self._tick()

    def _tc(self): return self.cfg.get("text_color", "#ffffff")
    def _cc(self): return self.cfg.get("card_color", "#1e1e1e")

    def _build_ui(self):
        tc = self._tc()

        # Header
        self._bar = tk.Frame(self, bg="#0c0c0c", pady=5, padx=10)
        self._bar.pack(fill="x")
        tk.Label(self._bar, text="●", fg=tc, bg="#0c0c0c",
                 font=("Segoe UI", 8)).pack(side="left")
        self.lbl_city = tk.Label(self._bar, text=self.cfg.get("label",""),
                                  fg=tc, bg="#0c0c0c",
                                  font=("Segoe UI", 9, "bold"), padx=5)
        self.lbl_city.pack(side="left")
        tk.Button(self._bar, text="✕", command=self._close,
                  bg="#0c0c0c", fg="#444444", relief="flat", bd=0,
                  font=("Segoe UI", 9), cursor="hand2",
                  activeforeground="#ff4444", padx=5).pack(side="right")
        tk.Button(self._bar, text="✎", command=self._edit,
                  bg="#0c0c0c", fg="#444444", relief="flat", bd=0,
                  font=("Segoe UI", 9), cursor="hand2",
                  activeforeground=tc, padx=5).pack(side="right")

        # Cards
        self._face = tk.Frame(self, bg="#000000", padx=10, pady=10)
        self._face.pack()
        self._row = tk.Frame(self._face, bg="#000000")
        self._row.pack()
        self.cards   = {}
        self._colons = []
        self._build_cards()

        # Footer — Fix #4: bold, one tick larger
        foot = tk.Frame(self, bg="#000000", padx=10, pady=4)
        foot.pack(fill="x")
        self.lbl_date = tk.Label(foot, text="", fg=tc, bg="#000000",
                                  font=("Segoe UI", 11, "bold"))
        self.lbl_date.pack(side="left")
        self.lbl_tz = tk.Label(foot, text="", fg=tc, bg="#000000",
                                font=("Segoe UI", 11, "bold"))
        self.lbl_tz.pack(side="right")

        # Corner resize handle — Fix #1: track from window top-left, not relx/rely
        hc = tk.Canvas(self, width=HANDLE_PX, height=HANDLE_PX,
                        bg="#000000", highlightthickness=0, cursor="size_nw_se")
        hc.place(relx=1.0, rely=1.0, anchor="se")
        hc.create_polygon(HANDLE_PX,0, HANDLE_PX,HANDLE_PX, 0,HANDLE_PX,
                           fill="#2a2a2a", outline="")
        hc.bind("<ButtonPress-1>",   self._rs_start)
        hc.bind("<B1-Motion>",       self._rs_move)
        hc.bind("<ButtonRelease-1>", self._rs_end)
        self._handle = hc

        self._blink_on = True
        self._blink()

    def _build_cards(self):
        for w in self._row.winfo_children():
            w.destroy()
        self.cards   = {}
        self._colons = []
        s  = self._scale
        cf = max(14, int(38 * s))
        px = max(2,  int(3  * s))
        cy = max(4,  int(12 * s))

        def add_card(key):
            fc = FlipCard(self._row, self._tc(), self._cc(), s)
            fc.pack(side="left", padx=px)
            self.cards[key] = fc

        def add_colon():
            c = tk.Label(self._row, text=":", fg="#303030", bg="#000000",
                         font=("Segoe UI", cf, "bold"))
            c.pack(side="left", padx=max(2,int(4*s)), pady=(0,cy))
            self._colons.append(c)

        for k in ("h1","h2"): add_card(k)
        add_colon()
        for k in ("m1","m2"): add_card(k)
        add_colon()
        for k in ("s1","s2"): add_card(k)

        for key, d in self._prev.items():
            if key in self.cards:
                self.cards[key]._cur = d
                self.cards[key]._draw_static(d)

        self._bind_drag(self._row)

    def _blink(self):
        if not self.winfo_exists(): return
        c = "#484848" if self._blink_on else "#1c1c1c"
        for col in self._colons:
            try: col.config(fg=c)
            except: pass
        self._blink_on = not self._blink_on
        self.after(500, self._blink)

    # ── Window drag ───────────────────────────────────────────────────────────
    def _bind_drag(self, w):
        w.bind("<ButtonPress-1>",   self._ds, add="+")
        w.bind("<B1-Motion>",       self._dm, add="+")
        w.bind("<ButtonRelease-1>", self._de, add="+")
        for c in w.winfo_children():
            self._bind_drag(c)

    def _ds(self, e):
        if e.widget is self._handle: return
        self._drag = (e.x_root - self.winfo_x(), e.y_root - self.winfo_y())

    def _dm(self, e):
        if self._drag:
            self.geometry(f"+{e.x_root-self._drag[0]}+{e.y_root-self._drag[1]}")

    def _de(self, e):
        if self._drag:
            self.cfg["x"] = self.winfo_x()
            self.cfg["y"] = self.winfo_y()
        self._drag = None

    # ── Corner resize ────────────────────────────────────────────────────────
    # Strategy: window top-left is FIXED. The cursor drags the bottom-right
    # corner. Scale is derived directly from (mouse_x - window_left) so the
    # handle tracks the cursor with no repositioning arithmetic needed.
    def _rs_start(self, e):
        self._drag = None
        self.update_idletasks()
        # Natural window width at current scale — used to derive pixels-per-unit
        win_w = self.winfo_width()
        self._resize = (
            self.winfo_x(),   # window left — stays fixed throughout
            self.winfo_y(),   # window top  — stays fixed throughout
            self._scale,       # scale at drag start
            win_w,             # window width at drag start
            e.x_root,         # mouse x at drag start
        )

    def _rs_move(self, e):
        if not self._resize: return
        wx, wy, s0, w0, rx0 = self._resize
        # How far the cursor has moved from drag-start
        dx = e.x_root - rx0
        # Derive new scale: width grows by dx, each pixel = s0/w0 scale units
        new_scale = round(max(SCALE_MIN, min(SCALE_MAX, s0 + dx * s0 / w0)), 2)
        if abs(new_scale - self._scale) >= 0.01:
            self._scale = new_scale
            for fc in self.cards.values():
                fc.rebuild(new_scale)
            cf = max(14, int(38 * new_scale))
            cy = max(4,  int(12 * new_scale))
            px = max(2,  int(3  * new_scale))
            for c in self._colons:
                c.config(font=("Segoe UI", cf, "bold"))
                c.pack_configure(padx=max(2, int(4 * new_scale)), pady=(0, cy))
            for fc in self.cards.values():
                fc.pack_configure(padx=px)
            # Keep window pinned at its original top-left
            self.geometry(f"+{wx}+{wy}")

    def _rs_end(self, e):
        if self._resize:
            self.cfg["scale"] = self._scale
            self.cfg["x"] = self.winfo_x()
            self.cfg["y"] = self.winfo_y()
            self._build_cards()
            self._handle.place(relx=1.0, rely=1.0, anchor="se")
        self._resize = None

    def _close(self): self.on_close(self)
    def _edit(self):  self.on_edit(self)

    def _tick(self):
        if not self.winfo_exists(): return
        try:
            tz  = zoneinfo.ZoneInfo(self.cfg["tz"])
            now = datetime.datetime.now(tz)
            h, m, s = now.strftime("%H"), now.strftime("%M"), now.strftime("%S")
            for key, d in [("h1",h[0]),("h2",h[1]),
                           ("m1",m[0]),("m2",m[1]),
                           ("s1",s[0]),("s2",s[1])]:
                if self._prev.get(key) != d:
                    self.cards[key].set(d)
                    self._prev[key] = d
            self.lbl_date.config(text=now.strftime("%a  %b %d  %Y"))
            self.lbl_tz.config(text=self.cfg.get("label", self.cfg["tz"]))
        except Exception as ex:
            try: self.lbl_date.config(text=str(ex)[:50])
            except: pass
        self.after(250, self._tick)

    def refresh_style(self):
        tc = self._tc()
        self.lbl_city.config(text=self.cfg.get("label",""), fg=tc)
        self.lbl_date.config(fg=tc)
        self.lbl_tz.config(fg=tc)
        self._build_cards()


# ─────────────────────────────────────────────────────────────────────────────
#  SwatchPicker
# ─────────────────────────────────────────────────────────────────────────────
class SwatchPicker(tk.Frame):
    COLS = 6

    def __init__(self, parent, variable, **kw):
        super().__init__(parent, bg="#0d0d0d", **kw)
        self.var = variable
        for i, (color, name) in enumerate(PALETTE):
            light = color in ("#ffffff","#ffd700","#aaaaaa")
            sel   = "#000000" if light else "#ffffff"
            rb = tk.Radiobutton(self, variable=variable, value=color,
                                bg=color, activebackground=color,
                                selectcolor=sel,
                                indicatoron=False, relief="flat", bd=2,
                                width=3, height=1, cursor="hand2")
            rb.grid(row=i // self.COLS, column=i % self.COLS, padx=3, pady=3)
            self._tooltip(rb, name)

    def _tooltip(self, widget, text):
        def show(e):
            self._tip = tk.Label(self.winfo_toplevel(), text=text,
                                  bg="#333333", fg="#ffffff",
                                  font=("Segoe UI", 8), padx=4, pady=2)
            rx = e.x_root - self.winfo_toplevel().winfo_rootx()
            ry = e.y_root - self.winfo_toplevel().winfo_rooty()
            self._tip.place(x=rx+10, y=ry+10)
        def hide(e):
            if hasattr(self, "_tip"):
                try: self._tip.destroy()
                except: pass
        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)


# ─────────────────────────────────────────────────────────────────────────────
#  EditDialog
# ─────────────────────────────────────────────────────────────────────────────
class EditDialog(tk.Toplevel):
    def __init__(self, master, cfg, on_save):
        super().__init__(master)
        self.cfg     = cfg
        self.on_save = on_save
        self.title("Edit Clock")
        self.configure(bg="#0d0d0d")
        self.resizable(False, False)
        self.wm_attributes("-topmost", True)
        self.grab_set()
        self._user_edited_label = False
        self._build()

    def _build(self):
        P   = dict(padx=16, pady=9)
        LBL = dict(bg="#0d0d0d", fg="#888888", font=("Segoe UI", 11))
        ENT = dict(bg="#1c1c1c", fg="#ffffff", insertbackground="#ffffff",
                   font=("Segoe UI", 11), relief="flat",
                   highlightbackground="#333333", highlightthickness=1)

        tk.Label(self, text="Label", **LBL).grid(row=0, column=0, sticky="w", **P)
        self.e_lbl = tk.Entry(self, **ENT, width=26)
        self.e_lbl.insert(0, self.cfg.get("label",""))
        self.e_lbl.grid(row=0, column=1, **P, sticky="ew")
        self.e_lbl.bind("<Key>", lambda e: setattr(self, "_user_edited_label", True))

        tk.Label(self, text="Timezone", **LBL).grid(row=1, column=0, sticky="nw", **P)
        tz_f = tk.Frame(self, bg="#0d0d0d")
        tz_f.grid(row=1, column=1, **P, sticky="ew")
        self.sv = tk.StringVar()
        self.sv.trace("w", self._on_tz_change)
        tk.Entry(tz_f, textvariable=self.sv, **ENT, width=32).pack(fill="x")
        self.sv.set(self.cfg.get("tz","UTC"))

        lb_wrap = tk.Frame(tz_f, bg="#1c1c1c")
        lb_wrap.pack(fill="both", expand=True, pady=(2,0))
        sb = tk.Scrollbar(lb_wrap, orient="vertical", bg="#0d0d0d", troughcolor="#0d0d0d")
        sb.pack(side="right", fill="y")
        self.lb = tk.Listbox(lb_wrap, bg="#1c1c1c", fg="#aaaaaa",
                              selectbackground="#1e3a5f", selectforeground="#ffffff",
                              font=("Segoe UI",10), relief="flat", height=7,
                              yscrollcommand=sb.set, activestyle="none", bd=0)
        self.lb.pack(fill="both", expand=True)
        sb.config(command=self.lb.yview)
        self._populate(ALL_ZONES)
        self.lb.bind("<<ListboxSelect>>", self._on_lb_select)

        tk.Label(self, text="Text Color", **LBL).grid(row=2, column=0, sticky="nw", **P)
        self.tcv = tk.StringVar(value=self.cfg.get("text_color","#ffffff"))
        SwatchPicker(self, self.tcv).grid(row=2, column=1, **P, sticky="w")

        tk.Label(self, text="Card Color", **LBL).grid(row=3, column=0, sticky="nw", **P)
        self.ccv = tk.StringVar(value=self.cfg.get("card_color","#1e1e1e"))
        SwatchPicker(self, self.ccv).grid(row=3, column=1, **P, sticky="w")

        def match_card():
            self.ccv.set(PAL_DARK.get(self.tcv.get(), "#1e1e1e"))
        tk.Button(self, text="Auto-match card colour →", command=match_card,
                  bg="#1a1a1a", fg="#888888", font=("Segoe UI",9),
                  relief="flat", pady=3, cursor="hand2",
                  activeforeground="#ffffff"
                  ).grid(row=4, column=1, padx=16, pady=(0,6), sticky="w")

        bf = tk.Frame(self, bg="#0d0d0d")
        bf.grid(row=5, column=0, columnspan=2, pady=14)
        tk.Button(bf, text="  Save  ", command=self._save,
                  bg="#1e3a5f", fg="#ffffff", font=("Segoe UI",10,"bold"),
                  relief="flat", padx=18, pady=7, cursor="hand2",
                  activebackground="#2a4a7f").pack(side="left", padx=6)
        tk.Button(bf, text="Cancel", command=self.destroy,
                  bg="#0d0d0d", fg="#666666", font=("Segoe UI",10),
                  relief="flat", padx=18, pady=7, cursor="hand2").pack(side="left", padx=6)

        self.columnconfigure(1, weight=1)

    def _populate(self, zones):
        self.lb.delete(0, "end")
        for z in zones: self.lb.insert("end", z)

    def _on_tz_change(self, *_):
        q = self.sv.get().lower()
        self._populate([z for z in ALL_ZONES if q in z.lower()])
        if not self._user_edited_label:
            city = self.sv.get().split("/")[-1].replace("_"," ")
            self.e_lbl.delete(0,"end")
            self.e_lbl.insert(0, city)

    def _on_lb_select(self, _):
        s = self.lb.curselection()
        if not s: return
        tz_val = self.lb.get(s[0])
        was_edited = self._user_edited_label
        self.sv.set(tz_val)
        if not was_edited:
            city = tz_val.split("/")[-1].replace("_"," ")
            self.e_lbl.delete(0,"end")
            self.e_lbl.insert(0, city)
            self._user_edited_label = False

    def _save(self):
        tz = self.sv.get().strip()
        try: zoneinfo.ZoneInfo(tz)
        except Exception:
            tk.messagebox.showerror("Invalid Timezone",
                                    f"'{tz}' is not valid.\nPick one from the list.")
            return
        self.cfg["tz"]         = tz
        self.cfg["label"]      = self.e_lbl.get().strip() or tz.split("/")[-1]
        self.cfg["text_color"] = self.tcv.get()
        self.cfg["card_color"] = self.ccv.get()
        self.on_save()
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
#  ManagerWindow
# ─────────────────────────────────────────────────────────────────────────────
class ManagerWindow(tk.Toplevel):
    def __init__(self, master, on_add, on_quit, on_startup_toggle, startup_var):
        super().__init__(master)
        self.title("Solari")
        self.configure(bg="#0d0d0d")
        self.resizable(False, False)
        self.wm_attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._hide)
        self._on_add  = on_add
        self._on_quit = on_quit
        self._on_st   = on_startup_toggle
        self._sv      = startup_var
        self._build()

    def _hide(self):
        # Only hide to tray if tray is available — otherwise quitting is safer
        # than leaving the app running with no way to get it back
        if HAS_TRAY:
            self.withdraw()
        else:
            self._on_quit()
    def show(self):  self.deiconify(); self.lift()

    def _build(self):
        top = tk.Frame(self, bg="#0d0d0d", pady=28, padx=32)
        top.pack(fill="x")
        tk.Label(top, text="🕐", bg="#0d0d0d", fg="#ffffff",
                 font=("Segoe UI Emoji", 36)).pack()
        tk.Label(top, text="Solari", bg="#0d0d0d", fg="#ffffff",
                 font=("Segoe UI", 22, "bold")).pack(pady=(8,3))
        tk.Label(top, text="Floating flip-clock widgets for your desktop",
                 bg="#0d0d0d", fg="#555555", font=("Segoe UI",11)).pack()

        tk.Frame(self, bg="#222222", height=1).pack(fill="x")

        tk.Label(self, text="Drag ◢ corner to resize   ·   Drag anywhere to move",
                 bg="#0d0d0d", fg="#383838", font=("Segoe UI",10)).pack(pady=(12,4))

        btn_f = tk.Frame(self, bg="#0d0d0d", padx=28, pady=10)
        btn_f.pack(fill="x")

        tk.Button(btn_f, text="＋  Add Clock", command=self._on_add,
                  bg="#1e3a5f", fg="#ffffff", font=("Segoe UI",12,"bold"),
                  relief="flat", pady=12, cursor="hand2",
                  activebackground="#2a4a7f").pack(fill="x", pady=(0,10))

        st_f = tk.Frame(btn_f, bg="#151515")
        st_f.pack(fill="x", pady=(0,8))
        tk.Label(st_f, text="  Launch at Windows startup",
                 bg="#151515", fg="#aaaaaa", font=("Segoe UI",11)
                 ).pack(side="left", pady=8)
        tk.Checkbutton(st_f, variable=self._sv, command=self._on_st,
                       bg="#151515", selectcolor="#1e3a5f",
                       activebackground="#151515", relief="flat",
                       cursor="hand2").pack(side="right", padx=10)

        tk.Button(btn_f, text="Quit", command=self._on_quit,
                  bg="#1a1a1a", fg="#666666", font=("Segoe UI",11),
                  relief="flat", pady=10, cursor="hand2",
                  activeforeground="#ff5555").pack(fill="x")

        tk.Frame(self, bg="#1a1a1a", height=1).pack(fill="x", pady=(12,0))
        foot = tk.Frame(self, bg="#0d0d0d", pady=12)
        foot.pack(fill="x")
        tk.Label(foot, text="Config  %APPDATA%\\Solari",
                 bg="#0d0d0d", fg="#2a2a2a", font=("Segoe UI",9)).pack()
        tk.Label(foot, text="by  Cervezagua",
                 bg="#0d0d0d", fg="#333333", font=("Segoe UI",10,"italic")).pack(pady=(5,0))

        self.minsize(360, 0)
        self.update_idletasks()


# ─────────────────────────────────────────────────────────────────────────────
#  Tray icon
# ─────────────────────────────────────────────────────────────────────────────
def _make_tray_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    d.ellipse([4, 4, 60, 60], outline="white", width=4)
    d.line([32, 14, 32, 34], fill="white", width=4)
    d.line([32, 34, 46, 48], fill="white", width=4)
    return img


# ─────────────────────────────────────────────────────────────────────────────
#  App
# ─────────────────────────────────────────────────────────────────────────────
COLOR_CYCLE = ["#ffffff","#2277ff","#cc0000","#00aa00","#ffd700","#8833cc"]

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        self.title("Solari")
        self.windows: list[ClockWindow] = []
        self._tray   = None
        self._load()
        self._sv = tk.BooleanVar(value=get_startup())
        self.mgr = ManagerWindow(self,
                                  on_add=self._add,
                                  on_quit=self._quit,
                                  on_startup_toggle=self._toggle_startup,
                                  startup_var=self._sv)
        for cfg in self.configs:
            self._spawn(cfg)
        self.protocol("WM_DELETE_WINDOW", self._quit)
        if HAS_TRAY:
            self._start_tray()

    def _load(self):
        try:
            with open(config_path()) as f:
                data = json.load(f)
            assert isinstance(data, list) and len(data) > 0
            for c in data:
                if "text_color" not in c:
                    c["text_color"] = c.pop("color","#ffffff")
                if "card_color" not in c:
                    c["card_color"] = PAL_DARK.get(c.get("text_color","#ffffff"),"#1e1e1e")
                if "scale" not in c:
                    c["scale"] = 1.0
                # migrate old greys
                if c.get("text_color") in ("#bbbbbb","#ddcc00"):
                    c["text_color"] = "#ffffff"
            self.configs = data
        except Exception:
            self.configs = [dict(c) for c in DEFAULT_CONFIG]

    def _save(self):
        try:
            with open(config_path(),"w") as f:
                json.dump(self.configs, f, indent=2)
        except Exception: pass

    def _start_tray(self):
        import threading, traceback

        log_path = os.path.join(config_dir(), "tray_error.log")

        def _tray_thread():
            try:
                menu = pystray.Menu(
                    pystray.MenuItem("Show Manager", lambda: self.after(0, self.mgr.show), default=True),
                    pystray.MenuItem("Add Clock",    lambda: self.after(0, self._add)),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Quit",         lambda: self.after(0, self._quit)),
                )
                icon = pystray.Icon("Solari", _make_tray_image(), "Solari", menu)
                self._tray = icon

                def _setup(ic):
                    ic.visible = True

                icon.run(setup=_setup)
            except Exception:
                with open(log_path, "w") as f:
                    traceback.print_exc(file=f)
                self._tray = None
                # Show manager as fallback so user isn't stranded
                self.after(0, self.mgr.deiconify)

        threading.Thread(target=_tray_thread, daemon=True).start()

    def _toggle_startup(self):
        if not set_startup(self._sv.get()):
            tk.messagebox.showwarning("Startup",
                "Could not modify startup registry.\nTry running as administrator.")
            self._sv.set(not self._sv.get())

    def _spawn(self, cfg):
        w = ClockWindow(self, cfg, on_close=self._remove, on_edit=self._edit)
        self.windows.append(w)

    def _add(self):
        n  = len(self.configs)
        tc = COLOR_CYCLE[n % len(COLOR_CYCLE)]
        cfg = {"tz":"UTC","label":"UTC",
               "x":150+n*40,"y":150+n*30,
               "text_color":tc,
               "card_color":PAL_DARK.get(tc,"#1e1e1e"),
               "scale":1.0}
        self.configs.append(cfg)
        self._spawn(cfg)
        self._edit(self.windows[-1])
        self._save()

    def _remove(self, win):
        if win.cfg in self.configs: self.configs.remove(win.cfg)
        if win in self.windows:     self.windows.remove(win)
        win.destroy()
        self._save()

    def _edit(self, win):
        def saved():
            win.refresh_style()
            self._save()
        EditDialog(self, win.cfg, saved)

    def _quit(self):
        self._save()
        # Stop tray first — its thread must not touch tkinter after destroy()
        if self._tray:
            try: self._tray.stop()
            except: pass
            self._tray = None
        # Explicitly destroy all clock windows to cancel their after() callbacks
        for win in list(self.windows):
            try: win.destroy()
            except: pass
        self.windows.clear()
        # Finally destroy the root window
        try: self.destroy()
        except: pass


if __name__ == "__main__":
    App().mainloop()
