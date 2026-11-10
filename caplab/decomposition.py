"""Reciprocal pi decomposition referred to the floating P/N measurement port.

All quantities are frequency-dependent parallel equivalents, with the existing
bias terminals held at their small-signal reference conditions. The ground-path
contribution is a complex series combination, not the sum of two shunt C values.
Only capacitances add. Quality factors are calculated from each complex Y.
"""
import math
import sys

MATRIX_REL_TOL = 1e-10
MATRIX_ROUNDOFF_REL_TOL = 64 * sys.float_info.epsilon
RECIPROCITY_REL_TOL = 1e-6
RECONSTRUCTION_REL_TOL = 1e-6
# Separate model-fit policy, not a relaxation of strict reciprocity validity.
# A fit is labelled approximate even when the resulting total agrees closely:
# matrix nonreciprocity <=0.01% and floating complex-Y error <=1 ppm.
APPROXIMATE_RECIPROCITY_REL_TOL = 1e-4
APPROXIMATE_RECONSTRUCTION_REL_TOL = 1e-6
RAW_Y_KEYS = frozenset('Y{}_{}_S'.format(pair, part)
                       for pair in ('11', '12', '21', '22') for part in ('G', 'B'))
MEASUREMENT_KEYS = frozenset(('point', 'code', 'frequency_Hz', 'C_F', 'Q',
                              'G_S', 'B_S', 'status', 'Q_signed', 'decomposition')) | RAW_Y_KEYS


def result_names(row):
    """Return decimal-register fields without interpreting nested measurements."""
    return [name for name in row if name not in MEASUREMENT_KEYS]


def _finite(y):
    return math.isfinite(y.real) and math.isfinite(y.imag)


def _metric(y, omega):
    if not _finite(y):
        return None
    capacitance = y.imag / omega
    if not math.isfinite(capacitance):
        return None
    tolerance = max(1e-30, max(abs(y.real), abs(y.imag)) * 1e-14)
    quality = None
    signed_quality = y.imag / y.real if abs(y.real) > tolerance else None
    if signed_quality is not None and not math.isfinite(signed_quality):
        signed_quality = None
    if abs(y) == 0:
        status = 'Zero admittance; Q undefined'
    elif y.real < -tolerance:
        status = 'Negative effective conductance; passive Q undefined'
    else:
        status = 'Capacitive' if y.imag > 0 else 'Non-capacitive'
        if y.real > tolerance:
            quality = y.imag / y.real
            if not math.isfinite(quality):
                quality = None
        elif y.imag > 0:
            status = 'Lossless / conductance below resolution'
    return dict(C_F=capacitance, Q=quality, Q_signed=signed_quality, G_S=y.real, B_S=y.imag, status=status)


def _floating(a, b, c, d, scale):
    """Modal Schur complement, avoiding cancellation in det(Y)/sum(Y).

Inputs are normalized; scale is applied only after elimination. All four
row/column sums are checked before accepting the genuinely floating special
case. A tiny but resolved ground return is retained.
"""
    rows = (a + b, c + d, a + c, b + d)
    if max(abs(value) for value in rows) <= MATRIX_ROUNDOFF_REL_TOL:
        return (a - b - c + d) * (scale / 4), 'No resolved AC-ground return'
    ycc = a + b + c + d
    ydc = (a + b - c - d) / 2
    ycd = (a + c - b - d) / 2
    ydd = (a - b - c + d) / 4
    common_scale = max(abs(value) for value in rows)
    if abs(ycc) <= MATRIX_REL_TOL * common_scale:
        return None, 'Singular floating common mode / cancelling ground return'
    value = (ydd - (ydc / ycc) * ycd) * scale
    return (value, '') if _finite(value) else (None, 'Floating-port admittance exceeds numerical range')


