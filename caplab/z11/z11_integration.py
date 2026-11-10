"""Per-Maestro launch context and asynchronous native schematic export."""

import argparse
import copy
import json
import math
import os
import time
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from tkinter import messagebox, ttk

from studio_v2 import Studio
from z11_schematic import build_plan, serialize_skill


def _atom(value: 'str'):
    if value == "nil":
        return None
    return json.loads(value)


class MaestroSource(namedtuple('MaestroSourceBase','library cell view work_dir library_path broker_dir')):
    __slots__=()

    @classmethod
    def read(cls, filename):
        lines = Path(filename).read_text(encoding="utf-8").splitlines()
        if len(lines) != 2 or lines[0] != "Z11_CONTEXT_1":
            raise ValueError("Maestro 启动上下文无效。")
        fields = lines[1].split("\t")
        if len(fields) != 7 or fields[0] != "C":
            raise ValueError("Maestro 启动身份字段无效。")
        values = [_atom(field) for field in fields[1:]]
        if not all(isinstance(v, str) and v for v in values):
            raise ValueError("Maestro 启动身份不完整。")
        return cls(*values)


class MaestroStudio(Studio):
    def __init__(self, source=None):
        self.maestro_source = source
        self._export_pending = None
        self._export_started = 0.0
        super().__init__()
        self.protocol("WM_DELETE_WINDOW", self._close_bound_window)

    def _build_ui(self):
        super()._build_ui()
        header = self.actionbar
        self.schematic_button = ttk.Button(
            header, text="创建 schematic", command=self.create_schematic,
            style="TButton")
        self.schematic_button.pack(side="right", padx=4)
        source_bar = ttk.Frame(self, padding=(12, 0, 12, 5))
        source_bar.pack(fill="x", before=self.winfo_children()[1])
        source = self.maestro_source
        identity = (f"Maestro：{source.library} / {source.cell} / {source.view}"
                    f"    ·    schematic 创建到 {source.library}"
                    if source else "独立模式 · 从 Maestro 的 Plug-in 菜单打开后可创建 schematic")
        ttk.Label(source_bar, text=identity).pack(side="left")
        if source is None:
            self.schematic_button.state(["disabled"])

    def _refresh_title(self):
        super()._refresh_title()
        if self.maestro_source:
            self.title(f"{self.title()}  ·  {self.maestro_source.library}")

    def create_schematic(self):
        if self.maestro_source is None or self._export_pending is not None:
            return
        source = self.maestro_source
        broker = Path(source.broker_dir)
        if not broker.is_dir() or not (broker / "context.tsv").exists():
            messagebox.showerror("无法导出", "来源 Virtuoso 会话已不可用，请从 Maestro 菜单重新打开。", parent=self)
            return
        try:
            # Export the canvas document; curve previews never replace this snapshot.
            document = copy.deepcopy(self.document)
            start, stop = float(self.start_var.get()), float(self.stop_var.get())
            reference = math.sqrt(start * stop) if start > 0 and stop > 0 else 1.0
            plan = build_plan(document, reference_frequency_ghz=reference)
            cell = f"z11_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}"
            payload = serialize_skill([cell, plan]) + "\n"
            request = broker / "request.data"
            result = broker / "result.tsv"
            if request.exists():
                raise ValueError("上一次导出仍在处理。")
            if result.exists():result.unlink()
            temp = broker / "request.tmp"
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, request)
        except (OSError, ValueError, TypeError, KeyError) as error:
            messagebox.showerror("创建 schematic 失败", str(error), parent=self)
            return
        self._export_pending = cell
        self._export_started = time.monotonic()
        self.schematic_button.state(["disabled"])
        self.status.set(f"正在 {source.library} 创建 {cell}/schematic…")
        self.after(250, self._poll_export)

    def _poll_export(self):
        if self._export_pending is None:
            return
        result = Path(self.maestro_source.broker_dir) / "result.tsv"
        if result.exists():
            try:
                text = result.read_text(encoding="utf-8")
                fields = text.splitlines()[0].split("\t")
                if fields[0] != "OK":
                    raise ValueError("\n".join(text.splitlines()[1:]) or "Virtuoso 创建失败，请查看 CIW。")
                if fields[1:4] != [self.maestro_source.library, self._export_pending, "schematic"]:
                    raise ValueError("导出结果与当前 Maestro 身份不一致。")
                action = "已创建并打开" if len(fields) > 4 and fields[4] == "opened" else "已创建"
                self.status.set(f"{action} {fields[1]}/{fields[2]}/schematic；器件、数值和连接已回读验证。")
            except (OSError, ValueError, IndexError) as error:
                self.status.set(f"创建 schematic 失败：{error}")
                messagebox.showerror("创建 schematic 失败", str(error), parent=self)
            self._export_pending = None
            self.schematic_button.state(["!disabled"])
            return
        if time.monotonic() - self._export_started > 30:
            self.status.set("等待来源 Virtuoso 完成 schematic；请关闭该会话中的模态对话框。")
        self.after(500, self._poll_export)

    def _close_bound_window(self):
        if not self._confirm_discard():
            return
        self.stop_optimization()
        if self.maestro_source:
            broker = Path(self.maestro_source.broker_dir)
            (broker / "closed").touch()
            # Cancel requests the source session has not consumed yet. A
            # schematic already being built may finish in its source library.
            if (broker / "request.data").exists():(broker / "request.data").unlink()
        self.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--maestro-context-file")
    args = parser.parse_args()
    source = MaestroSource.read(args.maestro_context_file) if args.maestro_context_file else None
    MaestroStudio(source).mainloop()


if __name__ == "__main__":
    main()
