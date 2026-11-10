"""Translate the current Z11 drawing into inert, verified Virtuoso plan data.

Coordinates and wire components retain the editor's electrical semantics.  In
particular, an unmarked interior crossing stays electrically separate.  The
SKILL side derives connection points from the installed analogLib masters.
"""

import hashlib
import json
import math
import re
from network_model import _Union, _on_segment, _segments, terminals
from network_model_v2 import validate

PLAN_VERSION = 1
SCALE = 1.0 / 80.0
CROSSING_GAP = 5.0


def serialize_skill(value) -> 'str':
    """Serialize data for SKILL read(), never executable SKILL expressions."""
    if value is None or value is False:
        return "nil"
    if value is True:
        return "t"
    if isinstance(value, str):
        if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
            raise ValueError("导出数据含有不支持的控制字符。")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("SKILL 导出数据必须为有限数值。")
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "(" + " ".join(serialize_skill(item) for item in value) + ")"
    raise TypeError("Unsupported SKILL data: " + type(value).__name__)


def _number(value: 'float') -> 'str':
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("器件参数必须为有限数值。")
    text = repr(value)
    return text[:-2] if text.endswith(".0") else text


def _point(point) -> 'list[float]':
    return [float(point[0]) * SCALE, -float(point[1]) * SCALE]


def _instance_names(parts: 'list[dict]') -> 'dict[str, str]':
    used, result = set(), {}
    for part in parts:
        name = re.sub(r"[^A-Za-z0-9_]", "_", part["id"])
        if not name or not name[0].isalpha():
            name = "Z_" + name
        base, suffix = name, 1
        while name in used:
            suffix += 1
            name = base + "_" + str(suffix)
        used.add(name)
        result[part["id"]] = name
    return result


def _graph(document: 'dict'):
    """Same union rules as extract_nodes, including owner endpoint semantics."""
    parts = [p for p in document["parts"] if p["kind"] != "K"]
    segments, owners = _segments(document)
    offset = 2 * len(parts)
    union = _Union(offset + len(segments))
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
            first = document["wires"][owners[i]]
            second = document["wires"][owners[j]]
            if (owners[i] == owners[j]
                    or any(_on_segment(tuple(p), c, d) for p in (first[0], first[-1]))
                    or any(_on_segment(tuple(p), a, b) for p in (second[0], second[-1]))
                    or any(_on_segment(tuple(p), a, b) and _on_segment(tuple(p), c, d)
                           for p in document.get("junctions", []))):
                union.merge(offset + i, offset + j)
    nodes = {(p["id"], j): union.find(2 * i + j)
             for i, p in enumerate(parts) for j in (0, 1)}
    return nodes, segments, [union.find(offset + k) for k in range(len(segments))]


def _split_crossings(segments, nets):
    """Put a small graphical gap into separate-net crossings and overlaps."""
    cuts = [[] for _ in segments]
    for i, (a, b) in enumerate(segments):
        for j in range(i):
            if nets[i] == nets[j]:
                continue
            c, d = segments[j]
            hi, hj = a[1] == b[1], c[1] == d[1]
            if hi != hj:
                h, v = (i, j) if hi else (j, i)
                ha, hb = segments[h]
                va, vb = segments[v]
                p = (va[0], ha[1])
                if _on_segment(p, ha, hb) and _on_segment(p, va, vb):
                    cuts[h].append((p[0] - CROSSING_GAP, p[0] + CROSSING_GAP))
            elif (hi and a[1] == c[1]) or (not hi and a[0] == c[0]):
                axis = 0 if hi else 1
                lo = max(min(a[axis], b[axis]), min(c[axis], d[axis]))
                high = min(max(a[axis], b[axis]), max(c[axis], d[axis]))
                if lo <= high:
                    cuts[i].append((lo - CROSSING_GAP, high + CROSSING_GAP))
    result = []
    for i, (a, b) in enumerate(segments):
        axis = 0 if a[1] == b[1] else 1
        lo, high = sorted((float(a[axis]), float(b[axis])))
        cursor = lo
        for start, stop in sorted(cuts[i]):
            start, stop = max(lo, start), min(high, stop)
            if stop < cursor:
                continue
            if start > cursor:
                p, q = list(a), list(a)
                p[axis], q[axis] = cursor, start
                result.append([nets[i], [_point(p), _point(q)]])
            cursor = max(cursor, stop)
        if cursor < high:
            p, q = list(a), list(a)
            p[axis], q[axis] = cursor, high
            result.append([nets[i], [_point(p), _point(q)]])
    return result


