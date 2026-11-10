"""NSGA-II search and Pareto ranking for multiple impedance objectives."""


import copy
import math
from collections import deque
from threading import Event
from typing import Callable

import numpy as np

from network_model_v2 import solve
from optimizer import _attained, _objective_cost, apply_parameter, evaluate_trace


def _fronts(candidates: 'list[dict]') -> 'tuple[list[list[int]], np.ndarray, np.ndarray]':
    """Constraint-first nondominated fronts and NSGA-II crowding distance."""
    count = len(candidates)
    costs = np.asarray([item["costs"] for item in candidates], dtype=float)
    feasible = np.asarray([item["feasible"] for item in candidates], dtype=bool)
    violation = np.asarray([item["violation"] for item in candidates])
    dominates = ((feasible[:, None] & ~feasible[None, :]) |
                 (feasible[:, None] & feasible[None, :] &
                  np.all(costs[:, None, :] <= costs[None, :, :], axis=2) &
                  np.any(costs[:, None, :] < costs[None, :, :], axis=2)) |
                 (~feasible[:, None] & ~feasible[None, :] &
                  (violation[:, None] < violation[None, :])))
    fronts: 'list[list[int]]' = []
    rank = np.full(count, count, dtype=int)
    domination_count = np.count_nonzero(dominates, axis=0).astype(int)
    front = np.flatnonzero(domination_count == 0)
    while len(front):
        fronts.append(front.tolist())
        rank[front] = len(fronts) - 1
        domination_count -= np.count_nonzero(dominates[front], axis=0)
        domination_count[rank < count] = -1
        front = np.flatnonzero(domination_count == 0)
    crowding = np.zeros(count)
    for front in fronts:
        if len(front) <= 2:
            crowding[front] = math.inf
            continue
        for axis in range(costs.shape[1]):
            order = sorted(front, key=lambda index: costs[index, axis])
            crowding[order[0]] = crowding[order[-1]] = math.inf
            span = costs[order[-1], axis] - costs[order[0], axis]
            if math.isfinite(span) and span > 0:
                for position in range(1, len(order) - 1):
                    crowding[order[position]] += (
                        costs[order[position + 1], axis] -
                        costs[order[position - 1], axis]) / span
    return fronts, rank, crowding


def _representative(front: 'list[dict]') -> 'dict':
    """Choose a balanced point on the current feasible front."""
    feasible = [item for item in front if item["feasible"]]
    if not feasible:
        return min(front, key=lambda item: item["violation"])
    costs = np.asarray([item["costs"] for item in feasible])
    spread = np.ptp(costs, axis=0)
    normalized = (costs - np.min(costs, axis=0)) / np.where(spread > 0, spread, 1)
    return feasible[int(np.argmin(np.sum(normalized, axis=1)))]


