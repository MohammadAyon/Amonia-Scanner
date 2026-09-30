"""
PDF Recolor - Batch PDF recoloring tool
---------------------------------
Batch-recolors PDFs: dark strokes become CONTENT color, paper becomes
BACKGROUND color, thin lines are thickened so they survive printing/scanning.

Run directly:   python PDF_Recolor_App.py
Build an .exe:  double-click Build_EXE.bat
"""

import io
import json
import multiprocessing
import os
import re
import stat
import queue
import threading
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from concurrent.futures.process import BrokenProcessPool
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk

import fitz  # PyMuPDF
import numpy as np
from PIL import Image, ImageFilter, ImageTk

# Optional drag & drop support (pip install tkinterdnd2)
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _BaseTk = TkinterDnD.Tk
    HAS_DND = True
except Exception:  # library missing -> app still works with buttons
    _BaseTk = tk.Tk
    HAS_DND = False

APP_NAME = "PDF Recolor"
__version__ = "1.0.0"
SETTINGS_FILE = Path.home() / ".pdf_recolor.json"
LEGACY_SETTINGS_FILE = Path.home() / ".amonia_scanner.json"

DEFAULTS = {
    "dpi": 300,
    "jpeg_quality": 95,
    "thicken": True,
    "background": "#D6DDF0",   # (214, 221, 240)
    "content": "#4849B2",      # (72, 73, 178)
    "output_mode": "same",     # "same" or "folder"
    "output_folder": "",
    "suffix": "_recolored",
}


# --------------------------------------------------------------------------
# Security helpers
# --------------------------------------------------------------------------
_MAX_PIXELS = 100_000_000   # ~300 MB per page – prevents OOM on oversized pages

_HEX_COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')


def _is_valid_hex_color(s):
    """Return True only for well-formed 7-char hex colors like #A1B2C3."""
    return isinstance(s, str) and bool(_HEX_COLOR_RE.match(s))


def _safe_suffix(raw):
    """Strip path separators and illegal filename characters from the suffix."""
    s = raw.strip().replace("/", "").replace("\\", "").replace("..", "")
    s = re.sub(r'[<>:"|?*]', '', s)
    return s or "_recolored"


def _is_unc_path(p):
    """Return True if the path is a UNC / network path."""
    return str(p).startswith("\\\\") or str(p).startswith("//")


# --------------------------------------------------------------------------
# Core processing (no GUI code in here)
# --------------------------------------------------------------------------
class Cancelled(Exception):
    pass


def hex_to_rgb(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)


def build_lut(background_hex, content_hex):
    """Lookup table indexed by (R+G+B) 0..765 -> output RGB.
    Same math as the original script (gray = mean, t = gray/245), but much
    faster and far lighter on memory than float math over a whole page."""
    bg, fg = hex_to_rgb(background_hex), hex_to_rgb(content_hex)
    s = np.arange(766, dtype=np.float32)
    t = np.clip((s / 3.0) / 245.0, 0.0, 1.0)[:, None]
    return (fg * (1.0 - t) + bg * t).astype(np.uint8)


def _min3(a):
    """3x3 minimum filter (thickens dark strokes), done as two 1-D passes.
    Pixel-for-pixel identical to PIL's MinFilter(3), which clamps the window at
    the image edges (equivalent to edge padding) - but ~40x faster."""
    p = np.pad(a, ((1, 1), (1, 1), (0, 0)), mode="edge")
    h = np.minimum(np.minimum(p[:, :-2], p[:, 1:-1]), p[:, 2:])   # across columns
    return np.minimum(np.minimum(h[:-2], h[1:-1]), h[2:])          # across rows


def recolor(pil_img, lut, thicken=True):
    a = np.asarray(pil_img, dtype=np.uint8)
    if thicken:
        a = _min3(a)
    s = a[..., 0].astype(np.uint16)          # R+G+B per pixel, 0..765
    s += a[..., 1]
    s += a[..., 2]
    return Image.fromarray(np.take(lut, s, axis=0))


def render_page(page, dpi):
    pix = page.get_pixmap(dpi=dpi, alpha=False, colorspace=fitz.csRGB)
    if pix.width * pix.height > _MAX_PIXELS:
        raise ValueError(
            f"Page too large ({pix.width}×{pix.height} px). "
            f"Try a lower DPI or split the document."
        )
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def _page_to_jpeg(page, dpi, quality, thicken, lut):
    img = recolor(render_page(page, dpi), lut, thicken)
    bio = io.BytesIO()
    img.save(bio, format="JPEG", quality=quality)
    return bio.getvalue(), page.rect.width, page.rect.height


# ---- worker-process side (each worker keeps its source PDF open) -----------
_WORKER = {"path": None, "doc": None}


def _worker_cleanup():
    """Close the cached fitz document when the worker process exits."""
    if _WORKER["doc"] is not None:
        try:
            _WORKER["doc"].close()
        except Exception:
            pass
        _WORKER["doc"] = None
        _WORKER["path"] = None


import atexit
atexit.register(_worker_cleanup)


