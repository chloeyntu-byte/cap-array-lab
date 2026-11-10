"""AnalogLib passive and Touchstone instance definitions."""
from .model import add_instance, number
from .touchstone import inspect

DEFAULTS = {
    'res': {'r': '1k'},
    'indq': {'l': '1n', 'q': '50', 'fq': '1G', 'mode': '1', 'rdc': '0'},
    'capq': {'c': '1p', 'q': '50', 'fq': '1G', 'mode': '1'},
    'mind': {'ind1': '', 'ind2': '', 'k': '0.5'},
}


def _symbol(cell, names):
    h = max(0.6, (len(names) + 1) * 0.25)
    pins = []
    if cell == 'nport':
        count = len(names)//2
        h = max(0.7, (count + 1)*0.35)
        for i in range(count):
            y = h - (i+1)*(2*h/(count+1))
            for prefix, x in (('p', -1.0), ('m', 1.0)):
                pins.append({'name': prefix+str(i+1), 'direction':'inputOutput', 'xy':[x,y]})
    elif names:
        pins = [{'name':names[0], 'direction':'inputOutput', 'xy':[-1.0,0.0]},
                {'name':names[1], 'direction':'inputOutput', 'xy':[1.0,0.0]}]
    return {'library':'analogLib','cell':cell,'pins':pins,
            'bbox':[-1.0,-h,1.0,h], 'shapes':[{'kind':'rect','points':[-0.7,-h,0.7,h]}]}


def create_passive(project, cell, x=350, y=240, props=None, symbol=None):
    if cell not in DEFAULTS: raise ValueError('Unsupported passive: '+str(cell))
    values = dict(DEFAULTS[cell]); values.update(props or {})
    unknown = set(values)-set(DEFAULTS[cell])
    if unknown: raise ValueError('Unsupported parameters: '+', '.join(sorted(unknown)))
    node = add_instance(project, symbol or _symbol(cell, [] if cell == 'mind' else ['PLUS','MINUS']), x, y)
    node.update(kind='passive', view='spectre', props=values, enabled=True, rotation=0)
    return node


def create_snp(project, path, x=350, y=240, meta=None, symbol=None):
    meta = meta if meta is not None else inspect(path)
    names = [prefix+str(i) for i in range(1,meta['ports']+1) for prefix in ('p','m')]
    node = add_instance(project, symbol or _symbol('nport', names), x, y)
    node.update(kind='snp', view='spectre', touchstone=meta, props={}, enabled=True, rotation=0)
    return node


def native_properties(node, source_path=None):
    if node.get('kind') == 'snp':
        meta = node['touchstone']
        return {'dataFile':str(source_path or meta['path']), 'datafmt':'touchstone'}
    return dict(node['props'])


def validate_device(node, instances=None):
    if node.get('kind') == 'snp':
        meta = inspect(node['touchstone']['path'])
        if meta != node['touchstone']: raise ValueError('Touchstone file changed; re-import it')
        return
    cell, props = node['cell'], node['props']
    if cell not in DEFAULTS or set(props) != set(DEFAULTS[cell]): raise ValueError('Invalid passive parameters')
    if cell == 'mind':
        a, b = props['ind1'], props['ind2']
        by_id = {i['id']:i for i in (instances or []) if i.get('enabled', True)}
        if not a or a == b or any(i not in by_id or by_id[i].get('cell') != 'indq' for i in (a,b)):
            raise ValueError('Mutual coupling requires two distinct enabled inductors')
        if abs(number(props['k'])) > 1: raise ValueError('Coupling coefficient must be between -1 and 1')
    else:
        keys = ('r',) if cell == 'res' else (('l','q','fq') if cell == 'indq' else ('c','q','fq'))
        if any(number(props[k]) <= 0 for k in keys): raise ValueError('Device values, Q and reference frequency must be positive')
        if cell in ('indq','capq') and str(props['mode']) not in ('1','2','3'): raise ValueError('Invalid Q model mode')
        if cell == 'indq' and number(props['rdc']) < 0: raise ValueError('DC resistance must be nonnegative')


def _skill_string(value):
    return '"' + str(value).replace('\\','\\\\').replace('"','\\"').replace('\n','\\n').replace('\r','\\r') + '"'


def instance_skill(node, x, y, source_path=None):
    """Place one native AnalogLib device; caller connects its master terminals."""
    cell = node['cell']
    orientation = node.get('rotation', 0)
    orientations = {0:'R0', 90:'R90', 180:'R180', 270:'R270', 'R0':'R0', 'R90':'R90', 'R180':'R180', 'R270':'R270'}
    if orientation not in orientations: raise ValueError('Invalid device rotation')
    q = _skill_string
    out = 'm=dbOpenCellViewByType("analogLib" {} "symbol" "" "r")\nunless(m error("Missing analogLib symbol"))\n'.format(q(cell))
    if node.get('kind') == 'snp':
        count = int(node['touchstone']['ports'])
        if not 1 <= count <= 1000: raise ValueError('Invalid port count')
        out += 'i=dbCreateParamInst(cv m {} list({} {}) {} 1 list(list("p" "int" {}) list("checkBoxValue" "int" 1)))\n'.format(q(node['id']),x,y,q(orientations[orientation]),count)
    else:
        out += 'i=dbCreateInst(cv m {} list({} {}) {})\n'.format(q(node['id']),x,y,q(orientations[orientation]))
    out += 'unless(i error("Cannot create analogLib instance"))\n'
    for key,value in sorted(native_properties(node,source_path).items()):
        out += 'dbReplaceProp(i {} "string" {})\n'.format(q(key),q(value))
    return out


