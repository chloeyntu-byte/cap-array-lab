import copy
import json
import math
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = os.environ.get('CAPLAB_WORKSPACE', '')


def number(text):
    text = str(text).strip()
    m = re.fullmatch(r'([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(meg|[TGMKkmunpf]?)\s*(?:Hz|V)?', text)
    if not m:
        raise ValueError('Invalid number: ' + text)
    scales = {'': 1, 'T': 1e12, 'G': 1e9, 'M': 1e6, 'meg': 1e6, 'K': 1e3, 'k': 1e3,
              'm': 1e-3, 'u': 1e-6, 'n': 1e-9, 'p': 1e-12, 'f': 1e-15}
    value = float(m[1]) * scales[m[2]]
    if not math.isfinite(value):
        raise ValueError('The number must be finite')
    return value


def frequencies(text):
    values = sorted(set(number(v) for v in re.split(r'[,，;；\s]+', text.strip()) if v))
    if not values or len(values) > 32 or any(v <= 0 for v in values):
        raise ValueError('Enter 1–32 positive observation frequencies, e.g. 1G, 2.4G, 5G')
    return values


def codes(text, bits):
    if not 1 <= bits <= 32:
        raise ValueError('Register width must be 1–32 bits')
    result = []
    for token in re.split(r'[,，;；\s]+', text.strip()):
        if not token:
            continue
        if ':' in token:
            parts = [int(x) for x in token.split(':')]
            if len(parts) not in (2, 3):
                raise ValueError('Use start:stop[:step], with an inclusive stop')
            start, stop = parts[:2]
            step = parts[2] if len(parts) == 3 else (1 if stop >= start else -1)
            if step == 0 or (stop - start) * step < 0:
                raise ValueError('Code range and step have inconsistent directions')
            count = abs(stop - start) // abs(step) + 1
            if count > 4096:
                raise ValueError('At most 4096 codes are supported per run')
            result.extend(range(start, stop + (1 if step > 0 else -1), step))
        else:
            result.append(int(token, 10))
        if len(result) > 4096:
            raise ValueError('At most 4096 codes are supported per run')
    result = list(dict.fromkeys(result))
    if not result or any(v < 0 or v >= 2 ** bits for v in result):
        raise ValueError('Codes must be in the range 0–{}'.format(2 ** bits - 1))
    return result


def expand_pin(name):
    m = re.fullmatch(r'(.+)<(\d+):(\d+)>', name)
    if not m:
        return [name]
    a, b = int(m[2]), int(m[3])
    if abs(a - b) > 1023:
        raise ValueError('Bus widths above 1024 bits are unsupported')
    return ['{}<{}>'.format(m[1], i) for i in range(a, b + (1 if b >= a else -1), 1 if b >= a else -1)]


def initial_role(pin):
    name = pin.upper()
    if name in ('P', 'N', 'VDD', 'VSS'):
        return name
    if name in ('GND', 'GND!', 'VSS!'):
        return 'VSS'
    m = re.fullmatch(r'(?:SW|BIN|D|B|CTRL|CT)<(\d+)>', name)
    return 'BIT:{}'.format(int(m[1])) if m else ('BIT:0' if name == 'SW' else '')


def new_project():
    return {'version': 3, 'registers': [], 'workspace': DEFAULT_WORKSPACE, 'library': os.environ.get('CAPLAB_LIBRARY', 'CAP_DEMO'),
            'reference': {'library': os.environ.get('CAPLAB_LIBRARY', 'CAP_DEMO'), 'cell': 'TB_C'},
            'settings': {'frequencies': '1G, 2.4G, 5G', 'bits': 4, 'vdd': '1',
                         'vcm': '0.5', 'codes': '0:15', 'temperature': '27',
                         'analysis': 'sp', 'decomposition': True, 'simulation_start': '100M', 'simulation_stop': '50G',
                         'simulation_points': 301, 'simulation_sweep': 'log'},
            'instances': [], 'edges': [], 'private': None}


def add_instance(project, symbol, x=350, y=240):
    used = {i['id'] for i in project['instances']}
    n = 0
    while 'I{}'.format(n) in used:
        n += 1
    inst = {'id': 'I{}'.format(n), 'library': symbol['library'], 'cell': symbol['cell'],
            'view': 'schematic', 'x': x, 'y': y, 'symbol': copy.deepcopy(symbol), 'roles': {}}
    for pin in symbol['pins']:
        for scalar in expand_pin(pin['name']):
            role = initial_role(scalar)
            if project.get('version',1)>=2 and (role.startswith('BIT:') or (project['instances'] and role in ('P','N'))):
                role = ''
            inst['roles'][scalar] = role
    project['instances'].append(inst)
    project['private'] = None
    return inst