def build_plan(document: 'dict', reference_frequency_ghz: 'float' = 1.0) -> 'list':
    """Return [version, instances, wires, external pins, metadata].

    instances: [name, analogLib cell, centre, editor rotation, properties,
                [[master terminal name, editor endpoint, expected net], ...]].
    Coupled inductor PLUS terminals follow the editor dots, so mind k stays
    nonnegative and the actual terminal orientation carries its mutual sign.
    Q is copied unchanged; fq is an explicit reference because Spectre's
    analogLib loss models differ from the editor's constant-series-Q model.
    """
    validate(document)
    reference_frequency_ghz = float(reference_frequency_ghz)
    if not math.isfinite(reference_frequency_ghz) or reference_frequency_ghz <= 0:
        raise ValueError("Q 参考频率必须大于 0 GHz。")
    names = _instance_names(document["parts"])
    nodes, segments, segment_nodes = _graph(document)
    port = next(p for p in document["parts"] if p["kind"] == "P")
    positive, negative = nodes[(port["id"], 0)], nodes[(port["id"], 1)]
    net_names = {positive: "P" if negative != positive else "P_N"}
    if negative != positive:
        net_names[negative] = "N"
    for node in list(nodes.values()) + segment_nodes:
        if node not in net_names:
            net_names[node] = "Z11_N" + str(len(net_names) + 1)
    fq = _number(reference_frequency_ghz) + "G"
    instances = []
    for part in document["parts"]:
        name, kind = part["id"], part["kind"]
        if kind == "P":
            continue
        props = [["z11SourceId", name], ["z11SourceKind", kind]]
        connections = []
        if kind == "K":
            cell = "mind"
            props += [["ind1", names[part["l1"]]], ["ind2", names[part["l2"]]],
                      ["k", _number(part["value"])]]
        else:
            if kind == "R":
                cell = "res"
                props.append(["r", _number(part["value"])])
            else:
                ideal = math.isinf(float(part["q"]))
                cell = ("ind" if kind == "L" else "cap") + ("" if ideal else "q")
                props.append(["l" if kind == "L" else "c",
                              _number(part["value"]) + ("p" if kind == "L" else "f")])
                if not ideal:
                    props += [["q", _number(part["q"])], ["fq", fq], ["mode", "1" if kind == "L" else "3"]]
                elif kind == "L":
                    props.append(["r", "0"])
                if kind == "L":
                    props.append(["z11Dot", str(part.get("dot", 0))])
            endpoint_order = (1, 0) if kind == "L" and part.get("dot", 0) else (0, 1)
            for master_term, endpoint in zip(("PLUS", "MINUS"), endpoint_order):
                connections.append([master_term, _point(terminals(part)[endpoint]),
                                    net_names[nodes[(name, endpoint)]]])
        instances.append([names[name], cell, _point((part["x"], part["y"])),
                          part["rotation"], props, connections])
    wires = _split_crossings(segments, [net_names[n] for n in segment_nodes])
    if positive == negative:
        ports = [["P_N", _point(terminals(port)[0]), "P_N"]]
    else:
        ports = [[pin_name, _point(point), net_names[node]]
                 for pin_name, point, node in zip(("P", "N"), terminals(port), (positive, negative))]
    digest = hashlib.sha256(json.dumps(document, sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":")).encode("utf-8")).hexdigest()
    metadata = [["source_sha256", digest], ["q_reference_frequency", fq],
                ["q_model_note", "UI: constant series Q. analogLib: indq mode 1 (Q at fq), capq mode 3 (parallel Q); series/parallel and broadband loss models differ."],
                ["wire_crossing_note", "Unmarked crossings use small gaps and identical net labels to preserve connectivity."],
                ["port_note", "PORT1 is exported as unloaded P/N inputOutput pins." if positive != negative
                 else "Shorted PORT1 is exported as one unloaded common P_N pin."]]
    return [PLAN_VERSION, instances, wires, ports, metadata]
