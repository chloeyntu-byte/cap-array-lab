"""Parse a single invoking Maestro window's private, non-executable records."""
import json
import os
import re
import stat

SCHEMA = 'CAPLAB_MAESTRO_CONTEXT_1'
MAX_BYTES = 1024 * 1024
ENV_NAMES = ('HOME', 'PDK', 'PATH', 'CDS_LIC_FILE', 'LM_LICENSE_FILE',
             'LD_LIBRARY_PATH', 'LD_PRELOAD', 'PYTHONHOME', 'PYTHONPATH',
             'TCL_LIBRARY', 'TK_LIBRARY', 'QT_PLUGIN_PATH')
TOKEN = re.compile(r'\s*("(?:[^"\\\r\n]|\\.)*"|nil|t|-?[0-9]+)(?=\s|$)')


def _fields(line):
    fields, pos = [], 0
    while line[pos:].strip():
        m = TOKEN.match(line, pos)
        if not m:
            raise ValueError('Invalid Maestro context field')
        token = m.group(1)
        fields.append(None if token == 'nil' else True if token == 't' else json.loads(token))
        pos = m.end()
    return fields


def parse_records(raw):
    if isinstance(raw, bytes):
        if len(raw) > MAX_BYTES:
            raise ValueError('Maestro context too large')
        raw = raw.decode('utf-8')
    if len(raw.encode('utf-8')) > MAX_BYTES:
        raise ValueError('Maestro context too large')
    lines = raw.splitlines()
    if not lines or lines[0] != SCHEMA:
        raise ValueError('Unsupported Maestro context schema')
    source = {'tests': [], 'modified': None, 'window_id': None}
    snapshot = {'global': {'available': False, 'properties': {}, 'opaque': {}},
                'tests': [], 'run_options': {'available': False},
                'run_mode': {'available': False, 'value': None}, 'storage': {}}
    context = {'schema': SCHEMA, 'source': source, 'snapshot': snapshot,
               'native_environment': {}, 'libraries': {}, 'histories': []}
    sizes = {'C': 7, 'R': 1, 'E': 2, 'H': 3, 'L': 2, 'T': 5,
             'S': 1, 'W': 1, 'M': 1, 'P': 3, 'J': 4, 'N': 4, 'O': 4,
             'D': 2, 'Q': 1, 'U': 3, 'V': 3}
    seen, policies = set(), {}
    for line in lines[1:]:
        if not line.strip():
            continue
        tag, sep, body = line.partition('\t')
        fields = _fields(body)
        if not sep or tag not in sizes or len(fields) != sizes[tag]:
            raise ValueError('Invalid Maestro context record: ' + tag)
        if tag in ('C', 'R', 'S', 'W', 'M', 'Q'):
            if tag in seen:
                raise ValueError('Duplicate Maestro context record: ' + tag)
            seen.add(tag)
        if tag == 'C':
            source.update(zip(('library', 'cell', 'view', 'workspace', 'setup_dir', 'cds_lib', 'ocean'), fields))
        elif tag in ('R', 'E'):
            key, value = ('LD_LIBRARY_PATH', fields[0]) if tag == 'R' else fields
            if key not in ENV_NAMES or key in context['native_environment'] or (value is not None and not isinstance(value, str)):
                raise ValueError('Invalid environment snapshot')
            context['native_environment'][key] = {'state': 'unset' if value is None else 'empty' if value == '' else 'value'}
            if value is not None:
                context['native_environment'][key]['value'] = value
        elif tag == 'T':
            if not isinstance(fields[0], str) or any(t['name'] == fields[0] for t in source['tests']):
                raise ValueError('Invalid or duplicate test')
            source['tests'].append(dict(zip(('name', 'design_library', 'design_cell', 'design_view', 'simulator'), fields)))
        elif tag == 'L':
            if not isinstance(fields[0], str) or fields[0] in context['libraries']:
                raise ValueError('Invalid or duplicate library')
            context['libraries'][fields[0]] = fields[1]
        elif tag == 'H':
            context['histories'].append(dict(zip(('name', 'results_dir', 'rdb_file'), fields)))
        elif tag == 'S': context['selected_history'] = fields[0]
        elif tag == 'W': source['window_id'] = fields[0]
        elif tag == 'M':
            if fields[0] not in (True, None, 'unknown'):
                raise ValueError('Invalid modified state')
            source['modified'] = None if fields[0] == 'unknown' else bool(fields[0])
        elif tag == 'P':
            scope, name, available = fields
            key = (scope, name)
            if key in policies or scope not in ('global', 'test'):
                raise ValueError('Invalid job policy')
            row = {'available': available is True, 'properties': {}, 'opaque': {}}
            policies[key] = row
            if scope == 'global': snapshot['global'] = row
            else: row['name'] = name; snapshot['tests'].append(row)
        elif tag in ('J', 'N', 'O'):
            scope, name, key, value = fields
            row = policies.get((scope, name))
            if row is None or not isinstance(key, str): raise ValueError('Orphan policy field')
            target = row['opaque' if tag == 'O' else 'properties']
            if key in target: raise ValueError('Duplicate policy field')
            if tag == 'N':
                if not isinstance(value, str) or not re.match(r'^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$', value): raise ValueError('Unsupported numeric policy value')
                value = float(value) if any(c in value for c in '.eE') else int(value)
            target[key] = value
        elif tag == 'D':
            row = policies.get(tuple(fields)); global_row = policies.get(('global', None))
            if row is None or global_row is None: raise ValueError('Orphan equal policy')
            row['properties'] = dict(global_row['properties']); row['opaque'] = dict(global_row['opaque'])
        elif tag == 'Q': snapshot['run_mode'] = {'available': fields[0] is not None, 'value': fields[0]}
        elif tag == 'U':
            key, available, value = fields
            opts = snapshot['run_options']; opts['available'] = opts['available'] or available is True
            if key.startswith('JobLimit:'): opts.setdefault('JobLimit', []).append([key[9:], value])
            else: opts[key] = value
        elif tag == 'V':
            key, available, value = fields
            if key in snapshot['storage']: raise ValueError('Duplicate storage field')
            snapshot['storage'][key] = {'available': available is True, 'value': value}
    if 'C' not in seen or not all(isinstance(source.get(k), str) and source[k] for k in ('library', 'cell', 'view', 'workspace', 'setup_dir')):
        raise ValueError('Incomplete Maestro source identity')
    if not os.path.isabs(source['workspace']) or not os.path.isabs(source['setup_dir']):
        raise ValueError('Maestro source paths must be absolute')
    source['setup_path'] = os.path.join(source['setup_dir'], 'maestro.sdb')
    if set(context['native_environment']) != set(ENV_NAMES):
        raise ValueError('Incomplete native environment snapshot')
    return context


