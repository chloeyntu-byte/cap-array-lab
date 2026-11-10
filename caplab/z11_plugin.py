"""Launch unmodified Z11 with an independent native-schematic broker.

The host survives its parent UI and owns only its private request directory and
its own Z11/Cadence processes. The Z11 source tree is never modified.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from .backend import Backend, sq
from .model import ROOT

_RUNNING = {}
_CELL = re.compile(r'^\s*\(\s*"([A-Za-z][A-Za-z0-9_]*)"\s+\(', re.S)


def discover(plugin_dir=None, project=None):
    """Find an installed, intact Z11. Explicit choices take precedence."""
    project=project or {}
    explicit=plugin_dir or os.environ.get('CAPLAB_Z11_PATH') or (project.get('plugins') or {}).get('z11_path')
    candidates=[explicit] if explicit else [ROOT/'caplab'/'z11']
    for candidate in candidates:
        if not candidate:continue
        path=Path(candidate).expanduser().resolve()
        if all((path/name).is_file() for name in ('z11_launch','z11_integration.py','z11_schematic.il')):
            return str(path)
    raise ValueError('Z11 installation not found; set CAPLAB_Z11_PATH or plugins.z11_path.')


def _runtime_copy(source):
    """Run a private copy: original Z11 writes saved_state next to its code."""
    source=Path(source); digest=hashlib.sha256()
    for path in sorted(source.rglob('*')):
        if not path.is_file() or '__pycache__' in path.parts or '.bak-' in path.name:continue
        if 'saved_state' in path.parts or 'saved_structures' in path.parts:continue
        digest.update(str(path.relative_to(source)).encode('utf-8'));digest.update(path.read_bytes())
    root=ROOT/'.work'/'z11_installations';root.mkdir(parents=True,exist_ok=True)
    target=root/digest.hexdigest()[:20]
    if not target.is_dir():
        temp=Path(tempfile.mkdtemp(prefix='staging_',dir=str(root)))
        try:
            shutil.copytree(str(source),str(temp/'install'),ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.bak-*'))
            try:os.rename(str(temp/'install'),str(target))
            except OSError:
                if not target.is_dir():raise
        finally:shutil.rmtree(str(temp),ignore_errors=True)
    return str(target)


def launch_standalone():
    """Check optional dependencies off the Tk thread before launching offline."""
    python=os.environ.get('Z11_PYTHON',sys.executable)
    check=subprocess.run([python,'-c','import numpy, matplotlib, tkinter'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
    if check.returncode:
        detail=check.stderr.decode('utf-8',errors='replace').strip().splitlines()
        raise RuntimeError('Z11 Studio needs NumPy, Matplotlib and Tk. Install requirements-z11.txt in your environment or select Z11_PYTHON. '+(detail[-1] if detail else 'Dependency check failed.'))
    plugin=Path(_runtime_copy(discover()))
    return subprocess.Popen([python,str(plugin/'z11_integration.py')],cwd=str(plugin),env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))


def source_identity(project):
    runtime=project.get('maestro_runtime') or {};source=runtime.get('source') or {}
    reference=project.get('reference') or {}
    library=source.get('library') or project.get('library')
    workspace=source.get('workspace') or project.get('workspace')
    cell=source.get('cell') or reference.get('cell') or 'CAPLAB'
    view=source.get('view') or 'schematic'
    if not library or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',library):raise ValueError('Select a valid target library before opening Z11.')
    if not workspace or not os.path.isdir(workspace):raise ValueError('The project workspace is unavailable.')
    if any(not isinstance(v,str) or not v or any(c in v for c in '\t\r\n') for v in (library,cell,view,workspace)):
        raise ValueError('Invalid Z11 source identity.')
    return {'library':library,'cell':cell,'view':view,'workspace':str(Path(workspace).resolve())}


def launch_z11(project, plugin_dir=None):
    """Launch or reuse a host for this exact source context; return Popen.

    Bootstrap and native errors are recorded in process.caplab_z11_dir/error.txt.
    """
    plugin=_runtime_copy(discover(plugin_dir,project));identity=source_identity(project)
    frozen=copy.deepcopy(project);frozen.pop('private',None)
    key=hashlib.sha256(json.dumps([plugin,identity,frozen.get('maestro_runtime'),frozen.get('extra_libraries')],sort_keys=True,default=str).encode('utf-8')).hexdigest()
    existing=_RUNNING.get(key)
    if existing is not None and existing.poll() is None:return existing
    root=ROOT/'.work'/'z11_hosts';root.mkdir(parents=True,exist_ok=True)
    folder=Path(tempfile.mkdtemp(prefix='host_',dir=str(root)));os.chmod(str(folder),0o700)
    config={'project':frozen,'identity':identity,'plugin':plugin,'directory':str(folder)}
    (folder/'host.json').write_text(json.dumps(config,ensure_ascii=True),encoding='utf-8')
    output=(folder/'host.log').open('ab')
    try:
        proc=subprocess.Popen([sys.executable,'-m','caplab.z11_plugin','--host',str(folder/'host.json')],cwd=str(ROOT),stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
    finally:output.close()
    proc.caplab_z11_dir=str(folder);_RUNNING[key]=proc
    return proc


def _backend(project):
    backend=Backend();runtime=project.get('maestro_runtime') or {};source=runtime.get('source') or {}
    storage=(runtime.get('snapshot') or {}).get('storage') or {}
    backend.native_workdir=((storage.get('source_work_dir') or {}).get('value') or source.get('workspace')) if source else None
    backend.native_environment=runtime.get('native_environment') or None
    backend.native_virtuoso=(os.path.join(os.path.dirname(source['ocean']),'virtuoso') if source.get('ocean') else os.environ.get('CAPLAB_NATIVE_VIRTUOSO'))
    if backend.native_environment and not backend.native_virtuoso:raise ValueError('Invoking Virtuoso executable is unavailable.')
    return backend


def _host(config):
    folder=Path(config['directory']);project=config['project'];source=config['identity'];plugin=Path(config['plugin'])
    backend=_backend(project);workspace=Backend.context(project)
    out=folder/'library_path.txt'
    body='''lib=ddGetObj({lib})
unless(lib && lib~>readPath error("Z11 target library unavailable"))
o=outfile({out}) fprintf(o "%s" lib~>readPath) close(o)
'''.format(lib=sq(source['library']),out=sq(out))
    backend.native(workspace,body,folder/'probe',timeout=180)
    libpath=out.read_text(encoding='utf-8').strip()
    if not libpath or any(c in libpath for c in '\t\r\n'):raise ValueError('Invalid resolved target library path.')
    context=['Z11_CONTEXT_1','C\t'+'\t'.join(json.dumps(v,ensure_ascii=True) for v in (source['library'],source['cell'],source['view'],source['workspace'],libpath,str(folder)))]
    (folder/'context.tsv').write_text('\n'.join(context)+'\n',encoding='utf-8')
    gui=subprocess.Popen([os.environ.get('Z11_PYTHON', sys.executable),str(plugin/'z11_integration.py'),'--maestro-context-file',str(folder/'context.tsv')],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.STDOUT,start_new_session=True)
    (folder/'gui.pid').write_text(str(gui.pid),encoding='ascii')
    try:
        while gui.poll() is None and not (folder/'closed').exists():
            request=folder/'request.data'
            if request.is_file():
                try:
                    data=request.read_text(encoding='utf-8');match=_CELL.match(data)
                    if not match or len(data)>5*1024*1024:raise ValueError('Invalid Z11 export request.')
                    cell=match.group(1)
                    body='''load({skill})
in=infile({request}) unless(in error("Cannot read Z11 request"))
data=read(in) close(in)
unless(listp(data) && length(data)==2 && equal(car(data) {cell}) error("Invalid Z11 request"))
unless(z11CreateSchematic({lib} car(data) cadr(data) {path}) error("Z11 creation failed"))
'''.format(skill=sq(plugin/'z11_schematic.il'),request=sq(request),cell=sq(cell),lib=sq(source['library']),path=sq(libpath))
                    backend.native(workspace,body,folder/'jobs',timeout=600)
                    result='OK\t{}\t{}\tschematic\tcreated\n'.format(source['library'],cell)
                except Exception as exc:
                    result='ERROR\n'+str(exc).replace('\n',' ')+'\n'
                try:request.unlink()
                except OSError:pass
                tmp=folder/'result.tmp';tmp.write_text(result,encoding='utf-8');os.replace(str(tmp),str(folder/'result.tsv'))
            else:time.sleep(.2)
    finally:
        if gui.poll() is None:gui.wait()


def main():
    if len(sys.argv)!=3 or sys.argv[1]!='--host':raise SystemExit('Internal Z11 host entry point.')
    config=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
    try:_host(config)
    except Exception as exc:
        Path(config['directory'],'error.txt').write_text(str(exc)+'\n',encoding='utf-8')
        raise


if __name__=='__main__':main()
