"""One owned native Hierarchy Editor session."""
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from .model import ROOT
from .backend import sq


class ConfigViewer:
    def __init__(self):
        self.process=None
        self.folder=None
        self.workspace=None
        self.identity=None
        self.lock=threading.Lock()

    def open(self,workspace,library,cell,view="config",mode="a",
             native_environment=None,native_virtuoso=None,cds_lib=None,native_workdir=None):
        with self.lock:
            return self._open(workspace,library,cell,view,mode,native_environment,
                              native_virtuoso,cds_lib,native_workdir)

    def _open(self,workspace,library,cell,view="config",mode="a",
              native_environment=None,native_virtuoso=None,cds_lib=None,native_workdir=None):
        if mode not in ("r", "a"):
            raise ValueError("Native config mode must be r or a.")
        workspace=str(Path(workspace).resolve())
        if not os.environ.get('DISPLAY'):
            raise ValueError('Start the workbench on a graphical desktop to open native config.')
        mapping = str(Path(cds_lib).resolve()) if cds_lib else None
        if mapping and not Path(mapping).is_file():
            raise ValueError('Source cds.lib is unavailable.')
        if native_environment and not native_virtuoso:
            raise ValueError('Invoking Virtuoso executable is unavailable.')
        native_workdir=str(Path(native_workdir).resolve()) if native_workdir else None
        identity=json.dumps([workspace,mapping,native_environment,native_virtuoso,native_workdir],sort_keys=True)
        fresh=not self.process or self.process.poll() is not None or self.identity!=identity
        if fresh:
            self.folder=ROOT/'.work'/('config_viewer_'+uuid.uuid4().hex[:8])
            self.folder.mkdir(parents=True)
            self.workspace=workspace
            self.identity=identity
            if not native_workdir and mapping and mapping != str((Path(workspace)/'cds.lib').resolve()):
                (self.folder/'cds.lib').write_text('INCLUDE '+sq(mapping)+'\n',encoding='utf-8')
                workspace=str(self.folder)
        request=self.folder/'request.il';ack=self.folder/('ack_'+uuid.uuid4().hex[:8])
        text='''progn(
envSetVal("ddserv.he" "openConfig" 'cyclic "config")
calOpened=errset(deOpenCellView(%s %s %s "config" nil %s) t)
calAck=outfile(%s)
if(calOpened && car(calOpened) then fprintf(calAck "OK") else fprintf(calAck "ERROR"))
close(calAck)
)
''' % (sq(library),sq(cell),sq(view),sq(mode),sq(ack))
        tmp=request.with_suffix('.tmp');tmp.write_text(text,encoding='utf-8');tmp.replace(request)
        if fresh:
            script=self.folder/'viewer.il'
            script.write_text('''procedure(calPollConfig()
 when(isFile(%s)
  errset(load(%s) t)
  deleteFile(%s))
 hiRegTimer("calPollConfig()" 10)
)
hiRegTimer("calPollConfig()" 1)
''' % (sq(request),sq(request),sq(request)),encoding='utf-8')
            env=os.environ.copy();env['LC_ALL']='en_US.utf8'
            for key in list(env):
                if key.startswith('CDS_MPS_') or key in ('CDS_LIB_PATH','CDS_LOAD_PATH'):
                    env.pop(key,None)
            if native_workdir:
                env['CAPLAB_NATIVE_CDS_LIB']=mapping or str((Path(workspace)/'cds.lib').resolve())
            else:
                env.pop('CAPLAB_NATIVE_CDS_LIB',None)
            if native_environment:
                env['CAPLAB_NATIVE_ENV_JSON']=json.dumps(native_environment)
                env['CAPLAB_NATIVE_VIRTUOSO']=native_virtuoso
            else:
                env.pop('CAPLAB_NATIVE_ENV_JSON',None)
                env.pop('CAPLAB_NATIVE_VIRTUOSO',None)
            with (self.folder/'viewer.stdout').open('w') as stream:
                self.process=subprocess.Popen(['/bin/bash',str(ROOT/'cadence-child.sh'),str(script),str(self.folder/'viewer.log'),'gui'],cwd=native_workdir or workspace,env=env,stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
        start=time.monotonic()
        while not ack.exists():
            if self.process.poll() is not None:raise RuntimeError('The native config viewer could not start.')
            if time.monotonic()-start>120:raise RuntimeError('Opening the native config viewer timed out.')
            time.sleep(.2)
        if ack.read_text()!='OK':raise RuntimeError('Could not open the requested native config.')
        return str(self.folder)
