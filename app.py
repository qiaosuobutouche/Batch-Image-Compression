"""批量图片压缩：桌面界面。"""

from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import sv_ttk
from tkinterdnd2 import COPY, DND_FILES, TkinterDnD

import engine
from engine import Mode, OutputFormat, Settings

APP_NAME = "批量图片压缩"
APP_ID = "PhotoCompressor.BatchImageCompressor"
MODES = ((Mode.QUALITY, "无损压缩"), (Mode.SCALE, "等比缩放"), (Mode.SIZE, "指定尺寸"))
FORMAT_CHOICES = (
    ("保持原格式", OutputFormat.KEEP),
    ("JPG", OutputFormat.JPEG),
    ("PNG", OutputFormat.PNG),
    ("WebP", OutputFormat.WEBP),
)
DEFAULT_QUALITY_INDEX = 3
DEFAULT_SCALE = 50
MAX_WORKERS = max(1, min(4, os.cpu_count() or 1))
IMAGE_PATTERNS = " ".join(f"*{ext}" for ext in sorted(engine.SUPPORTED_EXTENSIONS))


def resource_path(name: str) -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / name


def prefs_path() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME / "settings.json"


def load_prefs() -> dict:
    try:
        data = json.loads(prefs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_prefs(prefs: dict) -> None:
    path = prefs_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _read_registry(path: str, name: str):
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
        return winreg.QueryValueEx(key, name)[0]


def default_output_dir() -> Path:
    try:
        pictures = Path(os.path.expandvars(_read_registry(
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders", "My Pictures")))
    except (OSError, ImportError):
        pictures = Path.home() / "Pictures"
    return pictures / "照片压缩"


def system_uses_dark_theme() -> bool:
    try:
        return _read_registry(r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "AppsUseLightTheme") == 0
    except (OSError, ImportError):
        return False


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def set_dark_title_bar(window: tk.Misc, dark: bool) -> None:
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except (AttributeError, OSError):
        pass


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.2f} MB"


def percent_floor(part: int, whole: int) -> int:
    return part * 100 // whole if whole else 0


def parse_int(text: str) -> int:
    text = text.strip()
    return int(text) if text.isdigit() else 0


def digits_validator(max_length: int):
    def validate(proposed: str) -> bool:
        return proposed == "" or (proposed.isdigit() and len(proposed) <= max_length)

    return validate


@dataclass
class Item:
    path: Path
    info: engine.ImageInfo | None = None
    result: engine.Result | None = None
    error: str | None = None


# 后台线程只接触事件队列和普通数据，不能持有 App 或任何 Tk 对象：
# 否则最后一个引用可能在工作线程中释放，Tk 变量会在错误的线程里析构。

def scan_worker(events: queue.Queue, paths: list[str], exclude: str) -> None:
    events.put(("found", engine.collect_images(paths, exclude)))


def probe_worker(events: queue.Queue, jobs: list[tuple[str, Path]]) -> None:
    for iid, path in jobs:
        try:
            events.put(("probed", iid, engine.probe(path), None))
        except engine.CompressError as exc:
            events.put(("probed", iid, None, str(exc)))


def run_worker(events: queue.Queue, cancel: threading.Event, jobs: list[tuple[str, Path]],
               settings: Settings, output_dir: Path) -> None:
    def work(iid: str, path: Path) -> None:
        if cancel.is_set():
            events.put(("skipped", iid))
            return
        events.put(("started", iid))
        try:
            result = engine.compress_image(path, settings, output_dir)
        except engine.CompressError as exc:
            events.put(("failed", iid, str(exc)))
        except Exception as exc:  # 单张图片的意外错误不能中断整批
            events.put(("failed", iid, f"处理失败：{exc}"))
        else:
            events.put(("done", iid, result))

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for iid, path in jobs:
            pool.submit(work, iid, path)
    events.put(("finished",))


class App:
    def __init__(self, root: tk.Tk, initial_paths: list[str] | None = None):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.items: dict[str, Item] = {}
        self.keys: dict[str, str] = {}
        self.next_id = 0
        self.running = False
        self.cancel = threading.Event()
        self.run_stats: dict[str, int] = {}
        self.prefs = load_prefs()
        self.scale_factor = root.winfo_fpixels("1i") / 96
        self._syncing_scale = False
        self.lockable: list[ttk.Widget] = []

        self._setup_theme()
        self._create_variables()
        self._build()
        self._bind_events()
        self._show_mode_panel()
        self._refresh_empty_state()
        self._update_idle_status()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Destroy>", self._on_destroy, add="+")
        self._poll_job = root.after(40, self._poll)
        if initial_paths:
            self.add_paths(initial_paths)

    def px(self, value: float) -> int:
        return int(round(value * self.scale_factor))

    # ---------- 外观 ----------

    def _setup_theme(self) -> None:
        self.dark = system_uses_dark_theme()
        sv_ttk.set_theme("dark" if self.dark else "light", self.root)

        families = set(tkfont.families(self.root))
        family = next((f for f in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI") if f in families), None)
        sizes = {
            "SunValleyCaptionFont": (9, "normal"),
            "SunValleyBodyFont": (10, "normal"),
            "SunValleyBodyStrongFont": (10, "bold"),
            "SunValleyBodyLargeFont": (11, "normal"),
            "SunValleySubtitleFont": (15, "bold"),
            "SunValleyTitleFont": (18, "bold"),
        }
        for name, (size, weight) in sizes.items():
            font = tkfont.nametofont(name, self.root)
            font.configure(size=size, weight=weight, **({"family": family} if family else {}))
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            font = tkfont.nametofont(name, self.root)
            font.configure(size=10, **({"family": family} if family else {}))

        body = tkfont.nametofont("SunValleyBodyFont", self.root)
        self.muted = "#9a9a9a" if self.dark else "#6e6e6e"
        style = ttk.Style(self.root)
        style.configure("Treeview", rowheight=body.metrics("linespace") + self.px(12))
        style.configure("Title.TLabel", font="SunValleySubtitleFont")
        style.configure("Muted.TLabel", foreground=self.muted)
        style.configure("Hint.TLabel", foreground=self.muted, font="SunValleyCaptionFont")
        style.configure("Empty.TLabel", foreground=self.muted, font="SunValleyBodyLargeFont", justify="center")
        self.tag_colors = {
            "err": "#ff99a4" if self.dark else "#c42b1c",
            "busy": "#60cdff" if self.dark else "#005fb8",
        }

    def _create_variables(self) -> None:
        prefs = self.prefs
        mode_values = {mode.value for mode, _ in MODES}
        self.mode = tk.StringVar(value=prefs.get("mode") if prefs.get("mode") in mode_values else Mode.QUALITY.value)

        quality_index = prefs.get("quality_index", DEFAULT_QUALITY_INDEX)
        if not isinstance(quality_index, int) or not 0 <= quality_index < len(engine.QUALITY_LEVELS):
            quality_index = DEFAULT_QUALITY_INDEX
        self.quality_index = tk.IntVar(value=quality_index)

        scale = prefs.get("scale", DEFAULT_SCALE)
        scale = scale if isinstance(scale, int) and 1 <= scale <= 100 else DEFAULT_SCALE
        self.scale_value = tk.DoubleVar(value=scale)
        self.scale_text = tk.StringVar(value=str(scale))

        self.width_text = tk.StringVar(value=str(prefs.get("width", "")))
        self.height_text = tk.StringVar(value=str(prefs.get("height", "")))
        self.keep_ratio = tk.BooleanVar(value=bool(prefs.get("keep_ratio", False)))

        labels = [label for label, _ in FORMAT_CHOICES]
        self.format_label = tk.StringVar(value=prefs.get("format") if prefs.get("format") in labels else labels[0])
        self.output_dir = tk.StringVar(value=prefs.get("output_dir") or str(default_output_dir()))

    # ---------- 布局 ----------

    def _build(self) -> None:
        root = self.root
        root.title(APP_NAME)
        root.geometry(f"{self.px(1000)}x{self.px(700)}")
        root.minsize(self.px(880), self.px(600))
        try:
            root.iconbitmap(default=str(resource_path("assets/app.ico")))
        except tk.TclError:
            pass

        outer = ttk.Frame(root, padding=(self.px(24), self.px(18), self.px(24), self.px(16)))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="原图不会被修改，结果另存到输出文件夹", style="Muted.TLabel").pack(
            side="left", padx=(self.px(14), 0), pady=(self.px(5), 0))

        mode_bar = ttk.Frame(outer)
        mode_bar.pack(fill="x", pady=(self.px(16), self.px(12)))
        for mode, text in MODES:
            button = ttk.Radiobutton(mode_bar, text=text, value=mode.value, variable=self.mode,
                                     style="Toggle.TButton", width=10, command=self._show_mode_panel)
            button.pack(side="left", padx=(0, self.px(8)), ipady=self.px(3))
            self.lockable.append(button)

        card = ttk.Frame(outer, style="Card.TFrame", padding=(self.px(20), self.px(16)))
        card.pack(fill="x")
        card.columnconfigure(0, weight=1)
        self.panels = {
            Mode.QUALITY.value: self._build_quality_panel(card),
            Mode.SCALE.value: self._build_scale_panel(card),
            Mode.SIZE.value: self._build_size_panel(card),
        }
        ttk.Separator(card).grid(row=1, column=0, sticky="ew", pady=self.px(14))
        self._build_output_row(card).grid(row=2, column=0, sticky="ew")

        self._build_file_list(outer)
        self._build_action_bar(outer)

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(self.px(12), 0))
        footer.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(footer, mode="determinate", maximum=1, value=0)
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, self.px(16)))
        self.status = ttk.Label(footer, style="Muted.TLabel")
        self.status.grid(row=0, column=1, sticky="e")

        set_dark_title_bar(root, self.dark)

    def _build_quality_panel(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="压缩质量").grid(row=0, column=0, sticky="w", padx=(0, self.px(18)))
        levels = ttk.Frame(frame)
        levels.grid(row=0, column=1, sticky="w")
        for index, (name, _) in enumerate(engine.QUALITY_LEVELS):
            button = ttk.Radiobutton(levels, text=name, value=index, variable=self.quality_index,
                                     command=self._refresh_hints)
            button.pack(side="left", padx=(0, self.px(22)))
            self.lockable.append(button)
        self.quality_hint = ttk.Label(frame, style="Hint.TLabel")
        self.quality_hint.grid(row=1, column=0, columnspan=2, sticky="w", pady=(self.px(10), 0))
        return frame

    def _build_scale_panel(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="缩放比例").grid(row=0, column=0, sticky="w", padx=(0, self.px(18)))
        slider = ttk.Scale(frame, from_=1, to=100, orient="horizontal", variable=self.scale_value,
                           command=self._on_scale_slide)
        slider.grid(row=0, column=1, sticky="ew", padx=(0, self.px(14)))
        spin = ttk.Spinbox(frame, from_=1, to=100, increment=1, width=5, textvariable=self.scale_text,
                           validate="key", validatecommand=(self.root.register(digits_validator(3)), "%P"),
                           command=self._on_scale_typed)
        spin.grid(row=0, column=2)
        spin.bind("<KeyRelease>", lambda _e: self._on_scale_typed(commit=False))
        spin.bind("<FocusOut>", lambda _e: self._on_scale_typed())
        spin.bind("<Return>", lambda _e: self._on_scale_typed())
        ttk.Label(frame, text="%").grid(row=0, column=3, padx=(self.px(6), 0))
        self.scale_hint = ttk.Label(frame, style="Hint.TLabel")
        self.scale_hint.grid(row=1, column=0, columnspan=4, sticky="w", pady=(self.px(10), 0))
        self.lockable += [slider, spin]
        return frame

    def _build_size_panel(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent)
        validate = (self.root.register(digits_validator(5)), "%P")
        ttk.Label(frame, text="宽度").grid(row=0, column=0, sticky="w")
        width_entry = ttk.Entry(frame, textvariable=self.width_text, width=8, validate="key", validatecommand=validate)
        width_entry.grid(row=0, column=1, padx=self.px(8))
        ttk.Label(frame, text="×").grid(row=0, column=2, padx=self.px(4))
        ttk.Label(frame, text="高度").grid(row=0, column=3, padx=(self.px(8), 0))
        height_entry = ttk.Entry(frame, textvariable=self.height_text, width=8, validate="key", validatecommand=validate)
        height_entry.grid(row=0, column=4, padx=self.px(8))
        ttk.Label(frame, text="像素").grid(row=0, column=5)
        ratio = ttk.Checkbutton(frame, text="保持原图比例（不拉伸）", variable=self.keep_ratio,
                                command=self._refresh_hints)
        ratio.grid(row=0, column=6, padx=(self.px(28), 0))
        self.size_hint = ttk.Label(frame, style="Hint.TLabel")
        self.size_hint.grid(row=1, column=0, columnspan=7, sticky="w", pady=(self.px(10), 0))
        self.lockable += [width_entry, height_entry, ratio]
        return frame

    def _build_output_row(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent)
        frame.columnconfigure(3, weight=1)
        ttk.Label(frame, text="输出格式").grid(row=0, column=0, sticky="w", padx=(0, self.px(10)))
        combo = ttk.Combobox(frame, textvariable=self.format_label, state="readonly", width=10,
                             values=[label for label, _ in FORMAT_CHOICES])
        combo.grid(row=0, column=1, padx=(0, self.px(28)))
        combo.bind("<<ComboboxSelected>>", lambda _e: (combo.selection_clear(), self._refresh_hints()))
        ttk.Label(frame, text="保存到").grid(row=0, column=2, sticky="w", padx=(0, self.px(10)))
        ttk.Entry(frame, textvariable=self.output_dir, state="readonly").grid(row=0, column=3, sticky="ew")
        change = ttk.Button(frame, text="更改…", command=self.choose_output_dir)
        change.grid(row=0, column=4, padx=(self.px(8), 0))
        ttk.Button(frame, text="打开", command=self.open_output_dir).grid(row=0, column=5, padx=(self.px(6), 0))
        self.lockable += [combo, change]
        return frame

    def _build_file_list(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, pady=(self.px(16), self.px(12)))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        columns = {
            "name": ("文件名", 250, "w", True),
            "size": ("原尺寸", 115, "center", False),
            "bytes": ("原大小", 90, "center", False),
            "new_size": ("新尺寸", 115, "center", False),
            "new_bytes": ("新大小", 90, "center", False),
            "status": ("状态", 230, "w", False),
        }
        self.tree = ttk.Treeview(frame, columns=tuple(columns), show="headings", selectmode="extended")
        for column, (title, width, anchor, stretch) in columns.items():
            self.tree.heading(column, text=title, anchor=anchor)
            self.tree.column(column, width=self.px(width), minwidth=self.px(60), anchor=anchor, stretch=stretch)
        for tag, color in self.tag_colors.items():
            self.tree.tag_configure(tag, foreground=color)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.empty_label = ttk.Label(
            frame, style="Empty.TLabel", justify="center",
            text="把图片或文件夹拖到这里\n支持 JPG、PNG、WebP、BMP、GIF、TIFF，可一次添加多张")
        self.drop_targets = [self.root, frame, self.tree, self.empty_label]

        self.menu = tk.Menu(self.root, tearoff=False)
        self.menu.add_command(label="打开结果", command=lambda: self._open_selected(result=True))
        self.menu.add_command(label="打开原图", command=lambda: self._open_selected(result=False))
        self.menu.add_command(label="在文件夹中显示结果", command=self._reveal_selected)
        self.menu.add_separator()
        self.menu.add_command(label="从列表移除", command=self.remove_selected)

    def _build_action_bar(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x")
        buttons = [
            ("添加图片", self.pick_files),
            ("添加文件夹", self.pick_folder),
            ("移除所选", self.remove_selected),
            ("清空列表", self.clear),
        ]
        for index, (text, command) in enumerate(buttons):
            button = ttk.Button(bar, text=text, command=command)
            button.pack(side="left", padx=(0 if index == 0 else self.px(8), 0))
            self.lockable.append(button)
        self.start_button = ttk.Button(bar, text="开始压缩", style="Accent.TButton", width=12,
                                       command=self.toggle_run)
        self.start_button.pack(side="right", ipady=self.px(3))

    def _bind_events(self) -> None:
        for widget in self.drop_targets:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)
        self.tree.bind("<Delete>", lambda _e: self.remove_selected())
        self.tree.bind("<Control-a>", lambda _e: (self.tree.selection_set(self.tree.get_children()), "break")[1])
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-3>", self._on_right_click)
        self.root.bind("<Control-o>", lambda _e: self.pick_files())
        self.root.bind("<Control-Return>", lambda _e: self.toggle_run())

    # ---------- 模式与提示 ----------

    def _show_mode_panel(self) -> None:
        current = self.mode.get()
        for value, panel in self.panels.items():
            if value == current:
                panel.grid(row=0, column=0, sticky="ew")
            else:
                panel.grid_remove()
        self._refresh_hints()

    def _refresh_hints(self) -> None:
        name, quality = engine.QUALITY_LEVELS[self.quality_index.get()]
        self.quality_hint.configure(
            text=f"质量 {quality}%：保持原图宽高不变，只减小文件体积；PNG 图片按无损方式优化（画质不变）。")
        percent = self.current_scale()
        self.scale_hint.configure(
            text=f"宽和高按同一比例缩小，例如 4000×3000 → {4000 * percent // 100}×{3000 * percent // 100}；画质 {engine.RESIZE_QUALITY}%。")
        if self.keep_ratio.get():
            size_text = "按原图比例缩小到指定宽高以内，可以只填一边；本来就更小的图片保持原尺寸。"
        else:
            size_text = "输出精确的宽×高，不能大于原图；比例不同的图片会被拉伸。"
        self.size_hint.configure(text=f"{size_text}画质 {engine.RESIZE_QUALITY}%。")

    def current_scale(self) -> int:
        value = parse_int(self.scale_text.get())
        return value if 1 <= value <= 100 else max(1, min(100, int(round(self.scale_value.get()))))

    def _on_scale_slide(self, value: str) -> None:
        if self._syncing_scale:
            return
        self._syncing_scale = True
        self.scale_text.set(str(int(round(float(value)))))
        self._syncing_scale = False
        self._refresh_hints()

    def _on_scale_typed(self, commit: bool = True) -> None:
        value = parse_int(self.scale_text.get())
        if commit:
            value = max(1, min(100, value or int(round(self.scale_value.get()))))
            self.scale_text.set(str(value))
        if 1 <= value <= 100:
            self._syncing_scale = True
            self.scale_value.set(value)
            self._syncing_scale = False
        self._refresh_hints()

    # ---------- 添加与移除 ----------

    def pick_files(self) -> None:
        if self.running:
            return
        paths = filedialog.askopenfilenames(
            parent=self.root, title="选择图片", initialdir=self.prefs.get("last_dir") or None,
            filetypes=[("图片", IMAGE_PATTERNS), ("所有文件", "*.*")])
        if paths:
            self.prefs["last_dir"] = str(Path(paths[0]).parent)
            self.add_paths(list(paths))

    def pick_folder(self) -> None:
        if self.running:
            return
        folder = filedialog.askdirectory(parent=self.root, title="选择包含图片的文件夹", mustexist=True,
                                         initialdir=self.prefs.get("last_dir") or None)
        if folder:
            self.prefs["last_dir"] = folder
            self.add_paths([folder])

    def add_paths(self, paths: list[str]) -> None:
        paths = [p for p in paths if p]
        if not paths:
            return
        self._set_status("正在读取图片…")
        threading.Thread(target=scan_worker, args=(self.events, paths, self.output_dir.get()), daemon=True).start()

    def _on_drop(self, event):
        if self.running:
            self._set_status("正在压缩，请完成后再添加")
        else:
            self.add_paths(list(self.root.tk.splitlist(event.data)))
        return COPY

    def _on_found(self, paths: list[Path]) -> None:
        new_jobs = []
        for path in paths:
            key = engine.path_key(path)
            if key in self.keys:
                continue
            iid = f"i{self.next_id}"
            self.next_id += 1
            self.keys[key] = iid
            self.items[iid] = Item(path)
            self.tree.insert("", "end", iid=iid, values=(path.name, "…", "", "", "", "读取中"))
            new_jobs.append((iid, path))
        self._refresh_empty_state()
        if new_jobs:
            threading.Thread(target=probe_worker, args=(self.events, new_jobs), daemon=True).start()
            self._update_idle_status()
        else:
            self._set_status("这些图片已经在列表中" if paths else "没有找到支持的图片")

    def _on_probed(self, iid: str, info: engine.ImageInfo | None, error: str | None) -> None:
        item = self.items.get(iid)
        if item is None:
            return
        item.info, item.error = info, error
        if info:
            self.tree.item(iid, values=(item.path.name, f"{info.width} × {info.height}",
                                        format_bytes(info.file_size), "", "", "待压缩"), tags=())
        else:
            self.tree.item(iid, values=(item.path.name, "", "", "", "", f"无法读取：{error}"), tags=("err",))
        if not self.running:
            self._update_idle_status()

    def remove_selected(self) -> None:
        if self.running:
            return
        for iid in self.tree.selection():
            item = self.items.pop(iid, None)
            if item:
                self.keys.pop(engine.path_key(item.path), None)
            self.tree.delete(iid)
        self._refresh_empty_state()
        self._update_idle_status()

    def clear(self) -> None:
        if self.running:
            return
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self.keys.clear()
        self.progress.configure(value=0)
        self._refresh_empty_state()
        self._update_idle_status()

    def _refresh_empty_state(self) -> None:
        if self.items:
            self.empty_label.place_forget()
        else:
            self.empty_label.place(relx=0.5, rely=0.55, anchor="center")

    # ---------- 压缩 ----------

    def collect_settings(self) -> Settings | None:
        output_format = dict(FORMAT_CHOICES)[self.format_label.get()]
        mode = Mode(self.mode.get())
        if mode is Mode.QUALITY:
            quality = engine.QUALITY_LEVELS[self.quality_index.get()][1]
            return Settings(mode=mode, quality=quality, output_format=output_format)
        if mode is Mode.SCALE:
            self._on_scale_typed()
            return Settings(mode=mode, scale_percent=self.current_scale(), output_format=output_format)

        width, height = parse_int(self.width_text.get()), parse_int(self.height_text.get())
        keep_ratio = self.keep_ratio.get()
        if keep_ratio and not (width or height):
            messagebox.showwarning(APP_NAME, "请至少填写目标宽度或高度（像素）。", parent=self.root)
            return None
        if not keep_ratio and not (width and height):
            messagebox.showwarning(APP_NAME, "请填写目标宽度和高度（像素）。", parent=self.root)
            return None
        return Settings(mode=mode, width=width, height=height, keep_ratio=keep_ratio, output_format=output_format)

    def toggle_run(self) -> None:
        if self.running:
            self.cancel.set()
            self.start_button.configure(text="正在停止…", state="disabled")
            return

        jobs = [(iid, self.items[iid].path) for iid in self.tree.get_children() if self.items[iid].info]
        if not jobs:
            message = "列表中没有可以处理的图片。" if self.items else "请先添加图片：拖入图片或文件夹，或点击「添加图片」。"
            messagebox.showinfo(APP_NAME, message, parent=self.root)
            return
        settings = self.collect_settings()
        if settings is None:
            return
        output_dir = Path(self.output_dir.get())
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法创建输出文件夹：\n{output_dir}\n\n{exc}", parent=self.root)
            return

        self.running = True
        self.cancel.clear()
        self.run_stats = {"total": len(jobs), "done": 0, "ok": 0, "failed": 0, "skipped": 0,
                          "before": 0, "after": 0}
        for iid, _ in jobs:
            item = self.items[iid]
            item.result, item.error = None, None
            self._set_row(iid, new_size="", new_bytes="", status="等待中", tags=())
        self.progress.configure(maximum=len(jobs), value=0)
        self._set_locked(True)
        self.start_button.configure(text="停止", style="TButton")
        self._save_prefs()
        self._update_running_status()
        threading.Thread(target=run_worker, args=(self.events, self.cancel, jobs, settings, output_dir),
                         daemon=True).start()

    def _on_done(self, iid: str, result: engine.Result) -> None:
        item = self.items[iid]
        item.result = result
        before = item.info.file_size
        if result.kept_original:
            status = "已是最优，保留原图"
        elif result.file_size <= before:
            status = f"完成 · 减小 {percent_floor(before - result.file_size, before)}%"
        else:
            status = f"完成 · 增大 {math.ceil((result.file_size - before) * 100 / before)}%"
        self._set_row(iid, new_size=f"{result.width} × {result.height}", new_bytes=format_bytes(result.file_size),
                      status=status, tags=())
        self.run_stats["ok"] += 1
        self.run_stats["before"] += before
        self.run_stats["after"] += result.file_size
        self._advance()

    def _on_failed(self, iid: str, message: str) -> None:
        self.items[iid].error = message
        self._set_row(iid, status=f"失败：{message}", tags=("err",))
        self.run_stats["failed"] += 1
        self._advance()

    def _on_skipped(self, iid: str) -> None:
        self._set_row(iid, status="已取消", tags=())
        self.run_stats["skipped"] += 1
        self._advance()

    def _advance(self) -> None:
        self.run_stats["done"] += 1
        self.progress.configure(value=self.run_stats["done"])
        self._update_running_status()

    def _on_finished(self) -> None:
        self.running = False
        self._set_locked(False)
        self.start_button.configure(text="开始压缩", style="Accent.TButton", state="normal")
        stats = self.run_stats
        parts = [f"完成 {stats['ok']} 张"]
        if stats["failed"]:
            parts.append(f"失败 {stats['failed']} 张")
        if stats["skipped"]:
            parts.append(f"取消 {stats['skipped']} 张")
        saved = stats["before"] - stats["after"]
        if saved > 0 and stats["before"]:
            parts.append(f"共节省 {format_bytes(saved)}（{percent_floor(saved, stats['before'])}%）")
        self._set_status("，".join(parts))

    def _set_row(self, iid: str, **changes) -> None:
        if not self.tree.exists(iid):
            return
        columns = ("name", "size", "bytes", "new_size", "new_bytes", "status")
        values = list(self.tree.item(iid, "values")) + [""] * len(columns)
        for index, column in enumerate(columns):
            if column in changes:
                values[index] = changes[column]
        options = {"values": values[: len(columns)]}
        if "tags" in changes:
            options["tags"] = changes["tags"]
        self.tree.item(iid, **options)

    def _set_locked(self, locked: bool) -> None:
        for widget in self.lockable:
            widget.state(["disabled"] if locked else ["!disabled"])

    # ---------- 状态栏 ----------

    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def _update_idle_status(self) -> None:
        if not self.items:
            self._set_status("尚未添加图片")
            return
        readable = [item.info for item in self.items.values() if item.info]
        total = sum(info.file_size for info in readable)
        text = f"共 {len(self.items)} 张图片，{format_bytes(total)}"
        unreadable = sum(1 for item in self.items.values() if item.error and not item.info)
        if unreadable:
            text += f"（{unreadable} 张无法读取）"
        self._set_status(text)

    def _update_running_status(self) -> None:
        stats = self.run_stats
        self._set_status(f"正在压缩 {stats['done']}/{stats['total']}…")

    # ---------- 文件操作 ----------

    def choose_output_dir(self) -> None:
        folder = filedialog.askdirectory(parent=self.root, title="选择输出文件夹",
                                         initialdir=self.output_dir.get() or None)
        if folder:
            self.output_dir.set(str(Path(folder)))
            self._save_prefs()

    def open_output_dir(self) -> None:
        folder = Path(self.output_dir.get())
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(folder)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法打开文件夹：\n{exc}", parent=self.root)

    def _target_path(self, iid: str, result: bool) -> Path | None:
        item = self.items.get(iid)
        if item is None:
            return None
        if result and item.result and item.result.output.exists():
            return item.result.output
        return item.path if not result or item.result is None else None

    def _on_double_click(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            path = self._target_path(iid, result=True) or self._target_path(iid, result=False)
            self._start_file(path)

    def _on_right_click(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        has_result = self.items[iid].result is not None
        for index in (0, 2):
            self.menu.entryconfigure(index, state="normal" if has_result else "disabled")
        self.menu.entryconfigure(4, state="disabled" if self.running else "normal")
        self.menu.tk_popup(event.x_root, event.y_root)

    def _open_selected(self, result: bool) -> None:
        selection = self.tree.selection()
        if selection:
            self._start_file(self._target_path(selection[0], result))

    def _reveal_selected(self) -> None:
        selection = self.tree.selection()
        path = self._target_path(selection[0], result=True) if selection else None
        if path:
            subprocess.Popen(f'explorer /select,"{path}"')

    def _start_file(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            os.startfile(path)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法打开：\n{path}\n\n{exc}", parent=self.root)

    # ---------- 事件循环与退出 ----------

    def _poll(self) -> None:
        handlers = {
            "found": self._on_found,
            "probed": self._on_probed,
            "started": lambda iid: self._set_row(iid, status="压缩中…", tags=("busy",)),
            "done": self._on_done,
            "failed": self._on_failed,
            "skipped": self._on_skipped,
            "finished": self._on_finished,
        }
        try:
            for _ in range(300):
                kind, *args = self.events.get_nowait()
                handlers[kind](*args)
        except queue.Empty:
            pass
        self._poll_job = self.root.after(40, self._poll)

    def _on_destroy(self, event) -> None:
        if event.widget is self.root and self._poll_job:
            self.root.after_cancel(self._poll_job)
            self._poll_job = None

    def _save_prefs(self) -> None:
        self.prefs.update({
            "mode": self.mode.get(),
            "quality_index": self.quality_index.get(),
            "scale": self.current_scale(),
            "width": self.width_text.get(),
            "height": self.height_text.get(),
            "keep_ratio": self.keep_ratio.get(),
            "format": self.format_label.get(),
            "output_dir": self.output_dir.get(),
        })
        save_prefs(self.prefs)

    def on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno(APP_NAME, "正在压缩，确定要退出吗？\n已经完成的图片会保留。", parent=self.root):
                return
            self.cancel.set()
        self._save_prefs()
        self.root.destroy()


def main() -> None:
    enable_dpi_awareness()
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError):
        pass
    root = TkinterDnD.Tk()
    App(root, sys.argv[1:])
    root.mainloop()


if __name__ == "__main__":
    main()
