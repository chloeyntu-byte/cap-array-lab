"""Saved small-signal admittance spectra and observation-frequency evaluation.

Interpolation is linear in frequency on complex admittance, matching Cadence
value() for a sampled waveform. Capacitance/Q are calculated after interpolation.
No interpolation is permitted outside the actually simulated range.
"""
import bisect
import math
from .decomposition import decompose, MEASUREMENT_KEYS


def simulation_grid(settings):
    from .model import number, frequencies
    observations = frequencies(settings.get('frequencies', '1G'))
    start = number(settings.get('simulation_start', min(observations)))
    stop = number(settings.get('simulation_stop', max(observations)))
    if not 0 < start <= stop:
        raise ValueError('Simulation range must satisfy 0 < Start <= Stop.')
    try:
        count = int(settings.get('simulation_points', 301 if start < stop else 1))
    except (ValueError, TypeError):
        raise ValueError('Simulation points must be an integer.')
    if not 1 <= count <= 10001 or (start < stop and count < 2):
        raise ValueError('Use 2–10001 frequency samples, or 1 for a single frequency.')
    mode = str(settings.get('simulation_sweep', 'log')).lower()
    if mode not in ('log', 'linear'):
        raise ValueError('Simulation sweep must be Log or Linear.')
    if any(f < start or f > stop for f in observations):
        raise ValueError('Observation frequencies must be inside the simulation range.')
    if start == stop:
        return [start]
    if mode == 'linear':
        grid = [start + (stop - start) * i / (count - 1) for i in range(count)]
    else:
        ratio = math.log(stop) - math.log(start)
        grid = [start * math.exp(ratio * i / (count - 1)) for i in range(count)]
    grid[0], grid[-1] = start, stop
    return grid


def include_observations(grid, observations):
    """Prefer exact requested points without nearly identical Spectre samples."""
    result = list(grid)
    for freq in observations:
        match = next((idx for idx, value in enumerate(result)
                      if math.isclose(value, freq, rel_tol=1e-12, abs_tol=0)), None)
        if match is None:
            result.append(freq)
        else:
            result[match] = freq
    return sorted(result)


def resample(spectrum, observations):
    """Evaluate saved G/B, preserving code identities and signed C/Q."""
    from .model import cq, frequencies
    if isinstance(observations, str):
        observations = frequencies(observations)
    observations = sorted(set(float(f) for f in observations))
    if not observations or any(not math.isfinite(f) or f <= 0 for f in observations):
        raise ValueError('Enter positive observation frequencies.')
    raw = spectrum.get('rows', []) if isinstance(spectrum, dict) else spectrum
    if not raw:
        raise ValueError('No saved frequency spectrum is available. Run a measurement first.')
    measurements = set(MEASUREMENT_KEYS) - {'point','code'}
    matrix_keys = ['Y{}_{}_S'.format(ij, part) for ij in ('11','12','21','22') for part in ('G','B')]
    groups = {}
    for row in raw:
        identity = tuple(sorted((k, v) for k, v in row.items() if k not in measurements))
        groups.setdefault(identity, []).append(row)
    rows = []
    for identity, samples in groups.items():
        samples = sorted(samples, key=lambda row: row['frequency_Hz'])
        xs = [float(row['frequency_Hz']) for row in samples]
        if len(set(xs)) != len(xs):
            raise ValueError('Saved spectrum contains duplicate frequency samples.')
        if observations[0] < xs[0] or observations[-1] > xs[-1]:
            raise ValueError('Observation frequencies are outside the saved simulation range ({:.6g}–{:.6g} Hz).'.format(xs[0], xs[-1]))
        has_matrix = [all(key in sample for key in matrix_keys) for sample in samples]
        if ((isinstance(spectrum,dict) and spectrum.get('version',1)>=2) or any(any(key in sample for key in matrix_keys) for sample in samples)) and not all(has_matrix):
            raise ValueError('Saved two-port spectrum is incomplete.')
        for freq in observations:
            idx = bisect.bisect_left(xs, freq)
            if idx < len(xs) and math.isclose(xs[idx], freq, rel_tol=1e-13, abs_tol=0):
                sample = samples[idx]
                g, b = sample['G_S'], sample['B_S']
                matrix = {key: sample[key] for key in matrix_keys} if all(has_matrix) else None
            else:
                a, z = samples[idx-1], samples[idx]
                fraction = (freq - xs[idx-1]) / (xs[idx] - xs[idx-1])
                g = a['G_S'] + fraction * (z['G_S'] - a['G_S'])
                b = a['B_S'] + fraction * (z['B_S'] - a['B_S'])
                matrix = {key: a[key] + fraction * (z[key] - a[key]) for key in matrix_keys} if all(has_matrix) else None
            if not all(math.isfinite(v) for v in (g, b)):
                raise ValueError('Saved admittance contains a non-finite value.')
            c, q, status = cq(complex(g, b), freq)
            row = dict(identity)
            row.update(frequency_Hz=freq, C_F=c, Q=q, G_S=g, B_S=b, status=status)
            if matrix is not None:
                if not all(math.isfinite(value) for value in matrix.values()):
                    raise ValueError('Saved two-port spectrum contains a non-finite value.')
                parts = [complex(matrix['Y{}_G_S'.format(ij)], matrix['Y{}_B_S'.format(ij)]) for ij in ('11','12','21','22')]
                split = decompose(*(parts + [freq]))
                if split.get('total') is None:
                    raise ValueError(split.get('message') or 'Floating P/N total is undefined at this frequency.')
                # Derive after interpolation. Interpolating previously reduced
                # total/capacitances would not preserve the network definition.
                row.update({key:split['total'][key] for key in ('C_F','Q','G_S','B_S','status')});row['decomposition']=split
            rows.append(row)
    key = 'point' if 'point' in rows[0] else 'code'
    return sorted(rows, key=lambda row: (row['frequency_Hz'], row[key]))