def validate(project):
    if project.get('version',1)>=2:
        from .registers import validate_explicit
        return simulation_config(validate_explicit(project), project['settings'])
    s = project['settings']
    bits = int(s['bits'])
    fs = frequencies(s['frequencies'])
    cs = codes(s['codes'], bits)
    vdd, vcm, temp = number(s['vdd']), number(s['vcm']), number(s['temperature'])
    if vdd <= 0 or vdd > 100:
        raise ValueError('VDD must be greater than 0 V and no greater than 100 V')
    if not project['instances']:
        raise ValueError('Place an array symbol on the canvas first')
    nodes, roles, instance_ids = {}, {}, set()
    for inst in project['instances']:
        if not re.fullmatch(r'I\d+', inst['id']) or inst['id'] in instance_ids:
            raise ValueError('Invalid or duplicate instance identifier')
        instance_ids.add(inst['id'])
        for pin in inst['symbol']['pins']:
            for scalar in expand_pin(pin['name']):
                key = inst['id'] + '.' + scalar
                nodes[key] = key
                roles[key] = inst['roles'].get(scalar, '').strip()
    def find(k):
        while nodes[k] != k:
            nodes[k] = nodes[nodes[k]]
            k = nodes[k]
        return k
    for a, b in project['edges']:
        if a not in nodes or b not in nodes:
            raise ValueError('Connection references a missing pin')
        nodes[find(b)] = find(a)
    groups = {}
    for k in nodes:
        groups.setdefault(find(k), []).append(k)
    netmap, seen, fixed = {}, set(), {}
    for idx, members in enumerate(groups.values()):
        rr = set(roles[k] for k in members if roles[k])
        if len(rr) > 1:
            raise ValueError('Different roles are shorted: ' + ', '.join(members))
        role = next(iter(rr), '')
        if not role and len(members) == 1:
            raise ValueError('Pin has no role or connection: ' + members[0])
        if role.startswith('BIT:'):
            bit = int(role[4:])
            if not 0 <= bit < bits:
                raise ValueError('Register bit is outside the configured width: ' + role)
            net = 'CAL_B<{}>'.format(bit)
        elif role.startswith('DC:'):
            voltage = number(role[3:])
            net = 'CAL_DC{}'.format(len(fixed))
            fixed[net] = voltage
        elif role in ('P', 'N', 'VDD', 'VSS'):
            net = 'CAL_' + role
        elif not role:
            net = 'CAL_W{}'.format(idx)
        else:
            raise ValueError('Unknown pin role: ' + role)
        seen.add(role)
        for k in members:
            netmap[k] = net
    if not {'P', 'N'}.issubset(seen):
        raise ValueError('Assign both differential measurement terminals P and N')
    assigned = {int(r[4:]) for r in seen if r.startswith('BIT:')}
    if assigned != set(range(bits)):
        raise ValueError('Assign BIT:0 through BIT:{}; bit 0 is the LSB'.format(bits - 1))
    return simulation_config({'frequencies': fs, 'codes': cs, 'bits': bits, 'vdd': vdd, 'vcm': vcm,
            'temperature': temp, 'nets': netmap, 'fixed': fixed}, s)


def simulation_config(cfg, settings):
    """Validate the simulated range independently from editable observations."""
    from .spectrum import simulation_grid
    grid = simulation_grid(settings)
    if any(f < grid[0] * (1 - 1e-12) or f > grid[-1] * (1 + 1e-12) for f in cfg['frequencies']):
        raise ValueError('Observation frequencies must lie within the simulated frequency range')
    cfg.update(simulation_frequencies=grid, simulation_start=grid[0], simulation_stop=grid[-1],
               simulation_points=int(settings.get('simulation_points', 301)),
               simulation_sweep=settings.get('simulation_sweep', 'log'),
               analysis=settings.get('analysis', 'sp'), decomposition=bool(settings.get('decomposition',False)))
    return cfg


def save_project(path, project):
    path = Path(path)
    if path.exists():
        shutil.copy2(str(path), str(path) + datetime.now().strftime('.bak-%Y%m%d-%H%M%S'))
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def load_project(path):
    p = json.loads(Path(path).read_text(encoding='utf-8'))
    if p.get('kind')=='caplab-workbook' and p.get('version')==3:
        from .workbook import workbook
        return workbook(p)
    if p.get('version') not in (1,2,3) or not isinstance(p.get('instances'), list):
        raise ValueError('Unsupported Cap Array Lab project')
    return p


def cq(y, freq):
    c = y.imag / (2 * math.pi * freq)
    q = None if abs(y.real) < 1e-30 else y.imag / abs(y.real)
    status = 'Capacitive' if c > 0 else 'Non-capacitive'
    if y.real < -1e-15:
        status = 'Negative conductance'
    return c, q, status