def _approximate_pi(a, b, c, d, scale, omega, exact, reciprocity):
    """Auditable symmetric projection for a weakly nonreciprocal terminal Y.

The projection minimizes the matrix Frobenius error among reciprocal matrices.
It is not an exact physical three-branch identification. For delta=(b-c)/2,
Yexact=Yfit+delta**2/sum(Y); the correction is retained independently and never
silently labelled a ground shunt. Loss cancellation can amplify relative G/Q
errors, so those diagnostics remain available separately from complex-Y error.
"""
    if reciprocity > APPROXIMATE_RECIPROCITY_REL_TOL:
        return None
    mutual = -(b + c) / 2
    pg, ng = a - mutual, d - mutual
    denominator = pg + ng
    if abs(denominator) <= MATRIX_REL_TOL * max(abs(pg), abs(ng)):
        return None
    ground = (pg / denominator) * ng
    fitted = (mutual + ground) * scale
    if not _finite(fitted):
        return None
    residual = abs(fitted - exact) / max(abs(exact), scale * MATRIX_ROUNDOFF_REL_TOL)
    if not math.isfinite(residual) or residual > APPROXIMATE_RECONSTRUCTION_REL_TOL:
        return None
    values = tuple(_metric(y * scale, omega) for y in (mutual, ground, mutual + ground, pg, ng))
    correction = _metric(exact - fitted, omega)
    original = _metric(exact, omega)
    if any(value is None for value in values) or correction is None or original is None:
        return None
    coupling, ground_metric, total, p_ground, n_ground = values
    c_residual = coupling['C_F'] + ground_metric['C_F'] - original['C_F']
    g_residual = total['G_S'] - original['G_S']
    g_relative = abs(g_residual) / max(abs(original['G_S']), scale * MATRIX_ROUNDOFF_REL_TOL)
    c_relative = abs(c_residual) / max(abs(original['C_F']), scale * MATRIX_ROUNDOFF_REL_TOL / omega)
    matrix_error = abs(b - c) / math.sqrt(2 * sum(abs(y)**2 for y in (a,b,c,d)))
    if not all(math.isfinite(value) for value in (c_residual, g_residual, g_relative, c_relative, matrix_error)):
        return None
    return dict(method='Symmetric reciprocal pi fit', coupling=coupling,
                ground=ground_metric, total=total, p_ground=p_ground,
                n_ground=n_ground, correction=correction,
                reconstruction_error=residual, matrix_relative_error=matrix_error,
                c_sum_residual_F=c_residual, c_reconstruction_relative_error=c_relative,
                g_sum_residual_S=g_residual, g_reconstruction_relative_error=g_relative,
                policy=dict(max_reciprocity_error=APPROXIMATE_RECIPROCITY_REL_TOL,
                            max_reconstruction_error=APPROXIMATE_RECONSTRUCTION_REL_TOL),
                message='Approximate terminal equivalent; exact floating total retained; not a physical capacitor inventory')