def load_context(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Maestro context must be a private file owned by this user')
        return parse_records(stream.read(MAX_BYTES + 1))


def child_environment(base=None, context=None):
    """Restore the invoking editor environment, including unset and empty values."""
    env = dict(os.environ if base is None else base)
    if context is None:
        encoded = env.get('CAPLAB_NATIVE_ENV_JSON')
        if not encoded:
            return env
        context = json.loads(encoded)
    snapshot = context.get('native_environment', context)
    if not isinstance(snapshot, dict) or set(snapshot) != set(ENV_NAMES):
        raise ValueError('Incomplete native environment snapshot')
    for name in ENV_NAMES:
        entry = snapshot[name]
        if not isinstance(entry, dict) or entry.get('state') not in ('unset', 'empty', 'value'):
            raise ValueError('Invalid native environment state: ' + name)
        state = entry['state']
        if state == 'unset':
            if 'value' in entry: raise ValueError('Invalid unset environment value')
            env.pop(name, None)
        else:
            value = entry.get('value')
            if not isinstance(value, str) or (state == 'empty' and value != '') or (state == 'value' and value == ''):
                raise ValueError('Invalid native environment value: ' + name)
            env[name] = value
    env.pop('CAPLAB_NATIVE_ENV_JSON', None)
    return env


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 3 or sys.argv[1] != '--exec':
        raise SystemExit('Expected --exec executable [args...]')
    os.execvpe(sys.argv[2], sys.argv[2:], child_environment())