def _render_job(job):
    path, idx, dpi, quality, thicken, bg, fg = job
    if _WORKER["path"] != path:
        if _WORKER["doc"] is not None:
            _WORKER["doc"].close()
        _WORKER["doc"] = fitz.open(path)
        _WORKER["path"] = path
    data, w, h = _page_to_jpeg(_WORKER["doc"][idx], dpi, quality, thicken, build_lut(bg, fg))
    return data, w, h


def make_pool(total_pages):
    """A process pool for batches big enough to benefit; None = run serially."""
    workers = min((os.cpu_count() or 1) - 1, 6)
    if workers < 2 or total_pages < 4:
        return None
    try:
        return ProcessPoolExecutor(max_workers=workers)
    except Exception:
        return None


def _pages_serial(src, cfg, lut, cancel_event):
    for page in src:
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        yield _page_to_jpeg(page, cfg["dpi"], cfg["jpeg_quality"], cfg["thicken"], lut)


def _pages_parallel(src_path, n, cfg, pool, cancel_event):
    """Yield pages IN ORDER while workers render ahead (small sliding window)."""
    window = getattr(pool, "_max_workers", 2) * 2
    pending, nxt = deque(), 0
    try:
        for _ in range(n):
            while nxt < n and len(pending) < window:
                pending.append(pool.submit(_render_job, (
                    src_path, nxt, cfg["dpi"], cfg["jpeg_quality"], cfg["thicken"],
                    cfg["background"], cfg["content"])))
                nxt += 1
            fut = pending.popleft()
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise Cancelled()
                try:
                    yield fut.result(timeout=0.2)
                    break
                except FutureTimeout:
                    continue
    finally:
        for f in pending:
            f.cancel()


def _build(src, src_path, out_path, cfg, progress_cb, cancel_event, pool):
    dst = fitz.open()
    try:
        n = len(src)
        if pool is not None and n >= 2:
            pages = _pages_parallel(src_path, n, cfg, pool, cancel_event)
        else:
            pages = _pages_serial(src, cfg, build_lut(cfg["background"], cfg["content"]),
                                  cancel_event)
        for i, (data, w, h) in enumerate(pages, 1):
            npg = dst.new_page(width=w, height=h)
            npg.insert_image(fitz.Rect(0, 0, w, h), stream=data)
            if progress_cb:
                progress_cb(i, n)
        try:  # keep bookmarks when possible
            toc = src.get_toc()
            if toc:
                dst.set_toc(toc)
        except Exception:
            pass
        dst.save(out_path, garbage=4, deflate=True)
    finally:
        dst.close()


def process_pdf(src_path, out_path, cfg, progress_cb=None, cancel_event=None, pool=None):
    src = fitz.open(src_path)
    try:
        if src.needs_pass:
            raise ValueError("PDF is password-protected")
        try:
            _build(src, src_path, out_path, cfg, progress_cb, cancel_event, pool)
        except BrokenProcessPool:      # a worker died -> redo this file serially
            _build(src, src_path, out_path, cfg, progress_cb, cancel_event, None)
    finally:
        src.close()


def unique_path(path, max_attempts=10000):
    path = Path(path)
    if not path.exists():
        return path
    for n in range(1, max_attempts + 1):
        cand = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not cand.exists():
            return cand
    raise RuntimeError(f"Cannot find a unique filename after {max_attempts} attempts")


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
try:  # crisp text on high-DPI Windows screens
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# Palette
BG = "#F3F4FA"        # window background
CARD = "#FFFFFF"      # card surface
BORDER = "#DDE1F0"
TEXT = "#1E2140"
MUTED = "#7A7FA3"
ACCENT = "#4849B2"
ACCENT_HOVER = "#3A3B96"
SOFT = "#ECEEFA"      # hover / selected tint
OK = "#1E7B4F"
ERR = "#C0392B"

FF = "Segoe UI"       # falls back to the system font on other platforms

PRESETS = [           # name, paper (background), ink (content)
    ("Indigo", "#D6DDF0", "#4849B2"),
    ("Paper", "#F6F1E4", "#2B2B33"),
    ("Night", "#141829", "#E6E9FA"),
    ("Mint", "#DDF2E7", "#1F7A55"),
    ("Sepia", "#EDDFC6", "#5B4028"),
]

QUALITY = [
    ("Standard  -  200 DPI, smaller files", 200),
    ("High  -  300 DPI, recommended", 300),
    ("Maximum  -  400 DPI, large files", 400),
]


def readable_on(hex_color):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return "#111111" if (r * 299 + g * 587 + b * 114) / 1000 > 140 else "#FFFFFF"


