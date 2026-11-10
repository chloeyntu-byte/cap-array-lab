import copy
import csv
import hashlib
import json
import math
import os
import re
import shlex
import signal
import shutil
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from .model import ROOT, expand_pin, validate, cq
from .registers import all_nodes, code_expression
from .native_register import REGISTER_HELPERS
from .canvas_tools import native_wire_plan, native_instance_origin
from .devices import validate_device, instance_skill
from . import loss_native
from .spectrum import simulation_grid, include_observations, resample
from .decomposition import decompose
from .maestro_runtime import selected_test, project_tool_setup, normalize_overrides, runtime_apply_skill, storage_apply_skill, runtime_preopen_skill, simulation_monitor_root


def sq(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r') + '"'


def sl(values):
    return 'list(' + ' '.join(sq(v) for v in values) + ')'


def floating_y_expression(a, b, c, d):
    """Native scalar Schur reduction, retaining a completely floating DUT.

    The only zero-ground shortcut is a row-and-column roundoff nullspace.
    Small real ground returns must not be clipped against the main coupling.
    """
    return ('let((calA calB calC calD calS calScale calNull calDD calDC calCD) '
            'calA={a} calB={b} calC={c} calD={d} '
            'calS=calA+calB+calC+calD '
            'calScale=max(abs(real(calA)) abs(imag(calA)) abs(real(calB)) abs(imag(calB)) '
            'abs(real(calC)) abs(imag(calC)) abs(real(calD)) abs(imag(calD)) 1e-300) '
            'calNull=max(abs(calA+calB) abs(calC+calD) abs(calA+calC) abs(calB+calD)) '
            'calDD=(calA-calB-calC+calD)/4 calDC=(calA+calB-calC-calD)/2 '
            'calCD=(calA+calC-calB-calD)/2 '
            'if(calNull<=1.4210854715202004e-14*calScale then '
            'calDD else if(abs(calS)<=1e-10*calNull then nil '
            'else calDD-(calDC/calS)*calCD)))').format(a=a, b=b, c=c, d=d)


def digest(project):
    # Never copy the potentially large saved spectrum just to omit it.
    p = copy.deepcopy({key: value for key, value in project.items() if key != 'private'})
    if 'simulation_start' in p.get('settings', {}):
        p['settings'].pop('frequencies', None)
    # Physical wire endpoints and instance locations jointly define connectivity.
    if not p.get('wires'):
        for i in p['instances']+p.get('registers',[]):
            i.pop('x', None)
            i.pop('y', None)
    return hashlib.sha256(json.dumps(p, sort_keys=True).encode('utf-8')).hexdigest()


NATIVE_HELPERS = r'''
procedure(calStub(cv inst termName netName)
 let((term fig p q wire b cx cy dx dy ymin ymax pp)
  term=dbFindTermByName(inst~>master termName)
  unless(term error("Missing master terminal %s" termName))
  fig=car(car(term~>pins)~>figs)
  unless(fig fig=car(term~>pins)~>fig)
  unless(fig error("Terminal has no figure %s" termName))
  p=dbTransformPoint(centerBox(fig~>bBox) inst~>transform)
  b=inst~>bBox cx=xCoord(centerBox(b)) cy=yCoord(centerBox(b))
  ymin=1e20 ymax=-1e20
  foreach(tt inst~>master~>terminals
   pp=dbTransformPoint(centerBox(car(tt~>pins)~>fig~>bBox) inst~>transform)
   ymin=min(ymin yCoord(pp)) ymax=max(ymax yCoord(pp)))
  if(ymax-ymin>0.01 && (abs(yCoord(p)-ymax)<0.001 || abs(yCoord(p)-ymin)<0.001) then
   q=list(xCoord(p) yCoord(p)+if(abs(yCoord(p)-ymax)<0.001 then 0.35 else -0.35))
  else
   q=list(xCoord(p)+if(xCoord(p)>=cx then 0.35 else -0.35) yCoord(p))
  )
  wire=car(schCreateWire(cv "draw" "full" list(p q) 0.0625 0.0625 0.0))
  unless(wire error("Cannot wire %s" termName))
  schCreateWireLabel(cv wire q netName "centerCenter" "R0" "stick" 0.055 nil)
 ))
procedure(calDrawWire(cv p q netName)
 let((wire)
  wire=car(schCreateWire(cv "draw" "full" list(p q) 0.0625 0.0625 0.0))
  unless(wire error("Cannot draw canvas wire"))
  schCreateWireLabel(cv wire p netName "centerCenter" "R0" "stick" 0.055 nil)))
procedure(calSource(cv name xy plus minus dc mag phase)
 let((m inst)
  m=dbOpenCellViewByType("analogLib" "vsource" "symbol" "" "r")
  inst=dbCreateInst(cv m name xy "R0")
  dbReplaceProp(inst "srcType" "string" "dc")
  dbReplaceProp(inst "vdc" "string" dc)
  dbReplaceProp(inst "smallSig" "boolean" t)
  dbReplaceProp(inst "acm" "string" mag)
  dbReplaceProp(inst "acp" "string" phase)
  calStub(cv inst "PLUS" plus) calStub(cv inst "MINUS" minus)
  inst
 ))
'''


class Backend:
    _cache = {}
    _library_paths = {}
    _cache_lock = threading.Lock()

    def __init__(self, progress=None):
        self._callback = progress or (lambda event: None)
        self.cancelled = threading.Event()
        self.process = None
        self.lock = threading.Lock()
        self.native_environment = None
        self.native_virtuoso = None
        self.native_home = None
        self.native_workdir = None

    def progress(self, message):
        """Accept legacy messages while publishing structured worker events."""
        event = dict(message) if isinstance(message, dict) else {'kind': 'log', 'level': 'info', 'message': str(message)}
        event.setdefault('message', '')
        event.setdefault('time', time.time())
        self._callback(event)

    def activity(self, message, completed=0, total=1, active=True):
        self.progress({'kind': 'activity', 'message': message, 'completed': completed,
                       'total': total, 'active': active})
        self.progress({'kind': 'log', 'level': 'info', 'message': message})

    def _live_output(self, path, offsets):
        path = Path(path)
        try:
            key = str(path.resolve())
            with path.open('r', encoding='utf-8', errors='replace') as stream:
                stream.seek(offsets.get(key, 0))
                lines = stream.readlines()
                offsets[key] = stream.tell()
        except OSError:
            return
        for line in lines:
            text = line.strip()
            low = text.lower()
            if text.startswith('CAL_ACTIVITY\t'):
                fields = text.split('\t', 3)
                if len(fields) == 4:
                    self.activity(fields[3], int(fields[1]), int(fields[2]))
                continue
            if not text or 'helpapi' in low:
                continue
            if any(token in low for token in ('warning', '*warn*', 'error', 'fatal', 'failed', 'completes', 'simulation completed', 'starting simulation')):
                level = 'error' if any(token in low for token in ('error', 'fatal', 'failed')) and not re.search(r'0 errors?\b', low) else ('warning' if 'warn' in low else 'info')
                self.progress({'kind': 'log', 'level': level, 'message': text[:1000]})

    def _monitor_simulation(self, monitor, offsets):
        root = Path(monitor['root'])
        expected = monitor.get('tests', ['CAP_SP' if monitor.get('analysis') == 'sp' else 'CAP_AC'])
        for path in root.glob('**/spectre.out'):
            if len(path.parts) < 4 or not path.parts[-4].isdigit(): continue
            test = path.parts[-3]
            if test not in expected: continue
            try:
                actual = str(path.resolve()); stamp=path.stat()
                signature=(stamp.st_mtime_ns, stamp.st_size)
                if stamp.st_mtime < monitor['started'] - 1 or monitor.get('baseline', {}).get(actual) == signature: continue
                source=path.parent.parent/'netlist'/'input.scs'
                source_text=source.read_text(encoding='utf-8', errors='replace')
                name='CAL_POINT' if monitor.get('explicit') else 'CAL_CODE'
                match=re.search(r'(?<![A-Za-z0-9_])'+name+r'\s*=\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)',source_text)
                if not match: continue
                value=float(match.group(1)); codes=monitor.get('codes',[])
                if not value.is_integer() or int(value) not in codes: continue
                index=codes.index(int(value))
                self._live_output(path, offsets)
                text=path.read_text(encoding='utf-8',errors='replace')
            except OSError: continue
            state=('done' if 'completes' in text and re.search(r'0 errors?\b', text) else
                   'failed' if re.search(r'FATAL \(|ERROR \(', text) else 'running')
            record=monitor.setdefault('point_tests',{}).setdefault(str(index),{})
            record[test]={'path':actual,'state':state}
            states=[record.get(t,{}).get('state') for t in expected]
            combined=('failed' if 'failed' in states else 'done' if all(v=='done' for v in states) else 'running')
            logs={name:val['path'] for name,val in record.items()}
            preferred=logs.get(expected[0], next(iter(logs.values())))
            current={'point_index':index,'path':preferred,'state':combined,'logs':logs}
            if current != monitor.setdefault('points',{}).get(str(index)):
                monitor['points'][str(index)]=current
                self.progress(dict(kind='point_log',**current))
        count=sum(item['state']=='done' for item in monitor.get('points',{}).values())
        if count != monitor.get('last',-1):
            monitor['last']=count
            self.progress({'kind':'simulation','message':'Completed {}/{} simulation points'.format(count,monitor['total']),
                           'completed':count,'total':monitor['total'],'active':True})

    @staticmethod
    def _workspace_stamp(workspace):
        file = Path(workspace) / 'cds.lib'
        try:
            stat = file.stat()
            return (str(Path(workspace).resolve()), stat.st_mtime_ns, stat.st_size)
        except OSError:
            return (str(Path(workspace).resolve()),)

    def _scope(self, workspace):
        identity = json.dumps([self.native_virtuoso, self.native_workdir, self.native_environment], sort_keys=True, default=str).encode('utf-8')
        return (str(Path(workspace).resolve()), hashlib.sha256(identity).hexdigest())

    def _metadata_signature(self, workspace, lib, cell, view):
        folder = self._library_paths.get(self._scope(workspace), {}).get(lib)
        if not folder:
            return None
        root = Path(folder) / cell / view
        try:
            return tuple(sorted((str(path.name), path.stat().st_mtime_ns, path.stat().st_size)
                                for path in root.iterdir() if path.is_file() and not path.name.endswith('.cdslck')))
        except OSError:
            return None

    @staticmethod
    def context(project):
        source = Path(project['workspace']).resolve()
        extra = dict((project.get('maestro_runtime') or {}).get('libraries') or {})
        extra.update(project.get('extra_libraries', {}))
        source_info = (project.get('maestro_runtime') or {}).get('source') or {}
        cds_file = Path(source_info.get('cds_lib') or source / 'cds.lib').resolve()
        if not cds_file.is_file():
            raise ValueError('Invoking Maestro cds.lib is unavailable.')
        if not extra and cds_file == (source / 'cds.lib').resolve():
            return str(source)
        for name, path in extra.items():
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
                raise ValueError('Library names must use letters, digits, and underscores.')
            if not isinstance(path, str) or not Path(path).is_dir() or not (Path(path) / 'cdsinfo.tag').is_file():
                raise ValueError('Not an OpenAccess library: ' + path)
        key = hashlib.sha256(json.dumps([str(source), str(cds_file), extra], sort_keys=True).encode()).hexdigest()[:16]
        folder = ROOT / '.work' / ('libraries_' + key)
        folder.mkdir(parents=True, exist_ok=True)
        content = 'INCLUDE ' + sq(cds_file) + '\n'
        content += ''.join('DEFINE {} {}\n'.format(name, sq(Path(path).resolve())) for name, path in extra.items())
        libfile = folder / 'cds.lib'
        if not libfile.exists() or libfile.read_text(encoding='utf-8') != content:
            libfile.write_text(content, encoding='utf-8')
        return str(folder)

    def stop(self):
        self.cancelled.set()
        # All Cadence children for this tool have their own process group.
        with self.lock:
            if self.process and self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def native(self, workspace, body, folder, timeout=900, monitor=None):
        workspace = Path(workspace).resolve()
        if not (workspace / 'cds.lib').is_file():
            raise ValueError('No cds.lib found in the selected workspace.')
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        script = folder / ('job_' + uuid.uuid4().hex[:8] + '.il')
        done = script.with_suffix('.ok')
        script.write_text('''
unless(isContextLoaded("schView") loadContext(prependInstallPath("etc/context/schView.cxt")) callInitProc("schView"))
envSetVal("asimenv.startup" "simulator" 'string "spectre")
calResult=errset(progn(
''' + body + '\n) t)\nwhen(calResult calOut=outfile(' + sq(done) + ') fprintf(calOut "OK") close(calOut))\nif(calResult then exit(0) else exit(1))\n', encoding='utf-8')
        wrapper = ROOT / 'cadence-child.sh'
        # A separate loader catches parser failures too, rather than leaving a headless CIW waiting.
        driver=script.with_suffix('.driver.il')
        driver.write_text('calLoaded=errset(load('+sq(script)+') t)\nunless(calLoaded exit(1))\n',encoding='utf-8')
        log = script.with_suffix('.stdout')
        with log.open('w') as output:
            env = os.environ.copy()
            env.pop('CAPLAB_NATIVE_ENV_JSON', None)
            env.pop('CAPLAB_NATIVE_VIRTUOSO', None)
            env.pop('CAPLAB_NATIVE_CDS_LIB', None)
            if self.native_workdir:
                env['CAPLAB_NATIVE_CDS_LIB'] = str(workspace / 'cds.lib')
            for key in list(env):
                if key.startswith('CDS_MPS_') or key in ('CDS_LIB_PATH', 'CDS_LOAD_PATH'):
                    env.pop(key, None)
            env['LC_ALL'] = 'en_US.utf8'
            if self.native_home:
                env['HOME'] = self.native_home
            if self.native_environment:
                env['CAPLAB_NATIVE_ENV_JSON'] = json.dumps(self.native_environment)
                env['CAPLAB_NATIVE_VIRTUOSO'] = self.native_virtuoso
            with self.lock:
                if self.cancelled.is_set():
                    raise RuntimeError('Stopped by user.')
                self.process = subprocess.Popen(['/bin/bash', str(wrapper), str(driver), str(script.with_suffix('.log'))],
                    cwd=self.native_workdir or str(workspace), env=env, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                proc = self.process
            self.progress({'kind': 'log', 'level': 'info', 'message': 'Started Cadence worker (PID {})'.format(proc.pid)})
            start = time.monotonic()
            offsets = {}
            next_scan = 0
            while proc.poll() is None:
                self._live_output(log, offsets)
                if monitor and time.monotonic() >= next_scan:
                    self._monitor_simulation(monitor, offsets)
                    next_scan = time.monotonic() + 1
                if self.cancelled.is_set() or (timeout is not None and time.monotonic() - start > timeout):
                    was_cancelled = self.cancelled.is_set()
                    self.stop()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                    raise RuntimeError('Stopped by user.' if was_cancelled else 'Cadence operation timed out.')
                time.sleep(0.2)
            self._live_output(log, offsets)
            if monitor:
                self._monitor_simulation(monitor, offsets)
            self.process = None
        if proc.returncode or not done.exists():
            lines = log.read_text(encoding='utf-8', errors='replace').splitlines()
            errors = [x for x in lines if '*Error*' in x or 'ERROR' in x or 'error:' in x.lower()]
            useful = [x for x in errors if 'HELPAPI' not in x]
            raise RuntimeError(('\n'.join(useful[-4:]) or 'Cadence did not complete the operation. Check libraries, pins, and model setup.')[:900])
        return log

    def metadata_native(self, workspace, body, folder, timeout=180):
        from .native_metadata import request
        return request(workspace, body, folder, timeout=timeout,
                       cancelled=self.cancelled, progress=self.progress,
                       native_environment=self.native_environment, native_virtuoso=self.native_virtuoso,
                       native_workdir=self.native_workdir)

    def catalog(self, workspace, force=False):
        key = ('catalog', self._workspace_stamp(workspace), self._scope(workspace))
        with self._cache_lock:
            cached = self._cache.get(key)
        if cached and not force and time.monotonic() - cached[0] < 30:
            self.activity('Design libraries loaded from cache', 1, 1, False)
            return copy.deepcopy(cached[1])
        self.activity('Loading design libraries…')
        folder = ROOT / '.work' / ('catalog_' + uuid.uuid4().hex[:8])
        out = folder / 'catalog.tsv'
        body = '''when(getd('ddUpdateLibList) ddUpdateLibList())
o=outfile(%s)
foreach(lib ddGetLibList()
 fprintf(o "LIB\\t%%s\\t%%s\\n" lib~>name lib~>readPath)
 foreach(cell lib~>cells when(ddGetObj(lib~>name cell~>name "symbol") fprintf(o "CELL\\t%%s\\t%%s\\n" lib~>name cell~>name)))
)
close(o)
''' % sq(out)
        self.metadata_native(workspace, body, folder, 180)
        libs = {}
        for line in out.read_text(encoding='utf-8').splitlines():
            p = line.split('\t')
            if p[0] == 'LIB':
                libs[p[1]] = {'path': p[2], 'cells': []}
            elif p[0] == 'CELL':
                # Keep generated testbench/register cells out of the public chooser.
                if not re.fullmatch(r'CAL_\d{8}_\d{6}_[0-9a-f]{6}(?:_R\d+)?', p[2]):
                    libs[p[1]]['cells'].append(p[2])
        with self._cache_lock:
            self._cache[key] = (time.monotonic(), copy.deepcopy(libs))
            self._library_paths[self._scope(workspace)] = {name: value['path'] for name, value in libs.items()}
        self.activity('Design libraries ready', 1, 1, False)
        return libs

    def symbol(self, workspace, lib, cell, force=False):
        key = ('symbol', self._workspace_stamp(workspace), self._scope(workspace), lib, cell)
        stamp = self._metadata_signature(workspace, lib, cell, 'symbol')
        with self._cache_lock:
            cached = self._cache.get(key)
        if cached and not force and stamp == cached[0]:
            self.activity('Symbol ready from cache', 1, 1, False)
            return copy.deepcopy(cached[1])
        self.activity('Loading symbol and physical pins…')
        folder = ROOT / '.work' / ('symbol_' + uuid.uuid4().hex[:8])
        out = folder / 'symbol.tsv'
        body = '''cv=dbOpenCellViewByType(%s %s "symbol" "" "r")
unless(cv error("Symbol is unavailable"))
o=outfile(%s)
fprintf(o "BBOX\\t%%g\\t%%g\\t%%g\\t%%g\\n" xCoord(car(cv~>bBox)) yCoord(car(cv~>bBox)) xCoord(cadr(cv~>bBox)) yCoord(cadr(cv~>bBox)))
foreach(term cv~>terminals
 fig=car(term~>pins)~>fig
 unless(fig fig=car(car(term~>pins)~>figs))
 unless(fig error("Pin figure missing"))
 p=centerBox(fig~>bBox)
 fprintf(o "PIN\\t%%s\\t%%s\\t%%g\\t%%g\\n" term~>name term~>direction xCoord(p) yCoord(p))
)
foreach(s cv~>shapes
 when(member(s~>objType '("line" "polygon" "rect" "ellipse" "arc"))
 fprintf(o "SHAPE\\t%%s" s~>objType)
 if(member(s~>objType '("line" "polygon")) then
 foreach(p s~>points fprintf(o "\\t%%g\\t%%g" xCoord(p) yCoord(p)))
 else foreach(p s~>bBox fprintf(o "\\t%%g\\t%%g" xCoord(p) yCoord(p))))
 fprintf(o "\\n")
 )
)
close(o) dbClose(cv)
''' % (sq(lib), sq(cell), sq(out))
        self.metadata_native(workspace, body, folder, 180)
        symbol = {'library': lib, 'cell': cell, 'pins': [], 'shapes': []}
        for line in out.read_text(encoding='utf-8').splitlines():
            p = line.split('\t')
            if p[0] == 'BBOX':
                symbol['bbox'] = [float(x) for x in p[1:]]
            elif p[0] == 'PIN':
                symbol['pins'].append({'name': p[1], 'direction': p[2], 'xy': [float(p[3]), float(p[4])]})
            elif p[0] == 'SHAPE':
                symbol['shapes'].append({'kind': p[1], 'points': [float(x) for x in p[2:]]})
        with self._cache_lock:
            self._cache[key] = (self._metadata_signature(workspace, lib, cell, 'symbol'), copy.deepcopy(symbol))
        self.activity('Symbol ready', 1, 1, False)
        return symbol

    def cell_views(self,workspace,library,cell):
        self.activity('Loading available cellviews…')
        folder=ROOT/'.work'/('views_'+uuid.uuid4().hex[:8]);out=folder/'views.txt'
        body='obj=ddGetObj('+sq(library)+' '+sq(cell)+')\nunless(obj error("Cell is unavailable"))\no=outfile('+sq(out)+')\nforeach(v obj~>views fprintf(o "%s\\n" v~>name))\nclose(o)\n'
        self.metadata_native(workspace,body,folder,180)
        result = sorted(out.read_text().splitlines())
        self.activity('Cellviews ready', 1, 1, False)
        return result

    def reference_models(self, project, libs):
        runtime = project.get('maestro_runtime') or {}
        source = runtime.get('source') or {}
        if source and os.path.realpath(source.get('workspace', '')) != os.path.realpath(project['workspace']):
            raise ValueError('Maestro source and project workspace differ; select a matching source.')
        r = source if source else project['reference']
        if r['library'] not in libs:
            raise ValueError('Reference library {} is unavailable. Select a reference Maestro using the same process.'.format(r['library']))
        path = Path(source['setup_dir']) / 'active.state' if source else Path(libs[r['library']]['path']) / r['cell'] / 'maestro' / 'active.state'
        tree = ET.parse(str(path))
        test = selected_test(runtime) if source else None
        nodes = [node for node in tree.getroot().findall('Test') if node.get('Name') == test] if test else [tree.getroot()]
        if len(nodes) != 1:
            raise ValueError('Source Maestro test is missing or ambiguous.')
        fields = nodes[0].findall('.//component[@Name="modelSetup"]/field[@Name="modelFiles"]')
        models = []
        for f in fields[:1]:
            for filename, section in re.findall(r'\("([^"\n]+)"(?:\s+"([^"\n]*)")?\)', f.text or ''):
                if not Path(filename).is_absolute():
                    storage = (runtime.get('snapshot') or {}).get('storage') or {}
                    workdir = (storage.get('source_work_dir') or {}).get('value') or project['workspace']
                    filename = str((Path(workdir)/filename).resolve())
                if not Path(filename).is_file():
                    raise ValueError('Reference model file is missing: ' + filename)
                models.append((filename, section))
        if not models:
            raise ValueError('Reference Maestro has no model files. Select a reference testbench for this process.')
        return models

    def bind_private_home(self, runtime, folder):
        home = Path(folder) / 'native_home'
        home.mkdir(parents=True, exist_ok=True)
        (home / '.cadence' / 'jobpolicy').mkdir(parents=True, exist_ok=True)
        snapshot = copy.deepcopy(runtime.get('native_environment') or {})
        entry = snapshot.get('HOME') or {}
        source_home = entry.get('value') if entry.get('state') == 'value' else os.environ.get('HOME')
        src = Path(source_home) / '.cadence' / 'jobpolicy' if source_home else None
        dst = home / '.cadence' / 'jobpolicy'
        if src and src.is_dir() and not any(dst.iterdir()):
            for root, dirs, files in os.walk(str(src), followlinks=False):
                if any(os.path.islink(os.path.join(root, name)) for name in dirs + files):
                    raise ValueError('Source job policy directory contains a symbolic link.')
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.rmdir()
            shutil.copytree(str(src), str(dst))
        self.native_home = str(home)
        if snapshot:
            snapshot['HOME'] = {'state': 'value', 'value': str(home)}
        self.native_environment = snapshot or None

    def build(self, project, libs):
        self.native_home = None
        runtime = project.get('maestro_runtime') or {}
        self.native_home = None
        source = runtime.get('source') or {}
        storage = (runtime.get('snapshot') or {}).get('storage') or {}
        self.native_workdir = ((storage.get('source_work_dir') or {}).get('value') or source.get('workspace')) if source else None
        if self.native_workdir and not os.path.isdir(self.native_workdir):
            raise ValueError('Source Maestro working directory is unavailable.')
        self.native_environment = runtime.get('native_environment') or None
        self.native_virtuoso = (os.path.join(os.path.dirname(runtime['source']['ocean']), 'virtuoso')
                                if (runtime.get('source') or {}).get('ocean') else os.environ.get('CAPLAB_NATIVE_VIRTUOSO'))
        if self.native_environment and not self.native_virtuoso:
            raise ValueError('Invoking Virtuoso executable is unavailable.')
        overrides = normalize_overrides(runtime.get('overrides'))
        if overrides and not runtime.get('source'):
            raise ValueError('Select a source Maestro before applying runtime overrides.')
        cfg = validate(project)
        cfg['analysis'] = project['settings'].get('analysis', 'sp')
        cfg['decomposition'] = bool(project['settings'].get('decomposition', False)) and cfg['analysis'] == 'sp'
        cfg['simulation_frequencies'] = include_observations(simulation_grid(project['settings']), cfg['frequencies'])
        library = project['library']
        if library not in libs:
            raise ValueError('Target library is not available in this workspace.')
        baseline = {}
        if runtime.get('source'):
            setup_dir = Path(runtime['source']['setup_dir'])
            for filename in ('maestro.sdb', 'active.state'):
                baseline[filename] = hashlib.sha256((setup_dir / filename).read_bytes()).hexdigest()
        enabled_instances = [n for n in project['instances'] if n.get('enabled', True)]
        for node in enabled_instances:
            if node.get('kind') in ('passive', 'snp'):
                validate_device(node, project['instances'])
        pure_analog = all(n.get('kind') in ('passive', 'snp') for n in enabled_instances)
        models = [] if pure_analog else self.reference_models(project, libs)
        capture_loss = bool(project['settings'].get('capture_loss', False)) and cfg['analysis'] == 'sp'
        cfg['capture_loss'] = capture_loss
        loss_plan = loss_native.fixture_plan(project, cfg) if capture_loss else None
        self.activity('Building private schematic…', 0, 3)
        uid = datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:6]
        cell = 'CAL_' + uid
        folder = ROOT / '.work' / cell
        folder.mkdir(parents=True)
        if runtime.get('source'):
            self.bind_private_home(runtime, folder)
        cellpath = Path(libs[library]['path']) / cell
        if cellpath.exists():
            raise ValueError('Private cell name already exists.')
        manifest = {'cell': cell, 'library': library, 'folder': str(folder), 'cellpath': str(cellpath),
                    'digest': digest(project), 'models': models, 'cfg': cfg,
                    'spectrum_schema': 2 if cfg['decomposition'] else 1}
        if loss_plan is not None: manifest['loss_plan'] = loss_plan
        frozen = {}
        for inst in enabled_instances:
            if inst.get('kind') != 'snp': continue
            meta = inst['touchstone']; source = Path(meta['path'])
            target = folder / ('snp_' + inst['id'] + source.suffix.lower())
            with source.open('rb') as inp, target.open('xb') as out:
                shutil.copyfileobj(inp, out)
            if hashlib.sha256(target.read_bytes()).hexdigest() != meta['sha256']:
                raise ValueError('Touchstone file changed while copying: ' + str(source))
            frozen[inst['id']] = str(target)
        manifest['frozen_snp'] = frozen
        (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        saved_project = copy.deepcopy(project)
        (saved_project.get('maestro_runtime') or {}).pop('native_environment', None)
        (folder / 'project.json').write_text(json.dumps(saved_project, ensure_ascii=False, indent=2), encoding='utf-8')
        body = NATIVE_HELPERS + REGISTER_HELPERS
        if cfg.get('explicit'):
            for reg in cfg['registers']:
                body += 'calCreateRegister({} {} {} {})\n'.format(sq(library),sq(cell+'_'+reg['id']),reg['bits'],sq(reg['name']))
        body += '\ncv=dbOpenCellViewByType(' + sq(library) + ' ' + sq(cell) + ' "schematic" "schematic" "w")\nunless(cv error("Cannot create private schematic"))\n'
        nodes = all_nodes(project) if cfg.get('explicit') else [n for n in project['instances'] if n.get('enabled', True)]
        # Keep the user's relative layout while moving it clear of private fixtures.
        origins = [native_instance_origin(n) for n in nodes]
        xshift = 100.0 - min([p[0] for p in origins] or [0.0])
        for inst, origin in zip(nodes, origins):
            x, y = origin[0] + xshift, origin[1]
            if inst.get('kind') in ('passive', 'snp'):
                body += instance_skill(inst, x, y, frozen.get(inst['id']))
            else:
                master_lib = library if inst.get('kind')=='register' else inst['library']
                master_cell = cell+'_'+inst['id'] if inst.get('kind')=='register' else inst['cell']
                angle = int(inst.get('rotation', 0)) % 360
                if angle not in (0, 90, 180, 270): raise ValueError('Invalid instance rotation')
                body += 'm=dbOpenCellViewByType({} {} "symbol" "" "r")\nunless(m error("Missing symbol"))\ni=dbCreateInst(cv m {} list({} {}) {})\n'.format(sq(master_lib), sq(master_cell), sq(inst['id']), x, y, sq('R'+str(angle)))
            for pin in inst['symbol']['pins']:
                nets = [cfg['nets'][inst['id'] + '.' + p] for p in expand_pin(pin['name'])]
                label = (loss_native.pin_label(loss_plan, inst['id'], pin['name'])
                         if loss_plan is not None and inst.get('kind') != 'register' else ','.join(nets))
                body += 'calStub(cv i {} {})\n'.format(sq(pin['name']), sq(label))
        for segment in native_wire_plan(project, cfg, isolate_instances=bool(loss_plan)):
            (x1,y1),(x2,y2) = segment['points']
            label = ','.join(segment['nets'])
            body += 'calDrawWire(cv list({} {}) list({} {}) {})\n'.format(x1+xshift,y1,x2+xshift,y2,sq(label))
        if loss_plan is not None:
            body += loss_native.fixture_skill(loss_plan)
        sources = [('VDD', 'CAL_VDD', 'CAL_VSS', 'CAL_VDD', '0', '0'),
                   ('VSS', 'CAL_VSS', 'gnd!', '0', '0', '0')]
        if cfg['analysis'] == 'sp':
            # Keep the original port's DC termination. For matrix extraction its
            # RF branch opens via ideal dcfeed; ground probes are DC-blocked.
            body += '''m=dbOpenCellViewByType("analogLib" "port" "symbol" "" "r")
i=dbCreateInst(cv m "PORT0" list(-5 2) "R0")
dbReplaceProp(i "srcType" "string" "dc")
dbReplaceProp(i "r" "string" "50")
'''
            body += 'dbReplaceProp(i "num" "string" {})\ncalStub(cv i "PLUS" "CAL_P") calStub(cv i "MINUS" {})\n'.format(
                sq('3' if cfg['decomposition'] else '1'), sq('CAL_BIAS_N' if cfg['decomposition'] else 'CAL_N'))
            if cfg['decomposition']:
                body += '''m=dbOpenCellViewByType("analogLib" "dcfeed" "symbol" "" "r")
unless(m error("Ideal DC feed is unavailable"))
i=dbCreateInst(cv m "DC_BIAS" list(-3 2) "R0")
calStub(cv i "PLUS" "CAL_BIAS_N") calStub(cv i "MINUS" "CAL_N")
'''
                for index, name in enumerate(('P', 'N'), 1):
                    body += '''m=dbOpenCellViewByType("analogLib" "port" "symbol" "" "r")
i=dbCreateInst(cv m {port} list({x} 0) "R0")
dbReplaceProp(i "srcType" "string" "dc")
dbReplaceProp(i "r" "string" "50")
dbReplaceProp(i "num" "string" {num})
calStub(cv i "PLUS" {probe}) calStub(cv i "MINUS" "CAL_VSS")
m=dbOpenCellViewByType("analogLib" "dcblock" "symbol" "" "r")
unless(m error("Ideal DC block is unavailable"))
i=dbCreateInst(cv m {block} list({x} -2) "R0")
calStub(cv i "PLUS" {probe}) calStub(cv i "MINUS" {node})
'''.format(port=sq('PORT_'+name), x=-5+index*2, num=sq(str(index)), probe=sq('CAL_PROBE_'+name),
           block=sq('DC_BLOCK_'+name), node=sq('CAL_'+name))
        else:
            sources[:0] = [('VP', 'CAL_P', 'CAL_VSS', 'CAL_VCM', '0.5', '0'),
                           ('VN', 'CAL_N', 'CAL_VSS', 'CAL_VCM', '0.5', '180')]
        if not cfg.get('explicit'):
            for bit in range(cfg['bits']):
                expression = 'CAL_VDD*(floor(CAL_CODE/{})-2*floor(CAL_CODE/{}))'.format(2**bit, 2**(bit+1))
                sources.append(('VB{}'.format(bit), 'CAL_B<{}>'.format(bit), 'CAL_VSS', expression, '0', '0'))
        for n, v in cfg['fixed'].items():
            sources.append(('V' + n, n, 'CAL_VSS', str(v), '0', '0'))
        for n, src in enumerate(sources):
            name, plus, minus, dc, mag, phase = src
            body += 'calSource(cv {} list({} {}) {} {} {} {} {})\n'.format(sq(name), -5 + (n % 5)*2, 4+(n//5)*2, sq(plus), sq(minus), sq(dc), sq(mag), sq(phase))
        body += '''g=dbOpenCellViewByType("analogLib" "gnd" "symbol" "" "r")
i=dbCreateInst(cv g "GND" list(-7 4) "R0")
calStub(cv i "gnd!" "gnd!")
schCheck(cv) dbSave(cv) dbClose(cv)
cv=dbOpenCellViewByType(%s %s "schematic" "" "r")
o=outfile(%s)
foreach(i cv~>instances foreach(it i~>instTerms fprintf(o "%%s\\t%%s\\t%%s\\n" i~>name it~>name it~>net~>name)))
close(o) dbClose(cv)
''' % (sq(library), sq(cell), sq(folder / 'connectivity.tsv'))
        # Prepare the new config before entering Cadence, then create the OA
        # schematic and Maestro setup in one owned native session.
        self.activity('Preparing private config…', 0, 3)
        # Native config text format, independently editable; never touches source config.
        config = cellpath / 'config'
        config.mkdir(parents=True)
        bindings = []
        for inst in enabled_instances:
            if not re.fullmatch(r'[A-Za-z0-9_]+', inst['view']):
                raise ValueError('View names must use letters, digits, and underscores.')
            if inst['view']:
                bindings.append('inst ({}.{}:schematic).{} binding :{};'.format(library, cell, inst['id'], inst['view']))
        text = '//Revision 1\nconfig {};\ndesign {}.{}:schematic;\nliblist {};\nviewlist spectre, cmos_sch, schematic, veriloga, ahdl, extracted, av_extracted;\nstoplist spectre;\n{}\nendconfig\n'.format(cell, library, cell, ', '.join(libs), '\n'.join(bindings))
        if project.get('config_template'):
            text = project['config_template'].replace('@CAL_TOP@', library + '.' + cell).replace('@CAL_CELL@', cell)
        (config / 'expand.cfg').write_text(text, encoding='utf-8')
        (config / 'master.tag').write_text('-- Master.tag File, Rev:1.0\nexpand.cfg\n', encoding='utf-8')
        body += '\nprintf("CAL_ACTIVITY\\t1\\t3\\tPrivate schematic ready\\n")\n'
        from .source_hierarchy import projected_binding_skill
        if not project.get('config_template'):
            enabled_project = dict(project, instances=enabled_instances)
            body += projected_binding_skill(enabled_project, library, cell)
        body += self._setup_body(project, manifest)
        body += '\nprintf("CAL_ACTIVITY\\t2\\t3\\tPrivate Maestro ready\\n")\n'
        self.native(self.context(project), body, folder)
        source = runtime.get('source') or {}
        if source:
            test = selected_test(runtime)
            generated = cellpath / 'maestro'
            candidate = cellpath / ('maestro_projected_' + uuid.uuid4().hex[:8])
            if loss_plan is not None:
                from .backend_rf import project_rf_setup
                removed = project_rf_setup(source['setup_dir'], str(generated), str(candidate), test, (runtime.get('overrides') or {}).get('run_mode'))
            else:
                removed = project_tool_setup(source['setup_dir'], str(generated), str(candidate), test, (runtime.get('overrides') or {}).get('run_mode'))
            if any(hashlib.sha256((Path(source['setup_dir']) / name).read_bytes()).hexdigest() != value for name, value in baseline.items()):
                raise ValueError('Source Maestro changed since models were read; rebuild from a stable saved setup.')
            original = cellpath / ('maestro_generated_' + uuid.uuid4().hex[:8])
            os.rename(str(generated), str(original))
            os.rename(str(candidate), str(generated))
            readback = folder / 'runtime-native-readback.txt'
            test_name = 'CAP_SP' if cfg.get('analysis') == 'sp' else 'CAP_AC'
            apply = runtime_apply_skill(runtime, test_name, str(readback), cell[-6:])
            probe = storage_apply_skill(runtime) + runtime_preopen_skill(runtime, self.native_home, cell[-6:]) + '''s=maeOpenSetup(%s %s "maestro" ?mode "a")
unless(s error("Cannot open projected private Maestro"))
setup=axlGetMainSetupDB(s)
tests=axlGetTests(setup)
unless(tests && member(%s cadr(tests)) error("Projected test readback differs"))
''' % (sq(library), sq(cell), sq(test_name))
            probe += apply + self._fixture_options_skill(test_name, cfg, readback)
            if loss_plan is not None:
                probe += 'unless(member("CAP_LOSS_CM" cadr(tests)) error("Projected loss test missing"))\n'
                probe += runtime_apply_skill(runtime, 'CAP_LOSS_CM', str(readback), cell[-6:])
                probe += self._fixture_options_skill('CAP_LOSS_CM', cfg, readback)
            probe += 'maeSaveSetup(?session s) maeCloseSession(?session s)\n'
            self.native(self.context(project), probe, folder)
            shutil.move(str(original), str(folder / 'generated_maestro'))
            manifest['runtime_projection'] = {'source_test': test, 'history_entries_removed': removed,
                                              'readback': str(readback), 'policies_captured': bool((runtime.get('snapshot') or {}).get('global', {}).get('available'))}
        self._check_connectivity(project, cfg, folder, loss_plan)
        self.activity('Private testbench ready', 3, 3, False)
        from .config_state import config_hash
        manifest['config_sha256'] = config_hash(manifest)
        (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        project['private'] = manifest
        return manifest

    @staticmethod
    def _check_connectivity(project, cfg, folder, loss_plan=None):
        actual = {}
        for line in (folder / 'connectivity.tsv').read_text().splitlines():
            i, pin, net = line.split('\t')
            actual[i + '.' + pin] = net
        for inst in all_nodes(project):
            for pin in inst['symbol']['pins']:
                expected = (loss_native.pin_label(loss_plan, inst['id'], pin['name'])
                            if loss_plan is not None and inst.get('kind') != 'register'
                            else ','.join(cfg['nets'][inst['id'] + '.' + p] for p in expand_pin(pin['name'])))
                key = inst['id'] + '.' + pin['name']
                got = actual.get(key)
                scalars = expand_pin(pin['name'])
                if got is None and len(scalars) == 1:
                    got = actual.get(inst['id']+'.'+scalars[0])
                if got != expected:
                    raise RuntimeError('Native connectivity mismatch: {} {} != {}'.format(key, got, expected))
        if loss_plan is not None:
            for probe in loss_plan['probes']:
                for term, expected in (('PLUS',probe['net']), ('MINUS',probe['node'])):
                    key = probe['name'] + '.' + term
                    if actual.get(key) != expected: raise RuntimeError('Native loss fixture mismatch: '+key)
        if cfg.get('decomposition'):
            probes = {'PORT0.PLUS':'CAL_P', 'PORT0.MINUS':'CAL_BIAS_N',
                      'DC_BIAS.PLUS':'CAL_BIAS_N', 'DC_BIAS.MINUS':'CAL_N'}
            for name in ('P', 'N'):
                probes.update({'PORT_'+name+'.PLUS':'CAL_PROBE_'+name,
                               'PORT_'+name+'.MINUS':'CAL_VSS',
                               'DC_BLOCK_'+name+'.PLUS':'CAL_PROBE_'+name,
                               'DC_BLOCK_'+name+'.MINUS':'CAL_'+name})
            for terminal, expected in probes.items():
                if actual.get(terminal) != expected:
                    raise RuntimeError('Native decomposition fixture mismatch: '+terminal)

    @staticmethod
    def _fixture_options_skill(test_name, cfg, report_path=None):
        test, temperature = sq(test_name), sq(cfg['temperature'])
        body = ("unless(maeSetSimOption({} ?options list(list(\"temp\" {})) ?session s) error(\"Cannot set fixture temperature\"))\n".format(test, temperature) +
                "unless(equal(maeGetSimOption({} ?option \"temp\" ?session s) {}) error(\"Fixture temperature readback mismatch\"))\n".format(test, temperature) +
                "ts=maeGetTestSession({} ?session s)\n".format(test) +
                "asiSetKeepOptionVal(ts 'save \"allpub\")\nasiSetKeepOptionVal(ts 'currents \"selected\")\n" +
                "unless(asiGetKeepOptionVal(ts 'save)==\"allpub\" && asiGetKeepOptionVal(ts 'currents)==\"selected\" error(\"Fixture saving readback mismatch\"))\n")
        if report_path:
            body += 'o=outfile(' + sq(report_path) + ' "a")\n'
            body += 'fprintf(o "temperature %L\\nsave %L\\ncurrents %L\\n" maeGetSimOption(' + test + ' ?option "temp" ?session s) asiGetKeepOptionVal(ts \'save) asiGetKeepOptionVal(ts \'currents))\nclose(o)\n'
        return body

    def _setup_body(self, project, m):
        cfg = m['cfg']
        test_name = 'CAP_SP' if cfg.get('analysis') == 'sp' else 'CAP_AC'
        body = '''envSetVal("asimenv.startup" "projectDir" 'string %s)
envSetVal("adexl.historyNamePrefix" "showNameHistoryForm" 'boolean nil)
s=maeOpenSetup(%s %s "maestro" ?mode "a")
unless(s error("Cannot create Maestro"))
unless(maeCreateTest("CAP_AC" ?lib %s ?cell %s ?view "config" ?simulator "spectre" ?session s) error("Cannot create AC test"))
''' % (sq(Path(m['folder']) / 'simulation'), sq(m['library']), sq(m['cell']), sq(m['library']), sq(m['cell']))
        models = 'list(' + ' '.join(sl(row) for row in m['models']) + ')'
        body += 'unless(maeSetEnvOption("CAP_AC" ?options list(list("modelFiles" {}) list("controlMode" "batch")) ?session s) error("Cannot set models"))\n'.format(models)
        values = 'list(' + ' '.join(sq('{:.17g}'.format(f)) for f in cfg['simulation_frequencies']) + ')'
        if cfg.get('analysis') == 'sp':
            ports = 'list("/PORT_P" "/PORT_N")' if cfg.get('decomposition') else 'list("/PORT0")'
            options = 'list(list("sweep" "Frequency") list("ports" {}) list("donoise" "no") list("useDiscrete" t) list("values" {}))'.format(ports, values)
            body += 'unless(maeSetAnalysis("CAP_AC" "sp" ?options {} ?session s) error("Cannot set SP analysis"))\n'.format(options)
        else:
            body += 'unless(maeSetAnalysis("CAP_AC" "ac" ?options list(list("sweep" "Frequency") list("useDiscrete" t) list("values" {})) ?session s) error("Cannot set AC analysis"))\n'.format(values)
        variables = [('CAL_POINT' if cfg.get('explicit') else 'CAL_CODE', ' '.join(str(c) for c in cfg['codes'])), ('CAL_VDD', str(cfg['vdd'])), ('CAL_VCM', str(cfg['vcm']))]
        if cfg.get('explicit'):
            variables += [(r['name'],code_expression(r,cfg['registers'],cfg['sweep_mode'])) for r in cfg['registers']]
        if m.get('loss_plan') is not None: variables.append(('CAL_LOSS_MODE', '0'))
        for key, value in variables:
            body += 'unless(maeSetVar({} {} ?typeValue list("CAP_AC") ?session s) error("Cannot set test variable"))\n'.format(sq(key), sq(value))
        body += 'maeSetEnableTestVar("CAP_AC" {} ?enabled t ?session s)\n'.format(sl([key for key,value in variables]))
        audit_outputs = [('CODE_VALUE', 'VAR("{}")'.format('CAL_POINT' if cfg.get('explicit') else 'CAL_CODE')), ('VDD_VALUE', 'VAR("CAL_VDD")'), ('VCM_VALUE', 'VAR("CAL_VCM")')]
        if cfg.get('explicit'):
            audit_outputs += [('REG_'+r['id'], 'VAR("{}")'.format(r['name'])) for r in cfg['registers']]
        for name, expr in audit_outputs:
            body += 'maeAddOutput({} "CAP_AC" ?expr {} ?plot t ?save t ?session s)\n'.format(sq(name), sq(expr))
        body += 'maeSetSimOption("CAP_AC" ?options list(list("temp" {})) ?session s)\n'.format(sq(cfg['temperature']))
        body += 'ts=maeGetTestSession("CAP_AC" ?session s)\n'
        body += 'asiSetKeepOptionVal(ts \'save "allpub")\nasiSetKeepOptionVal(ts \'currents "selected")\nunless(asiGetKeepOptionVal(ts \'save)=="allpub" && asiGetKeepOptionVal(ts \'currents)=="selected" error("Saving readback mismatch"))\n'
        y = "ypm('sp 1 1)" if cfg.get('analysis') == 'sp' else '-(i("/VP/PLUS" ?result "ac")-i("/VN/PLUS" ?result "ac"))/2'
        for idx, freq in enumerate(cfg['frequencies']):
            # Save G and B as well: Q is undefined at exactly zero real conductance.
            expressions = {}
            if cfg.get('decomposition'):
                samples = []
                for row, col in ((1, 1), (1, 2), (2, 1), (2, 2)):
                    sample = "value(ypm('sp {} {}) {})".format(row, col, freq)
                    samples.append(sample)
                    expressions['Y{}{}_G'.format(row,col)] = 'real({})'.format(sample)
                    expressions['Y{}{}_B'.format(row,col)] = 'imag({})'.format(sample)
                scalar = floating_y_expression(*samples)
                expressions.update(C='imag({})/(2*acos(-1)*{})'.format(scalar, freq),
                                   G='real({})'.format(scalar), B='imag({})'.format(scalar),
                                   Q='imag({})/abs(real({}))'.format(scalar, scalar))
            else:
                expressions.update(C='value(imag({}) {})/(2*acos(-1)*{})'.format(y, freq, freq),
                                   G='value(real({}) {})'.format(y, freq),
                                   B='value(imag({}) {})'.format(y, freq),
                                   Q='value(imag({}) {})/abs(value(real({}) {}))'.format(y, freq, y, freq))
            for key, expr in expressions.items():
                body += 'unless(maeAddOutput({} "CAP_AC" ?expr {} ?plot t ?save t ?session s) error("Cannot add output"))\n'.format(sq('{}_{}'.format(key, idx)), sq(expr))
        if m.get('loss_plan') is not None:
            body += loss_native.setup_skill(m['loss_plan'], 'CAP_AC', cfg['simulation_frequencies'])
        body = body.replace('CAP_AC', test_name)
        if m.get('loss_plan') is not None: body += self._loss_cm_setup_body(m)
        body += 'maeSaveSetup(?session s)\nmaeCloseSession(?session s)\n'
        return body

    def _loss_cm_setup_body(self, m):
        cfg=m['cfg']; test='CAP_LOSS_CM'
        body='unless(maeCreateTest({} ?lib {} ?cell {} ?view "config" ?simulator "spectre" ?session s) error("Cannot create common-mode loss test"))\n'.format(sq(test),sq(m['library']),sq(m['cell']))
        models='list('+' '.join(sl(row) for row in m['models'])+')'
        body+='unless(maeSetEnvOption({} ?options list(list("modelFiles" {}) list("controlMode" "batch")) ?session s) error("Cannot set loss model files"))\n'.format(sq(test),models)
        variables=[('CAL_POINT' if cfg.get('explicit') else 'CAL_CODE',' '.join(str(c) for c in cfg['codes'])),('CAL_VDD',str(cfg['vdd'])),('CAL_VCM',str(cfg['vcm'])),('CAL_LOSS_MODE','1')]
        if cfg.get('explicit'):
            variables += [(r['name'],code_expression(r,cfg['registers'],cfg['sweep_mode'])) for r in cfg['registers']]
        for name,value in variables:
            body+='unless(maeSetVar({} {} ?typeValue list({}) ?session s) error("Cannot set loss test variable"))\n'.format(sq(name),sq(value),sq(test))
        body+='maeSetEnableTestVar({} {} ?enabled t ?session s)\n'.format(sq(test),sl([name for name,value in variables]))
        output='VAR("{}")'.format('CAL_POINT' if cfg.get('explicit') else 'CAL_CODE')
        body+='unless(maeAddOutput("LOSS_CODE_VALUE" {} ?expr {} ?plot t ?save t ?session s) error("Cannot set loss identity output"))\n'.format(sq(test),sq(output))
        body+=self._fixture_options_skill(test,cfg)
        body+=loss_native.setup_skill(m['loss_plan'],test,cfg['simulation_frequencies'])
        return body

    def _setup(self, project, m):
        self.native(self.context(project), self._setup_body(project, m), m['folder'])

    def refresh(self, project):
        private = project.get('private') or {}
        cellpath = private.get('cellpath')
        if private.get('config_sha256') and cellpath and (Path(cellpath) / 'config' / 'expand.cfg').is_file():
            from .config_state import capture_saved_config
            capture_saved_config(project)
        m = project.get('private') or {}
        if m.get('digest') != digest(project):
            raise ValueError('Circuit or simulation settings changed. Run a new measurement first.')
        spectrum = m.get('spectrum')
        if not spectrum:
            file = Path(m.get('folder', '')) / 'spectrum.json'
            if not file.is_file():
                raise ValueError('No saved frequency spectrum. Run a new measurement first.')
            data = file.read_bytes()
            if hashlib.sha256(data).hexdigest() != m.get('spectrum_sha256'):
                raise ValueError('Saved frequency spectrum failed its integrity check.')
            spectrum = json.loads(data.decode('utf-8'))
        data = json.dumps(spectrum, sort_keys=True, separators=(',', ':')).encode('utf-8')
        if hashlib.sha256(data).hexdigest() != m.get('spectrum_data_sha256'):
            raise ValueError('Saved frequency spectrum failed its integrity check.')
        rows = resample(spectrum, project['settings']['frequencies'])
        self.progress({'kind': 'log', 'level': 'info', 'message': 'Observation frequencies updated from saved {} spectrum; no simulation was started.'.format(spectrum.get('analysis', 'small-signal').upper())})
        return rows

    def run(self, project, libs):
        private = project.get('private') or {}
        cellpath = private.get('cellpath')
        if private.get('config_sha256') and cellpath and (Path(cellpath) / 'config' / 'expand.cfg').is_file():
            from .config_state import capture_saved_config
            capture_saved_config(project)
        runtime = project.get('maestro_runtime') or {}
        self.native_home = None
        source = runtime.get('source') or {}
        storage = (runtime.get('snapshot') or {}).get('storage') or {}
        self.native_workdir = ((storage.get('source_work_dir') or {}).get('value') or source.get('workspace')) if source else None
        if self.native_workdir and not os.path.isdir(self.native_workdir):
            raise ValueError('Source Maestro working directory is unavailable.')
        self.native_environment = runtime.get('native_environment') or None
        self.native_virtuoso = (os.path.join(os.path.dirname(runtime['source']['ocean']), 'virtuoso')
                                if (runtime.get('source') or {}).get('ocean') else os.environ.get('CAPLAB_NATIVE_VIRTUOSO'))
        if self.native_environment and not self.native_virtuoso:
            raise ValueError('Invoking Virtuoso executable is unavailable.')
        if runtime.get('overrides') and not runtime.get('source'):
            raise ValueError('Select a source Maestro before applying runtime overrides.')
        cfg = validate(project)
        cfg['decomposition'] = bool(project['settings'].get('decomposition', False)) and cfg.get('analysis') == 'sp'
        m = project.get('private')
        schema = 2 if cfg['decomposition'] else 1
        if not m or m['digest'] != digest(project) or not Path(m['cellpath']).is_dir() or m.get('spectrum_schema') != schema or bool(m.get('cfg',{}).get('decomposition')) != cfg['decomposition']:
            m = self.build(project, libs)
        native_cfg = m['cfg']
        if runtime.get('source'):
            self.bind_private_home(runtime, m['folder'])
        self.progress({'kind': 'simulation', 'message': 'Preparing simulation', 'completed': 0, 'total': len(cfg['codes']), 'active': True})
        self.progress('Running private Maestro and evaluating C/Q…')
        out = Path(m['folder']) / ('results_' + uuid.uuid4().hex[:8] + '.tsv')
        body = '''envSetVal("asimenv.startup" "projectDir" 'string %s)
envSetVal("adexl.historyNamePrefix" "showNameHistoryForm" 'boolean nil)
envSetVal("adexl.simulation" "saveLastNHistoryEntries" 'int 1000)
h=hdbOpen(%s %s "config" "r" "CDBA")
unless(h error("Private config is invalid"))
unless(hdbGetTopLibName(h)==%s && hdbGetTopCellName(h)==%s && hdbGetTopViewName(h)=="schematic" error("Private config top identity changed"))
hdbClose(h)
s=maeOpenSetup(%s %s "maestro" ?mode "a")
unless(s error("Cannot open private Maestro"))
runId=maeRunSimulation(?waitUntilDone t ?returnRunId t ?session s)
unless(runId error("Simulation submission failed"))
maeWaitUntilDone('All ?session s)
maeExportOutputView(?fileName %s ?session s)
maeWaitUntilDone('All ?session s)
rd=axlGetRunData(s runId)
hist=axlGetHistoryName(rd)
r=maeReadResDB(?historyName hist ?session s)
unless(r error("Missing result database"))
o=outfile(%s)
fprintf(o "HISTORY\\t%%s\\n" hist)
foreach(point r->points() foreach(corner point->corners()
 code=nil foreach(p corner->params() when(p->name=="CAL_CODE" code=p->value))
 foreach(test corner->tests()
 fprintf(o "TEST\\t%%L\\t%%L\\t%%L\\t%%L\\t%%L\\t%%L\\n" point->id corner->name test->name test->status code test->resultsDir)
 foreach(v test->outputs()
 if(numberp(v->value) then fprintf(o "OUT\\t%%L\\t%%L\\t%%.17g\\n" v->name v->type float(v->value))
 else fprintf(o "OUT\\t%%L\\t%%L\\t%%L\\n" v->name v->type v->value)))
 when(test->status=='done && test->name==@CAL_MAIN_TEST@
  openResults(strcat(test->resultsDir "/psf"))
  calY=@CAL_Y_EXPRESSION@
  calX=drGetWaveformXVec(calY) calV=drGetWaveformYVec(calY)
  for(calI 0 drVectorLength(calX)-1
   fprintf(o "SPECTRUM\\t%%.17g\\t%%.17g\\t%%.17g\\n" drGetElem(calX calI) real(drGetElem(calV calI)) imag(drGetElem(calV calI)))))
 )))
close(o)
maeSetHistoryLock(hist t ?session s)
maeSaveSetup(?session s) maeCloseSession(?session s)
''' % (sq(Path(m['folder']) / 'simulation'), sq(m['library']), sq(m['cell']), sq(m['library']), sq(m['cell']), sq(m['library']), sq(m['cell']), sq(Path(m['folder'])/('evaluated_'+uuid.uuid4().hex[:8]+'.csv')), sq(out))
        if runtime.get('source'):
            storage = (runtime.get('snapshot') or {}).get('storage') or {}
            if (storage.get('project_dir') or {}).get('available') or 'project_dir' in (runtime.get('overrides') or {}):
                body = body.replace("envSetVal(\"asimenv.startup\" \"projectDir\" 'string " + sq(Path(m['folder']) / 'simulation') + ')\n', '', 1)
            body = storage_apply_skill(runtime) + runtime_preopen_skill(runtime, self.native_home, m['cell'][-6:]) + body
            test_name = 'CAP_SP' if native_cfg.get('analysis') == 'sp' else 'CAP_AC'
            body = body.replace('runId=maeRunSimulation', runtime_apply_skill(runtime, test_name, str(Path(m['folder']) / 'runtime-run-readback.txt'), m['cell'][-6:]) + self._fixture_options_skill(test_name, native_cfg, Path(m['folder']) / 'runtime-run-readback.txt') + 'runId=maeRunSimulation')
        expression = "ypm('sp 1 1)" if native_cfg.get('analysis') == 'sp' else '-(getData("VP:p" ?result "ac")-getData("VN:p" ?result "ac"))/2'
        body = body.replace('@CAL_Y_EXPRESSION@', expression)
        body = body.replace('@CAL_MAIN_TEST@', sq('CAP_SP' if native_cfg.get('analysis') == 'sp' else 'CAP_AC'))
        if native_cfg.get('decomposition'):
            start = body.index('  calY=')
            end = body.index('\n )))', start)
            matrix = '''  calY11=ypm('sp 1 1) calY12=ypm('sp 1 2)
  calY21=ypm('sp 2 1) calY22=ypm('sp 2 2)
  calX=drGetWaveformXVec(calY11)
  calV11=drGetWaveformYVec(calY11) calV12=drGetWaveformYVec(calY12)
  calV21=drGetWaveformYVec(calY21) calV22=drGetWaveformYVec(calY22)
  unless(drVectorLength(calX)==drVectorLength(calV12) && drVectorLength(calX)==drVectorLength(calV21) && drVectorLength(calX)==drVectorLength(calV22) error("Admittance matrix frequency coverage differs"))
  for(calI 0 drVectorLength(calX)-1
   calM11=drGetElem(calV11 calI) calM12=drGetElem(calV12 calI)
   calM21=drGetElem(calV21 calI) calM22=drGetElem(calV22 calI)
   calY={total}
   unless(calY error("Floating common-mode admittance is unresolved"))
   fprintf(o "SPECTRUM\\t%.17g\\t%.17g\\t%.17g\\t%.17g\\t%.17g\\t%.17g\\t%.17g\\t%.17g\\t%.17g\\t%.17g\\t%.17g\\n" drGetElem(calX calI) real(calY) imag(calY) real(calM11) imag(calM11) real(calM12) imag(calM12) real(calM21) imag(calM21) real(calM22) imag(calM22))))'''.format(
                total=floating_y_expression('calM11','calM12','calM21','calM22'))
            body = body[:start] + matrix + body[end:]
        if m.get('loss_plan') is not None:
            marker = '\n )))\nclose(o)'
            if marker not in body: raise RuntimeError('Cannot locate loss reader insertion point')
            pos = body.index(marker)
            before = body[:pos].rstrip()
            if not before.endswith(')'): raise RuntimeError('Cannot locate completed TEST body')
            body = (before[:-1] + '\n' + loss_native.reader_skill(m['loss_plan']) + ')' +
                    "\n when(test->status=='done && test->name==\"CAP_LOSS_CM\"\n  openResults(strcat(test->resultsDir \"/psf\"))\n" +
                    loss_native.reader_skill(m['loss_plan']) + ')\n' + body[pos:])
        simulation_root = (simulation_monitor_root(runtime, m) if runtime.get('source')
                           else str(Path(m['folder']) / 'simulation'))
        baseline = {}
        for old_log in Path(simulation_root).glob('**/spectre.out') if simulation_root else []:
            try:
                stat = old_log.stat()
                baseline[str(old_log.resolve())] = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                pass
        monitor = ({'root': str(simulation_root), 'total': len(cfg['codes']), 'codes':cfg['codes'], 'explicit':bool(cfg.get('explicit')), 'analysis':native_cfg.get('analysis'), 'tests':(['CAP_SP','CAP_LOSS_CM'] if m.get('loss_plan') is not None else ['CAP_SP' if native_cfg.get('analysis') == 'sp' else 'CAP_AC']), 'started': time.time(), 'baseline': baseline}
                   if simulation_root else None)
        for inst in project['instances']:
            if inst.get('enabled', True) and inst.get('kind') in ('passive','snp'):
                validate_device(inst, project['instances'])
                if inst.get('kind') == 'snp':
                    frozen = Path(m.get('frozen_snp', {}).get(inst['id'], ''))
                    if not frozen.is_file() or hashlib.sha256(frozen.read_bytes()).hexdigest() != inst['touchstone']['sha256']:
                        raise ValueError('Frozen Touchstone file failed integrity check')
        try:
            self.native(self.context(project), body, m['folder'], None if runtime.get('source') else max(1200, 45 * len(cfg['codes'])), monitor=monitor)
        finally:
            if monitor:
                self._monitor_simulation(monitor, {})
                (Path(m['folder'])/'last_attempt.json').write_text(json.dumps({'points':monitor.get('points',{}), 'results_file':str(out)}, indent=2), encoding='utf-8')
        # Independently reconcile initial Maestro scalars before accepting raw spectra.
        self.read_results(out, native_cfg)
        spectra = self.read_spectrum(out, native_cfg)
        if m.get('loss_plan') is not None:
            m['loss_spectrum'] = self.read_loss_spectrum(out, native_cfg, m['loss_plan'])
            payload = {'plan':m['loss_plan'], 'spectrum':m['loss_spectrum']}
            m['loss_data_sha256'] = hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()
        m['spectrum'] = {'version': schema, 'analysis': native_cfg.get('analysis', 'ac'), 'rows': spectra,
                         'interpolation': 'Linear in frequency on complex admittance; no extrapolation.'}
        if native_cfg.get('decomposition'):
            m['spectrum']['decomposition'] = True
            m['spectrum']['fixture'] = 'Two DC-blocked ground-reference ports; original differential DC termination retained through ideal DC feed.'
        data = json.dumps(m['spectrum'], sort_keys=True, separators=(',', ':')).encode('utf-8')
        m['spectrum_data_sha256'] = hashlib.sha256(data).hexdigest()
        spectrum_path = Path(m['folder']) / 'spectrum.json'
        spectrum_path.write_bytes(data)
        m['spectrum_sha256'] = hashlib.sha256(data).hexdigest()
        rows = resample(m['spectrum'], cfg['frequencies'])
        self.progress({'kind': 'simulation', 'message': 'Measurement complete', 'completed': len(cfg['codes']), 'total': len(cfg['codes']), 'active': False})
        m['point_logs'] = dict((monitor or {}).get('points', {}))
        m['history']=next(line.split('\t',1)[1] for line in out.read_text().splitlines() if line.startswith('HISTORY\t'))
        csvpath = Path(m['folder']) / 'cq.csv'
        with csvpath.open('w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(dict(row, decomposition=json.dumps(row['decomposition'],sort_keys=True)) if 'decomposition' in row else row for row in rows)
        m['result_sha256'] = hashlib.sha256(csvpath.read_bytes()).hexdigest()
        (Path(m['folder'])/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2),encoding='utf-8')
        return rows

    @staticmethod
    def read_loss_spectrum(path, cfg, plan):
        tests = []
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            fields = line.split('\t')
            if fields[0] == 'TEST':
                if len(fields) != 7: raise ValueError('Malformed native TEST identity')
                if fields[4] == 'disabled':
                    tests.append(None)
                    continue
                tests.append({'test': json.loads(fields[3]), 'status':fields[4], 'path':json.loads(fields[6]), 'outputs':{}, 'lines':[]})
            elif fields[0] == 'OUT' and tests and tests[-1] is not None:
                try: tests[-1]['outputs'][json.loads(fields[1])] = float(fields[3])
                except ValueError: pass
            elif fields[0].startswith('LOSS_'):
                if not tests or tests[-1] is None: raise ValueError('Loss record precedes enabled TEST identity')
                tests[-1]['lines'].append(line)
        pairs = {}
        for test in tests:
            if test is None: continue
            code = test['outputs'].get('LOSS_CODE_VALUE' if test['test']=='CAP_LOSS_CM' else 'CODE_VALUE')
            if test['test'] not in ('CAP_SP','CAP_LOSS_CM') or test['status'] != 'done' or code is None or not code.is_integer():
                raise ValueError('Invalid native loss TEST identity')
            index = int(code)
            base=Path(test['path']); log=base/'psf'/'spectre.out'; netlist=base/'netlist'/'input.scs'
            if not log.is_file() or not netlist.is_file(): raise ValueError('Loss test native evidence is missing')
            logtext=log.read_text(encoding='utf-8',errors='replace')
            if not re.search(r'0 errors?\b',logtext) or 'completes' not in logtext: raise ValueError('Loss test Spectre did not complete successfully')
            match=re.search(r'(?<![A-Za-z0-9_])CAL_LOSS_MODE\s*=\s*([01])(?:\D|$)',netlist.read_text(encoding='utf-8',errors='replace'))
            if not match or int(match.group(1)) != (1 if test['test']=='CAP_LOSS_CM' else 0): raise ValueError('Loss test excitation identity does not match')
            if index not in cfg['codes'] or test['test'] in pairs.get(index,{}): raise ValueError('Duplicate or unexpected native loss point')
            samples = loss_native.parse_records(test['lines'], plan)
            expected = cfg.get('simulation_frequencies', cfg['frequencies'])
            if len(samples) != len(expected) or any(not math.isclose(row['frequency_Hz'], f, rel_tol=1e-12, abs_tol=0) for row,f in zip(samples,expected)):
                raise ValueError('Native loss spectrum frequency coverage differs')
            pairs.setdefault(index,{})[test['test']] = samples
        if set(pairs) != set(cfg['codes']) or any(set(v) != {'CAP_SP','CAP_LOSS_CM'} for v in pairs.values()): raise ValueError('Missing native loss excitation or point')
        return {str(index):loss_native.combine_records(rows['CAP_SP'],rows['CAP_LOSS_CM'],plan) for index,rows in pairs.items()}

    @staticmethod
    def read_results(path, cfg):
        tests = []
        active = False
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            p = line.split('\t')
            if p[0] == 'TEST':
                active = p[4] != 'disabled' and json.loads(p[3]) == ('CAP_SP' if cfg.get('analysis', 'sp' if cfg.get('decomposition') else 'ac') == 'sp' else 'CAP_AC')
                if not active: continue
                tests.append({'point': p[1], 'corner': json.loads(p[2]), 'test': json.loads(p[3]),
                              'status': p[4], 'code': None if p[5]=='nil' else float(p[5]), 'path': json.loads(p[6]), 'outputs': {}})
            elif p[0] == 'OUT' and tests and active:
                try:
                    tests[-1]['outputs'][json.loads(p[1])] = float(p[3])
                except ValueError:
                    pass
        rows = []
        for t in tests:
            if t['status'] != 'done':
                base=Path(t['path'])
                log=base/'psf'/'spectre.out'
                detail=''
                if log.is_file():
                    fatal=[line.strip() for line in log.read_text(errors='replace').splitlines() if 'FATAL (' in line or 'ERROR (' in line]
                    detail=' · '+fatal[-1] if fatal else ''
                raise RuntimeError('Simulation point {} failed{}'.format(t['point'],detail))
            t['code'] = t['outputs'].get('CODE_VALUE', t['code'])
            if t['code'] is None:
                raise RuntimeError('Missing the actual code value for this test.')
            for name, value in [('VDD_VALUE', cfg['vdd']), ('VCM_VALUE', cfg['vcm'])]:
                if name not in t['outputs'] or not math.isclose(t['outputs'][name], value, rel_tol=1e-9, abs_tol=1e-12):
                    raise RuntimeError('Actual simulation bias does not match the requested setup.')
            if not float(t['code']).is_integer():
                raise RuntimeError('Actual register code is not an integer.')
            if t['status'] != 'done':
                raise RuntimeError('The Maestro measurement point has not completed successfully.')
            if t['test'] != ('CAP_SP' if cfg.get('analysis', 'sp' if cfg.get('decomposition') else 'ac') == 'sp' else 'CAP_AC'):
                raise RuntimeError('Result test identity does not match.')
            # RDB points identify the exact PSF; never select results by latest directory.
            base = Path(t['path'])
            logs = [base / 'psf' / 'spectre.out', base / 'spectre.out']
            log = next((p for p in logs if p.is_file()), None)
            if not log or not re.search(r'0 errors?\b', log.read_text(errors='replace')) or 'completes' not in log.read_text(errors='replace'):
                raise RuntimeError('Code {} has no successful zero-error Spectre completion.'.format(t['code']))
            for idx, freq in enumerate(cfg['frequencies']):
                outputs = t['outputs']
                if not all('{}_{}'.format(key, idx) in outputs for key in ('C', 'G', 'B')):
                    raise RuntimeError('C/Q evaluation is incomplete for code {}.'.format(t['code']))
                y = complex(outputs['G_{}'.format(idx)], outputs['B_{}'.format(idx)])
                c, q, status = cq(y, freq)
                split = None
                if cfg.get('decomposition'):
                    matrix = []
                    for ij in ('11', '12', '21', '22'):
                        keys = ('Y{}_G_{}'.format(ij,idx), 'Y{}_B_{}'.format(ij,idx))
                        if any(key not in outputs or not math.isfinite(outputs[key]) for key in keys):
                            raise RuntimeError('Native two-port matrix evaluation is incomplete.')
                        matrix.append(complex(*(outputs[key] for key in keys)))
                    split = decompose(*(matrix+[freq]))
                    if split['total'] is None:
                        raise RuntimeError('Native matrix has no identified floating total: '+split['message'])
                    reconstructed = complex(split['total']['G_S'], split['total']['B_S'])
                    if abs(reconstructed-y) > max(abs(y),max(abs(v) for v in matrix),1e-300)*1e-6:
                        raise RuntimeError('Native scalar total does not match the complete admittance matrix.')
                native_c = outputs['C_{}'.format(idx)]
                if not math.isfinite(c) or not math.isclose(c, native_c, rel_tol=1e-8, abs_tol=1e-30):
                    raise RuntimeError('Maestro capacitance does not match the saved admittance.')
                if q is not None and (split is None or split['total']['Q'] is not None):
                    native_q = outputs.get('Q_{}'.format(idx))
                    if native_q is None or not math.isfinite(native_q) or not math.isclose(q, native_q, rel_tol=1e-8, abs_tol=1e-20):
                        raise RuntimeError('Maestro Q does not match the saved admittance.')
                if cfg.get('explicit'):
                    index=int(t['code'])
                    if not 0<=index<len(cfg['scan_points']):raise RuntimeError('Scan point identity does not match.')
                    row={'point':index}
                    for reg in cfg['registers']:
                        value=outputs.get('REG_'+reg['id'])
                        expected_code=cfg['scan_points'][index][reg['name']]
                        if value is None or value!=expected_code:
                            raise RuntimeError('Actual register {} does not match the scan plan.'.format(reg['name']))
                        row[reg['name']]=int(value)
                else:row={'code':int(t['code'])}
                row.update(frequency_Hz=freq,C_F=c,Q=q,G_S=y.real,B_S=y.imag,status=status)
                rows.append(row)
        key='point' if cfg.get('explicit') else 'code'
        expected = {(c, f) for c in cfg['codes'] for f in cfg['frequencies']}
        if {(r[key], r['frequency_Hz']) for r in rows} != expected or len(rows) != len(expected):
            raise RuntimeError('Returned sample count does not match the request; incomplete curves were withheld.')
        return sorted(rows, key=lambda r: (r['frequency_Hz'], r[key]))

    @staticmethod
    def read_spectrum(path, cfg):
        tests = []
        active = False
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            fields = line.split('\t')
            if fields[0] == 'TEST':
                active = fields[4] != 'disabled' and json.loads(fields[3]) == ('CAP_SP' if cfg.get('analysis', 'sp' if cfg.get('decomposition') else 'ac') == 'sp' else 'CAP_AC')
                if active: tests.append({'status': fields[4], 'outputs': {}, 'samples': []})
            elif fields[0] == 'OUT' and tests and active:
                try:
                    tests[-1]['outputs'][json.loads(fields[1])] = float(fields[3])
                except ValueError:
                    pass
            elif fields[0] == 'SPECTRUM' and tests and active:
                tests[-1]['samples'].append(tuple(float(value) for value in fields[1:]))
        expected = cfg.get('simulation_frequencies', cfg['frequencies'])
        rows, seen = [], set()
        for test in tests:
            if test['status'] != 'done':
                raise RuntimeError('A spectrum belongs to an incomplete simulation.')
            value = test['outputs'].get('CODE_VALUE')
            if value is None or not value.is_integer():
                raise RuntimeError('Missing actual spectrum code identity.')
            code = int(value)
            if code in seen:
                raise RuntimeError('Duplicate simulation spectrum identity.')
            seen.add(code)
            if cfg.get('explicit'):
                if not 0 <= code < len(cfg['scan_points']):
                    raise RuntimeError('Spectrum point is outside the scan plan.')
                identity = dict(cfg['scan_points'][code], point=code)
            else:
                identity = {'code': code}
            samples = sorted(test['samples'])
            if len(samples) != len(expected) or any(not math.isclose(a[0], b, rel_tol=1e-12, abs_tol=0) for a, b in zip(samples, expected)):
                raise RuntimeError('Saved spectrum does not cover all requested simulation frequencies.')
            for sample in samples:
                if len(sample) != (11 if cfg.get('decomposition') else 3):
                    raise RuntimeError('Saved spectrum is missing its complete two-port matrix.' if cfg.get('decomposition') else 'Saved single-port spectrum has unexpected fields.')
                freq, g, b = sample[:3]
                if not all(math.isfinite(v) for v in sample):
                    raise RuntimeError('Simulator returned a non-finite admittance sample.')
                row = dict(identity)
                row.update(frequency_Hz=freq, G_S=g, B_S=b)
                if cfg.get('decomposition'):
                    matrix = []
                    for offset,ij in enumerate(('11','12','21','22')):
                        re_y,im_y = sample[3+2*offset:5+2*offset]
                        row['Y{}_G_S'.format(ij)] = re_y
                        row['Y{}_B_S'.format(ij)] = im_y
                        matrix.append(complex(re_y,im_y))
                    split = decompose(*(matrix+[freq]))
                    if split['total'] is None:
                        raise RuntimeError('Saved matrix has no identified floating total: '+split['message'])
                    reconstructed = complex(split['total']['G_S'],split['total']['B_S'])
                    if abs(reconstructed-complex(g,b)) > max(abs(complex(g,b)),max(abs(v) for v in matrix),1e-300)*1e-6:
                        raise RuntimeError('Saved matrix and native total admittance disagree.')
                rows.append(row)
        if seen != set(cfg['codes']):
            raise RuntimeError('Saved spectrum does not cover the complete register scan.')
        return rows
