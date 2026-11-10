"""Interactive single-port RLC circuit editor and Z11 plotter."""


import copy
import csv
import json
import math
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import matplotlib

matplotlib.use("TkAgg")
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

from network_model import GRID, blank_document, example_document, extract_nodes, route, solve, terminals, validate_document


INK = "#23344d"
ACCENT = "#156e9a"
SELECT = "#e0782d"
WIRE = "#267392"
BG = "#f8fbfd"
UI_FONT = "Microsoft YaHei UI" if sys.platform == "win32" else "WenQuanYi Micro Hei"
SYMBOL_FONT = "Segoe UI" if sys.platform == "win32" else "WenQuanYi Micro Hei"


def wheel_delta(event) -> 'int':
    """Normalize Windows/macOS wheel deltas and X11 Button 4/5 events."""
    button = getattr(event, "num", None)
    if button in (4, 5):
        return 120 if button == 4 else -120
    return getattr(event, "delta", 0)


def bind_mousewheel(widget, handler, add=None) -> 'None':
    widget.bind("<MouseWheel>", handler, add=add)
    if sys.platform != "win32":
        for sequence in ("<Button-4>", "<Button-5>"):
            widget.bind(sequence, handler, add=add)


def snap(value: 'float') -> 'int':
    return round(value / GRID) * GRID


def transform(part: 'dict', x: 'float', y: 'float') -> 'tuple[float, float]':
    direction = part["rotation"] % 4
    if direction == 0:
        return part["x"] + x, part["y"] + y
    if direction == 1:
        return part["x"] - y, part["y"] + x
    if direction == 2:
        return part["x"] - x, part["y"] - y
    return part["x"] + y, part["y"] - x


def draw_symbol(canvas: 'tk.Canvas', part: 'dict', *, selected: 'bool' = False, tags: 'tuple[str, ...]' = (), label: 'bool' = True) -> 'None':
    color = SELECT if selected else INK
    width = 2.5 if selected else 2
    kind = part["kind"]

    def line(points: 'list[tuple[float, float]]', **kwargs) -> 'None':
        coordinates = [v for point in points for v in transform(part, *point)]
        canvas.create_line(*coordinates, fill=color, width=width, capstyle="round", joinstyle="round", tags=tags, **kwargs)

    line([(-40, 0), (-24, 0)])
    line([(24, 0), (40, 0)])
    if kind == "R":
        line([(-24, 0), (-18, -9), (-12, 9), (-6, -9), (0, 9), (6, -9), (12, 9), (18, -9), (24, 0)])
    elif kind == "L":
        points = []
        for turn in range(4):
            for step in range(9):
                if turn and step == 0:
                    continue
                t = step / 8
                points.append((-24 + 12 * (turn + t), -12 * math.sin(math.pi * t)))
        line(points)
    elif kind == "C":
        line([(-24, 0), (-7, 0)])
        line([(7, 0), (24, 0)])
        line([(-7, -15), (-7, 15)])
        line([(7, -15), (7, 15)])
    else:
        x1, y1 = transform(part, -22, -22)
        x2, y2 = transform(part, 22, 22)
        canvas.create_oval(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2), outline=color, width=width, fill=canvas.cget("background"), tags=tags)
        for symbol, x in (("+", -10), ("−", 10)):
            px, py = transform(part, x, 0)
            canvas.create_text(px, py, text=symbol, fill=color, font=(SYMBOL_FONT, 11, "bold"), tags=tags)
    for x, y in terminals(part):
        canvas.create_oval(x - 3, y - 3, x + 3, y + 3, outline=color, fill="white", tags=tags)
    if label:
        unit = {"R": "Ω", "L": "pH", "C": "fF", "P": ""}[kind]
        caption = part["id"] if kind == "P" else f"{part['id']}  {part['value']:g}{unit}"
        if part["rotation"] % 2:
            tx, ty = transform(part, 0, -36)
            anchor = "w" if part["rotation"] == 1 else "e"
        else:
            tx, ty = transform(part, 0, 28)
            anchor = "n" if part["rotation"] == 0 else "s"
        canvas.create_text(tx, ty, text=caption, fill=color, anchor=anchor,
                           font=(SYMBOL_FONT, 9, "bold"), tags=tags)


def wire_contact(document: 'dict', point: 'tuple[int, int]') -> 'tuple[set[tuple[int, int]], set[int], bool]':
    """Unique outgoing directions, touched wires and whether a wire ends here."""
    from network_model import _on_segment, _segments
    segments, owners = _segments(document)
    rays: 'set[tuple[int, int]]' = set()
    touched: 'set[int]' = set()
    endpoint = False
    for index, (a, b) in enumerate(segments):
        if not _on_segment(point, a, b):
            continue
        touched.add(owners[index])
        wire = document["wires"][owners[index]]
        endpoint |= point == tuple(wire[0]) or point == tuple(wire[-1])
        for other in (a, b):
            dx, dy = other[0] - point[0], other[1] - point[1]
            if dx or dy:
                rays.add(((dx > 0) - (dx < 0), (dy > 0) - (dy < 0)))
    return rays, touched, endpoint


