"""Bounded impedance optimization for the Z11 studio."""


import copy
import math
import re
from collections import deque

from threading import Event
from typing import Callable

import numpy as np

from network_model_v2 import parameters, solve


GLOBAL_Q_KINDS = {"global_cap_q": "C", "global_ind_q": "L"}
GLOBAL_Q_LABELS = {"global_cap_q": "global cap Q", "global_ind_q": "global ind Q"}


def apply_parameter(parts: 'dict[str, dict]', key: 'str', value: 'float') -> 'None':
    """Apply one optimization parameter to a trial or to the editor document."""
    if key in GLOBAL_Q_KINDS:
        for part in parts.values():
            if part["kind"] == GLOBAL_Q_KINDS[key]:
                part["q"] = float(value)
    else:
        name, field = key.split(".")
        parts[name][field] = float(value)


METRIC_LABELS = {
    "frequency": "频率 (GHz)",
    "real": "Re(Z) (Ω)",
    "imag": "Im(Z) (Ω)",
    "magnitude": "|Z| (Ω)",
    "phase": "相位 (°)",
    "bandwidth": "连续频率范围 (GHz)",
}


class TraceScore:
    def __init__(self, feasible, value=None, band=None, point_frequency=None, violation=math.inf):
        self.feasible=feasible
        self.value=value
        self.band=band
        self.point_frequency=point_frequency
        self.violation=violation


def _metrics(frequency: 'np.ndarray', impedance: 'np.ndarray') -> 'dict[str, np.ndarray]':
    return {
        "frequency": frequency,
        "real": impedance.real,
        "imag": impedance.imag,
        "magnitude": np.abs(impedance),
        "phase": np.angle(impedance, deg=True),
    }


def _margin(values: 'np.ndarray', condition: 'dict') -> 'np.ndarray':
    relation = condition["relation"]
    if relation == ">":
        return values - condition["low"]
    if relation == "<":
        return condition["low"] - values
    return np.minimum(values - condition["low"], condition["high"] - values)


def _bands(frequency: 'np.ndarray', margin: 'np.ndarray') -> 'list[tuple[float, float, int, int]]':
    """Continuous feasible runs, with linear crossing estimates at their edges."""
    mask = margin >= 0
    edges = np.diff(np.r_[False, mask, False].astype(int))
    result = []
    for begin, end_exclusive in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
        end = int(end_exclusive - 1)
        left, right = float(frequency[begin]), float(frequency[end])
        if begin > 0:
            a, b = float(margin[begin - 1]), float(margin[begin])
            if a < 0 <= b and b != a:
                left = float(frequency[begin - 1] + (-a / (b - a)) * (frequency[begin] - frequency[begin - 1]))
        if end + 1 < len(frequency):
            a, b = float(margin[end]), float(margin[end + 1])
            if a >= 0 > b and b != a:
                right = float(frequency[end] + (-a / (b - a)) * (frequency[end + 1] - frequency[end]))
        result.append((left, right, int(begin), end))
    return result


def _frequency_window(frequency: 'np.ndarray', constraints: 'list[dict]') -> 'tuple[float, float]':
    """Intersect the sweep with every frequency constraint."""
    low, high = float(frequency[0]), float(frequency[-1])
    for item in constraints:
        if item["metric"] != "frequency":
            continue
        if item["relation"] in (">", "range"):
            low = max(low, float(item["low"]))
        if item["relation"] == "<":
            high = min(high, float(item["low"]))
        elif item["relation"] == "range":
            high = min(high, float(item["high"]))
    return low, high


def _window_samples(frequency: 'np.ndarray', values: 'np.ndarray',
                    low: 'float', high: 'float', phase: 'bool' = False) -> 'tuple[np.ndarray, np.ndarray]':
    """Include interpolated boundaries; unwrap phase before interpolation."""
    inside = (frequency > low) & (frequency < high)
    points = np.r_[low, frequency[inside], high]
    source = np.rad2deg(np.unwrap(np.deg2rad(values))) if phase else values
    sampled = np.r_[np.interp(low, frequency, source), source[inside],
                    np.interp(high, frequency, source)]
    if phase:
        sampled = (sampled + 180.0) % 360.0 - 180.0
    return points, sampled


