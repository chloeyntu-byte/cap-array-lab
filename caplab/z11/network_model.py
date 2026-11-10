"""Wire graph and AC nodal solver for a single-port RLC drawing."""


import math
from collections import defaultdict, deque

import numpy as np


GRID = 20


def terminals(part: 'dict') -> 'tuple[tuple[int, int], tuple[int, int]]':
    x, y = int(part["x"]), int(part["y"])
    direction = part["rotation"] % 4
    if direction == 0:
        return ((x - 40, y), (x + 40, y))
    if direction == 1:
        return ((x, y - 40), (x, y + 40))
    if direction == 2:
        return ((x + 40, y), (x - 40, y))
    return ((x, y + 40), (x, y - 40))


def route(a: 'tuple[int, int]', b: 'tuple[int, int]', vertical_first: 'bool' = False) -> 'list[list[int]]':
    if a[0] == b[0] or a[1] == b[1]:
        return [list(a), list(b)]
    bend = (a[0], b[1]) if vertical_first else (b[0], a[1])
    return [list(a), list(bend), list(b)]


def example_document() -> 'dict':
    parts = [
        {"id": "L14", "kind": "L", "x": 120, "y": 180, "rotation": 1, "value": 120.0, "q": 20.0},
        {"id": "C4", "kind": "C", "x": 240, "y": 180, "rotation": 1, "value": 100.0, "q": 100.0},
        {"id": "L12", "kind": "L", "x": 120, "y": 380, "rotation": 1, "value": 120.0, "q": 20.0},
        {"id": "C2", "kind": "C", "x": 240, "y": 380, "rotation": 1, "value": 100.0, "q": 100.0},
        {"id": "C3", "kind": "C", "x": 360, "y": 280, "rotation": 1, "value": 100.0, "q": 100.0},
        {"id": "PORT1", "kind": "P", "x": 500, "y": 280, "rotation": 1, "value": 0.0, "q": 0.0},
    ]
    segments = [
        [[120, 100], [500, 100]], [[120, 460], [500, 460]],
        [[120, 100], [120, 140]], [[240, 100], [240, 140]],
        [[120, 220], [120, 280]], [[240, 220], [240, 280]],
        [[120, 280], [240, 280]],
        [[120, 280], [120, 340]], [[240, 280], [240, 340]],
        [[120, 420], [120, 460]], [[240, 420], [240, 460]],
        [[360, 100], [360, 240]], [[360, 320], [360, 460]],
        [[500, 100], [500, 240]], [[500, 320], [500, 460]],
    ]
    return {"parts": parts, "wires": segments, "junctions": []}


def blank_document() -> 'dict':
    return {"parts": [{"id": "PORT1", "kind": "P", "x": 400, "y": 280, "rotation": 1, "value": 0.0, "q": 0.0}], "wires": [], "junctions": []}


def _on_segment(point: 'tuple[int, int]', a: 'tuple[int, int]', b: 'tuple[int, int]') -> 'bool':
    x, y = point
    return (a[0] == b[0] == x and min(a[1], b[1]) <= y <= max(a[1], b[1])) or (
        a[1] == b[1] == y and min(a[0], b[0]) <= x <= max(a[0], b[0])
    )


def _segments(document: 'dict') -> 'tuple[list[tuple[tuple[int, int], tuple[int, int]]], list[int]]':
    segments = []
    owners = []
    for wire_index, points in enumerate(document["wires"]):
        for a, b in zip(points, points[1:]):
            a, b = tuple(a), tuple(b)
            if a != b:
                if a[0] != b[0] and a[1] != b[1]:
                    raise ValueError("连线必须水平或垂直。")
                segments.append((a, b))
                owners.append(wire_index)
    return segments, owners


class _Union:
    def __init__(self, size: 'int'):
        self.parent = list(range(size))

    def find(self, x: 'int') -> 'int':
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def merge(self, a: 'int', b: 'int') -> 'None':
        self.parent[self.find(a)] = self.find(b)


def extract_nodes(document: 'dict') -> 'tuple[dict[tuple[str, int], int], int]':
    parts = document["parts"]
    segments, owners = _segments(document)
    union = _Union(2 * len(parts) + len(segments))
    offset = 2 * len(parts)
    for i, part in enumerate(parts):
        for j, point in enumerate(terminals(part)):
            for k, (a, b) in enumerate(segments):
                if _on_segment(point, a, b):
                    union.merge(2 * i + j, offset + k)
            for previous in range(i):
                for other_j, other_point in enumerate(terminals(parts[previous])):
                    if point == other_point:
                        union.merge(2 * i + j, 2 * previous + other_j)
    for i, (a, b) in enumerate(segments):
        for j in range(i):
            c, d = segments[j]
            first_wire = document["wires"][owners[i]]
            second_wire = document["wires"][owners[j]]
            first_ends = (tuple(first_wire[0]), tuple(first_wire[-1]))
            second_ends = (tuple(second_wire[0]), tuple(second_wire[-1]))
            if (owners[i] == owners[j] or
                    any(_on_segment(p, c, d) for p in first_ends) or
                    any(_on_segment(p, a, b) for p in second_ends)):
                union.merge(offset + i, offset + j)
                continue
            for point in document.get("junctions", []):
                point = tuple(point)
                if _on_segment(point, a, b) and _on_segment(point, c, d):
                    union.merge(offset + i, offset + j)
                    break
    mapping = {(part["id"], j): union.find(2 * i + j) for i, part in enumerate(parts) for j in (0, 1)}
    return mapping, len(segments)


