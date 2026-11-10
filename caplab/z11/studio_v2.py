"""Z11 Studio: multipage RLC editor, optional linked sweeps and magnetic coupling."""


import copy
import csv
import itertools
import json
import math
import queue
import re
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from uuid import uuid4

# Pure Python dependencies stay with this project; shared environments are unchanged.
_vendor_dir = Path(__file__).with_name("vendor")
if _vendor_dir.is_dir() and str(_vendor_dir) not in sys.path:
    sys.path.insert(0, str(_vendor_dir))

# Set Windows DPI mode before Tk creates its first window, so controls render at native resolution.
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass

import fieldwork_tk as fw
from product_theme import apply_theme
fw.apply_matplotlib()

import matplotlib

matplotlib.use("TkAgg")
matplotlib.rcParams["font.sans-serif"] = (["Microsoft YaHei", "Segoe UI"] if sys.platform == "win32"
                                         else ["WenQuanYi Micro Hei", "WenQuanYi Zen Hei", "DejaVu Sans"])
matplotlib.rcParams["axes.unicode_minus"] = False
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

import editor_gui as legacy
from editor_gui import (EditorApp, UI_FONT, SYMBOL_FONT, bind_mousewheel, draw_symbol,
                        snap, transform, wheel_delta, wire_contact)
from network_model import GRID, blank_document, example_document, route, terminals
from network_model_v2 import parameters, solve, validate
from optimizer import GLOBAL_Q_LABELS, METRIC_LABELS, apply_parameter, optimize, validate_settings


BACK = fw.BG
PANEL = fw.PANEL
RAISED = fw.RAISED
PLOT = fw.PLOT_BG
PLOT_BG = fw.PLOT_BG
PLOT_TEXT = fw.DIM
PLOT_GRID = fw.tk_plot_colors()['grid']
TEXT = fw.TEXT
MUTED = fw.DIM
ACCENT = fw.accent('cap')
AMBER = fw.WARN
CANVAS_LIMIT = 100_000
MAX_CURVES = 2_000
MAX_TOTAL_POINTS = 3_000_000
COLORS = tuple(fw.SERIES)


class Tooltip:
    def __init__(self, widget: 'tk.Widget', text: 'str'):
        self.widget, self.text, self.popup, self.timer = widget, text, None, None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _=None):
        self.timer = self.widget.after(550, self._show)

    def _show(self):
        self.timer = None
        if self.popup:
            return
        self.popup = tk.Toplevel(self.widget)
        self.popup.wm_overrideredirect(True)
        self.popup.configure(background="#314c61")
        tk.Label(self.popup, text=self.text, background="#314c61", foreground="white", padx=10, pady=7,
                 wraplength=290, justify="left", font=(UI_FONT, 9)).pack()
        self.popup.geometry(f"+{self.widget.winfo_rootx()}+{self.widget.winfo_rooty() + self.widget.winfo_height() + 5}")

    def _hide(self, _=None):
        if self.timer:
            self.widget.after_cancel(self.timer)
            self.timer = None
        if self.popup:
            self.popup.destroy()
            self.popup = None


def default_optimization_settings() -> 'dict':
    global_q = [{"parameter": key, "enabled": False, "group": "独立",
                 "low": "10", "high": "100", "step": ""} for key in GLOBAL_Q_LABELS]
    return {"variables": global_q, "constraints": [], "mode": "custom", "target": "bandwidth",
            "direction": "max", "algorithm": "de", "desired": "", "desired_high": "",
            "tolerance": "0.01", "generations": "80", "stall": "20"}


def new_page(name: 'str', document: 'dict | None' = None) -> 'dict':
    return {"name": name, "document": document or blank_document(), "start": "0.1", "stop": "30",
            "count": "1001", "zoom": 1.0, "sweeps": [], "optimization": default_optimization_settings(),
            "optimization_result": None, "optimization_history": [], "view": None,
            "result": None, "stale": False, "history": [], "future": []}


def safe_name(name: 'str') -> 'str':
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "page"