class App(_BaseTk):
    def __init__(self):
        super().__init__()
        self.S = max(1.0, self.winfo_fpixels("1i") / 96.0)   # DPI scale factor
        self.title(f"{APP_NAME} v{__version__}")
        self.configure(bg=BG)
        self.geometry(f"{self.px(1040)}x{self.px(800)}")
        self.minsize(self.px(960), self.px(740))

        self.cfg = dict(DEFAULTS)
        self._load_settings()
        self.paper = self.cfg["background"]
        self.ink = self.cfg["content"]

        self.items = {}            # tree iid -> pdf path
        self.counter = 0
        self.msgs = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker = None
        self.last_folder = None
        self.preview_photo = None
        self.details_open = False

        self._styles()
        self._build_header()
        self._build_footer()
        self._build_body()
        self._build_log()

        self._refresh_look()
        self._toggle_output()
        self._refresh_list_state()

        if HAS_DND:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)

    def px(self, n):
        return int(round(n * self.S))

    # ---- settings -------------------------------------------------------
    def _load_settings(self):
        settings_file = SETTINGS_FILE if SETTINGS_FILE.exists() else LEGACY_SETTINGS_FILE
        try:
            raw = json.loads(settings_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            for key, default in DEFAULTS.items():
                if key in raw and isinstance(raw[key], type(default)):
                    self.cfg[key] = raw[key]
            # Clamp DPI to sane range regardless of what the file says
            self.cfg["dpi"] = max(72, min(self.cfg["dpi"], 600))
            # Validate hex colors; revert to defaults if invalid
            for ck in ("background", "content"):
                if not _is_valid_hex_color(self.cfg[ck]):
                    self.cfg[ck] = DEFAULTS[ck]
        except Exception:
            pass

    def _read_controls(self):
        dpi = dict(QUALITY).get(self.quality_var.get(), 300) if hasattr(self, "quality_var") else 300
        self.cfg.update({
            "dpi": dpi,
            "jpeg_quality": 95,
            "thicken": bool(self.thicken_var.get()),
            "background": self.paper,
            "content": self.ink,
            "output_mode": self.output_mode.get(),
            "output_folder": self.folder_var.get().strip(),
            "suffix": _safe_suffix(self.suffix_var.get()),
        })

    def _save_settings(self):
        try:
            self._read_controls()
            SETTINGS_FILE.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
            # Restrict permissions to owner-only (rw-------)
            try:
                SETTINGS_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass  # Non-fatal on platforms that don't support it
        except Exception:
            pass

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_NAME, "A conversion is running. Quit anyway?"):
                return
            self.cancel_event.set()
        self._save_settings()
        self.destroy()

    # ---- styling --------------------------------------------------------
    def _styles(self):
        px = self.px
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", font=(FF, 10), background=CARD, foreground=TEXT)

        for name, bg, fg, border in (
            ("Primary.TButton", ACCENT, "#FFFFFF", ACCENT),
            ("Secondary.TButton", CARD, ACCENT, BORDER),
        ):
            s.configure(name, background=bg, foreground=fg, bordercolor=border,
                        lightcolor=bg, darkcolor=bg, focuscolor=bg, borderwidth=1,
                        padding=(px(14), px(9)), font=(FF, 10, "bold"), anchor="center")
        s.configure("Field.TButton", background=CARD, foreground=ACCENT, bordercolor=BORDER,
                    lightcolor=CARD, darkcolor=CARD, focuscolor=CARD, borderwidth=1,
                    padding=(px(14), px(5)), font=(FF, 10, "bold"), anchor="center")
        s.map("Field.TButton", background=[("active", SOFT)], lightcolor=[("active", SOFT)],
              darkcolor=[("active", SOFT)], foreground=[("disabled", "#B0B3CF")])
        s.map("Primary.TButton",
              background=[("disabled", "#B9BBE0"), ("active", ACCENT_HOVER)],
              bordercolor=[("disabled", "#B9BBE0"), ("active", ACCENT_HOVER)],
              lightcolor=[("disabled", "#B9BBE0"), ("active", ACCENT_HOVER)],
              darkcolor=[("disabled", "#B9BBE0"), ("active", ACCENT_HOVER)],
              foreground=[("disabled", "#FFFFFF")])
        s.map("Secondary.TButton",
              background=[("active", SOFT)], lightcolor=[("active", SOFT)],
              darkcolor=[("active", SOFT)], foreground=[("disabled", "#B0B3CF")])

        for name, bg in (("Link.TButton", BG), ("CardLink.TButton", CARD)):
            s.configure(name, background=bg, foreground=ACCENT, borderwidth=0,
                        lightcolor=bg, darkcolor=bg, bordercolor=bg, focuscolor=bg,
                        padding=(px(8), px(4)), font=(FF, 10))
            s.map(name, background=[("active", SOFT)], lightcolor=[("active", SOFT)],
                  darkcolor=[("active", SOFT)], foreground=[("disabled", "#B0B3CF")])

        for name in ("Card.TRadiobutton", "Card.TCheckbutton"):
            s.configure(name, background=CARD, foreground=TEXT, font=(FF, 10),
                        focuscolor=CARD, padding=(0, px(2)))
            s.map(name, background=[("active", CARD)],
                  indicatorcolor=[("selected", ACCENT), ("!selected", "#FFFFFF")],
                  foreground=[("disabled", "#B0B3CF")])

        s.configure("TEntry", fieldbackground="#FFFFFF", bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, padding=(px(8), px(6)))
        s.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
              darkcolor=[("focus", ACCENT)], fieldbackground=[("disabled", "#F5F6FB")],
              foreground=[("disabled", "#A0A4C0")])
        s.configure("TCombobox", fieldbackground="#FFFFFF", background=CARD, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, arrowcolor=ACCENT,
                    padding=(px(8), px(6)))
        s.map("TCombobox", fieldbackground=[("readonly", "#FFFFFF")],
              bordercolor=[("focus", ACCENT)], foreground=[("readonly", TEXT)],
              selectbackground=[("readonly", "#FFFFFF")], selectforeground=[("readonly", TEXT)])
        self.option_add("*TCombobox*Listbox.font", (FF, 10))

        s.configure("Files.Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT,
                    rowheight=px(36), borderwidth=0, font=(FF, 10))
        s.map("Files.Treeview", background=[("selected", SOFT)], foreground=[("selected", TEXT)])
        s.layout("Files.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

        s.configure("Vertical.TScrollbar", background="#D5D9EE", troughcolor=CARD,
                    bordercolor=CARD, lightcolor="#D5D9EE", darkcolor="#D5D9EE",
                    arrowcolor=MUTED, relief="flat")
        s.configure("Accent.Horizontal.TProgressbar", troughcolor="#E3E6F5", background=ACCENT,
                    bordercolor="#E3E6F5", lightcolor=ACCENT, darkcolor=ACCENT,
                    thickness=px(10))

    # ---- building blocks ------------------------------------------------
    def _card(self, parent):
        outer = tk.Frame(parent, bg=CARD, highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=BORDER)
        inner = tk.Frame(outer, bg=CARD)
        inner.pack(fill="both", expand=True, padx=self.px(20), pady=self.px(18))
        return outer, inner

    def _title(self, parent, text):
        return tk.Label(parent, text=text, bg=CARD, fg=TEXT, font=(FF, 12, "bold"), anchor="w")

    # ---- header / footer ------------------------------------------------
    def _build_header(self):
        px = self.px
        h = tk.Frame(self, bg=BG)
        h.pack(side="top", fill="x", padx=px(32), pady=(px(26), px(18)))
        tk.Label(h, text="Recolor your scanned PDFs", bg=BG, fg=TEXT,
                 font=(FF, 20, "bold"), anchor="w", padx=0).pack(fill="x")
        tk.Label(h, text="Add files, pick a look, and convert. Your originals are never changed.",
                 bg=BG, fg=MUTED, font=(FF, 10), anchor="w", padx=0).pack(fill="x", pady=(px(2), 0))

    def _build_footer(self):
        px = self.px
        f = self.footer = tk.Frame(self, bg=BG)
        f.pack(side="bottom", fill="x", padx=px(32), pady=(px(18), px(24)))
        f.columnconfigure(1, weight=1)

        self.convert_btn = ttk.Button(f, style="Primary.TButton", width=16, command=self.start,
                                      cursor="hand2")
        self.convert_btn.grid(row=0, column=0, padx=(0, px(16)))
        self.progress = ttk.Progressbar(f, style="Accent.Horizontal.TProgressbar",
                                        maximum=100, mode="determinate")
        self.progress.grid(row=0, column=1, sticky="ew")
        self.cancel_btn = ttk.Button(f, text="Cancel", style="Secondary.TButton", width=9,
                                     command=self.cancel, state="disabled", cursor="hand2")
        self.cancel_btn.grid(row=0, column=2, padx=(px(16), px(8)))
        self.details_btn = ttk.Button(f, text="Details  \u25BE", style="Secondary.TButton",
                                      width=9, command=self.toggle_details, cursor="hand2")
        self.details_btn.grid(row=0, column=3)

        self.status = tk.Label(f, bg=BG, fg=MUTED, font=(FF, 10), anchor="w", padx=0,
                               text="Add one or more PDFs to get started.")
        self.status.grid(row=1, column=1, sticky="ew", pady=(px(8), 0))
        self.open_btn = ttk.Button(f, text="Open folder", style="Link.TButton",
                                   command=self.open_folder, cursor="hand2")
        self.open_btn.grid(row=1, column=2, columnspan=2, sticky="e", pady=(px(8), 0))
        self.open_btn.grid_remove()

    def _build_log(self):
        px = self.px
        self.log_frame = tk.Frame(self, bg=CARD, highlightthickness=1,
                                  highlightbackground=BORDER, highlightcolor=BORDER)
        self.log = tk.Text(self.log_frame, height=7, wrap="word", bd=0, relief="flat",
                           bg="#FAFBFF", fg=TEXT, font=("Consolas", 9), state="disabled",
                           padx=px(12), pady=px(10), highlightthickness=0)
        sb = ttk.Scrollbar(self.log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

    # ---- body -----------------------------------------------------------
    def _build_body(self):
        px = self.px
        body = tk.Frame(self, bg=BG)
        body.pack(side="top", fill="both", expand=True, padx=px(32))
        body.columnconfigure(0, weight=1, uniform="cols")
        body.columnconfigure(1, weight=1, uniform="cols")
        body.rowconfigure(1, weight=1)

        files_card, files = self._card(body)
        files_card.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, px(10)))
        self._build_files(files)

        look_card, look = self._card(body)
        look_card.grid(row=0, column=1, sticky="nsew", padx=(px(10), 0))
        self._build_look(look)

        out_card, out = self._card(body)
        out_card.grid(row=1, column=1, sticky="nsew", padx=(px(10), 0), pady=(px(16), 0))
        self._build_output(out)

    # -- files card
    def _build_files(self, f):
        px = self.px
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        top = tk.Frame(f, bg=CARD)
        top.grid(row=0, column=0, sticky="ew", pady=(0, px(12)))
        top.columnconfigure(0, weight=1)
        self._title(top, "Files").grid(row=0, column=0, sticky="w")
        self.add_btn = ttk.Button(top, text="Add PDFs", style="Secondary.TButton",
                                  command=self.add_files, cursor="hand2")
        self.add_btn.grid(row=0, column=1, padx=(0, px(8)))
        self.addf_btn = ttk.Button(top, text="Add folder", style="Secondary.TButton",
                                   command=self.add_folder, cursor="hand2")
        self.addf_btn.grid(row=0, column=2)

        area = tk.Frame(f, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        area.grid(row=1, column=0, sticky="nsew")
        area.columnconfigure(0, weight=1)
        area.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(area, style="Files.Treeview", columns=("status",),
                                 show="tree", selectmode="extended")
        self.tree.column("#0", stretch=True, minwidth=px(120))
        self.tree.column("status", width=px(120), minwidth=px(120), stretch=False, anchor="e")
        self.tree.tag_configure("done", foreground=OK)
        self.tree.tag_configure("failed", foreground=ERR)
        self.tree.tag_configure("busy", foreground=ACCENT)
        sb = ttk.Scrollbar(area, orient="vertical", command=self.tree.yview)
        def _sb_set(lo, hi):
            sb.set(lo, hi)
            if float(lo) <= 0.0 and float(hi) >= 1.0:
                sb.grid_remove()
            else:
                sb.grid()
        self.tree.configure(yscrollcommand=_sb_set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Delete>", lambda e: self.remove_selected())

        hint = "Drop PDFs here\nor click \u201CAdd PDFs\u201D" if HAS_DND else "Click \u201CAdd PDFs\u201D to begin"
        self.empty = tk.Label(area, text=hint, bg=CARD, fg=MUTED, font=(FF, 11),
                              justify="center", cursor="hand2")
        self.empty.place(relx=0.5, rely=0.5, anchor="center")
        self.empty.bind("<Button-1>", lambda e: self.add_files())

        bottom = tk.Frame(f, bg=CARD)
        bottom.grid(row=2, column=0, sticky="ew", pady=(px(10), 0))
        bottom.columnconfigure(0, weight=1)
        self.count_lbl = tk.Label(bottom, text="", bg=CARD, fg=MUTED, font=(FF, 10), anchor="w")
        self.count_lbl.grid(row=0, column=0, sticky="w")
        self.remove_btn = ttk.Button(bottom, text="Remove selected", style="CardLink.TButton",
                                     command=self.remove_selected, cursor="hand2")
        self.remove_btn.grid(row=0, column=1, padx=(0, px(4)))
        self.clear_btn = ttk.Button(bottom, text="Clear all", style="CardLink.TButton",
                                    command=self.clear_files, cursor="hand2")
        self.clear_btn.grid(row=0, column=2)

    # -- look card
    def _build_look(self, f):
        px = self.px
        f.columnconfigure(0, weight=1)
        self._title(f, "Look").grid(row=0, column=0, sticky="w", pady=(0, px(12)))

        chips = tk.Frame(f, bg=CARD)
        chips.grid(row=1, column=0, sticky="ew")
        self.chips = []
        n = len(PRESETS)
        for i, (name, bg, fg) in enumerate(PRESETS):
            chips.columnconfigure(i, weight=1, uniform="chip")
            chip = tk.Frame(chips, bg=CARD, highlightthickness=2, highlightbackground=CARD,
                            highlightcolor=CARD, cursor="hand2")
            chip.grid(row=0, column=i, sticky="ew",
                      padx=(0 if i == 0 else px(4), 0 if i == n - 1 else px(4)))
            sw = tk.Label(chip, text="Aa", bg=bg, fg=fg, font=(FF, 14, "bold"), pady=px(8))
            sw.pack(fill="x", padx=px(2), pady=(px(2), 0))
            nm = tk.Label(chip, text=name, bg=CARD, fg=MUTED, font=(FF, 9))
            nm.pack(pady=(px(4), px(4)))
            for w in (chip, sw, nm):
                w.bind("<Button-1>", lambda e, b=bg, k=fg: self.apply_preset(b, k))
            self.chips.append((chip, nm, bg, fg))

        cols = tk.Frame(f, bg=CARD)
        cols.grid(row=2, column=0, sticky="ew", pady=(px(16), 0))
        cols.columnconfigure(0, weight=1, uniform="col")
        cols.columnconfigure(1, weight=1, uniform="col")
        self.paper_btn = tk.Button(cols, bd=0, relief="flat", cursor="hand2", pady=px(8),
                                   font=(FF, 10, "bold"), highlightthickness=1,
                                   highlightbackground=BORDER, command=lambda: self.pick_color("paper"))
        self.paper_btn.grid(row=0, column=0, sticky="ew", padx=(0, px(6)))
        self.ink_btn = tk.Button(cols, bd=0, relief="flat", cursor="hand2", pady=px(8),
                                 font=(FF, 10, "bold"), highlightthickness=1,
                                 highlightbackground=BORDER, command=lambda: self.pick_color("ink"))
        self.ink_btn.grid(row=0, column=1, sticky="ew", padx=(px(6), 0))
        tk.Label(cols, text="Paper color", bg=CARD, fg=MUTED, font=(FF, 9)).grid(
            row=1, column=0, pady=(px(4), 0))
        tk.Label(cols, text="Ink color", bg=CARD, fg=MUTED, font=(FF, 9)).grid(
            row=1, column=1, pady=(px(4), 0))

        self.sample = tk.Label(f, text="Aa   Sample text", font=(FF, 12), anchor="w",
                               padx=px(14), pady=px(10))
        self.sample.grid(row=3, column=0, sticky="ew", pady=(px(14), 0))

        last = tk.Frame(f, bg=CARD)
        last.grid(row=4, column=0, sticky="ew", pady=(px(12), 0))
        last.columnconfigure(0, weight=1)
        self.thicken_var = tk.BooleanVar(value=self.cfg["thicken"])
        ttk.Checkbutton(last, text="Make thin lines bolder", variable=self.thicken_var,
                        style="Card.TCheckbutton", cursor="hand2").grid(row=0, column=0, sticky="w")
        ttk.Button(last, text="Preview first page", style="CardLink.TButton",
                   command=self.preview, cursor="hand2").grid(row=0, column=1, sticky="e")

    # -- output card
    def _build_output(self, f):
        px = self.px
        f.columnconfigure(0, minsize=px(130))
        f.columnconfigure(1, weight=1)
        self._title(f, "Output").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, px(12)))

        def label(r, text, sticky="w"):
            tk.Label(f, text=text, bg=CARD, fg=TEXT, font=(FF, 10), anchor="w").grid(
                row=r, column=0, sticky=sticky, pady=px(6))

        label(1, "Quality")
        current = next((t for t, d in QUALITY if d == self.cfg["dpi"]), QUALITY[1][0])
        self.quality_var = tk.StringVar(value=current)
        ttk.Combobox(f, textvariable=self.quality_var, values=[t for t, _ in QUALITY],
                     state="readonly", cursor="hand2").grid(row=1, column=1, sticky="ew", pady=px(6))

        label(2, "Save to", sticky="nw")
        self.output_mode = tk.StringVar(value=self.cfg["output_mode"])
        rb = tk.Frame(f, bg=CARD)
        rb.grid(row=2, column=1, sticky="ew", pady=px(6))
        rb.columnconfigure(0, weight=1)
        ttk.Radiobutton(rb, text="Next to each original", variable=self.output_mode, value="same",
                        style="Card.TRadiobutton", command=self._toggle_output,
                        cursor="hand2").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(rb, text="A different folder", variable=self.output_mode, value="folder",
                        style="Card.TRadiobutton", command=self._toggle_output,
                        cursor="hand2").grid(row=1, column=0, sticky="w", pady=(px(4), 0))
        pick = tk.Frame(rb, bg=CARD)
        pick.grid(row=2, column=0, sticky="ew", pady=(px(8), 0))
        pick.columnconfigure(0, weight=1)
        self.folder_var = tk.StringVar(value=self.cfg["output_folder"])
        self.folder_entry = ttk.Entry(pick, textvariable=self.folder_var)
        self.folder_entry.grid(row=0, column=0, sticky="ew", padx=(0, px(8)))
        self.folder_btn = ttk.Button(pick, text="Choose\u2026", style="Field.TButton",
                                     command=self.choose_folder, cursor="hand2")
        self.folder_btn.grid(row=0, column=1)

        label(3, "Add to file name")
        self.suffix_var = tk.StringVar(value=self.cfg["suffix"])
        ttk.Entry(f, textvariable=self.suffix_var).grid(row=3, column=1, sticky="ew", pady=px(6))

    # ---- look logic -----------------------------------------------------
    def _refresh_look(self):
        for btn, hx in ((self.paper_btn, self.paper), (self.ink_btn, self.ink)):
            fg = readable_on(hx)
            btn.config(bg=hx, activebackground=hx, fg=fg, activeforeground=fg, text=hx.upper())
        self.sample.config(bg=self.paper, fg=self.ink)
        cur = (self.paper.lower(), self.ink.lower())
        for chip, nm, bg, fg in self.chips:
            sel = (bg.lower(), fg.lower()) == cur
            col = ACCENT if sel else CARD
            chip.config(highlightbackground=col, highlightcolor=col)
            nm.config(fg=TEXT if sel else MUTED, font=(FF, 9, "bold" if sel else "normal"))

    def apply_preset(self, paper, ink):
        self.paper, self.ink = paper, ink
        self._refresh_look()

    def pick_color(self, which):
        cur = self.paper if which == "paper" else self.ink
        _, hx = colorchooser.askcolor(color=cur, title="Choose color", parent=self)
        if hx:
            if which == "paper":
                self.paper = hx
            else:
                self.ink = hx
            self._refresh_look()

    # ---- output logic ---------------------------------------------------
    def _toggle_output(self):
        state = "normal" if self.output_mode.get() == "folder" else "disabled"
        self.folder_entry.config(state=state)
        self.folder_btn.config(state=state)

    def choose_folder(self):
        d = filedialog.askdirectory(title="Choose output folder", parent=self)
        if d:
            self.folder_var.set(os.path.normpath(d))

    # ---- file list ------------------------------------------------------
    def _add_paths(self, paths):
        existing = set(self.items.values())
        added = 0
        for p in paths:
            p = os.path.normpath(p)
            if p.lower().endswith(".pdf") and os.path.isfile(p) and p not in existing:
                self.counter += 1
                iid = f"f{self.counter}"
                self.items[iid] = p
                self.tree.insert("", "end", iid=iid, text="  " + os.path.basename(p),
                                 values=("Ready",))
                existing.add(p)
                added += 1
        self._refresh_list_state()
        if added:
            self.open_btn.grid_remove()
            self._set_status(f"{added} file{'s' if added != 1 else ''} added.")
            self.progress.config(value=0)

    def add_files(self):
        self._add_paths(filedialog.askopenfilenames(
            title="Select PDFs", filetypes=[("PDF files", "*.pdf")], parent=self))

    def add_folder(self):
        d = filedialog.askdirectory(title="Select a folder with PDFs", parent=self)
        if d:
            self._add_paths(str(p) for p in sorted(Path(d).glob("*.pdf")))

    def _on_drop(self, event):
        paths = []
        for p in self.tk.splitlist(event.data):
            if os.path.isdir(p):
                paths.extend(str(x) for x in sorted(Path(p).glob("*.pdf")))
            else:
                paths.append(p)
        self._add_paths(paths)

    def remove_selected(self):
        if self.worker and self.worker.is_alive():
            return
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.items.pop(iid, None)
        self._refresh_list_state()

    def clear_files(self):
        if self.worker and self.worker.is_alive():
            return
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self._refresh_list_state()

    def _refresh_list_state(self):
        n = len(self.items)
        running = bool(self.worker and self.worker.is_alive())
        if n:
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.5, anchor="center")
        self.count_lbl.config(text=f"{n} file{'s' if n != 1 else ''}" if n else "")
        self.convert_btn.config(text=f"Convert {n} file{'s' if n != 1 else ''}" if n else "Convert",
                                state="normal" if (n and not running) else "disabled")
        for b in (self.add_btn, self.addf_btn):
            b.config(state="disabled" if running else "normal")
        for b in (self.remove_btn, self.clear_btn):
            b.config(state="normal" if (n and not running) else "disabled")

    # ---- misc UI --------------------------------------------------------
    def _set_status(self, text, color=MUTED):
        self.status.config(text=text, fg=color)

    def _log(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def toggle_details(self, force=None):
        want = (not self.details_open) if force is None else force
        if want == self.details_open:
            return
        self.details_open = want
        delta = self.px(150)
        w, h = self.winfo_width(), self.winfo_height()
        if want:
            self.log_frame.pack(side="bottom", fill="x", padx=self.px(32),
                                pady=(0, self.px(24)), before=self.footer)
            self.footer.pack_configure(pady=(self.px(18), self.px(14)))
            self.geometry(f"{w}x{h + delta}")
            self.details_btn.config(text="Details  \u25B4")
        else:
            self.log_frame.pack_forget()
            self.footer.pack_configure(pady=(self.px(18), self.px(24)))
            self.geometry(f"{w}x{max(h - delta, self.px(740))}")
            self.details_btn.config(text="Details  \u25BE")

    def open_folder(self):
        if self.last_folder and os.path.isdir(self.last_folder):
            folder = os.path.realpath(self.last_folder)
            if _is_unc_path(folder):
                return  # Block UNC paths to prevent NTLM hash capture
            try:
                os.startfile(folder)          # Windows
            except AttributeError:
                import subprocess, sys
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open",
                                  folder])

    # ---- preview --------------------------------------------------------
    def preview(self):
        sel = self.tree.selection() or self.tree.get_children()
        if not sel:
            messagebox.showinfo(APP_NAME, "Add a PDF first, then preview it.", parent=self)
            return
        path = self.items[sel[0]]
        try:
            self._read_controls()
            self.config(cursor="watch")
            self.update_idletasks()
            doc = fitz.open(path)
            if doc.needs_pass:
                raise ValueError("this PDF is password-protected")
            img = recolor(render_page(doc[0], min(self.cfg["dpi"], 150)),
                          build_lut(self.cfg["background"], self.cfg["content"]),
                          self.cfg["thicken"])
            doc.close()
        except Exception as e:
            self.config(cursor="")
            messagebox.showerror(APP_NAME, f"Could not create the preview:\n{e}", parent=self)
            return
        self.config(cursor="")
        img.thumbnail((self.px(640), self.px(760)))
        top = tk.Toplevel(self)
        top.title(f"Preview - {os.path.basename(path)}")
        top.configure(bg=BG)
        top.transient(self)
        self.preview_photo = ImageTk.PhotoImage(img)
        tk.Label(top, image=self.preview_photo, bg=BG, bd=0).pack(padx=self.px(16), pady=self.px(16))

    # ---- conversion -----------------------------------------------------
    def _output_path(self, src, cfg):
        p = Path(src)
        folder = Path(cfg["output_folder"]) if cfg["output_mode"] == "folder" else p.parent
        suffix = _safe_suffix(cfg.get("suffix", "_recolored"))
        return unique_path(folder / f"{p.stem}{suffix}.pdf")

    def start(self):
        if not self.items:
            return
        self._read_controls()
        if self.cfg["output_mode"] == "folder":
            if not self.cfg["output_folder"]:
                messagebox.showinfo(APP_NAME, "Please choose an output folder first.", parent=self)
                return
            if _is_unc_path(self.cfg["output_folder"]):
                messagebox.showerror(APP_NAME,
                    "Network (UNC) paths are not allowed as the output folder.\n"
                    "Please choose a local folder.", parent=self)
                return
            try:
                os.makedirs(self.cfg["output_folder"], exist_ok=True)
            except OSError as e:
                messagebox.showerror(APP_NAME, f"Cannot use that folder:\n{e}", parent=self)
                return
        self._save_settings()
        self.cancel_event.clear()
        self.open_btn.grid_remove()
        self.progress.config(value=0)
        jobs = [(iid, self.items[iid]) for iid in self.tree.get_children()]
        for iid, _ in jobs:
            self.tree.item(iid, values=("Waiting",), tags=())
        self.cancel_btn.config(state="normal")
        self.worker = threading.Thread(target=self._run, args=(jobs, dict(self.cfg)), daemon=True)
        self.worker.start()
        self._refresh_list_state()
        self._set_status("Starting\u2026", ACCENT)

    def cancel(self):
        self.cancel_event.set()
        self._set_status("Cancelling\u2026")

    def _run(self, jobs, cfg):
        ok = failed = 0
        last_folder = None
        total = len(jobs)
        pool = None
        try:
            pages = 0
            for _, src in jobs:
                with fitz.open(src) as d:
                    pages += len(d)
            pool = make_pool(pages)
        except Exception:
            pool = None
        try:
            ok, failed, last_folder = self._run_files(jobs, cfg, pool)
        finally:
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
        self.msgs.put(("done", (ok, failed, self.cancel_event.is_set(), last_folder)))

    def _run_files(self, jobs, cfg, pool):
        ok = failed = 0
        last_folder = None
        total = len(jobs)
        for n, (iid, src) in enumerate(jobs, 1):
            if self.cancel_event.is_set():
                break
            name = os.path.basename(src)
            try:
                out = self._output_path(src, cfg)

                def cb(i, t, iid=iid, n=n, name=name):
                    self.msgs.put(("item", iid, f"Page {i}/{t}", "busy"))
                    self.msgs.put(("progress", ((n - 1) + i / t) / total * 100))
                    self.msgs.put(("status", f"Converting {n} of {total}: {name}"))

                self.msgs.put(("item", iid, "Starting\u2026", "busy"))
                process_pdf(src, str(out), cfg, progress_cb=cb, cancel_event=self.cancel_event,
                            pool=pool)
                self.msgs.put(("item", iid, "Done \u2713", "done"))
                self.msgs.put(("log", f"OK      {src}\n   ->   {out}"))
                last_folder = str(out.parent)
                ok += 1
            except Cancelled:
                self.msgs.put(("item", iid, "Cancelled", ""))
                break
            except Exception as e:
                self.msgs.put(("item", iid, "Failed", "failed"))
                self.msgs.put(("log", f"FAILED  {src}\n   Reason: {e}"))
                failed += 1
        return ok, failed, last_folder

    def _poll(self):
        try:
            while True:
                kind, *val = self.msgs.get_nowait()
                if kind == "item":
                    iid, text, tag = val
                    if self.tree.exists(iid):
                        self.tree.item(iid, values=(text,), tags=(tag,) if tag else ())
                elif kind == "progress":
                    self.progress.config(value=val[0])
                elif kind == "status":
                    self._set_status(val[0], ACCENT)
                elif kind == "log":
                    self._log(val[0])
                elif kind == "done":
                    ok, failed, cancelled, folder = val[0]
                    self.worker = None
                    self.cancel_btn.config(state="disabled")
                    self._refresh_list_state()
                    self.last_folder = folder
                    if cancelled:
                        self._set_status(f"Cancelled. {ok} converted before stopping.")
                    elif failed:
                        self._set_status(f"{ok} converted, {failed} failed. See Details for the reason.", ERR)
                        self.toggle_details(True)
                    else:
                        self.progress.config(value=100)
                        self._set_status(f"All done - {ok} file{'s' if ok != 1 else ''} converted.", OK)
                    if ok and folder:
                        self.open_btn.grid()
        except queue.Empty:
            pass
        self.after(100, self._poll)


if __name__ == "__main__":
    multiprocessing.freeze_support()   # required for the packaged .exe
    App().mainloop()
