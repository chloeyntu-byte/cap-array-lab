"""General one-port RLC nodal solver with magnetically coupled inductors."""


import math
from collections import defaultdict, deque

import numpy as np

from network_model import extract_nodes

# Signed k is relative to the selected inductor dot orientations.
SIGNED_COUPLING = True
COUPLING_CONVENTION = "M = dot_sign * k * sqrt(L1*L2); abs(k) < 1"


def parameters(document: 'dict') -> 'dict[str, str]':
    """Numeric parameters available to the paired sweep editor."""
    found = {}
    for part in document["parts"]:
        name, kind = part["id"], part["kind"]
        if kind == "R":
            found[f"{name}.value"] = f"{name} · R (Ω)"
        elif kind == "L":
            found[f"{name}.value"] = f"{name} · L (pH)"
            found[f"{name}.q"] = f"{name} · Q"
        elif kind == "C":
            found[f"{name}.value"] = f"{name} · C (fF)"
            found[f"{name}.q"] = f"{name} · Q"
        elif kind == "K":
            found[f"{name}.value"] = f"{name} · k"
    return found


def validate(document: 'dict') -> 'None':
    if not isinstance(document, dict) or not isinstance(document.get("parts"), list) or not isinstance(document.get("wires"), list):
        raise ValueError("电路文件格式无效。")
    parts = document["parts"]
    if len(parts) > 200 or len(document["wires"]) > 500:
        raise ValueError("电路规模超过支持范围（200 个器件、500 根线）。")
    names = set()
    ports = 0
    inductors = {}
    for part in parts:
        if not isinstance(part, dict) or part.get("kind") not in ("R", "L", "C", "P", "K"):
            raise ValueError("电路中含有不支持的器件。")
        name, kind = part.get("id"), part["kind"]
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("器件编号缺失或重复。")
        names.add(name)
        if not all(isinstance(part.get(key), int) for key in ("x", "y")) or part.get("rotation") not in (0, 1, 2, 3):
            raise ValueError(f"{name} 的位置或方向无效。")
        ports += kind == "P"
        if kind == "L":
            if part.get("dot", 0) not in (0, 1):
                raise ValueError(f"{name} 的同名端设置无效。")
            inductors[name] = part
        if kind != "P":
            value = float(part.get("value", 0))
            if not math.isfinite(value) or (kind == "K" and not abs(value) < 1) or (kind != "K" and value <= 0):
                raise ValueError(f"{name} 的数值无效；k 须满足 |k|<1，其余数值须大于 0。")
            if kind in ("L", "C"):
                q = float(part.get("q", 0))
                if math.isnan(q) or q <= 0:
                    raise ValueError(f"{name} 的 Q 须大于 0，或填 inf。")
    if ports != 1:
        raise ValueError("每页必须恰好有一个 PORT1。")
    pairs = set()
    for part in parts:
        if part["kind"] != "K":
            continue
        a, b = part.get("l1"), part.get("l2")
        if a not in inductors or b not in inductors or a == b:
            raise ValueError(f"{part['id']} 必须绑定两个不同且存在的电感。")
        pair = frozenset((a, b))
        if pair in pairs:
            raise ValueError(f"{a} 与 {b} 已存在互感器件，请勿重复绑定。")
        pairs.add(pair)
    # Pairwise |k|<1 alone is insufficient for three or more windings.
    # Dot changes are a diagonal sign congruence and preserve PSD.
    if inductors:
        order = list(inductors)
        indices = {name: i for i, name in enumerate(order)}
        normalized = np.eye(len(order))
        for part in parts:
            if part["kind"] != "K":
                continue
            a, b = part["l1"], part["l2"]
            dot_sign = 1 if inductors[a].get("dot", 0) == inductors[b].get("dot", 0) else -1
            normalized[indices[a], indices[b]] = normalized[indices[b], indices[a]] = dot_sign * float(part["value"])
        minimum = float(np.linalg.eigvalsh(normalized).min())
        if minimum < -1e-10:
            raise ValueError(f"互感矩阵非正半定（最小归一化特征值 {minimum:.6g}）；请检查多绕组 k 和极性。")
    count = 0
    for wire in document["wires"]:
        if not isinstance(wire, list) or not 2 <= len(wire) <= 100:
            raise ValueError("连线格式无效。")
        count += len(wire) - 1
        for point in wire:
            if not isinstance(point, list) or len(point) != 2 or not all(isinstance(v, int) for v in point):
                raise ValueError("连线坐标无效。")
        for a, b in zip(wire, wire[1:]):
            if a[0] != b[0] and a[1] != b[1]:
                raise ValueError("连线须为水平或垂直。")
    if count > 1500:
        raise ValueError("连线段过多（上限 1500）。")
    for point in document.get("junctions", []):
        if not isinstance(point, list) or len(point) != 2 or not all(isinstance(v, int) for v in point):
            raise ValueError("连接点格式无效。")