def _window_violation(frequency: 'np.ndarray', metrics: 'dict[str, np.ndarray]',
                      constraints: 'list[dict]', low: 'float', high: 'float') -> 'float':
    """Every non-frequency constraint must hold throughout the chosen band."""
    deficit = 0.0
    for item in constraints:
        if item["metric"] == "frequency":
            continue
        _, sampled = _window_samples(frequency, metrics[item["metric"]], low, high,
                                     phase=item["metric"] == "phase")
        deficit = max(deficit, -float(np.min(_margin(sampled, item))))
    return max(0.0, deficit)


def _fixed_error(values: 'np.ndarray | float', settings: 'dict') -> 'np.ndarray | float':
    difference = values - settings["desired"]
    if settings["target"] == "phase":
        difference = (difference + 180.0) % 360.0 - 180.0
    return np.abs(difference)


def _objective_cost(value: 'float', settings: 'dict') -> 'float':
    """Lower is better for every direction; fixed/range goals use target distance."""
    direction = settings["direction"]
    if direction == "max":
        return -float(value)
    if direction == "min":
        return float(value)
    if direction == "value":
        return float(_fixed_error(float(value), settings))
    return max(settings["desired"] - float(value),
               float(value) - settings["desired_high"], 0.0)


def evaluate_trace(frequency: 'np.ndarray', impedance: 'np.ndarray', settings: 'dict') -> 'TraceScore':
    metrics = _metrics(frequency, impedance)
    constraints = settings["constraints"]
    mode = settings["mode"]
    target = settings["target"]
    direction = settings["direction"]

    # Scalar custom goals are whole-band guarantees: maximin or minimax.
    if mode == "custom" and target != "bandwidth":
        low, high = _frequency_window(frequency, constraints)
        if low >= high:
            return TraceScore(False, violation=1e9)
        violation = _window_violation(frequency, metrics, constraints, low, high)
        if violation > 0:
            return TraceScore(False, violation=violation)
        points, values = _window_samples(frequency, metrics[target], low, high,
                                         phase=target == "phase")
        if direction == "max":
            chosen = int(np.argmin(values))
        elif direction == "min":
            chosen = int(np.argmax(values))
        elif direction == "value":
            chosen = int(np.argmax(_fixed_error(values, settings)))
        else:
            chosen = int(np.argmax(np.maximum(settings["desired"] - values,
                                               values - settings["desired_high"])))
        return TraceScore(True, float(values[chosen]), point_frequency=float(points[chosen]),
                          violation=0.0)

    margins = [_margin(metrics[item["metric"]], item) for item in constraints]
    margin = np.minimum.reduce(margins) if margins else np.full(len(frequency), math.inf)
    valid = margin >= 0
    violation = max(0.0, -float(np.max(margin))) if margins else 0.0
    if not np.any(valid):
        return TraceScore(False, violation=violation)

    if mode == "3db":
        magnitude = metrics["magnitude"]
        peaks = np.flatnonzero((magnitude >= np.r_[magnitude[0], magnitude[:-1]]) &
                                (magnitude >= np.r_[magnitude[1:], magnitude[-1]]) & valid)
        candidates = []
        window_low, window_high = _frequency_window(frequency, constraints)
        for peak in peaks:
            if magnitude[peak] <= 0:
                continue
            half_power = magnitude[peak] / math.sqrt(2.0)
            # Locate real half-power crossings before applying other constraints.
            for left, right, begin, end in _bands(frequency, magnitude - half_power):
                if begin <= peak <= end and right > left and begin > 0 and end < len(frequency) - 1:
                    if left < window_low or right > window_high:
                        continue
                    if _window_violation(frequency, metrics, constraints, left, right) > 0:
                        continue
                    candidates.append((right - left, left, right))
                    break
        if not candidates:
            return TraceScore(False, violation=max(violation, 1e-9))
        width, left, right = min(candidates, key=lambda row: _objective_cost(row[0], settings))
        return TraceScore(True, float(width), (float(left), float(right)), violation=0.0)

    if mode == "custom" and target == "bandwidth":
        candidates = [(right - left, left, right) for left, right, _, _ in _bands(frequency, margin) if right > left]
        if not candidates:
            return TraceScore(False, violation=max(violation, 1e-9))
        width, left, right = min(candidates, key=lambda row: _objective_cost(row[0], settings))
        return TraceScore(True, float(width), (float(left), float(right)), violation=0.0)

    # Peak mode deliberately searches a single feasible frequency point.
    series = metrics["magnitude"]
    indices = np.flatnonzero(valid)
    chosen = int(min(indices, key=lambda index: _objective_cost(series[index], settings)))
    return TraceScore(True, float(series[chosen]), point_frequency=float(frequency[chosen]), violation=0.0)