class Studio(EditorApp):
    def __init__(self) -> 'None':
        self.pages = [new_page("页面 1", example_document())]
        self.zoom = 1.0
        self.wire_path: 'list[list[int]]' = []
        self.wire_drag: 'dict | None' = None
        self.selected_wire_segment: 'tuple[int, int] | None' = None
        self.copy_source: 'dict | None' = None
        self.active_page = 0
        self.project_path: 'Path | None' = None
        self.library_dir = Path(__file__).with_name("saved_structures")
        self.library_dir.mkdir(exist_ok=True)
        self.state_dir = Path(__file__).with_name("saved_state")
        self.state_dir.mkdir(exist_ok=True)
        self._loading_page = False
        self._ready = False
        self.highlighted: 'int | None' = None
        self.lines_by_curve: 'dict[int, list]' = {}
        self.template_files: 'dict[str, Path]' = {}
        self._opt_queue: 'queue.Queue' = queue.Queue()
        self._opt_cancel = threading.Event()
        self._opt_thread: 'threading.Thread | None' = None
        self._opt_page: 'dict | None' = None
        self._opt_document_fingerprint = ""
        self.optimization_history: 'list[dict]' = []
        self._opt_history_run: 'list[dict]' = []
        legacy.INK, legacy.SELECT, legacy.WIRE, legacy.BG = TEXT, AMBER, ACCENT, PLOT
        super().__init__()
        self._ready = True
        self._refresh_templates()
        self._refresh_title()
        self.update()
        self._set_initial_splitters()

    def _build_ui(self) -> 'None':
        self.pages[0]["document"] = self.document
        apply_theme(self,fw)
        self.style.configure('Mini.TButton',padding=(8,4))
        self.style.configure('Legend.Treeview',rowheight=24)
        self.geometry(f"{min(round(self.winfo_screenwidth() * .92), 2350)}x{min(round(self.winfo_screenheight() * .88), 1400)}")
        self.minsize(1200, 740)

        def global_actions(box):
            for caption, command in (("新建工程", self.new_document), ("打开工程", self.open_document), ("保存工作簿", self.save_document), ("另存为", self.save_as_project)):
                ttk.Button(box,text=caption,command=command).pack(side='left',padx=4)
        fw.header(self,'Z11 Studio','RLC network analysis',product='cap',right=global_actions).pack(fill='x')
        actions=ttk.Frame(self,padding=(16,8));actions.pack(fill='x');self.actionbar=actions
        for caption, command, style in (("计算",self.calculate,'Primary.TButton'),("导出 CSV",self.export_csv,'TButton'),("导出图",self.export_images,'TButton')):
            ttk.Button(actions,text=caption,command=command,style=style).pack(side='left',padx=4)
        pagebar = ttk.Frame(self, padding=(11, 0, 11, 5))
        pagebar.pack(fill="x")
        self.page_tabs = ttk.Notebook(pagebar, height=23)
        self.page_tabs.pack(side="left", fill="x", expand=True)
        self.page_tabs.add(ttk.Frame(self.page_tabs), text=self.pages[0]["name"])
        self.page_tabs.bind("<<NotebookTabChanged>>", self._page_changed)
        for caption, command in (("＋ 页面", self.add_page), ("重命名", self.rename_page), ("删除页", self.delete_page)):
            ttk.Button(pagebar, text=caption, command=command, style="Mini.TButton").pack(side="left", padx=3)

        main = ttk.Panedwindow(self, orient="horizontal")
        self.main_panes = main
        main.pack(fill="both", expand=True, padx=9, pady=(0, 2))
        side_rail = ttk.Frame(main, width=190)
        controls = ttk.Frame(main, width=870)
        charts = ttk.Frame(main, width=800)
        main.add(side_rail, weight=0)
        main.add(controls, weight=5)
        main.add(charts, weight=4)
        self._build_side_rail(side_rail)
        self._build_controls(controls)
        self._build_charts(charts)
        self.status = tk.StringVar(value="就绪。左侧拖入器件，中间编辑电路，右侧查看曲线；拖动分隔线调整布局，中键拖动画布。")
        feedback=ttk.Frame(self,padding=(16,8));feedback.pack(fill='x',side='bottom',before=main)
        self.status_chip=fw.StatusChip(feedback);self.status_chip.pack(side='left')
        self.run_progress=ttk.Progressbar(feedback,length=160,mode='indeterminate');self.run_progress.pack(side='right')
        self.status.trace_add('write',self._update_feedback)
        self._update_feedback()
        self._refresh_parameter_choices()
        self._refresh_sidebar()

    def _update_feedback(self,*args):
        message=self.status.get()
        state=('error' if any(word in message for word in ('失败','未完成')) else
               'running' if self._opt_thread is not None and self._opt_thread.is_alive() or '正在' in message else
               'done' if any(word in message for word in ('完成','已保存','已加载','结束')) else 'idle')
        self.status_chip.set(state,message[:95])
        if state=='running':self.run_progress.start(40)
        else:self.run_progress.stop()

    def _set_initial_splitters(self) -> 'None':
        width = self.main_panes.winfo_width()
        height = self.control_panes.winfo_height()
        if width > 800:
            self.main_panes.sashpos(0, 185)
            self.main_panes.sashpos(1, round(width * .58))
        if height > 400:
            self.control_panes.sashpos(0, round(height * .62))

    def _build_side_rail(self, parent: 'ttk.Frame') -> 'None':
        rail = ttk.Panedwindow(parent, orient="vertical")
        self.rail_panes = rail
        rail.pack(fill="both", expand=True)
        listing = ttk.LabelFrame(rail, text="曲线 / 扫描值", padding=5)
        rail.add(listing, weight=4)
        self.selected_values = tk.StringVar(value="选择曲线查看扫描值")
        ttk.Label(listing, textvariable=self.selected_values, foreground=MUTED,
                  wraplength=165, justify="left").pack(side="bottom", fill="x", pady=(5, 0))
        tree_frame = ttk.Frame(listing)
        tree_frame.pack(fill="both", expand=True)
        self.legend = ttk.Treeview(tree_frame, show="tree", selectmode="browse",
                                   style="Legend.Treeview", height=8)
        legend_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.legend.yview)
        self.legend.configure(yscrollcommand=legend_scroll.set)
        self.legend.pack(side="left", fill="both", expand=True)
        legend_scroll.pack(side="right", fill="y")
        self.legend.bind("<<TreeviewSelect>>", self._legend_select)
        palette = ttk.LabelFrame(rail, text="器件库", padding=5)
        rail.add(palette, weight=1)
        self._build_palette(palette)

    def _build_palette(self, parent: 'ttk.Frame') -> 'None':
        for kind, caption in (("R", "电阻  Ω"), ("L", "电感  pH"), ("C", "电容  fF"), ("K", "互感  k")):
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=2)
            tile = tk.Canvas(row, width=106, height=43, background=RAISED,
                             highlightthickness=1, highlightbackground="#36546a", cursor="hand2")
            tile.pack(side="left", padx=(0, 5))
            sample = {"id": kind, "kind": kind, "x": 53, "y": 21, "rotation": 0, "value": 0.2, "q": 20.0}
            if kind == "K":
                self._paint_coupler(tile, sample, tags=())
            else:
                draw_symbol(tile, sample, label=False)
            ttk.Label(row, text=caption, foreground=MUTED).pack(side="left")
            tile.bind("<ButtonPress-1>", lambda event, k=kind: self._palette_press(k, event))
            tile.bind("<B1-Motion>", lambda event, k=kind: self._palette_move(k, event))
            tile.bind("<ButtonRelease-1>", lambda event, k=kind: self._palette_release(k, event))
        ttk.Label(parent, text="拖入中间画布；L 小点为同名端", foreground=MUTED,
                  wraplength=160, justify="left").pack(anchor="w", pady=(5, 0))

    def _build_charts(self, parent: 'ttk.Frame') -> 'None':
        top = ttk.Frame(parent)
        top.pack(fill="x", pady=(4, 5))
        ttk.Label(top, text="分析曲线", font=(UI_FONT, 11, "bold")).pack(side="left")
        self.show_real, self.show_imag, self.show_mag = tk.BooleanVar(value=True), tk.BooleanVar(value=True), tk.BooleanVar(value=True)
        for caption, variable in (("实部", self.show_real), ("虚部", self.show_imag), ("幅值", self.show_mag)):
            ttk.Checkbutton(top, text=caption, variable=variable, command=self._visibility_changed).pack(side="right", padx=5)
        self.result_badge = tk.StringVar(value="尚未计算")
        ttk.Label(top, textvariable=self.result_badge, foreground=AMBER).pack(side="right", padx=(0, 15))

        figures = ttk.Panedwindow(parent, orient="vertical")
        self.chart_panes = figures
        figures.pack(fill="both", expand=True)
        self.plots = []
        for title in ("Z11 阻抗", "Z11 相位"):
            pane = ttk.Frame(figures)
            figures.add(pane, weight=1)
            figure = Figure(figsize=(6, 3), dpi=100, constrained_layout=True, facecolor=PLOT_BG)
            axis = figure.add_subplot(111)
            self._style_axis(axis)
            axis.set_title(title, color=PLOT_TEXT)
            view = FigureCanvasTkAgg(figure, master=pane)
            view.get_tk_widget().pack(fill="both", expand=True)
            toolbar = NavigationToolbar2Tk(view, pane, pack_toolbar=False)
            toolbar.update()
            toolbar.pack(fill="x")
            view.mpl_connect("pick_event", self._pick_curve)
            self.plots.append((figure, axis, view))
        self.pareto_pane = ttk.Frame(figures)
        self.pareto_figure = Figure(figsize=(6, 2.5), dpi=100, constrained_layout=True, facecolor=PLOT_BG)
        self.pareto_axis = self.pareto_figure.add_subplot(111)
        self._style_axis(self.pareto_axis)
        self.pareto_view = FigureCanvasTkAgg(self.pareto_figure, master=self.pareto_pane)
        self.pareto_view.get_tk_widget().pack(fill="both", expand=True)
        self.pareto_view.mpl_connect("pick_event", self._pick_curve)
        self._pareto_visible = False

    def _style_axis(self, axis) -> 'None':
        axis.set_facecolor(PLOT_BG)
        axis.tick_params(colors=PLOT_TEXT, labelsize=9)
        for spine in axis.spines.values():
            spine.set_color("#aab8c7")
        axis.xaxis.label.set_color(PLOT_TEXT)
        axis.yaxis.label.set_color(PLOT_TEXT)
        axis.grid(color=PLOT_GRID, alpha=0.85)

    def _build_controls(self, parent: 'ttk.Frame') -> 'None':
        sections = ttk.Panedwindow(parent, orient="vertical")
        self.control_panes = sections
        sections.pack(fill="both", expand=True)
        settings_host = ttk.Frame(sections, height=470)
        circuit_host = ttk.Frame(sections)
        sections.add(settings_host, weight=4)
        sections.add(circuit_host, weight=2)
        self.control_tabs = ttk.Notebook(settings_host)
        self.control_tabs.pack(fill="both", expand=True, pady=(3, 3))
        library_tab = ttk.Frame(self.control_tabs)
        property_tab = ttk.Frame(self.control_tabs)
        frequency_tab = ttk.Frame(self.control_tabs)
        sweep_tab = ttk.Frame(self.control_tabs)
        self.optimize_tab = ttk.Frame(self.control_tabs)
        for tab, title in ((library_tab, "电路结构"), (property_tab, "器件属性"),
                           (frequency_tab, "频率范围"), (sweep_tab, "Sweep"),
                           (self.optimize_tab, "自动优化")):
            self.control_tabs.add(tab, text=title)
        self.control_tabs.bind("<<NotebookTabChanged>>", self._control_tab_changed)

        library = ttk.LabelFrame(library_tab, text="电路结构库", padding=7)
        library.pack(fill="x", pady=5)
        self.template_name = tk.StringVar()
        self.template_combo = ttk.Combobox(library, textvariable=self.template_name, state="readonly")
        self.template_combo.pack(side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Button(library, text="加载", command=self.load_structure, style="Mini.TButton").pack(side="left", padx=2)
        ttk.Button(library, text="保存结构", command=self.save_structure, style="Mini.TButton").pack(side="left", padx=2)

        frequency = ttk.LabelFrame(frequency_tab, text="频率范围（GHz）", padding=(8, 5))
        frequency.pack(fill="x", pady=5)
        self.start_var, self.stop_var, self.count_var = tk.StringVar(value="0.1"), tk.StringVar(value="30"), tk.StringVar(value="1001")
        for column, (caption, variable) in enumerate((("起始", self.start_var), ("终止", self.stop_var), ("频点数", self.count_var))):
            ttk.Label(frequency, text=caption).grid(row=0, column=column, sticky="w", padx=3)
            ttk.Entry(frequency, textvariable=variable, width=10).grid(row=1, column=column, sticky="ew", padx=3, pady=(2, 0))
            frequency.columnconfigure(column, weight=1)
            variable.trace_add("write", lambda *_: self._sweep_changed())
        details = ttk.LabelFrame(property_tab, text="选中器件", padding=8)
        details.pack(fill="both", expand=True)
        self.part_name = tk.StringVar(value="请选择器件")
        ttk.Label(details, textvariable=self.part_name, foreground=ACCENT).grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, 5))
        self.value_var, self.q_var = tk.StringVar(), tk.StringVar()
        self.unit_var = tk.StringVar(value="数值")
        ttk.Label(details, textvariable=self.unit_var).grid(row=1, column=0, sticky="w")
        self.value_entry = ttk.Entry(details, textvariable=self.value_var, width=13)
        self.value_entry.grid(row=1, column=1, padx=(4, 10), sticky="ew")
        ttk.Label(details, text="Q").grid(row=1, column=2)
        self.q_entry = ttk.Entry(details, textvariable=self.q_var, width=10)
        self.q_entry.grid(row=1, column=3, padx=4, sticky="ew")
        self.l1_var, self.l2_var, self.dot_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.mutual_frame = ttk.Frame(details)
        self.mutual_frame.grid(row=2, column=0, columnspan=5, sticky="w", pady=(5, 0))
        ttk.Label(self.mutual_frame, text="电感 1").pack(side="left")
        self.l1_combo = ttk.Combobox(self.mutual_frame, textvariable=self.l1_var, state="readonly", width=10)
        self.l1_combo.pack(side="left", padx=4)
        ttk.Label(self.mutual_frame, text="电感 2").pack(side="left")
        self.l2_combo = ttk.Combobox(self.mutual_frame, textvariable=self.l2_var, state="readonly", width=10)
        self.l2_combo.pack(side="left", padx=4)
        self.dot_frame = ttk.Frame(details)
        self.dot_frame.grid(row=2, column=0, columnspan=5, sticky="w", pady=(5, 0))
        ttk.Label(self.dot_frame, text="同名端").pack(side="left")
        self.dot_combo = ttk.Combobox(self.dot_frame, textvariable=self.dot_var, values=("左/上", "右/下"), state="readonly", width=10)
        self.dot_combo.pack(side="left", padx=7)
        self.apply_button = ttk.Button(details, text="应用属性", command=self.apply_properties, style="Mini.TButton")
        self.apply_button.grid(row=1, column=5, rowspan=2, padx=(8, 0), sticky="nsew")
        for entry in (self.value_entry, self.q_entry):
            entry.bind("<Return>", lambda _: self.apply_properties())
        details.columnconfigure(1, weight=1)
        ttk.Label(details, text="选中下方器件后编辑数值。电感的小点为同名端；互感器件需选择两个不同电感；|k|<1，实际符号为k乘点号极性。\nW 连线，M 移动并带线，Shift+M 只移动器件。",
                  foreground=MUTED, wraplength=450, justify="left").grid(row=3, column=0, columnspan=6, sticky="w", pady=(11, 0))

        sweep = ttk.LabelFrame(sweep_tab, text="器件参数 Sweep", padding=8)
        sweep.pack(fill="both", expand=True)
        ttk.Label(sweep, text="启用 / 参数 / 联动组 / 方式", foreground=MUTED).grid(row=0, column=0, sticky="w")
        ttk.Label(sweep, text="起点 → 终点 / 条数", foreground=MUTED).grid(row=0, column=1, columnspan=2, sticky="w")
        box = ttk.Frame(sweep)
        box.grid(row=1, column=0, columnspan=3, sticky="nsew")
        sweep_view = ttk.Frame(box)
        sweep_view.pack(fill="both", expand=True)
        self.sweep_canvas = tk.Canvas(sweep_view, height=125, background=BACK, highlightthickness=0)
        scroll = ttk.Scrollbar(sweep_view, orient="vertical", command=self.sweep_canvas.yview)
        xscroll = ttk.Scrollbar(box, orient="horizontal", command=self.sweep_canvas.xview)
        self.sweep_canvas.configure(yscrollcommand=scroll.set, xscrollcommand=xscroll.set)
        self.sweep_canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        xscroll.pack(fill="x")
        self.sweep_inner = ttk.Frame(self.sweep_canvas)
        self.sweep_window = self.sweep_canvas.create_window((0, 0), window=self.sweep_inner, anchor="nw")
        self.sweep_inner.bind("<Configure>", lambda _: self._fit_row_canvas(
            self.sweep_canvas, self.sweep_window, self.sweep_inner))
        self.sweep_canvas.bind("<Configure>", lambda _: self._fit_row_canvas(
            self.sweep_canvas, self.sweep_window, self.sweep_inner))
        self.sweep_rows: 'list[dict]' = []
        ttk.Button(sweep, text="＋ 添加扫参变量", command=self.add_sweep, style="Mini.TButton").grid(row=2, column=0, columnspan=3, sticky="ew", pady=(5, 0))
        ttk.Label(sweep, text="默认独立：曲线数相乘；同组选中参数按序号配对。禁用的行保留设置但不参与计算。",
                  foreground=MUTED, wraplength=530).grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        sweep.columnconfigure(0, weight=1)
        self._build_optimization(self.optimize_tab)
        circuit = ttk.LabelFrame(circuit_host, text="电路编辑区", padding=5)
        circuit.pack(fill="both", expand=True, pady=(5, 2))
        tools = ttk.Frame(circuit)
        tools.pack(fill="x", pady=(0, 4))
        commands = (
            ("W", lambda: self.set_mode("wire"), "W：从引脚或线开始，空白点击加拐点，接到引脚或线才完成；Shift 改变拐弯方向。"),
            ("J", lambda: self.set_mode("junction"), "J：点击两根线的纯交叉处，加/取消连接点。"),
            ("R", self.rotate_selection, "R：每次同向旋转 90°；互感符号也可旋转。"),
            ("C", self.copy_selection, "C：复制选中的器件或线段，移动鼠标后单击放置；Esc 结束。"),
            ("F", self.fit_view, "F：将当前电路居中并适配画布。"),
            ("Del", self.delete_selection, "Delete：删除选中的器件或单段线；PORT1 始终保留。"),
            ("M", lambda: self.set_mode("move"), "M：拖动器件时，接在引脚上的线随器件移动。"),
            ("⇧M", lambda: self.set_mode("move_fixed"), "Shift+M：只移动器件，原有连线留在原位。"),
        )
        for caption, command, help_text in commands:
            button = ttk.Button(tools, text=caption, command=command, style="Mini.TButton", width=4)
            button.pack(side="left", padx=2)
            Tooltip(button, help_text)
        ttk.Label(tools, text="中键平移 · Ctrl+滚轮缩放 · Esc 取消", foreground=MUTED).pack(side="right", padx=5)
        self._build_canvas(circuit)
        self._update_inspector()

    def _build_optimization(self, parent: 'ttk.Frame') -> 'None':
        footer = ttk.Frame(parent, padding=(5, 2))
        footer.pack(side="bottom", fill="x")
        viewport = ttk.Frame(parent)
        viewport.pack(fill="both", expand=True)
        self.opt_page_canvas = tk.Canvas(viewport, background=BACK, highlightthickness=0)
        page_scroll = ttk.Scrollbar(viewport, orient="vertical", command=self.opt_page_canvas.yview)
        self.opt_page_canvas.configure(yscrollcommand=page_scroll.set)
        self.opt_page_canvas.pack(side="left", fill="both", expand=True)
        page_scroll.pack(side="right", fill="y")
        panel = ttk.Frame(self.opt_page_canvas, padding=5)
        page_window = self.opt_page_canvas.create_window((0, 0), window=panel, anchor="nw")
        panel.bind("<Configure>", lambda _: self.opt_page_canvas.configure(
            scrollregion=self.opt_page_canvas.bbox("all")))
        self.opt_page_canvas.bind("<Configure>", lambda event: self.opt_page_canvas.itemconfigure(
            page_window, width=event.width))
        panel.columnconfigure(0, weight=1)

        upper = ttk.Frame(panel)
        upper.grid(row=0, column=0, sticky="ew", pady=(0, 3))
        upper.columnconfigure(0, weight=1, uniform="opt_top")
        upper.columnconfigure(1, weight=1, uniform="opt_top")
        target_box = ttk.LabelFrame(upper, text="优化目标", padding=4)
        target_box.grid(row=0, column=0, sticky="nsew", padx=(0, 3))
        target_box.columnconfigure(1, weight=1)
        self.opt_mode = tk.StringVar(value="自定义")
        self.opt_target = tk.StringVar(value=METRIC_LABELS["bandwidth"])
        self.opt_direction = tk.StringVar(value="最大")
        self.opt_desired = tk.StringVar()
        self.opt_desired_high = tk.StringVar()
        self.opt_tolerance = tk.StringVar(value="0.01")
        self.opt_extra_targets: 'list[dict]' = []
        ttk.Label(target_box, text="模式").grid(row=0, column=0, sticky="w")
        mode_combo = ttk.Combobox(target_box, textvariable=self.opt_mode,
                                  values=("自定义", "3 dB 带宽", "峰值"),
                                  state="readonly", width=12)
        mode_combo.grid(row=0, column=1, sticky="ew", padx=3, pady=1)
        ttk.Label(target_box, text="目标").grid(row=1, column=0, sticky="w")
        self.opt_target_combo = ttk.Combobox(
            target_box, textvariable=self.opt_target,
            values=tuple(METRIC_LABELS[key] for key in ("bandwidth", "magnitude", "real", "imag", "phase")),
            state="readonly", width=18)
        self.opt_target_combo.grid(row=1, column=1, sticky="ew", padx=3, pady=1)
        ttk.Label(target_box, text="方式").grid(row=2, column=0, sticky="w")
        ttk.Combobox(target_box, textvariable=self.opt_direction,
                     values=("最大", "最小", "固定值", "范围"),
                     state="readonly", width=9).grid(row=2, column=1, sticky="ew", padx=3, pady=1)
        mode_combo.bind("<<ComboboxSelected>>", self._optimization_mode_changed)
        self.opt_mode_help = tk.StringVar(value="在指定频段内按目标方式评估。")
        ttk.Label(target_box, textvariable=self.opt_mode_help, foreground=MUTED,
                  wraplength=250, justify="left").grid(row=3, column=0, columnspan=2, sticky="w", pady=(3, 0))
        goal_fields = ttk.Frame(target_box)
        goal_fields.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(4, 1))
        for column, (caption, variable) in enumerate((
                ("期望值 / 目标下限", self.opt_desired),
                ("目标上限", self.opt_desired_high),
                ("允许误差", self.opt_tolerance))):
            cell = ttk.Frame(goal_fields)
            cell.grid(row=0, column=column, sticky="ew", padx=2)
            label = ttk.Label(cell, text=caption)
            label.pack(anchor="w")
            entry = ttk.Entry(cell, textvariable=variable, width=11)
            entry.pack(fill="x")
            goal_fields.columnconfigure(column, weight=1)
            if column == 0:
                self.opt_desired_label = label
            elif column == 1:
                self.opt_desired_high_entry = entry
        self.opt_extra_host = ttk.Frame(target_box)
        self.opt_extra_host.grid(row=5, column=0, columnspan=2, sticky="ew")
        ttk.Button(target_box, text="＋ 优化目标", command=self.add_optimization_target,
                   style="Mini.TButton").grid(row=6, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Label(target_box, text="多个目标自动使用 NSGA-II；右侧显示可点选的帕累托前沿。",
                  foreground=MUTED, wraplength=360).grid(
                      row=7, column=0, columnspan=2, sticky="w", pady=(3, 0))

        constraint_box = ttk.LabelFrame(upper, text="限制条件 · 同时满足", padding=4)
        constraint_box.grid(row=0, column=1, sticky="nsew", padx=(3, 0))
        constraint_view = ttk.Frame(constraint_box)
        constraint_view.pack(fill="x")
        self.opt_constraint_canvas = tk.Canvas(constraint_view, height=77, background=BACK, highlightthickness=0)
        con_scroll = ttk.Scrollbar(constraint_view, orient="vertical", command=self.opt_constraint_canvas.yview)
        con_xscroll = ttk.Scrollbar(constraint_box, orient="horizontal", command=self.opt_constraint_canvas.xview)
        self.opt_constraint_canvas.configure(yscrollcommand=con_scroll.set, xscrollcommand=con_xscroll.set)
        self.opt_constraint_canvas.pack(side="left", fill="x", expand=True)
        con_scroll.pack(side="right", fill="y")
        con_xscroll.pack(fill="x")
        self.opt_constraint_inner = ttk.Frame(self.opt_constraint_canvas)
        window = self.opt_constraint_canvas.create_window((0, 0), window=self.opt_constraint_inner, anchor="nw")
        self.opt_constraint_inner.bind("<Configure>", lambda _: self._fit_row_canvas(
            self.opt_constraint_canvas, window, self.opt_constraint_inner))
        self.opt_constraint_canvas.bind("<Configure>", lambda _: self._fit_row_canvas(
            self.opt_constraint_canvas, window, self.opt_constraint_inner))
        self.opt_constraints: 'list[dict]' = []
        ttk.Button(constraint_box, text="＋ 限制条件", command=self.add_optimization_constraint,
                   style="Mini.TButton").pack(fill="x", pady=(2, 0))

        self.opt_settings_expanded = False
        self.opt_settings_toggle = ttk.Button(panel, text="▸ 优化设置",
                                               command=self._toggle_optimization_settings,
                                               style="Mini.TButton")
        self.opt_settings_toggle.grid(row=1, column=0, sticky="ew", pady=(1, 2))
        budget = ttk.LabelFrame(panel, text="优化设置", padding=4)
        budget.grid(row=2, column=0, sticky="ew", pady=(0, 2))
        budget.grid_remove()
        self.opt_settings_frame = budget
        self.opt_algorithm = tk.StringVar(value="差分进化")
        self.opt_generations = tk.StringVar(value="80")
        self.opt_stall = tk.StringVar(value="20")
        settings_fields = (
            ("算法", self.opt_algorithm, "两个以上目标自动使用 NSGA-II。", ("差分进化", "CMA-ES", "NSGA-II")),
            ("最多代数", self.opt_generations, "最多搜索 5–500 代。", None),
            ("停滞代数", self.opt_stall, "连续无改进后停止。", None),
        )
        for index, (caption, variable, hint, choices) in enumerate(settings_fields):
            cell = ttk.Frame(budget)
            cell.grid(row=index // 3, column=index % 3, sticky="ew", padx=3, pady=2)
            label = ttk.Label(cell, text=caption)
            label.pack(anchor="w")
            entry = (ttk.Combobox(cell, textvariable=variable, values=choices, state="readonly", width=14)
                     if choices else ttk.Entry(cell, textvariable=variable, width=14))
            entry.pack(fill="x", pady=(2, 0))
            if index == 0:
                self.opt_algorithm_entry = entry
            Tooltip(label, hint)
            Tooltip(entry, hint)
            variable.trace_add("write", lambda *_: self._optimization_changed())
        for column in range(3):
            budget.columnconfigure(column, weight=1)

        variable_box = ttk.LabelFrame(panel, text="优化参数 · 联动组 / 下限 / 上限 / 步长", padding=4)
        variable_box.grid(row=3, column=0, sticky="nsew", pady=(2, 1))
        ttk.Button(variable_box, text="＋ 优化参数", command=self.add_optimization_variable,
                   style="Mini.TButton").pack(side="bottom", fill="x", pady=(2, 0))
        var_view = ttk.Frame(variable_box)
        var_view.pack(fill="both", expand=True)
        self.opt_variable_canvas = tk.Canvas(var_view, height=175, background=BACK, highlightthickness=0)
        var_scroll = ttk.Scrollbar(var_view, orient="vertical", command=self.opt_variable_canvas.yview)
        var_xscroll = ttk.Scrollbar(variable_box, orient="horizontal", command=self.opt_variable_canvas.xview)
        self.opt_variable_canvas.configure(yscrollcommand=var_scroll.set, xscrollcommand=var_xscroll.set)
        self.opt_variable_canvas.pack(side="left", fill="both", expand=True)
        var_scroll.pack(side="right", fill="y")
        var_xscroll.pack(side="bottom", fill="x")
        self.opt_variable_inner = ttk.Frame(self.opt_variable_canvas)
        window = self.opt_variable_canvas.create_window((0, 0), window=self.opt_variable_inner, anchor="nw")
        self.opt_variable_inner.bind("<Configure>", lambda _: self._fit_row_canvas(
            self.opt_variable_canvas, window, self.opt_variable_inner))
        self.opt_variable_canvas.bind("<Configure>", lambda _: self._fit_row_canvas(
            self.opt_variable_canvas, window, self.opt_variable_inner))
        bind_mousewheel(self.opt_variable_canvas, self._scroll_optimization_variables)
        self.opt_variables: 'list[dict]' = []

        actions = ttk.Frame(footer)
        actions.pack(fill="x", pady=(2, 1))
        self.opt_run_button = ttk.Button(actions, text="开始优化", command=self.start_optimization, style="Primary.TButton")
        self.opt_run_button.pack(side="left", padx=(0, 5))
        self.opt_stop_button = ttk.Button(actions, text="停止", command=self.stop_optimization, state="disabled")
        self.opt_stop_button.pack(side="left", padx=(0, 5))
        self.opt_apply_button = ttk.Button(actions, text="应用当前最佳值", command=self.apply_optimization, state="disabled")
        self.opt_apply_button.pack(side="left")
        self.opt_summary = tk.StringVar(value="选择并启用优化参数，然后开始优化。")
        ttk.Label(footer, textvariable=self.opt_summary, foreground=ACCENT,
                  wraplength=680, justify="left").pack(fill="x", pady=(2, 0))
        for variable in (self.opt_target, self.opt_desired, self.opt_desired_high, self.opt_tolerance):
            variable.trace_add("write", lambda *_: self._optimization_changed())
        self.opt_direction.trace_add("write", lambda *_: self._optimization_goal_changed())
        self._optimization_goal_changed()
        for item in default_optimization_settings()["variables"]:
            self.add_optimization_variable(item)
        self._bind_optimization_page_scroll(panel)

    def add_optimization_target(self, setting: 'dict | None' = None) -> 'None':
        setting = setting or {}
        card = ttk.LabelFrame(self.opt_extra_host, text=f"目标 {len(self.opt_extra_targets) + 2}", padding=3)
        card.pack(fill="x", pady=(4, 0))
        row = {"frame": card,
               "mode": tk.StringVar(value={"custom": "自定义", "3db": "3 dB 带宽",
                                           "peak": "峰值"}.get(setting.get("mode"), "自定义")),
               "target": tk.StringVar(value=METRIC_LABELS.get(setting.get("target"), METRIC_LABELS["magnitude"])),
               "direction": tk.StringVar(value={"max": "最大", "min": "最小",
                                                "value": "固定值", "range": "范围"}.get(
                                                    setting.get("direction"), "最大")),
               "desired": tk.StringVar(value=str(setting.get("desired", ""))),
               "desired_high": tk.StringVar(value=str(setting.get("desired_high", ""))),
               "tolerance": tk.StringVar(value=str(setting.get("tolerance", "0.01")))}
        columns = (
            ("模式", "mode", ("自定义", "3 dB 带宽", "峰值")),
            ("目标", "target", tuple(METRIC_LABELS[key] for key in
                                    ("magnitude", "real", "imag", "phase", "bandwidth"))),
            ("方式", "direction", ("最大", "最小", "固定值", "范围")),
            ("期望值/下限", "desired", None),
            ("目标上限", "desired_high", None),
            ("允许误差", "tolerance", None),
        )
        for index, (caption, key, choices) in enumerate(columns):
            cell = ttk.Frame(card)
            cell.grid(row=index // 3, column=index % 3, sticky="ew", padx=2, pady=1)
            ttk.Label(cell, text=caption).pack(anchor="w")
            widget = (ttk.Combobox(cell, textvariable=row[key], values=choices,
                                   state="readonly", width=12)
                      if choices else ttk.Entry(cell, textvariable=row[key], width=12))
            widget.pack(fill="x")
            row[key + "_widget"] = widget
            card.columnconfigure(index % 3, weight=1)
            row[key].trace_add("write", lambda *_: self._optimization_changed())
        ttk.Button(card, text="移除目标", style="Mini.TButton",
                   command=lambda current=row: self.remove_optimization_target(current)).grid(
                       row=2, column=0, columnspan=3, sticky="ew", pady=(2, 0))
        row["mode_widget"].bind("<<ComboboxSelected>>",
                                lambda _, current=row: self._extra_target_mode_changed(current))
        row["direction"].trace_add("write", lambda *_: self._extra_target_goal_changed(row))
        self.opt_extra_targets.append(row)
        self._extra_target_mode_changed(row)
        self._extra_target_goal_changed(row)
        self._sync_optimization_algorithm()
        self._optimization_changed()

    def remove_optimization_target(self, row: 'dict') -> 'None':
        self.opt_extra_targets.remove(row)
        row["frame"].destroy()
        for index, item in enumerate(self.opt_extra_targets, start=2):
            item["frame"].configure(text=f"目标 {index}")
        self._sync_optimization_algorithm()
        self._optimization_changed()

    def _extra_target_mode_changed(self, row: 'dict') -> 'None':
        mode = row["mode"].get()
        if mode in ("峰值", "3 dB 带宽"):
            row["target"].set(METRIC_LABELS["magnitude" if mode == "峰值" else "bandwidth"])
        row["target_widget"].configure(state="readonly" if mode == "自定义" else "disabled")
        self._optimization_changed()

    def _extra_target_goal_changed(self, row: 'dict') -> 'None':
        row["desired_high_widget"].configure(
            state="normal" if row["direction"].get() == "范围" else "disabled")
        self._optimization_changed()

    def _sync_optimization_algorithm(self) -> 'None':
        if self.opt_extra_targets:
            if self.opt_algorithm.get() != "NSGA-II":
                self._single_algorithm = self.opt_algorithm.get()
            self.opt_algorithm.set("NSGA-II")
            self.opt_algorithm_entry.configure(state="disabled")
        else:
            self.opt_algorithm_entry.configure(state="readonly")
            if self.opt_algorithm.get() == "NSGA-II":
                self.opt_algorithm.set(getattr(self, "_single_algorithm", "差分进化"))

    @staticmethod
    def _target_settings(mode, target, direction, desired, desired_high, tolerance) -> 'dict':
        return {"mode": {"自定义": "custom", "3 dB 带宽": "3db", "峰值": "peak"}[mode],
                "target": next((key for key, label in METRIC_LABELS.items() if label == target), "bandwidth"),
                "direction": {"最大": "max", "最小": "min", "固定值": "value", "范围": "range"}[direction],
                "desired": desired, "desired_high": desired_high, "tolerance": tolerance}

    def _toggle_optimization_settings(self) -> 'None':
        self.opt_settings_expanded = not self.opt_settings_expanded
        if self.opt_settings_expanded:
            self.opt_settings_frame.grid()
            self.opt_settings_toggle.configure(text="▾ 优化设置")
        else:
            self.opt_settings_frame.grid_remove()
            self.opt_settings_toggle.configure(text="▸ 优化设置")

    def _bind_optimization_page_scroll(self, widget) -> 'None':
        bind_mousewheel(widget, self._scroll_optimization_page, add="+")
        for child in widget.winfo_children():
            self._bind_optimization_page_scroll(child)

    def _scroll_optimization_page(self, event) -> 'str':
        delta = wheel_delta(event)
        if delta:
            self.opt_page_canvas.yview_scroll(-1 if delta > 0 else 1, "units")
        return "break"

    def _scroll_optimization_variables(self, event) -> 'str':
        delta = wheel_delta(event)
        if delta:
            before = self.opt_variable_canvas.yview()
            self.opt_variable_canvas.yview_scroll(-1 if delta > 0 else 1, "units")
            if before == self.opt_variable_canvas.yview():
                self.opt_page_canvas.yview_scroll(-1 if delta > 0 else 1, "units")
        return "break"

    def _optimization_goal_changed(self) -> 'None':
        goal = self.opt_direction.get()
        self.opt_desired_label.configure(text={
            "最大": "期望达到值（可空）", "最小": "期望达到值（可空）",
            "固定值": "目标值", "范围": "目标下限"}.get(goal, "目标值"))
        self.opt_desired_high_entry.configure(state="normal" if goal == "范围" else "disabled")
        self._optimization_changed()

    def _optimization_mode_changed(self, _=None) -> 'None':
        mode = self.opt_mode.get()
        if mode == "3 dB 带宽":
            self.opt_target.set(METRIC_LABELS["bandwidth"])
            self.opt_target_combo.configure(state="disabled")
            self.opt_mode_help.set("按两侧半功率交点计算 3 dB 带宽；可选最大/最小、固定值或范围目标。")
        elif mode == "峰值":
            self.opt_target.set(METRIC_LABELS["magnitude"])
            self.opt_target_combo.configure(state="disabled")
            self.opt_mode_help.set("在满足限制条件的频点中优化 |Z|；固定值/范围选最接近目标的频点。")
        else:
            self.opt_target_combo.configure(state="readonly")
            self.opt_mode_help.set("整个频率窗口评估：最大化最低值、最小化最高值；固定值/范围按最差频点达标。")
        self._optimization_changed()

    def _optimization_changed(self) -> 'None':
        if not self._ready or self._loading_page:
            return
        if self._opt_thread is not None and self._opt_thread.is_alive():
            self._opt_cancel.set()
        self.dirty = True
        self._refresh_title()
        self.status.set("优化设置已更改；原曲线仍保留，点击“开始优化”使用新设置。")

    def _refresh_optimization_parameter_choices(self) -> 'None':
        if not hasattr(self, "opt_variables"):
            return
        catalog = self._parameter_labels()
        labels = dict(catalog, **GLOBAL_Q_LABELS)
        for row in self.opt_variables:
            key = row["parameter"].get()
            row["combo"].configure(values=[GLOBAL_Q_LABELS[key]] if key in GLOBAL_Q_LABELS
                                   else list(catalog.values()))
            row["display"].set(labels.get(key, f"{key} · 已删除" if key else "请选择参数"))

    def _optimization_group_choices(self) -> 'list[str]':
        used = {row["group"].get() for row in self.opt_variables if row["group"].get() != "独立"}
        number = 1
        while f"联动 {number}" in used:
            number += 1
        choices = used | {f"联动 {number}"}
        return ["独立"] + sorted(choices, key=lambda name: int(name.split()[-1])
                                if name.split()[-1].isdigit() else 9999)

    def _refresh_optimization_group_choices(self) -> 'None':
        choices = self._optimization_group_choices()
        for row in self.opt_variables:
            row["group_combo"].configure(values=choices)

    def add_optimization_variable(self, setting: 'dict | None' = None) -> 'None':
        catalog = parameters(self.document)
        setting = setting or {}
        if not catalog and setting.get("parameter") not in GLOBAL_Q_LABELS:
            messagebox.showinfo("没有可优化参数", "请先放置 R、L、C 或互感器件。", parent=self)
            return
        used = {row["parameter"].get() for row in self.opt_variables}
        available = next((key for key in catalog if key not in used), None)
        if "parameter" not in setting and available is None:
            messagebox.showinfo("参数已全部加入", "当前电路的可优化参数都已添加。", parent=self)
            return
        key = setting.get("parameter", available)
        name, field = key.split(".") if "." in key else ("", "")
        part = next((item for item in self.document["parts"] if item["id"] == name), None)
        current = float(part.get(field, 1.0)) if part else 1.0
        if not math.isfinite(current):
            current = 50.0
        low, high = ((max(-.99, min(current * .5, current * 1.5)), min(.99, max(current * .5, current * 1.5, .01 if current == 0 else -1)))
                     if part and part["kind"] == "K" else (current * .8, current * 1.2))
        labels = dict(self._parameter_labels(), **GLOBAL_Q_LABELS)
        row = {"parameter": tk.StringVar(value=key), "display": tk.StringVar(value=labels.get(key, key)),
               "enabled": tk.BooleanVar(value=setting.get("enabled", True)),
               "group": tk.StringVar(value=str(setting.get("group", "独立"))),
               "low": tk.StringVar(value=str(setting.get("low", f"{low:g}"))),
               "high": tk.StringVar(value=str(setting.get("high", f"{high:g}"))),
               "step": tk.StringVar(value="" if setting.get("step") is None else str(setting.get("step", "")))}
        frame = ttk.Frame(self.opt_variable_inner)
        frame.pack(fill="x", pady=2)
        enabled = ttk.Checkbutton(frame, text="启用", variable=row["enabled"])
        enabled.pack(side="left", padx=(0, 3))
        Tooltip(enabled, "取消勾选后保留本行设置，但不参与本次优化。")
        choices = [labels[key]] if key in GLOBAL_Q_LABELS else list(self._parameter_labels().values())
        combo = ttk.Combobox(frame, textvariable=row["display"], values=choices,
                             state="disabled" if key in GLOBAL_Q_LABELS else "readonly", width=17)
        combo.pack(side="left", fill="x", expand=True, padx=(0, 3))
        combo.bind("<<ComboboxSelected>>", lambda _, current=row: self._choose_optimization_parameter(current))
        group_combo = ttk.Combobox(frame, textvariable=row["group"], state="readonly", width=9)
        group_combo.pack(side="left", padx=(0, 3))
        Tooltip(group_combo, "独立：单独优化。相同联动组共用一个搜索值；启用的组员上下限必须一致。")
        row["combo"], row["group_combo"], row["frame"] = combo, group_combo, frame
        for field_name, width in (("low", 9), ("high", 9), ("step", 8)):
            entry = ttk.Entry(frame, textvariable=row[field_name], width=width)
            entry.pack(side="left", padx=2)
            if field_name == "step":
                Tooltip(entry, "可留空表示连续优化。填写后仅取下限 + 整数倍步长，且不超过上限。联动组中非空步长须一致。")
        if key not in GLOBAL_Q_LABELS:
            ttk.Button(frame, text="×", command=lambda current=row: self.remove_optimization_variable(current),
                       style="Mini.TButton", width=2).pack(side="left", padx=2)
        self.opt_variables.append(row)
        for widget in (frame, *frame.winfo_children()):
            bind_mousewheel(widget, self._scroll_optimization_variables)
        for field_name in ("parameter", "enabled", "low", "high", "step"):
            row[field_name].trace_add("write", lambda *_: self._optimization_changed())
        row["group"].trace_add("write", lambda *_: (self._refresh_optimization_group_choices(),
                                                    self._optimization_changed()))
        self._refresh_optimization_group_choices()
        self._optimization_changed()

    def _choose_optimization_parameter(self, row: 'dict') -> 'None':
        selected = row["display"].get()
        key = next((key for key, label in self._parameter_labels().items()
                    if label == selected), "")
        if key:
            row["parameter"].set(key)
        else:
            self._refresh_optimization_parameter_choices()

    def remove_optimization_variable(self, row: 'dict') -> 'None':
        self.opt_variables.remove(row)
        row["frame"].destroy()
        self._refresh_optimization_group_choices()
        self._optimization_changed()

    def add_optimization_constraint(self, setting: 'dict | None' = None) -> 'None':
        setting = setting or {}
        metric = setting.get("metric", "magnitude")
        row = {"metric": tk.StringVar(value=METRIC_LABELS.get(metric, METRIC_LABELS["magnitude"])),
                "relation": tk.StringVar(value="范围" if setting.get("relation") == "range" else setting.get("relation", ">")),
               "low": tk.StringVar(value=str(setting.get("low", "100"))),
               "high": tk.StringVar(value=str(setting.get("high", "200")))}
        frame = ttk.Frame(self.opt_constraint_inner)
        frame.pack(fill="x", pady=2)
        ttk.Combobox(frame, textvariable=row["metric"], values=tuple(METRIC_LABELS[key] for key in ("frequency", "real", "imag", "magnitude", "phase")),
                     state="readonly", width=18).pack(side="left", fill="x", expand=True, padx=(0, 3))
        ttk.Combobox(frame, textvariable=row["relation"], values=(">", "<", "范围"), state="readonly", width=6).pack(side="left", padx=2)
        low_entry = ttk.Entry(frame, textvariable=row["low"], width=10)
        low_entry.pack(side="left", padx=2)
        high_entry = ttk.Entry(frame, textvariable=row["high"], width=10)
        high_entry.pack(side="left", padx=2)
        ttk.Button(frame, text="×", command=lambda current=row: self.remove_optimization_constraint(current),
                   style="Mini.TButton", width=2).pack(side="left", padx=2)
        row["frame"], row["high_entry"] = frame, high_entry
        self.opt_constraints.append(row)
        row["relation"].trace_add("write", lambda *_ , current=row: self._optimization_relation_changed(current))
        for field_name in ("metric", "low", "high"):
            row[field_name].trace_add("write", lambda *_: self._optimization_changed())
        self._optimization_relation_changed(row)

    def _optimization_relation_changed(self, row: 'dict') -> 'None':
        row["high_entry"].configure(state="normal" if row["relation"].get() == "范围" else "disabled")
        self._optimization_changed()

    def remove_optimization_constraint(self, row: 'dict') -> 'None':
        self.opt_constraints.remove(row)
        row["frame"].destroy()
        self._optimization_changed()

    def _optimization_settings(self) -> 'dict':
        primary = self._target_settings(
            self.opt_mode.get(), self.opt_target.get(), self.opt_direction.get(),
            self.opt_desired.get(), self.opt_desired_high.get(), self.opt_tolerance.get())
        objectives = [primary] + [
            self._target_settings(*(row[key].get() for key in
                                    ("mode", "target", "direction", "desired", "desired_high", "tolerance")))
            for row in self.opt_extra_targets]
        return {"variables": [{key: row[key].get() for key in ("parameter", "enabled", "group", "low", "high", "step")}
                              for row in self.opt_variables],
                "constraints": [{"metric": next((key for key, label in METRIC_LABELS.items() if label == row["metric"].get()), ""),
                                 "relation": "range" if row["relation"].get() == "范围" else row["relation"].get(),
                                 "low": row["low"].get(), "high": row["high"].get()} for row in self.opt_constraints],
                **primary, "objectives": objectives,
                "algorithm": {"差分进化": "de", "CMA-ES": "cma", "NSGA-II": "nsga"}[self.opt_algorithm.get()],
                "generations": self.opt_generations.get(), "stall": self.opt_stall.get()}

    def _load_optimization_settings(self, settings: 'dict') -> 'None':
        if self.opt_settings_expanded:
            self._toggle_optimization_settings()
        for row in self.opt_variables:
            row["frame"].destroy()
        self.opt_variables.clear()
        for row in self.opt_constraints:
            row["frame"].destroy()
        self.opt_constraints.clear()
        saved_rows = settings.get("variables", [])
        for default_row in default_optimization_settings()["variables"]:
            self.add_optimization_variable(next((item for item in saved_rows
                                                 if item.get("parameter") == default_row["parameter"]), default_row))
        for item in saved_rows:
            if item.get("parameter") not in GLOBAL_Q_LABELS:
                self.add_optimization_variable(item)
        for item in settings.get("constraints", []):
            self.add_optimization_constraint(item)
        for row in list(self.opt_extra_targets):
            row["frame"].destroy()
        self.opt_extra_targets.clear()
        targets = settings.get("objectives") or [settings]
        primary = targets[0]
        self.opt_mode.set({"custom": "自定义", "3db": "3 dB 带宽", "peak": "峰值"}.get(primary.get("mode"), "自定义"))
        self.opt_target.set(METRIC_LABELS.get(primary.get("target"), METRIC_LABELS["bandwidth"]))
        self.opt_direction.set({"max": "最大", "min": "最小", "value": "固定值",
                                "range": "范围"}.get(primary.get("direction"), "最大"))
        for variable, key in ((self.opt_desired, "desired"), (self.opt_desired_high, "desired_high"),
                              (self.opt_tolerance, "tolerance")):
            variable.set(str(primary.get(key, default_optimization_settings()[key])))
        for variable, key in ((self.opt_generations, "generations"), (self.opt_stall, "stall")):
            variable.set(str(settings.get(key, default_optimization_settings()[key])))
        self.opt_algorithm.set({"de": "差分进化", "cma": "CMA-ES",
                                "nsga": "NSGA-II"}.get(settings.get("algorithm"), "差分进化"))
        for item in targets[1:]:
            self.add_optimization_target(item)
        self._sync_optimization_algorithm()
        self._optimization_mode_changed()

    def start_optimization(self) -> 'None':
        if self._opt_thread is not None and self._opt_thread.is_alive():
            messagebox.showinfo("优化正在运行", "请等待当前优化结束，或先点击“停止”。", parent=self)
            return
        self.status.set("正在计算网络响应…")
        try:
            start, stop, count = float(self.start_var.get()), float(self.stop_var.get()), int(self.count_var.get())
            if not math.isfinite(start) or not math.isfinite(stop) or not 0 < start < stop or not 2 <= count <= 5001:
                raise ValueError("优化频率须满足 0<起始<终止 GHz；频点数须在 2–5001。")
            frequency = np.linspace(start, stop, count)
            settings = validate_settings(self.document, frequency, self._optimization_settings())
        except (ValueError, TypeError, KeyError) as error:
            messagebox.showerror("优化设置无效", str(error), parent=self)
            return
        self._save_page_state()
        self._opt_page = self.pages[self.active_page]
        self._opt_document_fingerprint = json.dumps(self.document, sort_keys=True, default=str)
        self._opt_cancel.clear()
        self._opt_history_run = []
        self.optimization_history = self._opt_history_run
        self._opt_page["optimization_history"] = self._opt_history_run
        self._opt_page["optimization_result"] = None
        self._refresh_sidebar()
        self.opt_summary.set("正在优化；左侧显示各代最佳候选。")
        self.opt_run_button.configure(state="disabled")
        self.opt_stop_button.configure(state="normal")
        self.opt_apply_button.configure(state="disabled")
        document = copy.deepcopy(self.document)
        self._opt_thread = threading.Thread(target=self._optimization_worker,
                                            args=(document, frequency, settings), daemon=True)
        self._opt_thread.start()
        self.after(100, self._poll_optimization)

    def _optimization_worker(self, document: 'dict', frequency: 'np.ndarray', settings: 'dict') -> 'None':
        try:
            result = optimize(document, frequency, settings,
                              progress=lambda item: self._opt_queue.put(("progress", item)),
                              cancel=self._opt_cancel)
            self._opt_queue.put(("done", result))
        except Exception as error:
            self._opt_queue.put(("error", str(error)))

    def stop_optimization(self) -> 'None':
        if self._opt_thread is not None and self._opt_thread.is_alive():
            self._opt_cancel.set()
            self.opt_summary.set("正在停止优化；已完成的最佳候选会保留。")

    def _poll_optimization(self) -> 'None':
        completed = False
        while True:
            try:
                kind, payload = self._opt_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                item = payload["best"]
                self._opt_history_run.append(item)
                if any(page is self._opt_page for page in self.pages):
                    self._opt_page["optimization_history"] = self._opt_history_run
                if self._opt_page is self.pages[self.active_page]:
                    self.opt_summary.set(f"第 {payload['generation']} 代 · 已计算 {payload['evaluations']} 次 · "
                                         + ("尚无可行频段" if item["value"] is None else f"当前最佳 {item['value']:.6g}"))
                    if self.control_tabs.select() == str(self.optimize_tab):
                        self._refresh_sidebar()
            elif kind == "done":
                self._finish_optimization(payload)
                completed = True
            else:
                self.opt_summary.set(f"优化计算失败：{payload}")
                self.status.set(f"优化计算失败：{payload}")
                completed = True
        if completed:
            self._opt_thread = None
            self.opt_run_button.configure(state="normal")
            self.opt_stop_button.configure(state="disabled")
        elif self._opt_thread is not None:
            self.after(100, self._poll_optimization)

    def _finish_optimization(self, result: 'dict') -> 'None':
        page = self._opt_page
        if page is None or not any(candidate is page for candidate in self.pages):
            return
        page["optimization_history"] = result.get("samples", result.get("history", []))
        best = result.get("best")
        pareto = result.get("pareto", [])
        selected_index = next((index for index, point in enumerate(pareto)
                               if best and point["parameters"] == best["parameters"]), None)
        metadata = {"success": result["success"], "reason": result["reason"], "best": best,
                    "generations": result.get("generations", 0), "evaluations": result.get("evaluations", 0),
                    "pareto": pareto, "objectives": result["settings"].get("objectives", []),
                    "selected_index": selected_index, "fingerprint": self._opt_document_fingerprint,
                    "frequency_spec": [float(result["frequency"][0]),
                                       float(result["frequency"][-1]), len(result["frequency"])]}
        page["optimization_result"] = metadata
        same_page = page is self.pages[self.active_page]
        if same_page:
            self.optimization_history = page["optimization_history"]
        unchanged = same_page and json.dumps(self.document, sort_keys=True, default=str) == self._opt_document_fingerprint
        if best and best["feasible"] and result.get("impedance") is not None and unchanged:
            metric = result["settings"]["target"]
            objective = "3 dB 带宽" if result["settings"]["mode"] == "3db" else METRIC_LABELS[metric]
            label = (f"帕累托候选 · {len(pareto)} 点" if pareto else
                     f"优化候选 · {objective}={best['value']:.6g}")
            page["result"] = {"frequency": result["frequency"],
                              "curves": [{"label": label, "values": result["impedance"],
                                          "sweep": best["parameters"], "ignored": 0}],
                              "source": "optimization"}
            page["stale"] = False
            self.data = result["frequency"], result["impedance"]
            self.highlighted = None
            self._plot_results()
            self.opt_apply_button.configure(state="normal")
        if same_page:
            detail = ""
            if best and best["feasible"]:
                if best.get("band"):
                    detail = f" 频段 {best['band'][0]:.5g}–{best['band'][1]:.5g} GHz。"
                elif best.get("point_frequency") is not None:
                    point_label = "瓶颈频点" if result["settings"]["mode"] == "custom" else "频点"
                    detail = f" {point_label} {best['point_frequency']:.5g} GHz。"
            if not unchanged:
                detail += " 电路已变化，候选图未覆盖当前结果。"
            shown = len(page["optimization_history"])
            if result.get("evaluations", 0) > shown:
                detail += f" 左侧保留最近 {shown} 个候选点。"
            self.opt_summary.set(result["reason"] + detail)
            self.status.set(f"优化结束：{result.get('generations', 0)} 代、{result.get('evaluations', 0)} 次计算。{result['reason']}")
            self._refresh_sidebar()

    def apply_optimization(self) -> 'None':
        page = self.pages[self.active_page]
        outcome = page.get("optimization_result")
        best = outcome.get("best") if outcome else None
        if not best or not best.get("feasible") or self.opt_apply_button.instate(["disabled"]):
            return
        try:
            current_spec = [float(self.start_var.get()), float(self.stop_var.get()),
                            int(self.count_var.get())]
        except ValueError:
            current_spec = None
        if (json.dumps(self.document, sort_keys=True, default=str) != outcome.get(
                "fingerprint", self._opt_document_fingerprint) or
                current_spec != outcome.get("frequency_spec", current_spec)):
            messagebox.showerror("无法应用", "优化后电路或频率范围已改变，请重新优化。", parent=self)
            self.opt_apply_button.configure(state="disabled")
            return
        parts = {part["id"]: part for part in self.document["parts"]}
        for key in best["parameters"]:
            if key not in GLOBAL_Q_LABELS and key.split(".")[0] not in parts:
                messagebox.showerror("无法应用", f"{key} 已从电路中删除，请重新优化。", parent=self)
                return
        for key, value in best["parameters"].items():
            apply_parameter(parts, key, value)
        self._changed()
        if page.get("result") and page["result"].get("source") == "optimization":
            page["result"]["source"] = "optimization_applied"
            page["stale"] = False
            self.result_badge.set("优化结果 · 已应用")
        self._refresh_parameter_choices()
        self._update_inspector()
        self.redraw()
        self.opt_apply_button.configure(state="disabled")
        self.status.set("已应用优化值到电路；可通过 Ctrl+Z 撤销。")

    def _build_canvas(self, parent: 'ttk.Frame') -> 'None':
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(frame, background=PLOT,
                                scrollregion=(-CANVAS_LIMIT, -CANVAS_LIMIT, CANVAS_LIMIT, CANVAS_LIMIT),
                                highlightthickness=1, highlightbackground="#36536a", takefocus=1)
        sx = ttk.Scrollbar(frame, orient="horizontal", command=lambda *args: self._scroll_canvas("x", *args))
        sy = ttk.Scrollbar(frame, orient="vertical", command=lambda *args: self._scroll_canvas("y", *args))
        self.canvas.configure(xscrollcommand=sx.set, yscrollcommand=sy.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns")
        sx.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        for sequence, handler in (("<ButtonPress-1>", self._canvas_press), ("<B1-Motion>", self._canvas_drag),
                                  ("<ButtonRelease-1>", self._canvas_release), ("<Motion>", self._canvas_motion),
                                  ("<Double-Button-1>", self._canvas_double),
                                  ("<ButtonPress-2>", self._pan_start), ("<B2-Motion>", self._pan_drag),
                                  ("<ButtonRelease-2>", self._pan_end),
                                  ("<ButtonPress-3>", self._context_menu),
                                  ("<Configure>", self._draw_visible_grid)):
            self.canvas.bind(sequence, handler)
        bind_mousewheel(self.canvas, self._wheel)
        self.after_idle(self._center_canvas_initial)

    def _center_canvas_initial(self) -> 'None':
        self.canvas.xview_moveto((CANVAS_LIMIT + 80) / (2 * CANVAS_LIMIT))
        self.canvas.yview_moveto((CANVAS_LIMIT + 80) / (2 * CANVAS_LIMIT))
        self._draw_visible_grid()

    def _restore_canvas_view(self, saved_view=None) -> 'None':
        if saved_view is None:
            points = [(part["x"], part["y"]) for part in self.document["parts"]]
            points += [point for wire in self.document["wires"] for point in wire]
            left = min((point[0] for point in points), default=80) - 70
            top = min((point[1] for point in points), default=80) - 70
        else:
            left, top = saved_view
        if saved_view is None:
            left, top = left * self.zoom, top * self.zoom
        edge = CANVAS_LIMIT * self.zoom
        self.canvas.xview_moveto((edge + left) / (2 * edge))
        self.canvas.yview_moveto((edge + top) / (2 * edge))
        self._draw_visible_grid()

    def _draw_visible_grid(self, _=None) -> 'None':
        if not hasattr(self, "canvas"):
            return
        canvas = self.canvas
        canvas.delete("grid")
        left, top = canvas.canvasx(0), canvas.canvasy(0)
        right = canvas.canvasx(canvas.winfo_width())
        bottom = canvas.canvasy(canvas.winfo_height())
        for x in range(math.floor(left / self.zoom / GRID) * GRID,
                       math.ceil(right / self.zoom / GRID) * GRID + 1, GRID):
            canvas.create_line(x * self.zoom, top, x * self.zoom, bottom, fill="#1c3042", tags="grid")
        for y in range(math.floor(top / self.zoom / GRID) * GRID,
                       math.ceil(bottom / self.zoom / GRID) * GRID + 1, GRID):
            canvas.create_line(left, y * self.zoom, right, y * self.zoom, fill="#1c3042", tags="grid")
        canvas.tag_lower("grid")

    def _scroll_canvas(self, axis: 'str', *args) -> 'None':
        (self.canvas.xview if axis == "x" else self.canvas.yview)(*args)
        self._draw_visible_grid()

    def _point(self, event: 'tk.Event') -> 'tuple[int, int]':
        return snap(self.canvas.canvasx(event.x) / self.zoom), snap(self.canvas.canvasy(event.y) / self.zoom)

    def _pointer_on_canvas(self, event: 'tk.Event') -> 'tuple[int, int] | None':
        x = event.x_root - self.canvas.winfo_rootx()
        y = event.y_root - self.canvas.winfo_rooty()
        if not (0 <= x < self.canvas.winfo_width() and 0 <= y < self.canvas.winfo_height()):
            return None
        return snap(self.canvas.canvasx(x) / self.zoom), snap(self.canvas.canvasy(y) / self.zoom)

    def _set_zoom(self, value: 'float', anchor: 'tuple[float, float] | None' = None) -> 'None':
        canvas = self.canvas
        x, y = anchor or (canvas.winfo_width() / 2, canvas.winfo_height() / 2)
        world_x = canvas.canvasx(x) / self.zoom
        world_y = canvas.canvasy(y) / self.zoom
        self.zoom = max(.12, min(4.0, value))
        edge = CANVAS_LIMIT * self.zoom
        canvas.configure(scrollregion=(-edge, -edge, edge, edge))
        self.redraw()
        canvas.xview_moveto((world_x * self.zoom - x + edge) / (2 * edge))
        canvas.yview_moveto((world_y * self.zoom - y + edge) / (2 * edge))
        self._draw_visible_grid()
        self.status.set(f"画布缩放 {self.zoom:.0%}；Ctrl+滚轮缩放，F 适配电路。")

    def fit_view(self) -> 'None':
        points = []
        for part in self.document["parts"]:
            points.extend(((part["x"] - 45, part["y"] - 45),
                           (part["x"] + 45, part["y"] + 45)))
        points.extend(tuple(point) for wire in self.document["wires"] for point in wire)
        if not points:
            return
        x1, x2 = min(x for x, _ in points), max(x for x, _ in points)
        y1, y2 = min(y for _, y in points), max(y for _, y in points)
        width, height = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        scale = min(3.0, width / max(140, x2 - x1 + 140),
                    height / max(140, y2 - y1 + 140))
        self._set_zoom(scale)
        edge = CANVAS_LIMIT * self.zoom
        self.canvas.xview_moveto((((x1 + x2) / 2) * self.zoom - width / 2 + edge) / (2 * edge))
        self.canvas.yview_moveto((((y1 + y2) / 2) * self.zoom - height / 2 + edge) / (2 * edge))
        self._draw_visible_grid()
        self.status.set("视角已适配当前电路。")

    def _wheel(self, event: 'tk.Event') -> 'str':
        if event.state & 0x0004:
            self._set_zoom(self.zoom * (1.2 if wheel_delta(event) > 0 else 1 / 1.2),
                           (event.x, event.y))
        else:
            super()._wheel(event)
            self._draw_visible_grid()
        return "break"

    def _pan_start(self, event: 'tk.Event') -> 'None':
        self.canvas.scan_mark(event.x, event.y)
        self.canvas.configure(cursor="fleur")

    def _pan_drag(self, event: 'tk.Event') -> 'None':
        self.canvas.scan_dragto(event.x, event.y, gain=1)
        self._draw_visible_grid()

    def _pan_end(self, _=None) -> 'None':
        self.canvas.configure(cursor="crosshair" if self.mode != "select" else "arrow")

    def _bind_shortcuts(self) -> 'None':
        super()._bind_shortcuts()
        for sequence, command in (("<KeyPress-m>", lambda: self.set_mode("move")),
                                  ("<KeyPress-M>", lambda: self.set_mode("move_fixed")),
                                  ("<Shift-m>", lambda: self.set_mode("move_fixed")),
                                  ("<KeyPress-c>", self.copy_selection),
                                  ("<KeyPress-C>", self.copy_selection),
                                  ("<KeyPress-f>", self.fit_view),
                                  ("<KeyPress-F>", self.fit_view)):
            self.bind_all(sequence, lambda event, fn=command: self._shortcut(event, fn))

    def set_mode(self, mode: 'str') -> 'None':
        super().set_mode(mode)
        self.wire_path = []
        self.wire_drag = None
        if mode != "copy":
            self.copy_source = None
        if mode == "wire":
            self.status.set("W 连线：从引脚或线开始；空白点击添加拐点，接到引脚或线才完成。Esc 取消。")
        elif mode == "move":
            self.status.set("M 移动：拖动器件会带线；也可垂直拖动选中的线段。Esc 返回选择。")
        elif mode == "move_fixed":
            self.status.set("Shift+M：只移动器件，连线保持原位；线段仍可单独拖动。")
        elif mode == "copy":
            self.status.set("C 复制：移动到目标位置并单击放置；可连续放置，Esc 结束。")
        self.redraw()

    def _context_menu(self, event: 'tk.Event') -> 'None':
        if self.mode != "select":
            return
        point = self._point(event)
        part_id = self._hit_part(point)
        segment = None if part_id else self._hit_wire_segment(event)
        menu = tk.Menu(self, tearoff=0, background=PANEL, foreground=TEXT,
                       activebackground=RAISED, activeforeground=TEXT)
        if part_id:
            self.selection, self.selected_wires = {part_id}, set()
            self.selected_wire_segment = None
            part = next(p for p in self.document["parts"] if p["id"] == part_id)
            if part["kind"] != "P":
                menu.add_command(label="复制  C", command=self.copy_selection)
            menu.add_command(label="旋转  R", command=self.rotate_selection)
            if part["kind"] != "P":
                menu.add_command(label="删除  Del", command=self.delete_selection)
            menu.add_command(label="编辑属性", command=lambda: self.control_tabs.select(1))
        elif segment is not None:
            self.selection, self.selected_wires = set(), {segment[0]}
            self.selected_wire_segment = segment
            menu.add_command(label="复制线段  C", command=self.copy_selection)
            menu.add_command(label="删除线段  Del", command=self.delete_selection)
            menu.add_command(label="从这里连线  W",
                             command=lambda: (self.set_mode("wire"), self._press_wire(event)))
        else:
            menu.add_command(label="开始连线  W", command=lambda: self.set_mode("wire"))
        menu.add_separator()
        menu.add_command(label="适配视角  F", command=self.fit_view)
        self._update_inspector()
        self.redraw()
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _wire_anchor(self, point: 'tuple[int, int]') -> 'bool':
        from network_model import _on_segment, _segments
        if any(point in terminals(part) for part in self.document["parts"] if part["kind"] != "K"):
            return True
        segments, _ = _segments(self.document)
        return any(_on_segment(point, a, b) for a, b in segments)

    def _press_wire(self, event: 'tk.Event') -> 'None':
        self.canvas.focus_set()
        point = self._point(event)
        if self.wire_start is None:
            if len(self.document["wires"]) >= 500:
                self.status.set("当前页已达到 500 根线的上限。")
                return
            if not self._wire_anchor(point):
                self.status.set("连线请从器件引脚或已有线段开始。")
                return
            self.wire_path = [list(point)]
            self.wire_start = point
            self.status.set("已选起点；空白点击添加拐点，接到引脚或线才完成。")
        elif point != self.wire_start:
            extension = route(self.wire_start, point, bool(event.state & 0x0001))
            candidate = self.wire_path + extension[1:]
            if len(candidate) > 100:
                self.status.set("单根连线拐点超过 100 个；请接到已有线后继续。")
                return
            if self._wire_anchor(point):
                self.document["wires"].append(candidate)
                self.wire_path = []
                self.wire_start = None
                self._changed()
                self.status.set("已接入引脚或线；可继续从另一处开始连线。")
            else:
                self.wire_path = candidate
                self.wire_start = point
                self.status.set("已添加拐点；请继续接到器件引脚或已有线段。")
        self.redraw()

    def copy_selection(self) -> 'None':
        chosen = [copy.deepcopy(part) for part in self.document["parts"]
                  if part["id"] in self.selection and part["kind"] != "P"]
        names = {part["id"] for part in chosen}
        if any(part["kind"] == "K" and
               (part.get("l1") not in names or part.get("l2") not in names)
               for part in chosen):
            self.status.set("复制互感时，请同时选中它绑定的两个电感。")
            return
        wires = []
        for index in sorted(self.selected_wires):
            points = self.document["wires"][index]
            if self.selected_wire_segment and self.selected_wire_segment[0] == index:
                segment = self.selected_wire_segment[1]
                wires.append(copy.deepcopy(points[segment:segment + 2]))
            else:
                wires.append(copy.deepcopy(points))
        if not chosen and not wires:
            self.status.set("请先选中器件或线段；PORT1 不可复制。")
            return
        anchor = ((chosen[0]["x"], chosen[0]["y"]) if chosen else tuple(wires[0][0]))
        self.copy_source = {"parts": chosen, "wires": wires, "anchor": anchor}
        self.set_mode("copy")
        self.cursor = (anchor[0] + 40, anchor[1] + 40)
        self.redraw()

    def _place_copy(self, event: 'tk.Event') -> 'None':
        source = self.copy_source
        if not source:
            self.set_mode("select")
            return
        point = self._point(event)
        dx, dy = point[0] - source["anchor"][0], point[1] - source["anchor"][1]
        if dx == 0 and dy == 0:
            self.status.set("请把复制预览移到新位置，再单击放置。")
            return
        if (len(self.document["parts"]) + len(source["parts"]) > 200 or
                len(self.document["wires"]) + len(source["wires"]) > 500):
            self.status.set("复制后会超过当前页 200 个器件或 500 根线的上限。")
            return
        used = {part["id"] for part in self.document["parts"]}
        mapping = {}
        new_parts = []
        for original in source["parts"]:
            kind = original["kind"]
            number = 1
            while f"{kind}{number}" in used:
                number += 1
            clone = copy.deepcopy(original)
            clone["id"] = f"{kind}{number}"
            clone["x"] += dx
            clone["y"] += dy
            used.add(clone["id"])
            mapping[original["id"]] = clone["id"]
            new_parts.append(clone)
        for part in new_parts:
            if part["kind"] == "K":
                part["l1"] = mapping[part["l1"]]
                part["l2"] = mapping[part["l2"]]
        first_wire = len(self.document["wires"])
        self.document["parts"].extend(new_parts)
        self.document["wires"].extend(
            [[list((x + dx, y + dy)) for x, y in wire] for wire in source["wires"]])
        self.selection = {part["id"] for part in new_parts}
        self.selected_wires = set(range(first_wire, len(self.document["wires"])))
        self.selected_wire_segment = None
        self.cursor = point
        self._changed()
        self._refresh_parameter_choices()
        self._update_inspector()
        self.status.set("复制已放置；移动鼠标可继续复制，Esc 结束。")
        self.redraw()

    def _hit_wire_segment(self, event: 'tk.Event') -> 'tuple[int, int] | None':
        x = self.canvas.canvasx(event.x) / self.zoom
        y = self.canvas.canvasy(event.y) / self.zoom
        tolerance = 8 / self.zoom
        closest = None
        best = tolerance + 1
        for wire_index, points in enumerate(self.document["wires"]):
            for segment_index, (a, b) in enumerate(zip(points, points[1:])):
                if a[0] == b[0]:
                    distance = abs(x - a[0])
                    inside = min(a[1], b[1]) - tolerance <= y <= max(a[1], b[1]) + tolerance
                elif a[1] == b[1]:
                    distance = abs(y - a[1])
                    inside = min(a[0], b[0]) - tolerance <= x <= max(a[0], b[0]) + tolerance
                else:
                    continue
                if inside and distance < best:
                    closest, best = (wire_index, segment_index), distance
        return closest

    def _wire_segment_taps(self, wire_index: 'int', segment_index: 'int') -> 'list[tuple[int, int]]':
        from network_model import _on_segment
        points = self.document["wires"][wire_index]
        a, b = tuple(points[segment_index]), tuple(points[segment_index + 1])
        taps = set()
        for index, other in enumerate(self.document["wires"]):
            if index != wire_index:
                taps.update(tuple(point) for point in (other[0], other[-1])
                            if _on_segment(tuple(point), a, b))
        for part in self.document["parts"]:
            if part["kind"] != "K":
                taps.update(point for point in terminals(part) if _on_segment(point, a, b))
        taps.update(tuple(point) for point in self.document.get("junctions", [])
                    if _on_segment(tuple(point), a, b))
        return sorted(taps - {a, b})

    @staticmethod
    def _shift_wire_segment(points: 'list[list[int]]', index: 'int',
                            delta: 'tuple[int, int]') -> 'list[list[int]]':
        a, b = points[index], points[index + 1]
        offset = (0, delta[1]) if a[1] == b[1] else (delta[0], 0)
        if offset == (0, 0):
            return copy.deepcopy(points)
        moved_a = [a[0] + offset[0], a[1] + offset[1]]
        moved_b = [b[0] + offset[0], b[1] + offset[1]]
        return copy.deepcopy(points[:index + 1]) + [moved_a, moved_b] + copy.deepcopy(points[index + 1:])

    def _canvas_press(self, event: 'tk.Event') -> 'None':
        if self.mode == "wire":
            self._press_wire(event)
            return
        if self.mode == "copy":
            self._place_copy(event)
            return
        point = self._point(event)
        part_id = self._hit_part(point)
        segment = None if part_id else self._hit_wire_segment(event)
        if self.mode in ("move", "move_fixed") and not part_id and segment is None:
            self.status.set("移动模式下，请点击器件或线段。")
            return
        if segment is not None and self.mode in ("select", "move", "move_fixed"):
            self.canvas.focus_set()
            wire_index, segment_index = segment
            if event.state & 0x0001:
                self.selected_wires.add(wire_index)
                self.selected_wire_segment = None
                self.wire_drag = None
            else:
                self.selection.clear()
                self.selected_wires = {wire_index}
                self.selected_wire_segment = segment
                if len(self.document["wires"][wire_index]) > 98:
                    self.status.set("该折线已接近 100 个顶点上限；请先拆分后移动此段。")
                    self.wire_drag = None
                    self.redraw()
                    return
                self.wire_drag = {"start": point, "wire": wire_index,
                                  "segment": segment_index,
                                  "original": copy.deepcopy(self.document["wires"]),
                                  "taps": self._wire_segment_taps(wire_index, segment_index),
                                  "delta": (0, 0)}
            self._update_inspector()
            self.redraw()
            return
        self.selected_wire_segment = None
        super()._canvas_press(event)

    def _canvas_drag(self, event: 'tk.Event') -> 'None':
        if self.wire_drag is not None:
            point = self._point(event)
            start = self.wire_drag["start"]
            delta = (point[0] - start[0], point[1] - start[1])
            self.wire_drag["delta"] = delta
            index = self.wire_drag["wire"]
            original = self.wire_drag["original"]
            segment = self.wire_drag["segment"]
            a, b = original[index][segment:segment + 2]
            offset = (0, delta[1]) if a[1] == b[1] else (delta[0], 0)
            wires = copy.deepcopy(original)
            wires[index] = self._shift_wire_segment(original[index], segment, delta)
            if offset != (0, 0):
                if len(wires) + len(self.wire_drag["taps"]) > 500:
                    self.status.set("保留所有分支会超过 500 根线；请先简化或拆分电路。")
                    return
                wires.extend([[list(tap), [tap[0] + offset[0], tap[1] + offset[1]]]
                              for tap in self.wire_drag["taps"]])
            self.document["wires"] = wires
            self._dots_dirty = True
            self.redraw()
            return
        if not self.drag:
            super()._canvas_drag(event)
            return
        point = self._point(event)
        start = self.drag["start"]
        delta = (point[0] - start[0], point[1] - start[1])
        self.drag["delta"] = delta
        old_to_new = {}
        for part in self.document["parts"]:
            if part["id"] not in self.drag["positions"]:
                continue
            x, y = self.drag["positions"][part["id"]]
            if part["kind"] != "K":
                old = terminals({**part, "x": x, "y": y})
            part["x"], part["y"] = x + delta[0], y + delta[1]
            if part["kind"] != "K":
                old_to_new.update(zip(old, terminals(part)))
        if self.mode != "move_fixed":
            self.document["wires"] = self._follow_terminals(self.drag["wires"], old_to_new)
        self._dots_dirty = True
        self.redraw()

    def _canvas_release(self, event: 'tk.Event') -> 'None':
        if self.wire_drag is not None:
            original = self.wire_drag["original"]
            index = self.wire_drag["wire"]
            moved = self.document["wires"] != original
            if moved:
                self.selected_wire_segment = (index, self.wire_drag["segment"] + 1)
            self.wire_drag = None
            if moved:
                self._changed()
                self.status.set("已单独移动线段；两端已保留正交连接，可用 Ctrl+Z 撤销。")
            self.redraw()
            return
        super()._canvas_release(event)

    def _add_part(self, kind: 'str', point: 'tuple[int, int]') -> 'None':
        if kind != "K":
            super()._add_part(kind, point)
            self._refresh_parameter_choices()
            return
        used = {p["id"] for p in self.document["parts"]}
        number = 1
        while f"K{number}" in used:
            number += 1
        inductors = [p["id"] for p in self.document["parts"] if p["kind"] == "L"]
        part = {"id": f"K{number}", "kind": "K", "x": point[0], "y": point[1], "rotation": 0,
                "value": 0.2, "q": 0.0, "l1": inductors[0] if inductors else "", "l2": inductors[1] if len(inductors) > 1 else ""}
        self.document["parts"].append(part)
        self.selection, self.selected_wires = {part["id"]}, set()
        self._update_inspector()
        self._changed()
        self._refresh_parameter_choices()
        self.redraw()

    def _paint_coupler(self, canvas: 'tk.Canvas', part: 'dict', tags: 'tuple[str, ...]', selected: 'bool' = False) -> 'None':
        color = AMBER if selected else ACCENT
        points = []
        for step in range(25):
            t = step / 24
            points.extend(transform(part, -23 + 46 * t, 6 - 14 * math.sin(math.pi * t)))
        canvas.create_line(*points, fill=color, width=2.2, smooth=True, tags=tags)
        for a, b in (((-23, 6), (-17, -1)), ((-23, 6), (-14, 7)),
                     ((23, 6), (17, -1)), ((23, 6), (14, 7))):
            canvas.create_line(*transform(part, *a), *transform(part, *b),
                               fill=color, width=2.2, tags=tags)
        if tags:
            x, y = transform(part, 31, 0)
            anchor = ("w", "n", "e", "s")[part["rotation"] % 4]
            canvas.create_text(x, y, text=f"{part['id']}  k={part['value']:g}",
                               anchor=anchor, fill=color,
                               font=(SYMBOL_FONT, 9, "bold"), tags=tags)

    def redraw(self) -> 'None':
        if not hasattr(self, "canvas"):
            return
        canvas = self.canvas
        canvas.delete("scene")
        for wire_index, points in enumerate(self.document["wires"]):
            for segment_index, (a, b) in enumerate(zip(points, points[1:])):
                selected = wire_index in self.selected_wires and (
                    self.selected_wire_segment is None or
                    self.selected_wire_segment == (wire_index, segment_index))
                canvas.create_line(*a, *b, fill=AMBER if selected else ACCENT,
                                   width=3 if selected else 2,
                                   joinstyle="round", capstyle="round", tags="scene")
        self._draw_junction_dots()
        for part in self.document["parts"]:
            if part["kind"] == "K":
                self._paint_coupler(canvas, part, ("scene",), part["id"] in self.selection)
            else:
                draw_symbol(canvas, part, selected=part["id"] in self.selection, tags=("scene",))
                if part["kind"] == "L":
                    x, y = terminals(part)[part.get("dot", 0)]
                    canvas.create_oval(x - 5, y - 5, x + 5, y + 5,
                                       fill=AMBER, outline=PLOT, width=1, tags="scene")
        if len(self.wire_path) > 1:
            canvas.create_line(*[v for point in self.wire_path for v in point],
                               fill=AMBER, width=2, tags="scene")
        if self.wire_start is not None and self.cursor is not None:
            points = route(self.wire_start, self.cursor)
            canvas.create_line(*[v for point in points for v in point],
                               fill=AMBER, width=2, dash=(5, 4), tags="scene")
            if self._wire_anchor(self.cursor):
                x, y = self.cursor
                canvas.create_oval(x - 6, y - 6, x + 6, y + 6,
                                   outline=AMBER, width=2, tags="scene")
        if self.mode.startswith("place_") and self.cursor:
            kind = self.mode[-1]
            sample = {"id": kind, "kind": kind, "x": self.cursor[0], "y": self.cursor[1],
                      "rotation": 0, "value": 0.2, "q": 20.0}
            if kind == "K":
                self._paint_coupler(canvas, sample, ("scene",), True)
            else:
                draw_symbol(canvas, sample, selected=True, tags=("scene",), label=False)
        if self.mode == "copy" and self.copy_source and self.cursor:
            source = self.copy_source
            dx = self.cursor[0] - source["anchor"][0]
            dy = self.cursor[1] - source["anchor"][1]
            for wire in source["wires"]:
                canvas.create_line(*[v for x, y in wire for v in (x + dx, y + dy)],
                                   fill=AMBER, width=2, dash=(4, 3), tags="scene")
            for original in source["parts"]:
                preview = {**original, "x": original["x"] + dx, "y": original["y"] + dy}
                if preview["kind"] == "K":
                    self._paint_coupler(canvas, preview, ("scene",), True)
                else:
                    draw_symbol(canvas, preview, selected=True, tags=("scene",), label=False)
        if self.box_start and self.box_end:
            canvas.create_rectangle(*self.box_start, *self.box_end, outline=AMBER,
                                    width=2, dash=(4, 3), tags="scene")
        if self.zoom != 1:
            canvas.scale("scene", 0, 0, self.zoom, self.zoom)

    def _update_inspector(self) -> 'None':
        if not hasattr(self, "part_name"):
            return
        selected = [p for p in self.document["parts"] if p["id"] in self.selection]
        self.mutual_frame.grid_remove()
        self.dot_frame.grid_remove()
        if len(selected) != 1:
            self.part_name.set("请选择器件" if not selected else f"已选择 {len(selected)} 个器件")
            self.value_var.set("")
            self.q_var.set("")
            for widget in (self.value_entry, self.q_entry, self.apply_button):
                widget.configure(state="disabled")
            return
        part = selected[0]
        kind = part["kind"]
        self.part_name.set(f"{part['id']}  ·  { {'R':'电阻','L':'电感','C':'电容','P':'端口','K':'互感'}[kind] }")
        self.unit_var.set({"R": "R (Ω)", "L": "L (pH)", "C": "C (fF)", "K": "k (0–1)", "P": "端口"}[kind])
        self.value_var.set("" if kind == "P" else f"{part['value']:g}")
        self.q_var.set(f"{part['q']:g}" if kind in ("L", "C") else "")
        self.value_entry.configure(state="disabled" if kind == "P" else "normal")
        self.q_entry.configure(state="normal" if kind in ("L", "C") else "disabled")
        self.apply_button.configure(state="disabled" if kind == "P" else "normal")
        if kind == "L":
            self.dot_var.set("左/上" if part.get("dot", 0) == 0 else "右/下")
            self.dot_frame.grid()
        if kind == "K":
            choices = [p["id"] for p in self.document["parts"] if p["kind"] == "L"]
            self.l1_combo.configure(values=choices)
            self.l2_combo.configure(values=choices)
            self.l1_var.set(part.get("l1", ""))
            self.l2_var.set(part.get("l2", ""))
            self.mutual_frame.grid()

    def apply_properties(self) -> 'None':
        selected = [p for p in self.document["parts"] if p["id"] in self.selection]
        if len(selected) != 1 or selected[0]["kind"] == "P":
            return
        part = selected[0]
        try:
            value = float(self.value_var.get())
            if not math.isfinite(value) or (part["kind"] == "K" and not abs(value) < 1) or (part["kind"] != "K" and value <= 0):
                raise ValueError("数值须为正数；互感 k 须满足 |k|<1，符号相对于电感点号。")
            q = float(self.q_var.get()) if part["kind"] in ("L", "C") else 0.0
            if part["kind"] in ("L", "C") and (math.isnan(q) or q <= 0):
                raise ValueError("Q 须大于 0；inf 表示理想器件。")
            if part["kind"] == "K":
                inductors = {p["id"] for p in self.document["parts"] if p["kind"] == "L"}
                l1, l2 = self.l1_var.get(), self.l2_var.get()
                if l1 not in inductors or l2 not in inductors or l1 == l2:
                    raise ValueError("互感必须选择两个不同的电感。")
                for other in self.document["parts"]:
                    if other["kind"] == "K" and other is not part and {other.get("l1"), other.get("l2")} == {l1, l2}:
                        raise ValueError("这对电感已绑定互感，请勿重复。")
        except ValueError as error:
            messagebox.showerror("属性无效", str(error), parent=self)
            return
        part["value"], part["q"] = value, q
        if part["kind"] == "L":
            part["dot"] = 0 if self.dot_var.get() == "左/上" else 1
        if part["kind"] == "K":
            part["l1"], part["l2"] = l1, l2
        self._changed()
        self._refresh_parameter_choices()
        self._update_inspector()
        self.redraw()

    def rotate_selection(self) -> 'None':
        selected = [p for p in self.document["parts"] if p["id"] in self.selection]
        if not selected:
            return
        moves = {}
        for part in selected:
            old = terminals(part) if part["kind"] != "K" else ()
            part["rotation"] = (part["rotation"] + 1) % 4
            if old:
                moves.update(zip(old, terminals(part)))
        self.document["wires"] = self._follow_terminals(self.document["wires"], moves)
        self._changed()
        self.redraw()

    def delete_selection(self) -> 'None':
        if self.selected_wire_segment is not None and not self.selection:
            wire_index, segment = self.selected_wire_segment
            if wire_index < len(self.document["wires"]):
                points = self.document["wires"][wire_index]
                remaining = []
                left, right = points[:segment + 1], points[segment + 1:]
                if len(left) >= 2:
                    remaining.append(left)
                if len(right) >= 2:
                    remaining.append(right)
                self.document["wires"][wire_index:wire_index + 1] = remaining
                self.selected_wires.clear()
                self.selected_wire_segment = None
                self._changed()
                self.redraw()
                self.status.set("已删除选中的线段，其余线段保留。")
                return
        removed = {name for name in self.selection if name != "PORT1"}
        if not removed and not self.selected_wires:
            self.status.set("PORT1 始终保留；可拖动或旋转。")
            return
        self.document["parts"] = [p for p in self.document["parts"] if p["id"] not in removed and
                                  not (p["kind"] == "K" and (p.get("l1") in removed or p.get("l2") in removed))]
        self.document["wires"] = [wire for i, wire in enumerate(self.document["wires"]) if i not in self.selected_wires]
        self.selection.clear()
        self.selected_wires.clear()
        self.selected_wire_segment = None
        self._update_inspector()
        self._changed()
        self._refresh_parameter_choices()
        self.redraw()

    def select_all(self) -> 'None':
        self.selected_wire_segment = None
        super().select_all()

    def _invalidate_results(self) -> 'None':
        # V2 retains the last successful plots until a new calculation succeeds.
        pass

    def _restore_history(self) -> 'None':
        self.selected_wire_segment = None
        self.wire_drag = None
        self.wire_path = []
        self.wire_start = None
        self.copy_source = None
        self.mode = "select"
        self.canvas.configure(cursor="arrow")
        super()._restore_history()
        if self._ready:
            self.pages[self.active_page]["document"] = self.document
            self.pages[self.active_page]["stale"] = True
            self.result_badge.set("旧结果 · 待计算" if self.pages[self.active_page]["result"] else "尚未计算")

    def _changed(self) -> 'None':
        if self._opt_thread is not None and self._opt_thread.is_alive():
            self._opt_cancel.set()
        junctions = []
        for point in self.document.get("junctions", []):
            rays, touched, endpoint = wire_contact(self.document, tuple(point))
            if len(rays) >= 4 and len(touched) >= 2 and not endpoint:
                junctions.append(list(point))
        self.document["junctions"] = junctions
        super()._changed()
        if self._ready:
            self.pages[self.active_page]["stale"] = True
            self.result_badge.set("旧结果 · 待计算" if self.pages[self.active_page]["result"] else "尚未计算")
            self.status.set("电路已修改；两张图保留上次结果，点击右上角“计算”更新。")

    def _sweep_changed(self) -> 'None':
        if not self._ready or self._loading_page:
            return
        if self._opt_thread is not None and self._opt_thread.is_alive():
            self._opt_cancel.set()
        self.dirty = True
        self.pages[self.active_page]["stale"] = True
        self.result_badge.set("旧结果 · 待计算" if self.pages[self.active_page]["result"] else "尚未计算")
        self._refresh_title()
        self.status.set("频率或器件扫参设置已修改；旧曲线保留，点击右上角“计算”更新。")

    def _parameter_labels(self) -> 'dict[str, str]':
        return parameters(self.document)

    def _refresh_parameter_choices(self) -> 'None':
        if not hasattr(self, "sweep_rows"):
            return
        labels = self._parameter_labels()
        for row in self.sweep_rows:
            row["combo"].configure(values=list(labels.values()))
            key = row["parameter"].get()
            row["display"].set(labels.get(key, f"{key}  ·  已删除" if key else "请选择参数"))
        self._refresh_optimization_parameter_choices()

    def _choose_sweep_parameter(self, row: 'dict') -> 'None':
        choices = self._parameter_labels()
        selected = row["display"].get()
        key = next((key for key, label in choices.items() if label == selected), "")
        if key:
            row["parameter"].set(key)
        else:
            self._refresh_parameter_choices()

    def _group_choices(self) -> 'list[str]':
        used = {row["group"].get() for row in self.sweep_rows if row["group"].get() != "独立"}
        number = 1
        while f"联动 {number}" in used:
            number += 1
        choices = used | {f"联动 {number}"}
        return ["独立"] + sorted(choices, key=lambda name: int(name.split()[-1]) if name.split()[-1].isdigit() else 9999)

    def _refresh_group_choices(self) -> 'None':
        choices = self._group_choices()
        for row in self.sweep_rows:
            row["group_combo"].configure(values=choices)

    def add_sweep(self, setting: 'dict | None' = None) -> 'None':
        catalog = parameters(self.document)
        if not catalog:
            messagebox.showinfo("没有可扫描的参数", "请先放置 R、L、C 或互感器件。", parent=self)
            return
        setting = setting or {}
        used = {row["parameter"].get() for row in self.sweep_rows}
        available = next((candidate for candidate in catalog if candidate not in used), None)
        if "parameter" not in setting and available is None:
            messagebox.showinfo("参数已全部加入", "当前电路的可扫描参数都已添加。", parent=self)
            return
        key = setting.get("parameter", available)
        part_name, field = key.split(".") if "." in key else ("", "")
        part = next((p for p in self.document["parts"] if p["id"] == part_name), None)
        current = float(part.get(field, 1)) if part else 1.0
        if not math.isfinite(current):
            current = 50.0
        lower, upper = (max(-.99, min(current * .5, current * 1.5)), min(.99, max(current * .5, current * 1.5, .01 if current == 0 else -1))) if part and part["kind"] == "K" else (current * 0.8, current * 1.2)
        labels = self._parameter_labels()
        row = {"parameter": tk.StringVar(value=key), "display": tk.StringVar(value=labels.get(key, f"{key}  ·  已删除")),
               "group": tk.StringVar(value=str(setting.get("group", "独立"))),
               "enabled": tk.BooleanVar(value=setting.get("enabled", True)),
               "scale": tk.StringVar(value={"linear": "线性", "log": "对数"}.get(
                   setting.get("scale", "linear"), "线性")),
               "start": tk.StringVar(value=str(setting.get("start", f"{lower:g}"))),
               "stop": tk.StringVar(value=str(setting.get("stop", f"{upper:g}"))),
               "count": tk.StringVar(value=str(setting.get("count", "5")))}
        frame = ttk.Frame(self.sweep_inner)
        frame.pack(fill="x", pady=3)
        enabled = ttk.Checkbutton(frame, text="启用", variable=row["enabled"])
        enabled.pack(side="left", padx=(0, 3))
        Tooltip(enabled, "取消勾选后保留本行设置，但不参与本次 Sweep。")
        combo = ttk.Combobox(frame, textvariable=row["display"], values=list(labels.values()),
                             state="readonly", width=15)
        combo.pack(side="left", padx=(0, 3), fill="x", expand=True)
        combo.bind("<<ComboboxSelected>>", lambda _, current=row: self._choose_sweep_parameter(current))
        group_combo = ttk.Combobox(frame, textvariable=row["group"], state="readonly", width=9)
        group_combo.pack(side="left", padx=(0, 3))
        Tooltip(group_combo, "独立：与其他扫描参数组合，曲线数相乘。选择相同联动组：组内参数按序号一一配对，条数须相同。")
        scale_combo = ttk.Combobox(frame, textvariable=row["scale"], values=("线性", "对数"),
                                   state="readonly", width=5)
        scale_combo.pack(side="left", padx=(0, 3))
        Tooltip(scale_combo, "线性：等差扫参；对数：等比扫参。对数扫参的起点和终点都须大于 0。")
        row["combo"], row["group_combo"], row["frame"] = combo, group_combo, frame
        for key_name, width in (("start", 7), ("stop", 7), ("count", 5)):
            ttk.Entry(frame, textvariable=row[key_name], width=width).pack(side="left", padx=2)
        ttk.Button(frame, text="×", command=lambda current=row: self.remove_sweep(current), style="Mini.TButton", width=2).pack(side="left", padx=2)
        self.sweep_rows.append(row)
        for key_name in ("parameter", "enabled", "scale", "start", "stop", "count"):
            row[key_name].trace_add("write", lambda *_: self._sweep_changed())
        row["group"].trace_add("write", lambda *_: (self._refresh_group_choices(), self._sweep_changed()))
        self._refresh_group_choices()
        if not self._loading_page:
            self._sweep_changed()

    def remove_sweep(self, row: 'dict') -> 'None':
        self.sweep_rows.remove(row)
        row["frame"].destroy()
        self._refresh_group_choices()
        self._sweep_changed()

    def _sweep_settings(self) -> 'list[dict]':
        return [{**{key: row[key].get() for key in ("parameter", "group", "start", "stop", "count", "enabled")},
                 "scale": {"线性": "linear", "对数": "log"}.get(row["scale"].get(), "")}
                for row in self.sweep_rows]

    def _save_page_state(self) -> 'None':
        page = self.pages[self.active_page]
        page["document"] = self.document
        page["start"], page["stop"], page["count"] = self.start_var.get(), self.stop_var.get(), self.count_var.get()
        page["sweeps"] = self._sweep_settings()
        page["optimization"] = self._optimization_settings()
        page["optimization_history"] = self.optimization_history
        page["view"] = (self.canvas.canvasx(0), self.canvas.canvasy(0))
        page["zoom"] = self.zoom
        page["history"], page["future"] = self.history, self.future

    def _sweep_curves(self) -> 'list[dict]':
        catalog = parameters(self.document)
        dimensions: 'list[list[tuple[str, np.ndarray]]]' = []
        linked: 'dict[str, list[tuple[str, np.ndarray]]]' = {}
        used = set()
        for setting in self._sweep_settings():
            if not setting["enabled"]:
                continue
            key = setting["parameter"]
            if key not in catalog:
                raise ValueError(f"Sweep 参数 {key or '(空)'} 已不存在；请重新选择。")
            if key in used:
                raise ValueError(f"{key} 被重复扫描；同一参数只可添加一次。")
            used.add(key)
            group = setting["group"]
            if group != "独立" and not re.fullmatch(r"联动 [1-9][0-9]*", group):
                raise ValueError(f"{catalog[key]} 的联动组无效；请重新选择。")
            try:
                start, stop = float(setting["start"]), float(setting["stop"])
                count = int(setting["count"])
            except ValueError as error:
                raise ValueError(f"{catalog[key]} 的扫描起点、终点或条数不是有效数字。") from error
            if not math.isfinite(start) or not math.isfinite(stop) or start == stop or not 2 <= count <= 200:
                raise ValueError(f"{catalog[key]}：起点和终点须为不同的有限数，条数须在 2–200。")
            if key.endswith(".value") and any(p["id"] == key.rsplit(".", 1)[0] and p["kind"] == "K" for p in self.document["parts"]):
                if min(start, stop) <= -1 or max(start, stop) >= 1:
                    raise ValueError(f"{catalog[key]} 的 k 须满足 -1<k<1；负区间请选择线性扫描。")
            elif min(start, stop) <= 0:
                raise ValueError(f"{catalog[key]} 的扫描值须大于 0。")
            scale = setting["scale"]
            if scale not in ("linear", "log"):
                raise ValueError(f"{catalog[key]} 的扫参方式无效；请选择线性或对数。")
            if scale == "log" and min(start, stop) <= 0:
                raise ValueError(f"{catalog[key]} 的对数扫参起点和终点必须大于 0。")
            values = (key, np.linspace(start, stop, count) if scale == "linear"
                      else np.geomspace(start, stop, count))
            if group == "独立":
                dimensions.append([values])
            else:
                if group not in linked:
                    linked[group] = []
                    dimensions.append(linked[group])
                linked[group].append(values)
        if not dimensions:
            return [{}]
        cases = []
        for dimension in dimensions:
            counts = {len(values) for _, values in dimension}
            if len(counts) != 1:
                group = next(setting["group"] for setting in self._sweep_settings()
                             if setting["parameter"] == dimension[0][0])
                raise ValueError(f"{group} 内的扫描条数不一致；请把该组每行“条数”设成相同值。")
            count = len(dimension[0][1])
            cases.append([{key: float(values[i]) for key, values in dimension} for i in range(count)])
        total = math.prod(len(case) for case in cases)
        if total > MAX_CURVES:
            raise ValueError(f"参数组合将产生 {total} 条曲线，超过 {MAX_CURVES} 条上限；请减少独立扫参数量或联动部分变量。")
        return [dict(itertools.chain.from_iterable(part.items() for part in combination))
                for combination in itertools.product(*cases)]

    def calculate(self) -> 'None':
        self.status.set("正在计算网络响应…")
        try:
            start, stop, count = float(self.start_var.get()), float(self.stop_var.get()), int(self.count_var.get())
            if not math.isfinite(start) or not math.isfinite(stop) or not 0 < start < stop or not 2 <= count <= 20001:
                raise ValueError("频率需满足 0<起始<终止 GHz，频点数需在 2–20001。")
            sweep_values = self._sweep_curves()
            if len(sweep_values) * count > MAX_TOTAL_POINTS:
                raise ValueError(f"本次扫描总采样点超过 {MAX_TOTAL_POINTS}；请减少曲线条数或频点数。")
            frequency = np.linspace(start, stop, count)
            curves = []
            catalog = parameters(self.document)
            for index, values in enumerate(sweep_values):
                document = copy.deepcopy(self.document)
                by_id = {p["id"]: p for p in document["parts"]}
                for key, value in values.items():
                    name, field = key.split(".")
                    by_id[name][field] = value
                impedance, ignored = solve(document, frequency)
                label = "基准参数" if not values else ", ".join(f"{catalog[key]}={value:g}" for key, value in values.items())
                curves.append({"label": label, "values": impedance, "sweep": values, "ignored": ignored})
                if (index + 1) % max(5, len(sweep_values) // 40) == 0:
                    self.status.set(f"正在计算第 {index + 1}/{len(sweep_values)} 条曲线…")
                    self.update_idletasks()
        except (ValueError, KeyError, TypeError, np.linalg.LinAlgError) as error:
            messagebox.showerror("计算未完成", str(error), parent=self)
            self.status.set("计算失败；原有曲线仍保留。请按提示修正参数或连线。")
            return
        self._save_page_state()
        self.pages[self.active_page]["result"] = {"frequency": frequency, "curves": curves}
        self.pages[self.active_page]["stale"] = False
        self.data = frequency, curves[0]["values"]
        self.highlighted = None
        self._plot_results()
        self.status.set(f"计算完成：{len(curves)} 条曲线、每条 {count} 点。" + (f" {max(c['ignored'] for c in curves)} 个未接入端口的器件已忽略。" if any(c["ignored"] for c in curves) else ""))

    def _visibility_changed(self) -> 'None':
        if not (self.show_real.get() or self.show_imag.get() or self.show_mag.get()):
            self.show_mag.set(True)
            self.status.set("阻抗图至少显示一项；已重新勾选幅值。")
        self._plot_results()

    def _plot_results(self) -> 'None':
        if not hasattr(self, "plots"):
            return
        result = self.pages[self.active_page].get("result")
        for _, axis, _ in self.plots:
            axis.clear()
            self._style_axis(axis)
        self.lines_by_curve = {}
        if result:
            frequency = result["frequency"]
            for index, curve in enumerate(result["curves"]):
                color = COLORS[index % len(COLORS)]
                impedance = curve["values"]
                lines = []
                for checked, data, style in ((self.show_real.get(), impedance.real, "-"),
                                             (self.show_imag.get(), impedance.imag, "--"),
                                             (self.show_mag.get(), np.abs(impedance), ":")):
                    if checked:
                        line, = self.plots[0][1].plot(frequency, data, color=color, linestyle=style, linewidth=1.5, picker=5)
                        line.set_gid(index)
                        lines.append(line)
                phase_line, = self.plots[1][1].plot(frequency, np.angle(impedance, deg=True), color=color, linewidth=1.7, picker=5)
                phase_line.set_gid(index)
                lines.append(phase_line)
                self.lines_by_curve[index] = lines
            self._apply_highlight()
        self.plots[0][1].set(xlabel="GHz", ylabel="Ω", title="Z11  ·  实线 Re / 虚线 Im / 点线 |Z|")
        self.plots[1][1].set(xlabel="GHz", ylabel="相位 (°)", title="Z11  ·  相位", ylim=(-185, 185))
        for _, axis, view in self.plots:
            axis.title.set_color(PLOT_TEXT)
            view.draw_idle()
        self._plot_pareto()
        page = self.pages[self.active_page]
        self.result_badge.set("旧结果 · 待计算" if page["stale"] and result else
                              "优化候选 · 未应用" if result and result.get("source") == "optimization" else
                              f"{len(result['curves'])} 条曲线" if result else "尚未计算")
        self._refresh_sidebar()

    def _plot_pareto(self) -> 'None':
        outcome = self.pages[self.active_page].get("optimization_result") or {}
        front = outcome.get("pareto", [])
        visible = bool(front and len(outcome.get("objectives", [])) > 1)
        if visible and not self._pareto_visible:
            self.chart_panes.add(self.pareto_pane, weight=1)
            self._pareto_visible = True
        elif not visible and self._pareto_visible:
            self.chart_panes.forget(self.pareto_pane)
            self._pareto_visible = False
        if not visible:
            return
        goals = outcome["objectives"]
        def axis_value(point, index):
            return (point["costs"][index] if goals[index]["direction"] in ("value", "range")
                    else point["objectives"][index])
        def axis_label(index):
            goal = goals[index]
            metric = METRIC_LABELS[goal["target"]]
            if goal["direction"] == "value":
                return f"{metric} 距 {goal['desired']:g} 的偏差"
            if goal["direction"] == "range":
                return f"{metric} 超出目标范围"
            if goal["mode"] == "custom" and goal["target"] != "bandwidth":
                return ("最低" if goal["direction"] == "max" else "最高") + metric
            return metric
        axis = self.pareto_axis
        axis.clear()
        self._style_axis(axis)
        selected = outcome.get("selected_index")
        colors = ["#d97731" if index == selected else "#087f9a" for index in range(len(front))]
        sizes = [70 if index == selected else 32 for index in range(len(front))]
        points = axis.scatter([axis_value(point, 0) for point in front],
                              [axis_value(point, 1) for point in front],
                              s=sizes, c=colors, edgecolors=fw.LINE, linewidths=.5, picker=7)
        points.set_gid("pareto")
        axis.set(xlabel=axis_label(0), ylabel=axis_label(1),
                 title=("帕累托前沿 · 前两目标投影" if len(goals) > 2 else "帕累托前沿 · 点击点查看曲线与参数"))
        axis.title.set_color(PLOT_TEXT)
        self.pareto_view.draw_idle()

    def _select_pareto(self, index: 'int') -> 'None':
        page = self.pages[self.active_page]
        outcome = page.get("optimization_result") or {}
        front = outcome.get("pareto", [])
        if not 0 <= index < len(front):
            return
        try:
            current_spec = [float(self.start_var.get()), float(self.stop_var.get()),
                            int(self.count_var.get())]
        except ValueError:
            self.status.set("频率范围无效；请检查输入后重新优化。")
            return
        if (json.dumps(self.document, sort_keys=True, default=str) !=
                outcome.get("fingerprint", self._opt_document_fingerprint) or
                current_spec != outcome.get("frequency_spec")):
            self.status.set("电路或频率范围已变化；请重新优化后再选用帕累托候选。")
            return
        point = front[index]
        try:
            frequency = np.linspace(float(self.start_var.get()), float(self.stop_var.get()),
                                    int(self.count_var.get()))
            trial = copy.deepcopy(self.document)
            parts = {part["id"]: part for part in trial["parts"]}
            for key, value in point["parameters"].items():
                apply_parameter(parts, key, value)
            impedance, _ = solve(trial, frequency)
        except (ValueError, TypeError, KeyError, np.linalg.LinAlgError) as error:
            messagebox.showerror("帕累托候选无法计算", str(error), parent=self)
            return
        outcome["selected_index"] = index
        outcome["best"] = {"parameters": point["parameters"], "value": point["objectives"][0],
                           "objectives": point["objectives"], "band": point.get("band"),
                           "point_frequency": point.get("point_frequency"), "feasible": True}
        details = " · ".join(f"{METRIC_LABELS[goal['target']]}={value:.6g}"
                             for goal, value in zip(outcome["objectives"], point["objectives"]))
        page["result"] = {"frequency": frequency,
                          "curves": [{"label": f"帕累托点 {index + 1} · {details}",
                                      "values": impedance, "sweep": point["parameters"], "ignored": 0}],
                          "source": "optimization"}
        page["stale"] = False
        self.data = frequency, impedance
        self.opt_apply_button.configure(state="normal")
        self._plot_results()
        self.selected_values.set(f"帕累托点 {index + 1} · {details}\n" +
                                 ", ".join(f"{key}={value:.6g}" for key, value in point["parameters"].items()))
        self.status.set(f"已选择帕累托点 {index + 1}；可点击“应用当前最佳值”写入电路。")

    def _control_tab_changed(self, _=None) -> 'None':
        self._fit_control_tab()
        self.after_idle(self._fit_control_tab)
        if hasattr(self, "legend"):
            self._refresh_sidebar()

    @staticmethod
    def _fit_row_canvas(canvas: 'tk.Canvas', window: 'int', inner: 'ttk.Frame') -> 'None':
        canvas.itemconfigure(window, width=max(canvas.winfo_width(), inner.winfo_reqwidth()))
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _fit_control_tab(self) -> 'None':
        # The user-adjustable vertical sash owns the settings height.
        pass

    def _refresh_sidebar(self) -> 'None':
        if not hasattr(self, "legend"):
            return
        self.legend.delete(*self.legend.get_children())
        if self.control_tabs.select() == str(self.optimize_tab):
            front = (self.pages[self.active_page].get("optimization_result") or {}).get("pareto", [])
            if front:
                heading = self.legend.insert("", "end", iid="front", text=f"帕累托前沿 · {len(front)} 点", open=True)
                for index, point in enumerate(front):
                    values = ", ".join(f"{value:.4g}" for value in point["objectives"])
                    self.legend.insert(heading, "end", iid=f"front_{index}",
                                       text=f"{index + 1}. {values}")
            for index, item in enumerate(self.optimization_history):
                value = "不可行" if item["value"] is None else f"{item['value']:.5g}"
                label = (f"点 {item['evaluation']} · {value}" if "evaluation" in item
                         else f"第 {item['generation']} 代 · {value}")
                self.legend.insert("", "end", iid=f"opt_{index}", text=label)
            result = self.pages[self.active_page].get("optimization_result")
            self.selected_values.set(result["reason"] if result else "优化过程与最佳参数会显示在这里。")
        else:
            result = self.pages[self.active_page].get("result")
            if result:
                for index, curve in enumerate(result["curves"]):
                    self.legend.insert("", "end", iid=str(index), text=f"{index + 1}. {curve['label']}")
            self.selected_values.set("单击曲线或左侧列表可查看扫描值。")

    def _apply_highlight(self) -> 'None':
        for index, lines in self.lines_by_curve.items():
            for line in lines:
                line.set_alpha(1.0 if self.highlighted is None or self.highlighted == index else 0.15)
                line.set_linewidth(2.7 if self.highlighted == index else 1.5)
                line.set_color(ACCENT if self.highlighted==index else COLORS[index % len(COLORS)])
        for _, _, view in self.plots:
            view.draw_idle()

    def _select_curve(self, index: 'int | None') -> 'None':
        self.highlighted = None if self.highlighted == index else index
        if self.highlighted is None:
            self.legend.selection_remove(self.legend.selection())
            self.selected_values.set("已恢复显示所有曲线。")
        else:
            curve = self.pages[self.active_page]["result"]["curves"][self.highlighted]
            self.selected_values.set(f"曲线 {self.highlighted + 1}：{curve['label']}")
            if self.control_tabs.select() != str(self.optimize_tab):
                self.legend.selection_set(str(self.highlighted))
                self.legend.see(str(self.highlighted))
        self._apply_highlight()

    def _pick_curve(self, event) -> 'None':
        index = event.artist.get_gid()
        if index == "pareto" and len(event.ind):
            self._select_pareto(int(event.ind[0]))
        elif isinstance(index, int):
            self._select_curve(index)

    def _legend_select(self, _=None) -> 'None':
        selected = self.legend.selection()
        if selected:
            chosen = selected[0]
            if chosen.startswith("front_"):
                self._select_pareto(int(chosen[6:]))
            elif chosen == "front":
                return
            elif chosen.startswith("opt_"):
                item = self.optimization_history[int(chosen[4:])]
                params = ", ".join(f"{key}={value:.6g}" for key, value in item["parameters"].items())
                detail = "不可行" if item["value"] is None else f"目标={item['value']:.6g}"
                if item.get("band"):
                    detail += f"；频段 {item['band'][0]:.5g}–{item['band'][1]:.5g} GHz"
                if item.get("point_frequency") is not None:
                    detail += f"；频点 {item['point_frequency']:.5g} GHz"
                index_text = (f"点 {item['evaluation']} · 第 {item['generation']} 代"
                              if "evaluation" in item else f"第 {item['generation']} 代")
                self.selected_values.set(f"{index_text} · {detail}\n{params}")
            else:
                index = int(chosen)
                if self.highlighted != index:
                    self._select_curve(index)

    def export_images(self) -> 'None':
        if not self.pages[self.active_page].get("result"):
            messagebox.showinfo("请先计算", "当前页还没有计算结果。", parent=self)
            return
        folder = filedialog.askdirectory(parent=self, title="选择图片保存文件夹")
        if not folder:
            return
        prefix = safe_name(self.pages[self.active_page]["name"])
        try:
            for index, suffix in ((0, "impedance"), (1, "phase")):
                figure = self.plots[index][0]
                curves = self.pages[self.active_page]["result"]["curves"]
                handles = [Line2D([0], [0], color=COLORS[i % len(COLORS)], lw=2, label=f"{i + 1}. {curve['label']}")
                           for i, curve in enumerate(curves)]
                legend = figure.legend(handles=handles, loc="upper left", bbox_to_anchor=(1.01, 0.98),
                                       facecolor=PLOT_BG, edgecolor="#aab8c7", labelcolor=PLOT_TEXT, fontsize=7)
                try:
                    figure.savefig(Path(folder) / f"{prefix}_Z11_{suffix}.png", dpi=180, facecolor=PLOT_BG, bbox_inches="tight")
                finally:
                    legend.remove()
            if self._pareto_visible:
                self.pareto_figure.savefig(Path(folder) / f"{prefix}_pareto.png",
                                           dpi=180, facecolor=PLOT_BG, bbox_inches="tight")
        except OSError as error:
            messagebox.showerror("图片导出失败", str(error), parent=self)
            return
        self.status.set(f"图已导出到 {folder}")

    def export_csv(self) -> 'None':
        result = self.pages[self.active_page].get("result")
        if not result:
            messagebox.showinfo("请先计算", "当前页还没有计算结果。", parent=self)
            return
        filename = filedialog.asksaveasfilename(parent=self, initialfile=f"{safe_name(self.pages[self.active_page]['name'])}_Z11.csv",
                                                defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not filename:
            return
        curves = result["curves"]
        header = ["Frequency_GHz"]
        for i, curve in enumerate(curves, 1):
            header.extend([f"Curve{i}_{curve['label']}_Re_Ohm", f"Curve{i}_Im_Ohm", f"Curve{i}_Abs_Ohm", f"Curve{i}_Phase_deg"])
        try:
            with open(filename, "w", newline="", encoding="utf-8-sig") as file:
                writer = csv.writer(file)
                writer.writerow(header)
                for index, frequency in enumerate(result["frequency"]):
                    row = [frequency]
                    for curve in curves:
                        value = curve["values"][index]
                        row.extend((value.real, value.imag, abs(value), math.degrees(math.atan2(value.imag, value.real))))
                    writer.writerow(row)
        except OSError as error:
            messagebox.showerror("CSV 导出失败", str(error), parent=self)
            return
        self.status.set(f"已导出 CSV：{filename}")

    def _page_changed(self, _=None) -> 'None':
        if not self._ready:
            return
        if self._opt_thread is not None and self._opt_thread.is_alive():
            self._opt_cancel.set()
        selected_tab = self.page_tabs.select()
        if not selected_tab:
            return
        index = self.page_tabs.index(selected_tab)
        if index == self.active_page:
            return
        if 0 <= self.active_page < len(self.pages):
            self._save_page_state()
        self.active_page = index
        page = self.pages[index]
        self.document = page["document"]
        self.history, self.future = page["history"], page["future"]
        self._snapshot = copy.deepcopy(self.document)
        self.selection.clear()
        self.selected_wires.clear()
        self.wire_start = None
        self.wire_path = []
        self.wire_drag = None
        self.selected_wire_segment = None
        self.copy_source = None
        self.mode = "select"
        self.canvas.configure(cursor="arrow")
        self._dots_dirty = True
        self._loading_page = True
        self.start_var.set(page["start"])
        self.stop_var.set(page["stop"])
        self.count_var.set(page["count"])
        for row in self.sweep_rows:
            row["frame"].destroy()
        self.sweep_rows.clear()
        for setting in page["sweeps"]:
            self.add_sweep(setting)
        self._load_optimization_settings(page.get("optimization", default_optimization_settings()))
        self.optimization_history = page.get("optimization_history", [])
        self._loading_page = False
        self.opt_summary.set(page["optimization_result"]["reason"] if page.get("optimization_result") else
                             "添加参数和限制条件，然后开始优化.")
        self.opt_apply_button.configure(state="disabled")
        self._update_inspector()
        self._refresh_parameter_choices()
        self.zoom = float(page.get("zoom", 1.0))
        edge = CANVAS_LIMIT * self.zoom
        self.canvas.configure(scrollregion=(-edge, -edge, edge, edge))
        self._restore_canvas_view(page.get("view"))
        self.redraw()
        self.highlighted = None
        self._plot_results()
        self._refresh_title()
        self.status.set(f"已切换到 {page['name']}。")

    def add_page(self) -> 'None':
        if len(self.pages) >= 30:
            messagebox.showerror("页面数量上限", "一个工程最多支持 30 页。", parent=self)
            return
        self._save_page_state()
        name = f"页面 {len(self.pages) + 1}"
        while any(p["name"] == name for p in self.pages):
            name += "*"
        page = new_page(name)
        self.pages.append(page)
        self.page_tabs.add(ttk.Frame(self.page_tabs), text=name)
        self.dirty = True
        self.page_tabs.select(len(self.pages) - 1)

    def rename_page(self) -> 'None':
        page = self.pages[self.active_page]
        name = simpledialog.askstring("重命名页面", "新页面名称：", initialvalue=page["name"], parent=self)
        if name is None:
            return
        name = name.strip()
        if not name or any(p is not page and p["name"] == name for p in self.pages):
            messagebox.showerror("名称无效", "页面名称不能为空，也不能与已有页面重复。", parent=self)
            return
        page["name"] = name
        self.page_tabs.tab(self.active_page, text=name)
        self.dirty = True
        self._refresh_title()

    def delete_page(self) -> 'None':
        if len(self.pages) == 1:
            messagebox.showinfo("保留一页", "工程至少保留一个页面。", parent=self)
            return
        page = self.pages[self.active_page]
        if not messagebox.askyesno("删除页面", f"确定删除“{page['name']}”及其电路吗？", parent=self):
            return
        index = self.active_page
        self.page_tabs.forget(index)
        self.pages.pop(index)
        self.active_page = -1
        self.dirty = True
        self.page_tabs.select(min(index, len(self.pages) - 1))
        self._page_changed()

    def _refresh_title(self) -> 'None':
        if not hasattr(self, "pages"):
            return
        project = self.project_path.name if self.project_path else "未命名工程"
        page = self.pages[self.active_page]["name"]
        self.title(f"Z11 Studio — {project}  /  {page}{'  *' if getattr(self, 'dirty', False) else ''}")

    def new_document(self) -> 'None':
        if not self._confirm_discard():
            return
        self.pages = [new_page("页面 1")]
        self.project_path = None
        self.dirty = False
        for tab in self.page_tabs.tabs():
            self.page_tabs.forget(tab)
        self.page_tabs.add(ttk.Frame(self.page_tabs), text="页面 1")
        self.active_page = -1
        self.page_tabs.select(0)
        self._page_changed()

    def save_as_project(self) -> 'None':
        previous = self.project_path
        self.project_path = None
        self.save_document()
        if self.project_path is None:
            self.project_path = previous

    def save_document(self) -> 'None':
        self._save_page_state()
        path = self.project_path
        if path is None:
            filename = filedialog.asksaveasfilename(parent=self, initialdir=str(self.state_dir), defaultextension=".z11project.json", initialfile="my_z11_project.z11project.json",
                                                    filetypes=[("Z11 工程", "*.z11project.json"), ("JSON", "*.json")])
            if not filename:
                return
            path = Path(filename)
        pages = []
        for page in self.pages:
            doc = copy.deepcopy(page["document"])
            try:
                validate(doc)
            except (ValueError, TypeError, KeyError) as error:
                messagebox.showerror("工程保存失败", f"{page['name']}：{error}", parent=self)
                return
            for part in doc["parts"]:
                if part["kind"] in ("L", "C") and math.isinf(float(part["q"])):
                    part["q"] = "inf"
            pages.append({key: page[key] for key in ("name", "start", "stop", "count", "sweeps", "optimization",
                                                  "optimization_result", "optimization_history", "view", "zoom")} | {"document": doc})
        try:
            path.write_text(json.dumps({"format": "z11-studio-v2", "pages": pages}, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        except (OSError, ValueError) as error:
            messagebox.showerror("工程保存失败", str(error), parent=self)
            return
        self.project_path = path
        self.dirty = False
        self._refresh_title()
        self.status.set(f"已保存 {len(self.pages)} 个页面到 {path}")

    def open_document(self) -> 'None':
        if not self._confirm_discard():
            return
        filename = filedialog.askopenfilename(parent=self, initialdir=str(self.project_path.parent if self.project_path else self.state_dir), filetypes=[("Z11 工程", "*.z11project.json"), ("JSON", "*.json")])
        if not filename:
            return
        try:
            raw = json.loads(Path(filename).read_text(encoding="utf-8"))
            if "parts" in raw:
                raw = {"pages": [{"name": "页面 1", "document": raw}]}
            raw_pages = raw["pages"]
            if not isinstance(raw_pages, list) or not 1 <= len(raw_pages) <= 30:
                raise ValueError("工程页面数量无效（允许 1–30 页）。")
            loaded = []
            names = set()
            for item in raw_pages:
                name = str(item["name"]).strip()
                if not name or name in names:
                    raise ValueError("工程中的页面名称为空或重复。")
                names.add(name)
                doc = item["document"]
                validate(doc)
                for part in doc["parts"]:
                    part["value"] = float(part["value"])
                    part["q"] = float(part.get("q", 0))
                page = new_page(name, doc)
                for key in ("start", "stop", "count"):
                    page[key] = str(item.get(key, page[key]))
                view = item.get("view")
                if view is not None:
                    if (not isinstance(view, list) or len(view) != 2 or
                            not all(isinstance(v, (int, float)) and math.isfinite(v) for v in view)):
                        raise ValueError(f"{name} 的画布视角无效。")
                    page["view"] = view
                zoom = float(item.get("zoom", 1.0))
                if not math.isfinite(zoom) or not .12 <= zoom <= 4.0:
                    raise ValueError(f"{name} 的画布缩放值无效。")
                page["zoom"] = zoom
                page["sweeps"] = item.get("sweeps", [])
                if not isinstance(page["sweeps"], list) or not all(isinstance(row, dict) for row in page["sweeps"]):
                    raise ValueError(f"{name} 的 Sweep 设置无效。")
                # Earlier project files paired every sweep row. Preserve that meaning on load.
                if page["sweeps"] and all("group" not in row for row in page["sweeps"]):
                    old_group = "联动 1" if len(page["sweeps"]) > 1 else "独立"
                    for row in page["sweeps"]:
                        row["group"] = old_group
                optimization = item.get("optimization", default_optimization_settings())
                if (not isinstance(optimization, dict) or
                        not isinstance(optimization.get("variables", []), list) or
                        not isinstance(optimization.get("constraints", []), list) or
                        not all(isinstance(row, dict) and isinstance(row.get("parameter"), str)
                                for row in optimization.get("variables", [])) or
                        not all(isinstance(row, dict) and isinstance(row.get("metric"), str)
                                for row in optimization.get("constraints", []))):
                    raise ValueError(f"{name} 的自动优化设置无效。")
                page["optimization"] = optimization
                outcome = item.get("optimization_result")
                if outcome is not None and (not isinstance(outcome, dict) or not isinstance(outcome.get("reason"), str)):
                    raise ValueError(f"{name} 的优化结果无效。")
                page["optimization_result"] = outcome
                records = item.get("optimization_history", [])
                if not isinstance(records, list) or not all(
                        isinstance(row, dict) and isinstance(row.get("generation"), int) and
                        (row.get("value") is None or isinstance(row.get("value"), (int, float))) and
                        isinstance(row.get("parameters"), dict) and
                        all(isinstance(key, str) and isinstance(value, (int, float)) and math.isfinite(value)
                            for key, value in row["parameters"].items())
                        for row in records):
                    raise ValueError(f"{name} 的优化记录无效。")
                page["optimization_history"] = records
                loaded.append(page)
        except (OSError, ValueError, TypeError, KeyError) as error:
            messagebox.showerror("无法打开工程", str(error), parent=self)
            return
        self.pages = loaded
        self.project_path = Path(filename)
        self.dirty = False
        for tab in self.page_tabs.tabs():
            self.page_tabs.forget(tab)
        for page in loaded:
            self.page_tabs.add(ttk.Frame(self.page_tabs), text=page["name"])
        self.active_page = -1
        self.page_tabs.select(0)
        self._page_changed()
        self.status.set(f"已加载 {len(loaded)} 个页面；每页可独立计算。")

    def _refresh_templates(self) -> 'None':
        self.template_files = {}
        for path in self.library_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                name = str(data["name"])
                if name:
                    self.template_files[name] = path
            except (OSError, ValueError, KeyError):
                continue
        self.template_combo.configure(values=sorted(self.template_files, key=str.casefold))

    def save_structure(self) -> 'None':
        name = simpledialog.askstring("保存电路结构", "给当前页的电路结构命名：", initialvalue=self.pages[self.active_page]["name"], parent=self)
        if name is None:
            return
        name = name.strip()
        if not name:
            messagebox.showerror("名称无效", "结构名称不能为空。", parent=self)
            return
        path = self.template_files.get(name)
        if path and not messagebox.askyesno("覆盖结构？", f"结构“{name}”已存在，是否覆盖？", parent=self):
            return
        path = path or self.library_dir / f"{uuid4().hex}.json"
        document = copy.deepcopy(self.document)
        try:
            validate(document)
        except (ValueError, TypeError, KeyError) as error:
            messagebox.showerror("结构保存失败", str(error), parent=self)
            return
        for part in document["parts"]:
            if part["kind"] in ("L", "C") and math.isinf(float(part["q"])):
                part["q"] = "inf"
        try:
            path.write_text(json.dumps({"name": name, "document": document}, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        except (OSError, ValueError) as error:
            messagebox.showerror("结构保存失败", str(error), parent=self)
            return
        self._refresh_templates()
        self.template_name.set(name)
        self.status.set(f"已将当前电路保存为结构“{name}”，可通过下拉框重新加载。")

    def load_structure(self) -> 'None':
        name = self.template_name.get()
        path = self.template_files.get(name)
        if not path:
            messagebox.showinfo("请选择结构", "先从下拉框选择一个已保存的结构。", parent=self)
            return
        try:
            document = json.loads(path.read_text(encoding="utf-8"))["document"]
            validate(document)
            for part in document["parts"]:
                part["value"] = float(part["value"])
                part["q"] = float(part.get("q", 0))
        except (OSError, ValueError, TypeError, KeyError) as error:
            messagebox.showerror("结构加载失败", str(error), parent=self)
            return
        self.document = document
        self.pages[self.active_page]["document"] = document
        self.selection.clear()
        self.selected_wires.clear()
        for row in self.sweep_rows:
            row["frame"].destroy()
        self.sweep_rows.clear()
        self._snapshot = copy.deepcopy(document)
        self.history.clear()
        self.future.clear()
        self._dots_dirty = True
        self._update_inspector()
        self._refresh_parameter_choices()
        self._sweep_changed()
        self.redraw()
        self.status.set(f"已将“{name}”加载到当前页；旧曲线保留，点击计算更新。")


if __name__ == "__main__":
    Studio().mainloop()