def validate_document(document: 'dict') -> 'None':
    if not isinstance(document, dict) or not isinstance(document.get("parts"), list) or not isinstance(document.get("wires"), list):
        raise ValueError("文件不是有效的 Z11 电路。")
    parts = document["parts"]
    if len(parts) > 200 or len(document["wires"]) > 500 or sum(max(0, len(wire) - 1) for wire in document["wires"] if isinstance(wire, list)) > 1500:
        raise ValueError("电路规模超出编辑器支持范围。")
    ids = set()
    ports = 0
    for part in parts:
        if not isinstance(part, dict) or part.get("kind") not in ("R", "L", "C", "P"):
            raise ValueError("文件包含不支持的器件。")
        name = part.get("id")
        if not isinstance(name, str) or not name or name in ids:
            raise ValueError("器件编号必须唯一。")
        ids.add(name)
        ports += part["kind"] == "P"
        if part.get("rotation") not in (0, 1, 2, 3) or not all(isinstance(part.get(key), int) for key in ("x", "y")):
            raise ValueError(f"{name} 的位置或方向无效。")
        if part["kind"] != "P":
            value = float(part.get("value", 0))
            q = float(part.get("q", 0))
            if not math.isfinite(value) or value <= 0 or (part["kind"] != "R" and (math.isnan(q) or q <= 0)):
                raise ValueError(f"{name} 的数值或 Q 无效。")
    if ports != 1:
        raise ValueError("电路必须恰好包含一个 PORT1。")
    for points in document["wires"]:
        if not isinstance(points, list) or not 2 <= len(points) <= 100:
            raise ValueError("连线数据无效。")
        for point in points:
            if not isinstance(point, list) or len(point) != 2 or not all(isinstance(x, int) for x in point):
                raise ValueError("连线坐标无效。")
    for point in document.get("junctions", []):
        if not isinstance(point, list) or len(point) != 2 or not all(isinstance(x, int) for x in point):
            raise ValueError("连接点坐标无效。")
    _segments(document)


def solve(document: 'dict', frequency_ghz: 'np.ndarray') -> 'tuple[np.ndarray, int]':
    """Return Z11 and the number of parts outside the port-connected network."""
    validate_document(document)
    f = np.asarray(frequency_ghz, dtype=float)
    if f.ndim != 1 or not len(f) or not np.all(np.isfinite(f)) or np.any(f <= 0):
        raise ValueError("频率必须是一组正数（GHz）。")
    nodes, _ = extract_nodes(document)
    port = next(p for p in document["parts"] if p["kind"] == "P")
    positive, negative = nodes[(port["id"], 0)], nodes[(port["id"], 1)]
    if positive == negative:
        return np.zeros_like(f, dtype=complex), 0

    branches = [(p, nodes[(p["id"], 0)], nodes[(p["id"], 1)]) for p in document["parts"] if p["kind"] != "P"]
    neighbors: 'dict[int, set[int]]' = defaultdict(set)
    for _, a, b in branches:
        neighbors[a].add(b)
        neighbors[b].add(a)
    reachable = {positive}
    queue = deque([positive])
    while queue:
        for neighbor in neighbors[queue.popleft()]:
            if neighbor not in reachable:
                reachable.add(neighbor)
                queue.append(neighbor)
    if negative not in reachable:
        raise ValueError("PORT1 两端之间没有导通路径；请检查连线和器件引脚。")
    active = [(p, a, b) for p, a, b in branches if a in reachable and b in reachable]
    ignored = len(branches) - len(active)
    numbered = {node: i for i, node in enumerate(sorted(reachable - {negative}))}
    result = np.empty(len(f), dtype=complex)
    n = len(numbered)
    for begin in range(0, len(f), 256):
        omega = 2 * np.pi * f[begin:begin + 256] * 1e9
        matrix = np.zeros((len(omega), n, n), dtype=complex)
        for part, a, b in active:
            kind = part["kind"]
            value = float(part["value"])
            q = float(part["q"])
            if kind == "R":
                z = np.full(len(omega), value, dtype=complex)
            elif kind == "L":
                x = omega * value * 1e-12
                z = x / q + 1j * x
            else:
                x = 1 / (omega * value * 1e-15)
                z = x / q - 1j * x
            y = 1 / z
            if a == b:
                continue
            ia, ib = numbered.get(a), numbered.get(b)
            if ia is not None:
                matrix[:, ia, ia] += y
            if ib is not None:
                matrix[:, ib, ib] += y
            if ia is not None and ib is not None:
                matrix[:, ia, ib] -= y
                matrix[:, ib, ia] -= y
        current = np.zeros((len(omega), n), dtype=complex)
        current[:, numbered[positive]] = 1
        try:
            voltage = np.linalg.solve(matrix, current[..., np.newaxis])[..., 0]
        except np.linalg.LinAlgError as error:
            raise ValueError("节点矩阵奇异；请检查悬空网络，或给理想器件设置有限 Q。") from error
        result[begin:begin + len(omega)] = voltage[:, numbered[positive]]
    if not np.all(np.isfinite(result)):
        raise ValueError("计算得到非有限结果，请检查电路或参数。")
    return result, ignored