Q_MODES = {
    'indq': {'1':'Constant series resistance at fq',
             '2':'Series resistance increases with sqrt(frequency)',
             '3':'Series resistance with skin effect'},
    'capq': {'1':'Constant parallel conductance at fq',
             '2':'Parallel conductance increases with sqrt(frequency)',
             '3':'Constant Q (parallel conductance proportional to frequency)'},
}


def native_symbol(workspace, cell, ports=None, backend=None):
    """Read the actual symbol, instantiating nport PCell only in a private library.

    Intended for a background worker: launching a native child can take seconds.
    """
    import copy
    import threading
    import uuid
    from pathlib import Path
    from .model import ROOT
    if backend is None: raise ValueError('A backend is required for native symbol lookup')
    if cell != 'nport':
        if cell not in DEFAULTS: raise ValueError('Unsupported analogLib cell')
        return backend.symbol(workspace, 'analogLib', cell)
    n = int(ports or 0)
    if not 1 <= n <= 1000: raise ValueError('Invalid nport count')
    key = (backend._scope(workspace), backend._workspace_stamp(workspace), n)
    with _SYMBOL_LOCK:
        if key in _SYMBOL_CACHE: return copy.deepcopy(_SYMBOL_CACHE[key])
    folder = ROOT / '.work' / ('rf_symbol_' + uuid.uuid4().hex[:12])
    libdir = folder / 'library'
    libdir.mkdir(parents=True)
    source = Path(workspace).resolve() / 'cds.lib'
    if not source.is_file(): raise ValueError('Workspace cds.lib unavailable')
    (folder / 'cds.lib').write_text('INCLUDE {}\nDEFINE caplab_rf_symbol {}\n'.format(_skill_string(source), _skill_string(libdir)), encoding='utf-8')
    out = folder / 'symbol.tsv'
    body = '''cv=dbOpenCellViewByType("caplab_rf_symbol" "probe" "schematic" "schematic" "w")
unless(cv error("Cannot open private symbol probe"))
m=dbOpenCellViewByType("analogLib" "nport" "symbol" "" "r")
unless(m error("Cannot open analogLib nport"))
i=dbCreateParamInst(cv m "I0" list(0 0) "R0" 1 list(list("p" "int" %d) list("checkBoxValue" "int" 1)))
unless(i error("Cannot instantiate nport PCell"))
master=i~>master
o=outfile(%s)
fprintf(o "BBOX\\t%%g\\t%%g\\t%%g\\t%%g\\n" xCoord(car(master~>bBox)) yCoord(car(master~>bBox)) xCoord(cadr(master~>bBox)) yCoord(cadr(master~>bBox)))
foreach(term master~>terminals
 fig=car(term~>pins)~>fig
 unless(fig fig=car(car(term~>pins)~>figs))
 unless(fig error("Pin figure missing"))
 p=centerBox(fig~>bBox)
 fprintf(o "PIN\\t%%s\\t%%s\\t%%g\\t%%g\\n" term~>name term~>direction xCoord(p) yCoord(p)))
foreach(s master~>shapes
 when(member(s~>objType '("line" "polygon" "rect" "ellipse" "arc"))
 fprintf(o "SHAPE\\t%%s" s~>objType)
 if(member(s~>objType '("line" "polygon")) then
  foreach(p s~>points fprintf(o "\\t%%g\\t%%g" xCoord(p) yCoord(p)))
 else foreach(p s~>bBox fprintf(o "\\t%%g\\t%%g" xCoord(p) yCoord(p))))
 fprintf(o "\\n")))
close(o) dbClose(cv)
''' % (n, _skill_string(out))
    backend.native(str(folder),body,folder,timeout=180)
    symbol = {'library':'analogLib','cell':'nport','pins':[],'shapes':[]}
    for line in out.read_text(encoding='utf-8').splitlines():
        part=line.split('\t')
        if part[0]=='BBOX': symbol['bbox']=[float(x) for x in part[1:]]
        elif part[0]=='PIN': symbol['pins'].append({'name':part[1],'direction':part[2],'xy':[float(part[3]),float(part[4])]})
        elif part[0]=='SHAPE': symbol['shapes'].append({'kind':part[1],'points':[float(x) for x in part[2:]]})
    expected = {prefix+str(i) for i in range(1,n+1) for prefix in ('p','m')}
    if {pin['name'] for pin in symbol['pins']} != expected or 'bbox' not in symbol:
        raise RuntimeError('Native nport PCell symbol terminals do not match requested ports')
    with _SYMBOL_LOCK: _SYMBOL_CACHE[key] = copy.deepcopy(symbol)
    return symbol


import threading as _threading
_SYMBOL_LOCK = _threading.Lock()
_SYMBOL_CACHE = {}