def optimize_nsga(document: 'dict', frequency: 'np.ndarray', config: 'dict',
                  progress: 'Callable[[dict], None] | None' = None,
                  cancel: 'Event | None' = None) -> 'dict':
    """Bounded NSGA-II with constraint domination and a selectable Pareto front."""
    objectives = config["objectives"]
    linked: 'dict[str, list[dict]]' = {}
    dimensions: 'list[list[dict]]' = []
    for row in config["variables"]:
        if row["group"] == "独立":
            dimensions.append([row])
        else:
            if row["group"] not in linked:
                linked[row["group"]] = []
                dimensions.append(linked[row["group"]])
            linked[row["group"]].append(row)
    low = np.asarray([group[0]["low"] for group in dimensions])
    high = np.asarray([group[0]["high"] for group in dimensions])
    steps = [next((row["step"] for row in group if row["step"] is not None), None)
             for group in dimensions]
    count = len(dimensions)
    size = max(16, 8 * count)
    if size % 2:
        size += 1
    rng = np.random.default_rng(1729)
    by_id = {part["id"]: part for part in document["parts"]}
    samples: 'deque[dict]' = deque(maxlen=5000)
    history: 'list[dict]' = []
    evaluations = 0
    generation = 0
    solver_error = ""

    def snap(vector: 'np.ndarray') -> 'np.ndarray':
        values = np.clip(vector, low, high).copy()
        for axis, step in enumerate(steps):
            if step is not None:
                last = math.floor((high[axis] - low[axis]) / step + 1e-10)
                position = np.clip(np.floor((values[axis] - low[axis]) / step + .5), 0, last)
                values[axis] = low[axis] + float(position) * step
        return values

    def parameter_values(vector: 'np.ndarray') -> 'dict[str, float]':
        return {row["parameter"]: float(value)
                for group, value in zip(dimensions, vector) for row in group}

    def evaluate(vector: 'np.ndarray') -> 'dict':
        nonlocal evaluations, solver_error
        evaluations += 1
        trial = copy.deepcopy(document)
        parts = {part["id"]: part for part in trial["parts"]}
        values = parameter_values(vector)
        for key, value in values.items():
            apply_parameter(parts, key, value)
        try:
            impedance, _ = solve(trial, frequency)
            scores = [evaluate_trace(frequency, impedance, {**config, **goal})
                      for goal in objectives]
            feasible = all(score.feasible for score in scores)
            violation = max((score.violation for score in scores), default=0.0)
            costs = [float(_objective_cost(score.value, goal)) if score.feasible else math.inf
                     for score, goal in zip(scores, objectives)]
        except (ValueError, np.linalg.LinAlgError) as error:
            solver_error = str(error)
            impedance, scores, feasible, violation = None, [], False, 1e9
            costs = [math.inf] * len(objectives)
        sample = {"evaluation": evaluations, "generation": generation,
                  "value": scores[0].value if scores else None, "feasible": feasible,
                  "objectives": [score.value for score in scores] if scores else [None] * len(objectives),
                  "parameters": values}
        samples.append(sample)
        return {"vector": vector, "parameters": values, "scores": scores, "costs": costs,
                "feasible": feasible, "violation": violation, "impedance": impedance}

    def report(population: 'list[dict]') -> 'tuple[dict, list[dict]]':
        fronts, _, _ = _fronts(population)
        front = [population[index] for index in fronts[0]]
        representative = _representative(front)
        first = representative["scores"][0] if representative["scores"] else None
        item = {"generation": generation, "value": first.value if first else None,
                "feasible": representative["feasible"],
                "objectives": [score.value for score in representative["scores"]],
                "band": first.band if first else None,
                "point_frequency": first.point_frequency if first else None,
                "parameters": representative["parameters"], "pareto_count": len(front)}
        history.append(item)
        if progress:
            progress({"generation": generation, "best": item,
                      "evaluations": evaluations, "pareto_count": len(front)})
        return representative, front

    population = []
    current = []
    for group in dimensions:
        row = group[0]
        key = row["parameter"]
        if key.startswith("global_"):
            kind = "C" if key == "global_cap_q" else "L"
            values = [float(part["q"]) for part in document["parts"]
                      if part["kind"] == kind and math.isfinite(float(part["q"]))]
            current.append(float(np.median(values)) if values else (row["low"] + row["high"]) / 2)
        else:
            name, field = key.split(".")
            current.append(float(by_id[name][field]))
    initial = rng.uniform(low, high, (size, count))
    initial[0] = current
    for vector in initial:
        if cancel is not None and cancel.is_set():
            break
        population.append(evaluate(snap(vector)))
    if not population:
        return {"success": False, "reason": "用户已停止优化。", "cancelled": True,
                "history": [], "samples": [], "generations": 0, "evaluations": 0,
                "pareto": [], "settings": config}
    representative, front = report(population)
    stagnant = 0
    ideal = np.min([item["costs"] for item in population], axis=0)
    for generation in range(1, config["generations"] + 1):
        if cancel is not None and cancel.is_set():
            break
        _, rank, crowding = _fronts(population)
        def tournament() -> 'np.ndarray':
            a, b = rng.integers(0, len(population), 2)
            winner = a if (rank[a], -crowding[a]) <= (rank[b], -crowding[b]) else b
            return population[winner]["vector"]
        offspring = []
        for _ in range(size):
            if cancel is not None and cancel.is_set():
                break
            a, b = tournament(), tournament()
            mix = rng.uniform(-.25, 1.25, count)
            child = mix * a + (1 - mix) * b
            mutate = rng.random(count) < 1.0 / count
            child[mutate] += rng.normal(0, .12, np.count_nonzero(mutate)) * (high - low)[mutate]
            offspring.append(evaluate(snap(child)))
        if cancel is not None and cancel.is_set():
            break
        combined = population + offspring
        fronts, _, crowding = _fronts(combined)
        population = []
        for indices in fronts:
            available = size - len(population)
            if available <= 0:
                break
            chosen = (indices if len(indices) <= available else
                      sorted(indices, key=lambda index: crowding[index], reverse=True)[:available])
            population.extend(combined[index] for index in chosen)
        representative, front = report(population)
        next_ideal = np.min([item["costs"] for item in population], axis=0)
        improved = np.any(next_ideal < ideal - 1e-10 * np.maximum(1, np.abs(ideal)))
        stagnant = 0 if improved else stagnant + 1
        ideal = np.minimum(ideal, next_ideal)
        if stagnant >= config["stall"]:
            break
    cancelled = cancel is not None and cancel.is_set()
    feasible_front = []
    for item in front:
        if item["feasible"] and not any(
                np.allclose(item["costs"], existing["costs"], rtol=1e-9, atol=1e-12)
                for existing in feasible_front):
            feasible_front.append(item)
    attained_front = [
        item for item in feasible_front
        if all(_attained(score, {**config, **goal})
               for score, goal in zip(item["scores"], objectives))]
    selected = _representative(attained_front or feasible_front) if feasible_front else representative
    first = selected["scores"][0] if selected["scores"] else None
    attained = bool(attained_front)
    success = attained and not cancelled
    reason = ("用户已停止优化。" if cancelled else
              "无法实现：在当前参数范围和搜索代数内没有满足全部限制的帕累托解。" if not feasible_front else
              "无法实现：在当前搜索预算内未达到全部目标期望值；已保留当前帕累托前沿。" if not attained else
              f"找到 {len(feasible_front)} 个帕累托候选，可在右侧点击选取。")
    if not feasible_front and solver_error:
        reason += " 最近求解提示：" + solver_error
    pareto = [{"parameters": item["parameters"],
               "objectives": [score.value for score in item["scores"]],
               "costs": item["costs"],
               "band": item["scores"][0].band,
               "point_frequency": item["scores"][0].point_frequency}
              for item in feasible_front]
    return {"success": success, "reason": reason, "cancelled": cancelled,
            "best": {"parameters": selected["parameters"],
                     "value": first.value if first else None,
                     "objectives": [score.value for score in selected["scores"]],
                     "band": first.band if first else None,
                     "point_frequency": first.point_frequency if first else None,
                     "feasible": selected["feasible"]},
            "impedance": selected["impedance"], "frequency": frequency,
            "pareto": pareto, "generations": generation,
            "evaluations": evaluations, "history": history,
            "samples": list(samples), "settings": config}