def validate_settings(document: 'dict', frequency: 'np.ndarray', settings: 'dict') -> 'dict':
    """Return a numeric, validated copy of UI settings."""
    config = copy.deepcopy(settings)
    if config.get("objectives"):
        if not isinstance(config["objectives"], list) or not isinstance(config["objectives"][0], dict):
            raise ValueError("优化目标列表无效。")
        for key in ("mode", "target", "direction", "desired", "desired_high", "tolerance"):
            if key in config["objectives"][0]:
                config[key] = config["objectives"][0][key]
    catalog = dict(parameters(document), **GLOBAL_Q_LABELS)
    variables = config.get("variables", [])
    if not isinstance(variables, list):
        raise ValueError("优化参数列表无效。")
    active = []
    for row in variables:
        if not isinstance(row, dict) or not isinstance(row.get("enabled", True), bool):
            raise ValueError("优化参数的启用状态无效。")
        if row.get("enabled", True):
            active.append(row)
    if not active:
        raise ValueError("请先启用至少一个需要优化的器件参数。")
    config["variables"] = active
    seen = set()
    linked = {}
    for row in active:
        key = row.get("parameter")
        if key not in catalog or key in seen:
            raise ValueError(f"优化参数 {key or '(空)'} 不存在或重复。")
        seen.add(key)
        if key in GLOBAL_Q_KINDS and not any(part["kind"] == GLOBAL_Q_KINDS[key]
                                              for part in document["parts"]):
            raise ValueError(f"{catalog[key]} 已启用，但当前电路没有对应器件。")
        group = row.get("group", "独立")
        if group != "独立" and (not isinstance(group, str) or
                                 re.fullmatch(r"联动 [1-9][0-9]*", group) is None):
            raise ValueError(f"{catalog[key]} 的联动组无效。")
        try:
            low, high = float(row["low"]), float(row["high"])
            raw_step = row.get("step", "")
            step = None if raw_step in (None, "") else float(raw_step)
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError(f"{catalog[key]} 的下限、上限或步长不是有效数字。") from error
        if not math.isfinite(low) or not math.isfinite(high) or low >= high:
            raise ValueError(f"{catalog[key]} 须满足有限下限 < 上限。")
        if key.endswith(".value") and any(p["id"] == key.rsplit(".", 1)[0] and p["kind"] == "K" for p in document["parts"]):
            if low <= -1 or high >= 1:
                raise ValueError(f"{catalog[key]} 的 k 范围须满足 -1<下限<上限<1。")
        elif low <= 0:
            raise ValueError(f"{catalog[key]} 的下限须大于 0。")
        if step is not None and (not math.isfinite(step) or step <= 0 or step > high - low or
                                 (high - low) / step > 1e12):
            raise ValueError(f"{catalog[key]} 的步长须大于 0、不超过参数范围，且不能小到失去数值精度。")
        row["low"], row["high"], row["step"], row["group"] = low, high, step, group
        if group != "独立":
            linked.setdefault(group, []).append(row)
    for global_key, kind in GLOBAL_Q_KINDS.items():
        if global_key in seen:
            conflicts = [row["parameter"] for row in active
                         if row["parameter"].endswith(".q") and
                         any(part["id"] == row["parameter"].split(".")[0] and part["kind"] == kind
                             for part in document["parts"])]
            if conflicts:
                raise ValueError(f"{GLOBAL_Q_LABELS[global_key]} 与单个器件 Q {', '.join(conflicts)} 不可同时启用。")
    for group, members in linked.items():
        first = members[0]
        if any(row["low"] != first["low"] or row["high"] != first["high"] for row in members[1:]):
            raise ValueError(f"{group} 内优化参数的下限和上限必须一致。")
        steps = [row["step"] for row in members if row["step"] is not None]
        if steps and any(not math.isclose(value, steps[0], rel_tol=1e-12) for value in steps[1:]):
            raise ValueError(f"{group} 内填写的优化步长必须一致；留空会沿用组内步长。")

    for item in config.get("constraints", []):
        metric, relation = item.get("metric"), item.get("relation")
        if metric not in METRIC_LABELS or metric == "bandwidth" or relation not in (">", "<", "range"):
            raise ValueError("限制条件的量或比较关系无效。")
        try:
            low = float(item["low"])
            high = float(item["high"]) if relation == "range" else None
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError(f"{METRIC_LABELS[metric]} 的限制值不是有效数字。") from error
        if not math.isfinite(low) or (high is not None and (not math.isfinite(high) or low >= high)):
            raise ValueError(f"{METRIC_LABELS[metric]} 的限制值须有限；范围须满足下限 < 上限。")
        item["low"], item["high"] = low, high
        if metric == "frequency" and ((relation == "range" and (high < frequency[0] or low > frequency[-1])) or
                                      (relation == ">" and low >= frequency[-1]) or
                                      (relation == "<" and low <= frequency[0])):
            raise ValueError("频率限制与当前扫频范围没有交集。")

    window_low, window_high = _frequency_window(frequency, config.get("constraints", []))
    if window_low >= window_high:
        raise ValueError("频率限制之间没有共同的有效频率范围。")

    if config.get("mode") not in ("custom", "peak", "3db") or config.get("direction") not in ("max", "min", "value", "range"):
        raise ValueError("优化模式或方向无效。")
    if config.get("algorithm", "de") not in ("de", "cma", "nsga"):
        raise ValueError("优化算法无效；请选择差分进化或 CMA-ES。")
    config["algorithm"] = config.get("algorithm", "de")
    if config.get("target") not in ("bandwidth", "real", "imag", "magnitude", "phase"):
        raise ValueError("优化目标无效。")
    if config["mode"] == "custom" and config["target"] == "bandwidth" and not config.get("constraints"):
        raise ValueError("优化连续频率范围时，请添加至少一个限制条件。")
    try:
        config["tolerance"] = float(config.get("tolerance", 0.01))
        config["generations"] = int(config.get("generations", 80))
        config["stall"] = int(config.get("stall", 20))
        desired = config.get("desired")
        config["desired"] = None if desired in (None, "") else float(desired)
        high = config.get("desired_high") if config["direction"] == "range" else None
        config["desired_high"] = None if high in (None, "") else float(high)
    except (ValueError, TypeError) as error:
        raise ValueError("目标值、允许误差或代数不是有效数字。") from error
    if (not math.isfinite(config["tolerance"]) or config["tolerance"] <= 0 or
            not 5 <= config["generations"] <= 500 or not 3 <= config["stall"] <= config["generations"] or
            (config["desired"] is not None and not math.isfinite(config["desired"])) or
            (config["desired_high"] is not None and not math.isfinite(config["desired_high"]))):
        raise ValueError("允许误差须大于 0；最大代数 5–500；停滞代数 3–最大代数。")
    if config["direction"] in ("value", "range") and config["desired"] is None:
        raise ValueError("固定值目标须填写目标值；范围目标须填写下限。")
    if config["direction"] == "range" and (
            config["desired_high"] is None or config["desired"] >= config["desired_high"]):
        raise ValueError("范围目标须填写上限，且下限 < 上限。")
    objectives = config.get("objectives", [])
    if objectives:
        if not isinstance(objectives, list) or not all(isinstance(item, dict) for item in objectives):
            raise ValueError("优化目标列表无效。")
        validated = []
        for index, objective in enumerate(objectives):
            if index == 0:
                checked = config
            else:
                item_config = {key: value for key, value in config.items() if key != "objectives"}
                item_config.update(objective)
                item_config["algorithm"] = "de"
                checked = validate_settings(document, frequency, item_config)
            validated.append({key: checked[key] for key in
                              ("mode", "target", "direction", "desired", "desired_high", "tolerance")})
        config["objectives"] = validated
    if len(config.get("objectives", [])) > 1 and config["algorithm"] != "nsga":
        raise ValueError("多目标优化须使用 NSGA-II。")
    if config["algorithm"] == "nsga" and len(config.get("objectives", [])) < 2:
        raise ValueError("NSGA-II 至少需要两个优化目标。")
    return config