def decompose(y11, y12, y21, y22, frequency):
    """Return JSON-safe C/Q metrics, or an explicit unavailable decomposition.

For a reciprocal matrix, Ymutual=-Y12=-Y21. The two shunt admittances
combine in series for a floating P/N source. Small reciprocity roundoff is
symmetrized consistently; a general nonreciprocal floating total is retained
when determined, while the three-branch physical decomposition is withheld.
"""
    result = dict(schema=1, valid=False, message='', reciprocity_error=0.0,
                  coupling=None, ground=None, total=None, p_ground=None,
                  n_ground=None, p_input=None, n_input=None, approximate=None, flags=[])
    try:
        values = tuple(complex(value) for value in (y11, y12, y21, y22))
        frequency = float(frequency)
        omega = 2 * math.pi * frequency
    except (TypeError, ValueError, OverflowError):
        result.update(message='Invalid matrix or frequency input', flags=['invalid_input'])
        return result
    if not math.isfinite(frequency) or frequency <= 0 or not math.isfinite(omega) or omega <= 0 or not all(_finite(y) for y in values):
        result.update(message='Matrix entries and positive frequency must be finite', flags=['invalid_input'])
        return result
    # Standard short-circuit input admittances, distinct from pi shunts.
    result['p_input'] = _metric(values[0], omega)
    result['n_input'] = _metric(values[3], omega)
    # The largest component is used instead of abs(complex) to avoid overflow
    # for finite real/imaginary inputs near the floating-point limit.
    scale = max(abs(part) for value in values for part in (value.real, value.imag))
    if scale == 0:
        metrics = [_metric(0j, omega) for _ in range(5)]
        result.update(valid=True, message='Zero network; no AC-ground return',
                      coupling=metrics[0], ground=metrics[1], total=metrics[2],
                      p_ground=metrics[3], n_ground=metrics[4],
                      reconstruction_error=0.0, additivity_error_F=0.0,
                      flags=['no_resolved_ground_return'])
        return result
    a, b, c, d = (value / scale for value in values)
    matrix_norm = max(abs(value) for value in (a, b, c, d))
    reciprocity = abs(b - c) / matrix_norm
    result['reciprocity_error'] = reciprocity
    try:
        exact, reason = _floating(a, b, c, d, scale)
        result['total'] = _metric(exact, omega) if exact is not None else None
        if exact is not None and result['total'] is None:
            reason = 'Equivalent capacitance exceeds numerical range'
        if reciprocity > RECIPROCITY_REL_TOL:
            result.update(message='Non-reciprocal matrix; physical three-branch pi decomposition unavailable',
                          flags=['non_reciprocal'])
            if reason:result['message'] += '; ' + reason
            if exact is not None and result['total'] is not None:
                result['approximate'] = _approximate_pi(a, b, c, d, scale, omega, exact, reciprocity)
                if result['approximate'] is not None:
                    result['flags'].append('approximate_reciprocal_pi_fit')
                    result['message'] += '; labelled approximate reciprocal fit available'
            return result
        if result['total'] is None:
            result.update(message=reason, flags=['singular_or_unresolved_common_mode'])
            return result
        # Symmetrizing the off-diagonals requires matching shunts. Mixing an
        # averaged mutual Y with unsymmetrized row sums is inconsistent.
        mutual = -(b + c) / 2
        pg, ng = a - mutual, d - mutual
        rows = (a + b, c + d, a + c, b + d)
        if max(abs(value) for value in rows) <= MATRIX_ROUNDOFF_REL_TOL:
            ground = 0j
            result['flags'].append('no_resolved_ground_return')
        else:
            denominator = pg + ng
            if abs(denominator) <= MATRIX_REL_TOL * max(abs(pg), abs(ng)):
                result.update(message='Singular pi ground path / cancelling shunts',
                              flags=['singular_ground_path'])
                return result
            ground = (pg / denominator) * ng
        reconstructed = (mutual + ground) * scale
        residual = abs(reconstructed - exact) / max(abs(exact), scale * MATRIX_ROUNDOFF_REL_TOL)
        if not math.isfinite(residual) or residual > RECONSTRUCTION_REL_TOL:
            result.update(message='Pi reconstruction is ill-conditioned; physical decomposition unavailable',
                          flags=['ill_conditioned_reconstruction'])
            return result
        metrics = tuple(_metric(value * scale, omega) for value in (mutual, ground, mutual + ground, pg, ng))
        if any(metric is None for metric in metrics):
            result.update(message='Equivalent component exceeds numerical range', flags=['numerical_range'])
            return result
        coupling, ground_metric, fitted_total, p_ground, n_ground = metrics
        total = result['total']  # Preserve the exact general-matrix floating total.
        result.update(valid=True, message='Reciprocal pi equivalent; floating P/N common mode; C adds, Q does not',
                      coupling=coupling, ground=ground_metric, total=total,
                      p_ground=p_ground, n_ground=n_ground,
                      reconstruction_error=residual,
                      additivity_error_F=coupling['C_F'] + ground_metric['C_F'] - total['C_F'])
        if reciprocity > 0:
            result['flags'].append('roundoff_symmetrized')
        if any(metric['G_S'] < -max(1e-30, max(abs(metric['G_S']), abs(metric['B_S'])) * 1e-14) for metric in metrics):
            result['flags'].append('negative_effective_conductance')
        return result
    except (ArithmeticError, ValueError):
        result.update(valid=False, message='Numerical failure in floating-port decomposition',
                      coupling=None, ground=None, p_ground=None, n_ground=None,
                      flags=['numerical_range'])
        return result
