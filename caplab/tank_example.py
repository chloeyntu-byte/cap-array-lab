"""Synthetic tank-scan example: analytic LC tank, switched array and cross-coupled gm.

No native tools, files or device models are used.  The records follow the native
module-current format (two raw excitations, every module terminal probed,
50-ohm ground-referenced ports), so the example exercises the full analysis.

Run ``python -m caplab.tank_example`` to regenerate examples/ideal_tank.json.
"""
import json
import math
from pathlib import Path

L_HALF, R_SER = 0.5e-9, 1.2          # each inductor half: R + jwL, centre tap at AC ground
C_UNIT, G_ARR = 0.25e-12, 0.4e-3     # array: C = C_UNIT * (2 + code), differential loss
GM, GDS, CG = 20e-3, 1.5e-3, 0.15e-12  # per side; Cg is each gate to ground
CODES = (0, 1, 2, 3)


def module_currents(f, vp, vn, code=2, gm=GM):
    """Instance -> [(pin, net, voltage, current INTO the terminal)]."""
    w = 2 * math.pi * f
    yl = 1 / (R_SER + 1j * w * L_HALF)
    ya = G_ARR + 1j * w * C_UNIT * (2 + code)
    return {
        'IND': [('A', 'CAL_P', vp, yl * vp), ('B', 'CAL_N', vn, yl * vn), ('CT', 'CAL_VDD', 0j, -yl * (vp + vn))],
        'ARR': [('P', 'CAL_P', vp, ya * (vp - vn)), ('N', 'CAL_N', vn, ya * (vn - vp)), ('VSS', 'CAL_VSS', 0j, 0j)],
        'GM': [('D1', 'CAL_P', vp, gm * vn + GDS * vp), ('D2', 'CAL_N', vn, gm * vp + GDS * vn),
               ('G1', 'CAL_N', vn, 1j * w * CG * vn), ('G2', 'CAL_P', vp, 1j * w * CG * vp),
               ('S', 'CAL_VSS', 0j, -(gm * vn + GDS * vp) - (gm * vp + GDS * vn) - 1j * w * CG * (vp + vn))],
    }


def dut_matrix(f, code=2, gm=GM, currents=module_currents):
    cols = []
    for vp, vn in ((1, 0), (0, 1)):
        cur = currents(f, vp, vn, code, gm)
        ip = sum(i for pins in cur.values() for _, net, _, i in pins if net == 'CAL_P')
        inn = sum(i for pins in cur.values() for _, net, _, i in pins if net == 'CAL_N')
        cols.append((ip, inn))
    return [[cols[0][0], cols[1][0]], [cols[0][1], cols[1][1]]]


def _solve(y, jp, jn):
    a = [[y[0][0] + 1 / 50, y[0][1]], [y[1][0], y[1][1] + 1 / 50]]
    det = a[0][0] * a[1][1] - a[0][1] * a[1][0]
    return ((jp * a[1][1] - a[0][1] * jn) / det, (a[0][0] * jn - a[1][0] * jp) / det)


def records(freqs, code=2, gm=GM, currents=module_currents):
    """(loss plan, native-format module records, two-port spectrum rows)."""
    probes, order = [], []
    for inst, pins in currents(1e9, 1, 0, code, gm).items():
        for pin, net, _, _ in pins:
            probes.append({'instance': inst, 'pin': pin, 'net': net})
            order.append((inst, pin))
    plan = {'probes': probes, 'port_fixture': 'ground_50ohm_each',
            'instances': [{'id': 'IND', 'kind': None, 'cell': 'ind'}, {'id': 'ARR', 'kind': None, 'cell': 'array'},
                          {'id': 'GM', 'kind': None, 'cell': 'pair'}]}
    loss, spectrum = [], []
    for f in freqs:
        y = dut_matrix(f, code, gm, currents)
        item = {'frequency_Hz': f}
        for tag, (jp, jn) in (('', (1, -1)), ('cm_', (1, 0))):
            vp, vn = _solve(y, jp, jn)
            cur = currents(f, vp, vn, code, gm)
            pins = {}
            for idx, (inst, pin) in enumerate(order):
                _, _, v, i = next(x for x in cur[inst] if x[0] == pin)
                pins[str(idx)] = [v.real, v.imag, i.real, i.imag]
            item[tag + 'input'] = [vp.real, vp.imag, vn.real, vn.imag]
            item[tag + 'pins'] = pins
        loss.append(item)
        row = {'code': code, 'frequency_Hz': f}
        for (i, j), key in (((0, 0), '11'), ((0, 1), '12'), ((1, 0), '21'), ((1, 1), '22')):
            row['Y{}_G_S'.format(key)] = y[i][j].real
            row['Y{}_B_S'.format(key)] = y[i][j].imag
        yd = (y[0][0] - y[0][1] - y[1][0] + y[1][1]) / 4
        row.update(G_S=yd.real, B_S=yd.imag)
        spectrum.append(row)
    return plan, loss, spectrum