class EditorApp(tk.Tk):
    def __init__(self) -> 'None':
        super().__init__()
        self.title("Z11 Studio · 单端口 RLC 网络")
        self.geometry("1510x860")
        self.minsize(1120, 700)
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self.style.configure("TButton", padding=(8, 5))
        self.style.configure("Accent.TButton", foreground="white", background=ACCENT)
        self.style.map("Accent.TButton", background=[("active", "#0e597f")])
        self.document = example_document()
        self.path: 'Path | None' = None
        self.dirty = False
        self.history = []
        self.future = []
        self._snapshot = copy.deepcopy(self.document)
        self.mode = "select"
        self.wire_start: 'tuple[int, int] | None' = None
        self.cursor: 'tuple[int, int] | None' = None
        self.selection: 'set[str]' = set()
        self.selected_wires: 'set[int]' = set()
        self.drag: 'dict | None' = None
        self._dots_dirty = True
        self._dots_cache: 'list[tuple[int, int]]' = []
        self.box_start: 'tuple[int, int] | None' = None
        self.box_end: 'tuple[int, int] | None' = None
        self.data: 'tuple[np.ndarray, np.ndarray] | None' = None
        self._build_ui()
        self._bind_shortcuts()
        self.redraw()
        self.calculate()

    def _build_ui(self) -> 'None':
        self.configure(background="#eaf0f5")
        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(fill="x")
        for text, command, style in (
            ("新建", self.new_document, None), ("打开", self.open_document, None), ("保存", self.save_document, None),
            ("撤销", self.undo, None), ("重做", self.redo, None),
            ("选择  Esc", lambda: self.set_mode("select"), None), ("连线  W", lambda: self.set_mode("wire"), None),
            ("连接点  J", lambda: self.set_mode("junction"), None), ("旋转  R", self.rotate_selection, None),
            ("删除  Del", self.delete_selection, None), ("计算 Z11", self.calculate, "Accent.TButton"),
            ("导出两张图", self.export_images, None), ("导出 CSV", self.export_csv, None),
        ):
            ttk.Button(bar, text=text, command=command, style=style or "TButton").pack(side="left", padx=3)

        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10)
        sidebar = ttk.Frame(body, padding=(6, 8), width=190)
        editor = ttk.Frame(body, padding=(4, 8))
        right = ttk.Frame(body, padding=(7, 8), width=470)
        body.add(sidebar, weight=0)
        body.add(editor, weight=3)
        body.add(right, weight=2)
        self._build_palette(sidebar)
        self._build_canvas(editor)
        self._build_right(right)
        for variable in (self.start_var, self.stop_var, self.count_var):
            variable.trace_add("write", lambda *_: self._sweep_changed())
        self.status = tk.StringVar(value="就绪。拖入器件，按 W 连线，点击计算 Z11。")
        ttk.Label(self, textvariable=self.status, padding=(13, 7)).pack(fill="x")

    def _build_palette(self, parent: 'ttk.Frame') -> 'None':
        ttk.Label(parent, text="器件库", font=(UI_FONT, 13, "bold")).pack(anchor="w", pady=(0, 7))
        for kind, title in (("R", "电阻  Ω"), ("L", "电感  pH"), ("C", "电容  fF")):
            holder = ttk.Frame(parent)
            holder.pack(fill="x", pady=4)
            view = tk.Canvas(holder, width=162, height=62, background="white", highlightthickness=1, highlightbackground="#ccd8e2", cursor="hand2")
            view.pack(fill="x")
            sample = {"id": kind, "kind": kind, "x": 81, "y": 29, "rotation": 0, "value": 1.0, "q": 20.0}
            draw_symbol(view, sample, label=False)
            ttk.Label(holder, text=title).pack(anchor="w", pady=(2, 0))
            view.bind("<ButtonPress-1>", lambda event, k=kind: self._palette_press(k, event))
            view.bind("<B1-Motion>", lambda event, k=kind: self._palette_move(k, event))
            view.bind("<ButtonRelease-1>", lambda event, k=kind: self._palette_release(k, event))
        ttk.Separator(parent).pack(fill="x", pady=14)
        for instruction in (
            "拖器件到画布，或点击器件后再点击放置。",
            "W：点击起点、终点完成正交连线；Shift 改变拐弯方向。",
            "空白处拖动可框选；Shift 可追加选择。",
            "R 旋转，Delete 删除，Esc 退出当前工具。",
            "线端碰到另一根线即连接；纯交叉需按 J 加连接点。",
            "PORT1 始终保留，可移动和旋转。",
        ):
            ttk.Label(parent, text=instruction, wraplength=173, foreground="#53657a").pack(anchor="w", pady=5)

    def _build_canvas(self, parent: 'ttk.Frame') -> 'None':
        ttk.Label(parent, text="电路画布", font=(UI_FONT, 13, "bold")).pack(anchor="w", pady=(0, 7))
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(frame, background=BG, scrollregion=(0, 0, 1600, 1000), highlightthickness=1, highlightbackground="#c7d7e4", takefocus=1)
        sx = ttk.Scrollbar(frame, orient="horizontal", command=self.canvas.xview)
        sy = ttk.Scrollbar(frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=sx.set, yscrollcommand=sy.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns")
        sx.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        for x in range(0, 1601, GRID):
            self.canvas.create_line(x, 0, x, 1000, fill="#eaf1f6", tags="grid")
        for y in range(0, 1001, GRID):
            self.canvas.create_line(0, y, 1600, y, fill="#eaf1f6", tags="grid")
        self.canvas.bind("<ButtonPress-1>", self._canvas_press)
        self.canvas.bind("<B1-Motion>", self._canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._canvas_release)
        self.canvas.bind("<Motion>", self._canvas_motion)
        self.canvas.bind("<Double-Button-1>", self._canvas_double)
        bind_mousewheel(self.canvas, self._wheel)

    def _build_right(self, parent: 'ttk.Frame') -> 'None':
        ttk.Label(parent, text="属性与结果", font=(UI_FONT, 13, "bold")).pack(anchor="w", pady=(0, 7))
        details = ttk.LabelFrame(parent, text="选中器件", padding=9)
        details.pack(fill="x")
        self.part_name = tk.StringVar(value="请选择一个器件")
        ttk.Label(details, textvariable=self.part_name, font=(SYMBOL_FONT, 10, "bold")).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        self.value_var = tk.StringVar()
        self.q_var = tk.StringVar()
        self.unit_var = tk.StringVar(value="数值")
        ttk.Label(details, textvariable=self.unit_var).grid(row=1, column=0, sticky="w")
        self.value_entry = ttk.Entry(details, textvariable=self.value_var, width=12)
        self.value_entry.grid(row=1, column=1, padx=(5, 12))
        ttk.Label(details, text="Q").grid(row=1, column=2)
        self.q_entry = ttk.Entry(details, textvariable=self.q_var, width=10)
        self.q_entry.grid(row=1, column=3, padx=5)
        self.apply_button = ttk.Button(details, text="应用参数", command=self.apply_properties)
        self.apply_button.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        self.value_entry.bind("<Return>", lambda _: self.apply_properties())
        self.q_entry.bind("<Return>", lambda _: self.apply_properties())
        self._update_inspector()

        sweep = ttk.LabelFrame(parent, text="扫频（GHz）", padding=9)
        sweep.pack(fill="x", pady=(9, 0))
        self.start_var = tk.StringVar(value="0.1")
        self.stop_var = tk.StringVar(value="30")
        self.count_var = tk.StringVar(value="1001")
        for col, (caption, variable) in enumerate((("起始", self.start_var), ("终止", self.stop_var), ("点数", self.count_var))):
            ttk.Label(sweep, text=caption).grid(row=0, column=col, padx=4, sticky="w")
            ttk.Entry(sweep, textvariable=variable, width=12).grid(row=1, column=col, padx=4, pady=(4, 0), sticky="ew")
            sweep.columnconfigure(col, weight=1)

        self.tabs = ttk.Notebook(parent)
        self.tabs.pack(fill="both", expand=True, pady=(10, 0))
        self.plots = []
        for title in ("阻抗", "相位"):
            tab = ttk.Frame(self.tabs)
            self.tabs.add(tab, text=title)
            figure = Figure(figsize=(5, 4), dpi=100, constrained_layout=True)
            axis = figure.add_subplot(111)
            view = FigureCanvasTkAgg(figure, master=tab)
            view.get_tk_widget().pack(fill="both", expand=True)
            toolbar = NavigationToolbar2Tk(view, tab, pack_toolbar=False)
            toolbar.update()
            toolbar.pack(fill="x")
            self.plots.append((figure, axis, view))
        ttk.Label(parent, text="两张图可分别缩放；“导出两张图”会生成 PNG 文件。\nQ 按每个频点恒定处理；L/C 损耗为串联电阻。", wraplength=450, foreground="#53657a").pack(anchor="w", pady=(7, 0))

    def _bind_shortcuts(self) -> 'None':
        for sequence, action in (
            ("<KeyPress-w>", lambda: self.set_mode("wire")),
            ("<KeyPress-W>", lambda: self.set_mode("wire")),
            ("<KeyPress-j>", lambda: self.set_mode("junction")),
            ("<KeyPress-J>", lambda: self.set_mode("junction")),
            ("<KeyPress-r>", self.rotate_selection),
            ("<KeyPress-R>", self.rotate_selection),
            ("<Escape>", lambda: self.set_mode("select")),
            ("<Delete>", self.delete_selection),
            ("<Control-a>", self.select_all),
            ("<Control-s>", self.save_document),
            ("<Control-o>", self.open_document),
            ("<Control-n>", self.new_document),
            ("<Control-z>", self.undo),
            ("<Control-y>", self.redo),
        ):
            self.bind_all(sequence, lambda event, fn=action: self._shortcut(event, fn))

    def _shortcut(self, event: 'tk.Event', action) -> 'str | None':
        if isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Combobox, tk.Text, tk.Spinbox)) and event.keysym not in ("Escape",):
            return None
        action()
        return "break"

    def _wheel(self, event: 'tk.Event') -> 'None':
        direction = -1 if wheel_delta(event) > 0 else 1
        (self.canvas.xview_scroll if event.state & 0x0001 else self.canvas.yview_scroll)(direction * 3, "units")

    def _point(self, event: 'tk.Event') -> 'tuple[int, int]':
        return snap(self.canvas.canvasx(event.x)), snap(self.canvas.canvasy(event.y))

    def _pointer_on_canvas(self, event: 'tk.Event') -> 'tuple[int, int] | None':
        x = event.x_root - self.canvas.winfo_rootx()
        y = event.y_root - self.canvas.winfo_rooty()
        if not (0 <= x < self.canvas.winfo_width() and 0 <= y < self.canvas.winfo_height()):
            return None
        return snap(self.canvas.canvasx(x)), snap(self.canvas.canvasy(y))

    def _palette_press(self, kind: 'str', event: 'tk.Event') -> 'None':
        self._palette_dragged = False
        self._palette_kind = kind

    def _palette_move(self, kind: 'str', event: 'tk.Event') -> 'None':
        self._palette_dragged = True
        self.cursor = self._pointer_on_canvas(event)
        self.mode = f"place_{kind}"
        self.redraw()

    def _palette_release(self, kind: 'str', event: 'tk.Event') -> 'None':
        point = self._pointer_on_canvas(event)
        if getattr(self, "_palette_dragged", False) and point is not None:
            self._add_part(kind, point)
            self.set_mode("select")
        else:
            self.set_mode(f"place_{kind}")
        self.cursor = None
        self.redraw()

    def set_mode(self, mode: 'str') -> 'None':
        self.mode = mode
        self.wire_start = None
        self.cursor = None
        self.drag = None
        self.box_start = self.box_end = None
        self.canvas.configure(cursor="crosshair" if mode != "select" else "arrow")
        self.status.set({"select": "选择模式：拖动器件或在空白处框选。", "wire": "连线模式：依次点击起点、终点；Shift 可切换拐弯方向。", "junction": "连接点模式：点击两根线的交叉处。"}.get(mode, "放置器件：点击画布。"))
        self.redraw()

    def _add_part(self, kind: 'str', point: 'tuple[int, int]') -> 'None':
        number = 1
        used = {part["id"] for part in self.document["parts"]}
        while f"{kind}{number}" in used:
            number += 1
        name = f"{kind}{number}"
        part = {"id": name, "kind": kind, "x": point[0], "y": point[1], "rotation": 0,
                "value": {"R": 50.0, "L": 120.0, "C": 100.0}[kind], "q": 0.0 if kind == "R" else 20.0 if kind == "L" else 100.0}
        self.document["parts"].append(part)
        self.selection, self.selected_wires = {name}, set()
        self._update_inspector()
        self._changed()

    def _hit_part(self, point: 'tuple[int, int]') -> 'str | None':
        x, y = point
        for part in reversed(self.document["parts"]):
            dx, dy = x - part["x"], y - part["y"]
            if part["rotation"] % 2:
                dx, dy = dy, -dx
            if abs(dx) <= 38 and abs(dy) <= 19:
                return part["id"]
        return None

    def _hit_wire(self, point: 'tuple[int, int]') -> 'int | None':
        x, y = point
        for i in range(len(self.document["wires"]) - 1, -1, -1):
            points = self.document["wires"][i]
            for a, b in zip(points, points[1:]):
                if a[0] == b[0] and abs(x - a[0]) <= 7 and min(a[1], b[1]) - 5 <= y <= max(a[1], b[1]) + 5:
                    return i
                if a[1] == b[1] and abs(y - a[1]) <= 7 and min(a[0], b[0]) - 5 <= x <= max(a[0], b[0]) + 5:
                    return i
        return None

    def _canvas_press(self, event: 'tk.Event') -> 'None':
        self.canvas.focus_set()
        point = self._point(event)
        if self.mode.startswith("place_"):
            self._add_part(self.mode[-1], point)
            self.set_mode("select")
            return
        if self.mode == "wire":
            if self.wire_start is None:
                self.wire_start = point
            elif point != self.wire_start:
                self.document["wires"].append(route(self.wire_start, point, bool(event.state & 0x0001)))
                self.wire_start = None
                self._changed()
            self.redraw()
            return
        if self.mode == "junction":
            self._toggle_junction(point)
            return
        part_id = self._hit_part(point)
        wire_id = None if part_id else self._hit_wire(point)
        additive = bool(event.state & 0x0001)
        if part_id:
            if additive:
                self.selection.add(part_id)
            elif part_id not in self.selection:
                self.selection, self.selected_wires = {part_id}, set()
            self.drag = {"start": point,
                         "positions": {p["id"]: (p["x"], p["y"]) for p in self.document["parts"] if p["id"] in self.selection},
                         "wires": copy.deepcopy(self.document["wires"]), "delta": (0, 0)}
        elif wire_id is not None:
            if additive:
                self.selected_wires.add(wire_id)
            else:
                self.selected_wires, self.selection = {wire_id}, set()
        else:
            if not additive:
                self.selection.clear()
                self.selected_wires.clear()
            self.box_start = self.box_end = point
        self._update_inspector()
        self.redraw()

    def _canvas_drag(self, event: 'tk.Event') -> 'None':
        point = self._point(event)
        if self.drag:
            start = self.drag["start"]
            delta = (point[0] - start[0], point[1] - start[1])
            self.drag["delta"] = delta
            old_to_new = {}
            for part in self.document["parts"]:
                if part["id"] in self.drag["positions"]:
                    x, y = self.drag["positions"][part["id"]]
                    old_terminals = terminals({**part, "x": x, "y": y})
                    part["x"], part["y"] = x + delta[0], y + delta[1]
                    old_to_new.update(zip(old_terminals, terminals(part)))
            self.document["wires"] = self._follow_terminals(self.drag["wires"], old_to_new)
            self._dots_dirty = True
        elif self.box_start is not None:
            self.box_end = point
        else:
            self.cursor = point
        self.redraw()

    @staticmethod
    def _wire_intersects_box(points: 'list', x1: 'int', y1: 'int', x2: 'int', y2: 'int') -> 'bool':
        for a, b in zip(points, points[1:]):
            if a[0] == b[0] and x1 <= a[0] <= x2 and max(min(a[1], b[1]), y1) <= min(max(a[1], b[1]), y2):
                return True
            if a[1] == b[1] and y1 <= a[1] <= y2 and max(min(a[0], b[0]), x1) <= min(max(a[0], b[0]), x2):
                return True
        return False

    def _canvas_release(self, event: 'tk.Event') -> 'None':
        if self.drag:
            moved = self.drag["delta"] != (0, 0)
            self.drag = None
            if moved:
                self._changed()
        elif self.box_start is not None and self.box_end is not None:
            x1, x2 = sorted((self.box_start[0], self.box_end[0]))
            y1, y2 = sorted((self.box_start[1], self.box_end[1]))
            if x2 > x1 or y2 > y1:
                self.selection.update(p["id"] for p in self.document["parts"] if x1 <= p["x"] <= x2 and y1 <= p["y"] <= y2)
                for i, points in enumerate(self.document["wires"]):
                    if self._wire_intersects_box(points, x1, y1, x2, y2):
                        self.selected_wires.add(i)
            self.box_start = self.box_end = None
            self._update_inspector()
        self.redraw()

    def _canvas_motion(self, event: 'tk.Event') -> 'None':
        if self.mode != "select":
            self.cursor = self._point(event)
            self.redraw()

    def _canvas_double(self, event: 'tk.Event') -> 'None':
        if self.mode == "select" and self._hit_part(self._point(event)):
            self.value_entry.focus_set()
            self.value_entry.selection_range(0, "end")

    @staticmethod
    def _follow_terminals(wires: 'list', old_to_new: 'dict') -> 'list':
        updated = []
        for points in wires:
            old_a, old_b = tuple(points[0]), tuple(points[-1])
            new_a = old_to_new.get(old_a, old_a)
            new_b = old_to_new.get(old_b, old_b)
            if new_a == old_a and new_b == old_b:
                updated.append(copy.deepcopy(points))
            elif (new_a[0] - old_a[0], new_a[1] - old_a[1]) == (new_b[0] - old_b[0], new_b[1] - old_b[1]):
                dx, dy = new_a[0] - old_a[0], new_a[1] - old_a[1]
                updated.append([[x + dx, y + dy] for x, y in points])
            else:
                updated.append(route(new_a, new_b))
        return updated

    def _toggle_junction(self, point: 'tuple[int, int]') -> 'None':
        rays, touched, endpoint = wire_contact(self.document, point)
        if len(rays) < 3 or len(touched) < 2:
            self.status.set("连接点需要至少三条线段在同一点相遇。")
            return
        if endpoint:
            self.status.set("线端接到另一根线时已自动连接；J 仅切换两根线的纯交叉连接。")
            return
        entry = list(point)
        if entry in self.document["junctions"]:
            self.document["junctions"].remove(entry)
        else:
            self.document["junctions"].append(entry)
        self._changed()
        self.redraw()

    def redraw(self) -> 'None':
        if not hasattr(self, "canvas"):
            return
        c = self.canvas
        c.delete("scene")
        for i, points in enumerate(self.document["wires"]):
            color = SELECT if i in self.selected_wires else WIRE
            c.create_line(*[coordinate for point in points for coordinate in point], fill=color, width=3 if i in self.selected_wires else 2, joinstyle="round", capstyle="round", tags="scene")
        self._draw_junction_dots()
        for part in self.document["parts"]:
            draw_symbol(c, part, selected=part["id"] in self.selection, tags=("scene",))
        if self.wire_start and self.cursor:
            points = route(self.wire_start, self.cursor)
            c.create_line(*[coordinate for point in points for coordinate in point], fill=SELECT, width=2, dash=(5, 4), tags="scene")
            x, y = self.wire_start
            c.create_oval(x - 4, y - 4, x + 4, y + 4, fill=SELECT, outline=SELECT, tags="scene")
        if self.mode.startswith("place_") and self.cursor:
            kind = self.mode[-1]
            sample = {"id": kind, "kind": kind, "x": self.cursor[0], "y": self.cursor[1], "rotation": 0, "value": 0.0, "q": 0.0}
            draw_symbol(c, sample, selected=True, tags=("scene",), label=False)
        if self.box_start and self.box_end:
            c.create_rectangle(*self.box_start, *self.box_end, outline=SELECT, width=2, dash=(5, 4), fill="", tags="scene")

    def _draw_junction_dots(self) -> 'None':
        if self._dots_dirty:
            candidates = {tuple(point) for point in self.document.get("junctions", [])}
            for wire in self.document["wires"]:
                candidates.update(tuple(point) for point in wire)
            explicit = {tuple(point) for point in self.document.get("junctions", [])}
            dots = []
            for point in candidates:
                rays, touched, endpoint = wire_contact(self.document, point)
                if len(rays) >= 3 and (point in explicit or endpoint or len(touched) == 1):
                    dots.append(point)
            self._dots_cache = dots
            self._dots_dirty = False
        for x, y in self._dots_cache:
            self.canvas.create_oval(x - 4, y - 4, x + 4, y + 4,
                                    fill=WIRE, outline=WIRE, tags="scene")

    def _update_inspector(self) -> 'None':
        if not hasattr(self, "part_name"):
            return
        selected = [p for p in self.document["parts"] if p["id"] in self.selection]
        if len(selected) != 1:
            self.part_name.set("请选择一个器件" if not selected else f"已选 {len(selected)} 个器件")
            self.value_var.set("")
            self.q_var.set("")
            self.value_entry.configure(state="disabled")
            self.q_entry.configure(state="disabled")
            self.apply_button.configure(state="disabled")
            return
        part = selected[0]
        self.part_name.set(f"{part['id']} · { {'R':'电阻','L':'电感','C':'电容','P':'端口'}[part['kind']] }")
        self.unit_var.set({"R": "阻值 Ω", "L": "电感 pH", "C": "电容 fF", "P": "端口"}[part["kind"]])
        self.value_var.set("" if part["kind"] == "P" else f"{part['value']:g}")
        self.q_var.set("" if part["kind"] in ("P", "R") else f"{part['q']:g}")
        self.value_entry.configure(state="disabled" if part["kind"] == "P" else "normal")
        self.q_entry.configure(state="normal" if part["kind"] in ("L", "C") else "disabled")
        self.apply_button.configure(state="disabled" if part["kind"] == "P" else "normal")

    def apply_properties(self) -> 'None':
        selected = [p for p in self.document["parts"] if p["id"] in self.selection]
        if len(selected) != 1 or selected[0]["kind"] == "P":
            return
        part = selected[0]
        try:
            value = float(self.value_var.get())
            if not math.isfinite(value) or value <= 0:
                raise ValueError("器件数值必须为正数。")
            quality = 0.0 if part["kind"] == "R" else float(self.q_var.get())
            if part["kind"] != "R" and (math.isnan(quality) or quality <= 0):
                raise ValueError("Q 必须为正数；inf 表示理想器件。")
        except ValueError as error:
            messagebox.showerror("参数无效", str(error), parent=self)
            return
        part["value"], part["q"] = value, quality
        self._changed()
        self._update_inspector()
        self.redraw()

    def rotate_selection(self) -> 'None':
        selected = [p for p in self.document["parts"] if p["id"] in self.selection]
        if not selected:
            return
        old_to_new = {}
        for part in selected:
            old = terminals(part)
            part["rotation"] = (part["rotation"] + 1) % 4
            old_to_new.update(zip(old, terminals(part)))
        self.document["wires"] = self._follow_terminals(self.document["wires"], old_to_new)
        self._changed()
        self.redraw()

    def delete_selection(self) -> 'None':
        removable = {name for name in self.selection if name != "PORT1"}
        if not removable and not self.selected_wires:
            self.status.set("PORT1 不能删除，可拖动或旋转。")
            return
        self.document["parts"] = [p for p in self.document["parts"] if p["id"] not in removable]
        self.document["wires"] = [w for i, w in enumerate(self.document["wires"]) if i not in self.selected_wires]
        self.selection.clear()
        self.selected_wires.clear()
        self._update_inspector()
        self._changed()
        self.redraw()

    def select_all(self) -> 'None':
        self.selection = {p["id"] for p in self.document["parts"]}
        self.selected_wires = set(range(len(self.document["wires"])))
        self._update_inspector()
        self.redraw()

    def _invalidate_results(self) -> 'None':
        self.data = None
        for _, axis, view in self.plots:
            axis.clear()
            axis.set_title("Calculate Z11 to update")
            view.draw_idle()

    def _sweep_changed(self) -> 'None':
        self._invalidate_results()
        self.status.set("扫频范围已更改。点击“计算 Z11”更新结果。")

    def undo(self) -> 'None':
        if not self.history:
            return
        self.future.append(copy.deepcopy(self.document))
        self.document = self.history.pop()
        self._restore_history()

    def redo(self) -> 'None':
        if not self.future:
            return
        self.history.append(copy.deepcopy(self.document))
        self.document = self.future.pop()
        self._restore_history()

    def _restore_history(self) -> 'None':
        self._dots_dirty = True
        self._snapshot = copy.deepcopy(self.document)
        self.selection.clear()
        self.selected_wires.clear()
        self._update_inspector()
        self._invalidate_results()
        self.dirty = True
        self._refresh_title()
        self.redraw()
        self.status.set("已恢复电路。点击“计算 Z11”更新结果。")

    def _changed(self) -> 'None':
        self._dots_dirty = True
        self.history.append(copy.deepcopy(self._snapshot))
        self.history = self.history[-40:]
        self.future.clear()
        self._snapshot = copy.deepcopy(self.document)
        self.dirty = True
        self._invalidate_results()
        self.status.set("电路已修改。点击“计算 Z11”更新两张图。")
        self._refresh_title()

    def _refresh_title(self) -> 'None':
        name = self.path.name if self.path else "未命名电路"
        self.title(f"Z11 Studio · {name}{' *' if self.dirty else ''}")

    def calculate(self) -> 'None':
        try:
            start, stop = float(self.start_var.get()), float(self.stop_var.get())
            count = int(self.count_var.get())
            if not math.isfinite(start) or not math.isfinite(stop) or not 0 < start < stop:
                raise ValueError("频率范围须满足 0 < 起始 < 终止。")
            if not 2 <= count <= 20001:
                raise ValueError("扫频点数须在 2 到 20001 之间。")
            frequencies = np.linspace(start, stop, count)
            values, ignored = solve(self.document, frequencies)
        except (ValueError, TypeError, KeyError) as error:
            messagebox.showerror("无法计算", str(error), parent=self)
            return
        self.data = frequencies, values
        fig, ax, view = self.plots[0]
        ax.clear()
        ax.plot(frequencies, values.real, color="#1765a3", label="Re(Z11)", linewidth=1.7)
        ax.plot(frequencies, values.imag, color="#d45526", label="Im(Z11)", linewidth=1.7)
        ax.plot(frequencies, np.abs(values), color="#43845b", label="|Z11|", linestyle="--", linewidth=1.5)
        ax.set(xlabel="Frequency (GHz)", ylabel="Impedance (Ω)", title="PORT1 · Z11")
        ax.grid(alpha=0.25)
        ax.legend(loc="best")
        view.draw_idle()
        fig, ax, view = self.plots[1]
        ax.clear()
        ax.plot(frequencies, np.angle(values, deg=True), color="#714bb5", linewidth=1.7)
        ax.set(xlabel="Frequency (GHz)", ylabel="Phase (degrees)", title="PORT1 · Z11 phase", ylim=(-185, 185))
        ax.grid(alpha=0.25)
        view.draw_idle()
        self.status.set(f"计算完成：{count} 点，{start:g}–{stop:g} GHz。" + (f"另有 {ignored} 个未接入端口的器件已忽略。" if ignored else ""))

    def export_images(self) -> 'None':
        if self.data is None:
            messagebox.showinfo("请先计算", "请先点击“计算 Z11”。", parent=self)
            return
        folder = filedialog.askdirectory(parent=self, title="选择两张图的保存文件夹")
        if not folder:
            return
        target = Path(folder)
        try:
            self.plots[0][0].savefig(target / "Z11_impedance.png", dpi=180)
            self.plots[1][0].savefig(target / "Z11_phase.png", dpi=180)
        except OSError as error:
            messagebox.showerror("保存失败", str(error), parent=self)
            return
        self.status.set(f"已保存 Z11_impedance.png 和 Z11_phase.png 到 {folder}")

    def export_csv(self) -> 'None':
        if self.data is None:
            messagebox.showinfo("请先计算", "请先点击“计算 Z11”。", parent=self)
            return
        name = filedialog.asksaveasfilename(parent=self, initialfile="Z11.csv", defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not name:
            return
        frequency, value = self.data
        try:
            with open(name, "w", newline="", encoding="utf-8-sig") as file:
                writer = csv.writer(file)
                writer.writerow(("Frequency_GHz", "Re_Z11_Ohm", "Im_Z11_Ohm", "Abs_Z11_Ohm", "Phase_deg"))
                writer.writerows(zip(frequency, value.real, value.imag, np.abs(value), np.angle(value, deg=True)))
        except OSError as error:
            messagebox.showerror("导出失败", str(error), parent=self)
            return
        self.status.set(f"已导出 {name}")

    def _confirm_discard(self) -> 'bool':
        return not self.dirty or messagebox.askyesno("放弃未保存的修改？", "当前电路有未保存的修改。继续将丢弃这些修改。", parent=self)

    def new_document(self) -> 'None':
        if not self._confirm_discard():
            return
        self.document = blank_document()
        self._dots_dirty = True
        self.path, self.dirty = None, False
        self.history.clear()
        self.future.clear()
        self._snapshot = copy.deepcopy(self.document)
        self._invalidate_results()
        self.selection.clear()
        self.selected_wires.clear()
        self.set_mode("select")
        self._update_inspector()
        self._refresh_title()

    def save_document(self) -> 'None':
        path = self.path
        if path is None:
            filename = filedialog.asksaveasfilename(parent=self, defaultextension=".z11.json", initialfile="my_network.z11.json", filetypes=[("Z11 电路", "*.z11.json"), ("JSON", "*.json")])
            if not filename:
                return
            path = Path(filename)
        data = copy.deepcopy(self.document)
        for part in data["parts"]:
            if part["kind"] in ("L", "C") and math.isinf(part["q"]):
                part["q"] = "inf"
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as error:
            messagebox.showerror("保存失败", str(error), parent=self)
            return
        self.path, self.dirty = path, False
        self._refresh_title()
        self.status.set(f"已保存 {path}")

    def open_document(self) -> 'None':
        if not self._confirm_discard():
            return
        filename = filedialog.askopenfilename(parent=self, filetypes=[("Z11 电路", "*.z11.json"), ("JSON", "*.json")])
        if not filename:
            return
        try:
            data = json.loads(Path(filename).read_text(encoding="utf-8"))
            validate_document(data)
            for part in data["parts"]:
                if part["kind"] in ("L", "C"):
                    part["q"] = float(part["q"])
                part["value"] = float(part["value"])
        except (OSError, ValueError, TypeError, KeyError) as error:
            messagebox.showerror("无法打开电路", str(error), parent=self)
            return
        self.document, self.path, self.dirty = data, Path(filename), False
        self._dots_dirty = True
        self.history.clear()
        self.future.clear()
        self._snapshot = copy.deepcopy(self.document)
        self._invalidate_results()
        self.selection.clear()
        self.selected_wires.clear()
        self.set_mode("select")
        self._update_inspector()
        self._refresh_title()
        self.status.set(f"已打开 {filename}；点击“计算 Z11”刷新结果。")


if __name__ == "__main__":
    EditorApp().mainloop()