def solve(document: 'dict', frequency_ghz: 'np.ndarray') -> 'tuple[np.ndarray, int]':
    """Z11 in ohms; constant Q; mutual M = dot_sign * signed k * sqrt(L1 L2)."""
    validate(document)
    f = np.asarray(frequency_ghz, dtype=float)
    if f.ndim != 1 or not f.size or not np.all(np.isfinite(f)) or np.any(f <= 0):
        raise ValueError("扫频频率必须全部大于 0 GHz。")
    electric = [p for p in document["parts"] if p["kind"] != "K"]
    couplers = [p for p in document["parts"] if p["kind"] == "K"]
    nodes, _ = extract_nodes({"parts": electric, "wires": document["wires"], "junctions": document.get("junctions", [])})
    port = next(p for p in electric if p["kind"] == "P")
    plus, minus = nodes[(port["id"], 0)], nodes[(port["id"], 1)]
    if plus == minus:
        return np.zeros(len(f), dtype=complex), 0
    branches = [(p, nodes[(p["id"], 0)], nodes[(p["id"], 1)]) for p in electric if p["kind"] != "P"]
    adjacency: 'dict[int, set[int]]' = defaultdict(set)
    for _, a, b in branches:
        adjacency[a].add(b)
        adjacency[b].add(a)
    islands: 'dict[int, int]' = {}
    for node in {plus, minus} | {n for _, a, b in branches for n in (a, b)}:
        if node in islands:
            continue
        queue = deque([node])
        while queue:
            current = queue.popleft()
            if current in islands:
                continue
            islands[current] = node
            queue.extend(adjacency[current] - islands.keys())
    if islands[plus] != islands[minus]:
        raise ValueError("PORT1 两端没有电气回路；请检查引脚和连线。")
    by_name = {p["id"]: (p, a, b) for p, a, b in branches}
    linked: 'dict[int, set[int]]' = defaultdict(set)
    for part in couplers:
        a = islands[by_name[part["l1"]][1]]
        b = islands[by_name[part["l2"]][1]]
        linked[a].add(b)
        linked[b].add(a)
    active_islands = {islands[plus]}
    queue = deque(active_islands)
    while queue:
        for other in linked[queue.popleft()]:
            if other not in active_islands:
                active_islands.add(other)
                queue.append(other)
    active = [(p, a, b) for p, a, b in branches if islands[a] in active_islands]
    active_couplers = [p for p in couplers if islands[by_name[p["l1"]][1]] in active_islands]
    ignored = len(branches) - len(active) + len(couplers) - len(active_couplers)
    reference = {island: (minus if island == islands[plus] else min(n for n, group in islands.items() if group == island)) for island in active_islands}
    active_nodes = {n for n, island in islands.items() if island in active_islands}
    voltage_nodes = {node: i for i, node in enumerate(sorted(active_nodes - set(reference.values())))}
    inductors = [(p, a, b) for p, a, b in active if p["kind"] == "L"]
    inductor_index = {p["id"]: i for i, (p, _, _) in enumerate(inductors)}
    n, nl = len(voltage_nodes), len(inductors)
    result = np.empty(len(f), dtype=complex)
    for start in range(0, len(f), 128):
        omega = 2 * np.pi * f[start:start + 128] * 1e9
        matrix = np.zeros((len(omega), n + nl, n + nl), dtype=complex)
        for part, a, b in active:
            kind = part["kind"]
            ia, ib = voltage_nodes.get(a), voltage_nodes.get(b)
            if kind == "L":
                index = n + inductor_index[part["id"]]
                if ia is not None:
                    matrix[:, ia, index] += 1
                    matrix[:, index, ia] += 1
                if ib is not None:
                    matrix[:, ib, index] -= 1
                    matrix[:, index, ib] -= 1
                reactance = omega * float(part["value"]) * 1e-12
                matrix[:, index, index] -= reactance / float(part["q"]) + 1j * reactance
                continue
            if a == b:
                continue
            if kind == "R":
                admittance = np.full(len(omega), 1 / float(part["value"]), dtype=complex)
            else:
                reactance = 1 / (omega * float(part["value"]) * 1e-15)
                admittance = 1 / (reactance / float(part["q"]) - 1j * reactance)
            if ia is not None:
                matrix[:, ia, ia] += admittance
            if ib is not None:
                matrix[:, ib, ib] += admittance
            if ia is not None and ib is not None:
                matrix[:, ia, ib] -= admittance
                matrix[:, ib, ia] -= admittance
        for coupling in active_couplers:
            left, right = inductor_index[coupling["l1"]], inductor_index[coupling["l2"]]
            p1, p2 = by_name[coupling["l1"]][0], by_name[coupling["l2"]][0]
            sign = 1 if p1.get("dot", 0) == p2.get("dot", 0) else -1
            mutual = sign * float(coupling["value"]) * math.sqrt(float(p1["value"]) * float(p2["value"])) * 1e-12
            matrix[:, n + left, n + right] -= 1j * omega * mutual
            matrix[:, n + right, n + left] -= 1j * omega * mutual
        current = np.zeros((len(omega), n + nl, 1), dtype=complex)
        current[:, voltage_nodes[plus], 0] = 1
        try:
            solution = np.linalg.solve(matrix, current)[..., 0]
        except np.linalg.LinAlgError as error:
            raise ValueError("求解矩阵奇异：检查悬空连接、理想器件或过强互感。") from error
        result[start:start + len(omega)] = solution[:, voltage_nodes[plus]]
    if not np.all(np.isfinite(result)):
        raise ValueError("计算出现非有限结果，请检查参数。")
    return result, ignored
