"""Strict, dependency-free Touchstone full-matrix metadata reader."""
import hashlib
import math
import re
from pathlib import Path


def inspect(path):
    path = Path(path).expanduser().resolve()
    raw = path.read_bytes()
    try:
        content = raw.decode('ascii')
    except UnicodeDecodeError:
        raise ValueError('Touchstone file must be ASCII')
    ext = re.search(r'\.s(\d+)p$', path.name, re.I)
    ext_ports = int(ext.group(1)) if ext else None
    n = ext_ports
    version = '1.0'
    unit, parameter, fmt, ref = 'ghz', 's', 'ma', 50.0
    matrix = 'full'
    freq_count = None
    reference = None
    section = None
    saw_v2 = False
    saw_end = False
    data = []
    for line in content.splitlines():
        line = line.split('!', 1)[0].strip()
        if not line:
            continue
        if line.startswith('#'):
            fields = line[1:].lower().split()
            if len(fields) > 5 or (len(fields) >= 4 and fields[3] != 'r') or len(fields) == 4:
                raise ValueError('Invalid Touchstone option line')
            defaults = ['ghz', 's', 'ma']
            unit, parameter, fmt = (fields + defaults[len(fields):])[:3]
            if len(fields) == 5:
                try: ref = float(fields[4])
                except ValueError: raise ValueError('Invalid reference resistance')
            if unit not in ('hz','khz','mhz','ghz') or parameter != 's' or fmt not in ('ri','ma','db') or not math.isfinite(ref) or ref <= 0:
                raise ValueError('Unsupported Touchstone options')
            continue
        if line.startswith('['):
            match = re.match(r'^\[([^]]+)\]\s*(.*)$', line)
            if not match: raise ValueError('Malformed Touchstone keyword')
            key, value = match.group(1).strip().lower(), match.group(2).strip()
            saw_v2 = True
            if key == 'version':
                version = value
                if version != '2.0': raise ValueError('Unsupported Touchstone version')
            elif key == 'number of ports':
                try: n = int(value)
                except ValueError: raise ValueError('Invalid port count')
                if ext_ports is not None and n != ext_ports: raise ValueError('Port count conflicts with extension')
            elif key == 'number of frequencies':
                try: freq_count = int(value)
                except ValueError: raise ValueError('Invalid frequency count')
            elif key == 'matrix format': matrix = value.lower()
            elif key == 'reference':
                try: reference = [float(x) for x in value.split()]
                except ValueError: raise ValueError('Invalid references')
                section = 'reference'
                continue
            elif key == 'network data': section = 'network'; continue
            elif key == 'end': saw_end = True; section = 'end'; continue
            elif key == 'two-port data order':
                if value.lower() not in ('21_12', '12_21'): raise ValueError('Invalid two-port order')
            else: raise ValueError('Unsupported Touchstone keyword: ' + key)
            section = None
            continue
        if saw_v2 and section == 'reference':
            try: reference.extend(float(x) for x in line.split())
            except ValueError: raise ValueError('Invalid references')
        elif not saw_v2 or section == 'network':
            try: data.extend(float(x) for x in line.split())
            except ValueError: raise ValueError('Invalid network data')
        else: raise ValueError('Unexpected Touchstone content')
    if saw_v2 and (version != '2.0' or not saw_end): raise ValueError('Incomplete Touchstone 2.0')
    if not n or n < 1 or n > 1000: raise ValueError('Explicit valid port count required')
    if matrix != 'full': raise ValueError('Only full matrices are supported')
    if reference is not None and (len(reference) != n or any(not math.isfinite(x) or x <= 0 for x in reference)):
        raise ValueError('Invalid per-port reference values')
    width = 1 + 2*n*n
    if not data or len(data) % width: raise ValueError('Incomplete network data matrix')
    if any(not math.isfinite(x) for x in data): raise ValueError('Non-finite network data')
    points = len(data)//width
    if freq_count is not None and points != freq_count: raise ValueError('Frequency count mismatch')
    scale = {'hz':1.0, 'khz':1e3, 'mhz':1e6, 'ghz':1e9}[unit]
    fs = [data[i*width]*scale for i in range(points)]
    if any(not math.isfinite(f) or f < 0 for f in fs) or any(a >= b for a,b in zip(fs,fs[1:])): raise ValueError('Frequencies must increase strictly')
    return {'path':str(path), 'sha256':hashlib.sha256(raw).hexdigest(), 'ports':n,
            'frequency_min_hz':fs[0], 'frequency_max_hz':fs[-1], 'points':points,
            'format':fmt.upper(), 'version':version, 'references':reference or [ref]*n}