def analytic(f, code=2):
    w = 2 * math.pi * f
    yl = 1 / (R_SER + 1j * w * L_HALF)
    return {'ind': yl / 2, 'arr': G_ARR + 1j * w * C_UNIT * (2 + code), 'gm': (GDS - GM) / 2 + 1j * w * CG / 2}


def exact_f0(code=2, gm_present=True):
    lo, hi = 1e9, 50e9

    def im(f):
        a = analytic(f, code)
        return (a['ind'] + a['arr'] + (a['gm'] if gm_present else 0)).imag
    for _ in range(200):
        mid = (lo + hi) / 2
        if (im(lo) < 0) == (im(mid) < 0):
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _symbol(cell, pins):
    n = len(pins)
    h = max(1.0, 0.5 * n)
    return {'library': 'GENERIC', 'cell': cell, 'bbox': [-1.6, -h, 1.6, h],
            'pins': [{'name': name, 'xy': [-1.6 if k % 2 == 0 else 1.6, h - (k // 2 + 1) * 2 * h / ((n + 1) // 2 + 1)]}
                     for k, name in enumerate(pins)],
            'shapes': [{'kind': 'rect', 'points': [-1.6, -h, 1.6, h]}]}


def make_workbook():
    from .model import new_project
    from .registers import add_register
    p = new_project()
    p.update(workspace='', library='GENERIC', reference={'library': 'GENERIC', 'cell': 'ideal_tank'})
    p['settings'].update(frequencies='3G', simulation_start='2G', simulation_stop='8G', simulation_points='201',
                         simulation_sweep='linear', capture_loss=True, decomposition=True, tank_gm_ref='10M')
    layout = (('IND', 'Ideal centre-tapped L', ['A', 'B', 'CT'], {'A': 'P', 'B': 'N', 'CT': 'VDD'}, 350),
              ('ARR', 'Ideal switched C', ['P', 'N', 'VSS', 'BANK<1:0>'],
               {'P': 'P', 'N': 'N', 'VSS': 'VSS', 'BANK<1>': 'BANK<1>', 'BANK<0>': 'BANK<0>'}, 850),
              ('GM', 'Ideal cross-coupled gm', ['D1', 'D2', 'G1', 'G2', 'S'],
               {'D1': 'P', 'D2': 'N', 'G1': 'N', 'G2': 'P', 'S': 'VSS'}, 1350))
    for ident, cell, pins, roles, x in layout:
        p['instances'].append({'id': ident, 'library': 'GENERIC', 'cell': cell, 'view': 'schematic', 'x': x, 'y': 300,
                               'symbol': _symbol(cell, pins), 'roles': roles})
    add_register(p, 'BANK', 2, '0:3', x=850, y=750)
    grid = [10e6] + [2e9 + 6e9 * i / 200 for i in range(201)]
    loss, rows = {}, []
    plan = None
    for code in CODES:
        plan, samples, spectrum = records(grid, code)
        for row in spectrum:
            row['point'] = code
            row['BANK'] = code
            row.pop('code')
        loss[str(code)] = samples
        rows += spectrum
    p['private'] = {'spectrum': {'version': 2, 'rows': rows}, 'loss_spectrum': loss, 'loss_plan': plan}
    page = {'id': 'ideal_tank', 'name': 'Ideal tank scan', 'project': p, 'result': None,
            'measurement_view': 'Tank scan', 'editor': {'tab': 'results'},
            'tank': {'roles': {'IND': 'inductor', 'ARR': 'array', 'GM': 'gm'},
                     'contains': {'inductor': True, 'array': True, 'gm': True}, 'gm_ref': '10M'}}
    return {'kind': 'caplab-workbook', 'version': 3, 'active_page': page['id'], 'pages': [page],
            'appearance': {'canvas': 'black', 'plot': 'black'},
            'description': 'Synthetic tank scan: analytic centre-tapped L (0.5 nH + 1.2 ohm per half), '
                           'switched C (0.25 pF x (2 + code), 0.4 mS) and a cross-coupled pair '
                           '(gm 20 mS, gds 1.5 mS, Cg 0.15 pF per side). No device models.'}


def _rounded(value):
    if isinstance(value, float):
        return float('{:.12g}'.format(value))
    if isinstance(value, list):
        return [_rounded(v) for v in value]
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items()}
    return value


def main():
    from .model import ROOT
    path = Path(ROOT) / 'examples' / 'ideal_tank.json'
    path.write_text(json.dumps(_rounded(make_workbook()), sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
    print(path)


if __name__ == '__main__':
    main()
