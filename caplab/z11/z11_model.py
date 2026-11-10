"""Impedance seen by PORT1 in the supplied LC schematic."""


import numpy as np


PARTS = ("L14", "C4", "L12", "C2", "C3")


def z11(frequency_ghz: 'np.ndarray', values: 'dict[str, float]', q: 'dict[str, float]') -> 'np.ndarray':
    """Return Z11 in ohms for the schematic's five lossy parts.

    L14 || C4 is in series with L12 || C2; C3 shunts both sections.
    Constant Q is interpreted at each sampled frequency: an inductor has
    series R = omega*L/Q and a capacitor has series ESR = 1/(omega*C*Q).
    Infinite Q makes a part ideal. The port's 50-ohm reference is excluded.
    """
    f = np.asarray(frequency_ghz, dtype=float)
    if f.size == 0 or not np.all(np.isfinite(f)) or np.any(f <= 0):
        raise ValueError("频率必须是正数（GHz）。")
    for part in PARTS:
        value = values[part]
        quality = q[part]
        if not np.isfinite(value) or value <= 0:
            raise ValueError(f"{part} 的 L/C 必须是正数。")
        if np.isnan(quality) or quality <= 0:
            raise ValueError(f"{part} 的 Q 必须是正数，或填写 inf 表示理想器件。")

    omega = 2 * np.pi * f * 1e9

    def impedance(part: 'str') -> 'np.ndarray':
        x = omega * values[part] * 1e-12 if part.startswith("L") else 1 / (omega * values[part] * 1e-15)
        resistance = x / q[part]
        return resistance + 1j * x if part.startswith("L") else resistance - 1j * x

    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        upper = 1 / (1 / impedance("L14") + 1 / impedance("C4"))
        lower = 1 / (1 / impedance("L12") + 1 / impedance("C2"))
        result = 1 / (1 / impedance("C3") + 1 / (upper + lower))
    return result
