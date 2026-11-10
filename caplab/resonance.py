"""First zero of the imaginary floating P/N impedance of a saved Y sweep.

Y is linearly interpolated between native frequency samples, then the floating
common mode is eliminated. The result is an estimate, not a newly simulated
frequency. A zero of admittance (an impedance pole) is not a resonance here.
"""
import math
from .decomposition import decompose

_KEYS = ['Y{}_{}_S'.format(pair, part) for pair in ('11', '12', '21', '22') for part in ('G', 'B')]


def _impedance(freq, values):
    matrix = [complex(values[i], values[i + 1]) for i in range(0, 8, 2)]
    result = decompose(*(matrix + [freq]))
    total = result.get('total')
    if total is None:
        return None
    y = complex(total['G_S'], total['B_S'])
    if not abs(y):
        return None
    z = 1 / y
    return z if math.isfinite(z.real) and math.isfinite(z.imag) else None


def find_first_resonance(samples):
    """Return first bracketed Im(Zdiff)=0 crossing, or an unavailable result.

    samples belong to ONE register point and contain all eight Y matrix fields.
    `kind` describes increasing-frequency slope of Im(Z), not a claim that a
    complex multi-resonator network is a simple lumped series/parallel circuit.
    """
    rows = sorted(samples, key=lambda r: float(r['frequency_Hz']))
    if len(rows) < 2:
        return {'available': False, 'reason': 'At least two sweep samples are required.'}
    xs, values = [], []
    for row in rows:
        f = float(row['frequency_Hz'])
        v = [float(row[k]) for k in _KEYS]
        if not math.isfinite(f) or f <= 0 or not all(math.isfinite(x) for x in v):
            raise ValueError('Frequency and full Y matrix must be finite; frequency must be positive.')
        if xs and f <= xs[-1]:
            raise ValueError('Sweep frequency samples must be unique.')
        xs.append(f)
        values.append(v)
    zs = [_impedance(f, v) for f, v in zip(xs, values)]
    for i in range(len(xs) - 1):
        za, zb = zs[i], zs[i+1]
        if za is None or zb is None:
            continue
        # A run of exactly zero imaginary values does not identify an isolated crossing.
        if za.imag == 0 and zb.imag == 0:
            continue
        if za.imag * zb.imag > 0:
            continue
        left, right = 0.0, 1.0
        z = za
        for unused in range(60):
            t = (left + right) / 2
            f = xs[i] + t * (xs[i+1] - xs[i])
            v = [a + t * (b-a) for a, b in zip(values[i], values[i+1])]
            z = _impedance(f, v)
            if z is None:
                break
            if za.imag == 0:
                t, z = 0.0, za
                break
            if zb.imag == 0:
                t, z = 1.0, zb
                break
            if z.imag == 0:
                break
            if (z.imag > 0) == (za.imag > 0):
                left = t
            else:
                right = t
        if z is None:
            continue
        # A sign flip through an unresolved pole cannot be reported as a zero.
        scale = max(abs(za), abs(zb), abs(z), 1e-300)
        if abs(z.imag) > 1e-8 * scale:
            continue
        return {'available': True, 'frequency_Hz': xs[i] + t * (xs[i+1] - xs[i]),
                'Z_real_ohm': z.real, 'Z_imag_ohm': z.imag,
                'kind': 'series-like' if zb.imag > za.imag else 'parallel-like',
                'bracket_Hz': [xs[i], xs[i+1]], 'estimated': True,
                'method': 'linear interpolation of complex Y matrix'}
    return {'available': False, 'reason': 'No resolved impedance zero crossing in the simulated range.'}