def _attained(score: 'TraceScore', config: 'dict') -> 'bool':
    if not score.feasible:
        return False
    desired = config["desired"]
    if desired is None:
        return True
    direction = config["direction"]
    if direction == "max":
        return score.value >= desired - config["tolerance"]
    if direction == "min":
        return score.value <= desired + config["tolerance"]
    scale = max(1.0, abs(score.value), abs(desired),
                abs(config["desired_high"]) if config["desired_high"] is not None else 0.0)
    return _objective_cost(score.value, config) <= config["tolerance"] + 1e-12 * scale


def optimize(document: 'dict', frequency: 'np.ndarray', settings: 'dict',
             progress: 'Callable[[dict], None] | None' = None,
             cancel: 'Event | None' = None) -> 'dict':
    """Run bounded single or multiobjective optimization."""
    config = validate_settings(document, frequency, settings)
    if config["algorithm"] == "nsga":
        from nsga_optimizer import optimize_nsga
        return optimize_nsga(document, frequency, config, progress, cancel)
    variables = config["variables"]
    dimensions: 'list[list[dict]]' = []
    linked: 'dict[str, list[dict]]' = {}
    for row in variables:
        group = row["group"]
        if group == "独立":
            dimensions.append([row])
        else:
            if group not in linked:
                linked[group] = []
                dimensions.append(linked[group])
            linked[group].append(row)
    bounds = np.array([(group[0]["low"], group[0]["high"]) for group in dimensions], dtype=float)
    low, high = bounds[:, 0], bounds[:, 1]
    steps = [next((row["step"] for row in group if row["step"] is not None), None)
             for group in dimensions]
    dimension = len(dimensions)
    size = max(16, 8 * dimension)
    rng = np.random.default_rng(1729)
    by_id = {part["id"]: part for part in document["parts"]}

    def snap(vector: 'np.ndarray') -> 'np.ndarray':
        values = np.clip(vector, low, high).copy()
        for index, step in enumerate(steps):
            if step is not None:
                last = math.floor((high[index] - low[index]) / step + 1e-10)
                position = np.clip(np.floor((values[index] - low[index]) / step + .5), 0, last)
                values[index] = low[index] + float(position) * step
        return values

    population = np.array([snap(vector) for vector in rng.uniform(low, high, (size, dimension))])
    def current_value(row: 'dict') -> 'float':
        key = row["parameter"]
        if key in GLOBAL_Q_KINDS:
            values = [float(part["q"]) for part in document["parts"]
                      if part["kind"] == GLOBAL_Q_KINDS[key] and math.isfinite(float(part["q"]))]
            return float(np.median(values)) if values else (row["low"] + row["high"]) / 2
        name, field = key.split(".")
        return float(by_id[name][field])

    current = np.array([current_value(group[0]) for group in dimensions])
    population[0] = snap(current)
    history: 'list[dict]' = []
    samples: 'deque[dict]' = deque(maxlen=5000)
    evaluations = 0
    generation = 0
    last_solver_error = ""

    def run(vector: 'np.ndarray') -> 'tuple[TraceScore, np.ndarray | None]':
        nonlocal evaluations, last_solver_error
        evaluations += 1
        trial = copy.deepcopy(document)
        parts = {part["id"]: part for part in trial["parts"]}
        for group, value in zip(dimensions, vector):
            for row in group:
                apply_parameter(parts, row["parameter"], float(value))
        try:
            impedance, _ = solve(trial, frequency)
            score = evaluate_trace(frequency, impedance, config)
        except (ValueError, np.linalg.LinAlgError) as error:
            last_solver_error = str(error)
            score, impedance = TraceScore(False, violation=1e9), None
        samples.append({"evaluation": evaluations, "generation": generation,
                        "value": score.value, "feasible": score.feasible,
                        "band": score.band, "point_frequency": score.point_frequency,
                        "parameters": {row["parameter"]: float(value)
                                       for group, value in zip(dimensions, vector) for row in group}})
        return score, impedance

    def rank(score: 'TraceScore') -> 'tuple[float, float]':
        if score.feasible:
            return (0.0, _objective_cost(score.value, config))
        return (1.0, score.violation)

    scores: 'list[TraceScore]' = []
    traces: 'list[np.ndarray | None]' = []
    for vector in population:
        if cancel is not None and cancel.is_set():
            return {"success": False, "reason": "用户已停止优化。", "cancelled": True,
                    "history": history, "samples": list(samples), "generations": 0, "evaluations": evaluations}
        score, trace = run(vector)
        scores.append(score)
        traces.append(trace)
    best_index = min(range(size), key=lambda index: rank(scores[index]))
    best_vector = population[best_index].copy()
    best_score = scores[best_index]
    best_trace = traces[best_index]
    stagnant = 0

    def record(generation: 'int') -> 'None':
        item = {"generation": generation, "value": best_score.value, "feasible": best_score.feasible,
                "band": best_score.band, "point_frequency": best_score.point_frequency,
                "parameters": {row["parameter"]: float(value)
                               for group, value in zip(dimensions, best_vector) for row in group}}
        history.append(item)
        if progress:
            progress({"generation": generation, "best": item, "evaluations": evaluations})

    record(0)
    strategy = None
    if config["algorithm"] == "cma":
        try:
            import cma
        except ImportError as error:
            raise ValueError("缺少 CMA-ES 依赖；请在所选 Python 环境安装可选 cma 依赖。") from error
        normalized_best = np.clip((best_vector - low) / (high - low), 1e-6, 1 - 1e-6)
        strategy = cma.CMAEvolutionStrategy(normalized_best.tolist(), 0.25,
                                            {"bounds": [0, 1], "popsize": size,
                                             "seed": 1729, "verbose": -9, "verb_log": 0})
    for generation in range(1, config["generations"] + 1):
        if cancel is not None and cancel.is_set():
            break
        if strategy is not None and strategy.stop():
            break
        old_best = best_score
        improved = False
        if strategy is None:
            for index in range(size):
                if cancel is not None and cancel.is_set():
                    break
                choices = rng.choice(np.delete(np.arange(size), index), size=3, replace=False)
                mutant = np.clip(population[choices[0]] + 0.75 * (population[choices[1]] - population[choices[2]]), low, high)
                crossover = rng.random(dimension) < 0.8
                crossover[rng.integers(dimension)] = True
                vector = snap(np.where(crossover, mutant, population[index]))
                score, trace = run(vector)
                if rank(score) < rank(scores[index]):
                    population[index] = vector
                    scores[index] = score
                    traces[index] = trace
                    if rank(score) < rank(best_score):
                        best_vector, best_score, best_trace = vector.copy(), score, trace
                        improved = True
        else:
            proposals = strategy.ask()
            generation_scores = []
            for proposal in proposals:
                if cancel is not None and cancel.is_set():
                    break
                vector = snap(low + np.asarray(proposal) * (high - low))
                score, trace = run(vector)
                generation_scores.append(score)
                if rank(score) < rank(best_score):
                    best_vector, best_score, best_trace = vector.copy(), score, trace
                    improved = True
            if cancel is not None and cancel.is_set():
                break
            ordered = sorted({rank(score) for score in generation_scores})
            fitness = [float(ordered.index(rank(score))) for score in generation_scores]
            strategy.tell(proposals, fitness)
        old_cost = _objective_cost(old_best.value, config) if old_best.feasible else math.inf
        new_cost = _objective_cost(best_score.value, config) if best_score.feasible else math.inf
        meaningful = improved and (not old_best.feasible or not best_score.feasible or
                                   old_cost - new_cost > 1e-10 * max(1.0, abs(old_cost)))
        stagnant = 0 if meaningful else stagnant + 1
        if improved or generation == 1 or generation % 5 == 0:
            record(generation)
        if stagnant >= config["stall"]:
            break

    cancelled = cancel is not None and cancel.is_set()
    success = not cancelled and _attained(best_score, config)
    if cancelled:
        reason = "用户已停止优化。"
    elif not best_score.feasible:
        reason = "无法实现：在指定参数范围和搜索代数内未找到满足限制条件的解。"
        if last_solver_error:
            reason += f" 最近一次电路求解提示：{last_solver_error}"
    elif not success:
        if config["direction"] == "value":
            reason = (f"无法实现：最差频点/候选值 {best_score.value:g}，距目标 {config['desired']:g} "
                      f"仍有 {_objective_cost(best_score.value, config):g}（允许误差 {config['tolerance']:g}）。")
        elif config["direction"] == "range":
            reason = (f"无法实现：最差频点/候选值 {best_score.value:g} 超出目标范围 "
                      f"[{config['desired']:g}, {config['desired_high']:g}]"
                      f"（允许误差 {config['tolerance']:g}）。")
        else:
            reason = f"无法实现：最佳结果 {best_score.value:g} 未达到目标 {config['desired']:g}（允许误差 {config['tolerance']:g}）。"
    else:
        if config["direction"] == "value":
            reason = (f"已达到固定值目标 {config['desired']:g}：最差频点/候选值 "
                      f"{best_score.value:g}，最大偏差 {_objective_cost(best_score.value, config):g}。")
        elif config["direction"] == "range":
            reason = (f"已达到目标范围 [{config['desired']:g}, {config['desired_high']:g}]："
                      f"最差频点/候选值 {best_score.value:g}。")
        else:
            reason = "已达到目标。" if config["desired"] is not None else "已找到满足限制条件的最佳候选。"
    return {"success": success, "reason": reason, "cancelled": cancelled,
            "best": {"parameters": {row["parameter"]: float(value)
                                     for group, value in zip(dimensions, best_vector) for row in group},
                     "value": best_score.value, "band": best_score.band,
                     "point_frequency": best_score.point_frequency, "feasible": best_score.feasible},
            "frequency": frequency, "impedance": best_trace,
            "generations": generation, "evaluations": evaluations, "history": history,
            "samples": list(samples), "settings": config}
