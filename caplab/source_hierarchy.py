"""Read source config occurrence bindings through Cadence HDB, without editing it.

The public HDB/PCDB traversal is adapted from maestro-circuit-optimizer's
config_hierarchy.py. Every occurrence remains distinct by its full path.
"""
import ast
import json
import os
import subprocess
import tempfile
from pathlib import Path
from .backend import sq
from .model import ROOT

_CONFIG_SKILL = '\nmcoConfigStack = nil\nmcoConfigPcdbSeen = makeTable(\'mcoConfigPcdbSeen \'mcoNotSeen)\nmcoConfigViewTypes = makeTable(\'mcoConfigViewTypes \'mcoNotSeen)\nprocedure(mcoConfigIsSchematic(viewName viewType)\n  stringp(viewName) && rexMatchp("^schematic" viewName) && viewType == "schematic"\n)\nprocedure(mcoConfigFailure(kind path detail)\n  fprintf(mcoOut "%s\\t%L\\t%L\\n" kind path detail)\n)\nprocedure(mcoConfigPcdb(libName cellName viewName path)\n  prog((key cached pcId masters header insts pcInst info index count ignored)\n    key = list(libName cellName viewName)\n    cached = mcoConfigPcdbSeen[key]\n    unless(cached == \'mcoNotSeen return(cached))\n    pcId = pcdbOpen(libName cellName viewName "r" "CDBA")\n    unless(pcId\n      mcoConfigFailure("CONFIG_PCDB_ERROR" path "read-only parent/child database could not be opened")\n      return(nil)\n    )\n    index = makeTable(\'mcoConfigPcdbInstances nil)\n    count = 0\n    foreach(ignored list(nil t)\n    masters = if(ignored then pcdbGetIgnoredInstMasterGen(pcId) else pcdbGetInstMasterGen(pcId))\n    header = pcdbNextInstMaster(masters)\n    while(header\n      info = list(pcdbInstMasterLib(header) pcdbInstMasterCell(header)\n        pcdbInstMasterView(header) pcdbInstMasterLibFixed(header)\n        pcdbInstMasterCellFixed(header) pcdbInstMasterViewFixed(header) ignored)\n      insts = pcdbGetInstGen(header)\n      pcInst = pcdbNextInst(insts)\n      while(pcInst\n        index[pcdbInstName(pcInst)] = info\n        count = count + 1\n        pcInst = pcdbNextInst(insts)\n      )\n      header = pcdbNextInstMaster(masters)\n    )\n    )\n    pcdbClose(pcId)\n    cached = list(index count)\n    mcoConfigPcdbSeen[key] = cached\n    return(cached)\n  )\n)\nprocedure(mcoConfigOpen(libName cellName viewName path)\n  prog((config)\n    unless(isCallable(\'hdbOpen) && isCallable(\'hdbHasIterInstBitRules)\n      mcoConfigFailure("CONFIG_API_ERROR" path "required public HDB API is unavailable")\n      return(nil)\n    )\n    config = hdbOpen(libName cellName viewName "r" "CDBA")\n    unless(config\n      mcoConfigFailure("CONFIG_OPEN_ERROR" path "config could not be opened read-only")\n      return(nil)\n    )\n    fprintf(mcoOut "CONFIG_DEP\\t%L\\t%L\\t%L\\n" libName cellName viewName)\n    when(hdbHasIterInstBitRules(config)\n      mcoConfigFailure("CONFIG_BIT_RULES_UNSUPPORTED" path "iterated-instance bit-specific rules require separate per-bit traversal")\n      hdbClose(config)\n      return(nil)\n    )\n    return(config)\n  )\n)\nprocedure(mcoConfigIgnoreReason(inst)\n  let((master)\n    master = inst~>master\n    ; PCDB "ignored" means not a sub-design, not ignored by the netlister.\n    ; Read the explicit OA action without obtaining any instance CDF. Native\n    ; schematic port/ground markers are metadata even without an nlAction.\n    cond(\n      (inst~>nlAction == "ignore" "instance nlAction=ignore")\n      (master~>nlAction == "ignore" "master nlAction=ignore")\n      ((master~>libName == "basic" && member(master~>cellName list("ipin" "opin" "iopin"))) "schematic port marker")\n      ((master~>libName == "analogLib" && master~>cellName == "gnd") "schematic ground marker")\n    )\n  )\n)\nprocedure(mcoConfigIsIgnored(inst)\n  if(mcoConfigIgnoreReason(inst) then t else nil)\n)\nprocedure(mcoConfigViewType(libName cellName viewName path)\n  prog((key cached masterFile readable streamResult typeResult)\n    key = list(libName cellName viewName)\n    cached = mcoConfigViewTypes[key]\n    unless(cached == \'mcoNotSeen return(cached))\n    masterFile = ddGetObj(libName cellName viewName "*")\n    unless(masterFile\n      mcoConfigFailure("CONFIG_VIEW_MISSING" path "resolved implementation has no master file")\n      return(nil)\n    )\n    streamResult = errset(infile(masterFile~>readPath) t)\n    unless(streamResult && car(streamResult)\n      mcoConfigFailure("CONFIG_VIEW_UNREADABLE" path "resolved implementation master file is not readable")\n      return(nil)\n    )\n    close(car(streamResult))\n    typeResult = errset(ddMapGetFileViewType(masterFile) t)\n    unless(typeResult\n      mcoConfigFailure("CONFIG_API_ERROR" path "resolved implementation view type lookup failed")\n      return(nil)\n    )\n    cached = if(car(typeResult) then car(typeResult) else "unknown")\n    mcoConfigViewTypes[key] = cached\n    return(cached)\n  )\n)\nprocedure(mcoProbeConfig(libName cellName viewName path depth)\n  prog((identity config topLib topCell topView vector)\n    identity = list(libName cellName viewName)\n    when(member(identity mcoConfigStack)\n      mcoConfigFailure("CONFIG_CYCLE" path identity)\n      return(nil)\n    )\n    config = mcoConfigOpen(libName cellName viewName path)\n    unless(config return(nil))\n    topLib = hdbGetTopLibName(config)\n    topCell = hdbGetTopCellName(config)\n    topView = hdbGetTopViewName(config)\n    unless(topLib && topCell && topView && topLib != "" && topCell != "" && topView != ""\n      mcoConfigFailure("CONFIG_TOP_ERROR" path "config has no complete top identity")\n      hdbClose(config)\n      return(nil)\n    )\n    when(path == "/"\n      fprintf(mcoOut "RESOLVED_TOP\\t%L\\t%L\\t%L\\n" topLib topCell topView)\n    )\n    vector = hdbCreatePathVector(config)\n    unless(vector\n      mcoConfigFailure("CONFIG_API_ERROR" path "HDB path vector could not be created")\n      hdbClose(config)\n      return(nil)\n    )\n    mcoConfigStack = cons(identity mcoConfigStack)\n    mcoProbeCell(topLib topCell topView path depth\n      list(vector libName cellName viewName path) nil)\n    mcoConfigStack = cdr(mcoConfigStack)\n    hdbDestroyPathVector(vector)\n    hdbClose(config)\n    return(t)\n  )\n)\nprocedure(mcoConfigBind(context pcdb inst ownerLib ownerCell ownerView parentPath instPath)\n  prog((flags master vector binding boundLib boundCell boundView resolvedLib resolvedCell\n        resolvedView stop config topLib topCell localPath viewType ignored traversable symbolMasked)\n    master = inst~>master\n    flags = car(pcdb)[inst~>name]\n    unless(flags && car(flags) == master~>libName &&\n      cadr(flags) == master~>cellName && nth(2 flags) == master~>viewName\n      mcoConfigFailure("CONFIG_PCDB_ERROR" instPath\n        sprintf(nil "PCDB and actual OA instance identities disagree: PCDB=%L OA=%L nlAction=%L; save/check the source schematic and inspect this occurrence"\n          flags list(master~>libName master~>cellName master~>viewName) inst~>nlAction))\n      return(nil)\n    )\n    vector = car(context)\n    ignored = if(mcoConfigIsIgnored(inst) then t else nil)\n    binding = if(ignored then list(car(flags) cadr(flags) nth(2 flags) nil)\n      else hdbBind(vector car(flags) cadr(flags) nth(2 flags) inst~>name\n      nth(3 flags) nth(4 flags) nth(5 flags) t nil))\n    unless(binding && car(binding) && cadr(binding) && nth(2 binding)\n      mcoConfigFailure("CONFIG_BINDING_ERROR" instPath "HDB did not resolve this occurrence")\n      return(nil)\n    )\n    boundLib = car(binding)\n    boundCell = cadr(binding)\n    boundView = nth(2 binding)\n    when(boundLib == "" || boundCell == "" || boundView == ""\n      mcoConfigFailure("CONFIG_OPEN_BINDING_UNSUPPORTED" instPath "binding to open has no optimizable implementation")\n      return(nil)\n    )\n    resolvedLib = boundLib\n    resolvedCell = boundCell\n    resolvedView = boundView\n    unless(ignored hdbPushCell(vector inst~>name boundLib boundCell boundView))\n    stop = if(ignored then t else hdbIsAtStopPoint(vector))\n    when(!stop && hdbIsConfig(boundLib boundCell boundView)\n      config = mcoConfigOpen(boundLib boundCell boundView instPath)\n      unless(config hdbPopCell(vector) return(nil))\n      topLib = hdbGetTopLibName(config)\n      topCell = hdbGetTopCellName(config)\n      resolvedLib = topLib\n      resolvedCell = topCell\n      resolvedView = hdbGetTopViewName(config)\n      hdbClose(config)\n      unless(topLib && topCell && resolvedView &&\n        (master~>libName == "*" || topLib == master~>libName) &&\n        (master~>cellName == "*" || topCell == master~>cellName)\n        mcoConfigFailure("CONFIG_TOP_MISMATCH" instPath "nested config top differs from the original instance master")\n        hdbPopCell(vector)\n        return(nil)\n      )\n    )\n    localPath = if(nth(4 context) == "/" then instPath\n      else substring(instPath strlen(nth(4 context))+1))\n    viewType = if(ignored then nil else mcoConfigViewType(resolvedLib resolvedCell resolvedView instPath))\n    unless(ignored || viewType\n      hdbPopCell(vector)\n      return(nil)\n    )\n    ; Netlister stop and scanner recursion are separate: HDL, extracted,\n    ; simulator and symbol implementations never masquerade as schematics.\n    traversable = !ignored && !stop && mcoConfigIsSchematic(resolvedView viewType)\n    symbolMasked = !ignored && resolvedView == "symbol"\n    fprintf(mcoOut "CONFIG_BINDING\\t%L\\t%L\\t%L\\t%L\\t%L\\t%L"\n      instPath inst~>name parentPath master~>libName master~>cellName master~>viewName)\n    fprintf(mcoOut "\\t%L\\t%L\\t%L\\t%L\\t%L\\t%L\\t%L\\t%L\\t%L\\t%L"\n      ownerLib ownerCell ownerView boundLib boundCell boundView\n      resolvedLib resolvedCell resolvedView stop)\n    fprintf(mcoOut "\\t%L\\t%L\\t%L\\t%L\\t%L\\t%L\\t%L\\t%L\\t%L\\t%L\\t%L\\t%L\\t%L\\n"\n      nth(1 context) nth(2 context) nth(3 context) nth(4 context) localPath\n      nth(3 flags) nth(4 flags) nth(5 flags) viewType ignored nth(6 flags) traversable symbolMasked)\n    return(list(boundLib boundCell boundView resolvedLib resolvedCell resolvedView stop nth(3 binding) ignored traversable symbolMasked viewType))\n  )\n)\n'

_WALK_SKILL = r"""
mcoProbeStack = nil
procedure(mcoProbeCell(libName cellName viewName hierPath depth @optional bindingContext bindingSignature)
 prog((identity cv pcdb inst master instPath binding childLib childCell childView)
  identity=list(libName cellName viewName)
  when(member(identity mcoProbeStack)
   mcoConfigFailure("CONFIG_CYCLE" hierPath identity) return(nil))
  when(hdbIsConfig(libName cellName viewName)
   mcoProbeConfig(libName cellName viewName hierPath depth) return(nil))
  cv=dbOpenCellViewByType(libName cellName viewName "schematic" "r")
  unless(cv mcoConfigFailure("CONFIG_VIEW_UNREADABLE" hierPath identity) return(nil))
  unless(cv~>cellViewType == "schematic"
   mcoConfigFailure("CONFIG_VIEW_UNSUPPORTED" hierPath identity) dbClose(cv) return(nil))
  unless(bindingContext
   mcoConfigFailure("CONFIG_CONTEXT_REQUIRED" hierPath "Test design is not a config; its switch/stop lists are required")
   dbClose(cv) return(nil))
  pcdb=mcoConfigPcdb(libName cellName viewName hierPath)
  unless(pcdb dbClose(cv) return(nil))
  unless(cadr(pcdb) == length(cv~>instances)
   mcoConfigFailure("CONFIG_PCDB_ERROR" hierPath "PCDB and OA instance counts disagree")
   dbClose(cv) return(nil))
  mcoProbeStack=cons(identity mcoProbeStack)
  foreach(inst cv~>instances
   master=inst~>master
   instPath=if(hierPath == "/" then strcat("/" inst~>name) else strcat(hierPath "/" inst~>name))
   binding=mcoConfigBind(bindingContext pcdb inst libName cellName viewName hierPath instPath)
   when(binding
    childLib=car(binding) childCell=cadr(binding) childView=nth(2 binding)
    when(nth(9 binding)
     if(depth < 100 then
      mcoProbeCell(childLib childCell childView instPath depth+1 bindingContext nth(7 binding))
     else mcoConfigFailure("CONFIG_DEPTH_LIMIT" instPath "Traversal exceeded 100 levels")))
    unless(nth(8 binding) hdbPopCell(car(bindingContext)))
   )
  )
  mcoProbeStack=cdr(mcoProbeStack)
  dbClose(cv)
  return(t)
 ))
"""

def _literal(value):
    if value == 'nil': return None
    if value == 't': return True
    try: return ast.literal_eval(value)
    except (ValueError, SyntaxError): return value


def parse_records(text, source):
    result = {'root': None, 'config': None, 'occurrences': [], 'errors': []}
    names = ('path','name','parent','raw_library','raw_cell','raw_view',
             'owner_library','owner_cell','owner_view',
             'bound_library','bound_cell','bound_view','library','cell','view','stop',
             'config_library','config_cell','config_view','config_path','local_path',
             'lib_fixed','cell_fixed','view_fixed','view_type','ignored','pcdb_ignored',
             'traversable','symbol_masked')
    for line in text.splitlines():
        fields=line.split('\t'); kind=fields[0]; values=[_literal(x) for x in fields[1:]]
        if kind == 'RESOLVED_TOP' and len(values)==3:
            result['root']=dict(zip(('library','cell','view'), values))
        elif kind == 'CONFIG_BINDING' and len(values)==len(names):
            result['occurrences'].append(dict(zip(names,values)))
        elif kind == 'CONFIG_BINDING':
            result['errors'].append({'kind':'CONFIG_RECORD_ERROR','path':'/','detail':'Malformed binding record'})
        elif kind.startswith('CONFIG_') and kind != 'CONFIG_DEP':
            result['errors'].append({'kind':kind,'path':values[0] if values else '/',
                                     'detail':values[1] if len(values)>1 else ''})
        elif kind == 'PROBE_FAILED':
            result['errors'].append({'kind':kind,'path':'/','detail':'Native traversal failed'})
    if source.get('design_view'):
        result['config']=dict(zip(('library','cell','view'), (source['design_library'], source['design_cell'], source['design_view'])))
    return result


def scan_source_hierarchy(workspace, source, timeout=180, native_environment=None,
                          native_virtuoso=None, cds_lib=None, native_workdir=None):
    """Read a selected test design. Errors are explicit and block safe use of partial trees."""
    identity=[source.get('design_'+x) for x in ('library','cell','view')]
    if not all(isinstance(v,str) and v for v in identity):
        raise ValueError('Selected test has no complete design library/cell/view')
    workspace=str(Path(workspace).resolve())
    mapping = Path(cds_lib).resolve() if cds_lib else Path(workspace) / 'cds.lib'
    if not mapping.is_file():
        raise ValueError('Source cds.lib is unavailable')
    with tempfile.TemporaryDirectory(prefix='caplab_hierarchy_') as folder:
        folder=Path(folder); out=folder/'records'; script=folder/'scan.il'; log=folder/'scan.log'
        if not native_workdir and cds_lib and mapping != (Path(workspace) / 'cds.lib').resolve():
            (folder/'cds.lib').write_text('INCLUDE '+sq(mapping)+'\n',encoding='utf-8')
            workspace=str(folder)
        script.write_text('mcoOut=outfile(%s)\n' % sq(out) + _CONFIG_SKILL + _WALK_SKILL +
            '\nmcoProbeResult=errset(mcoProbeCell(%s "/" 0) t)\nunless(mcoProbeResult fprintf(mcoOut "PROBE_FAILED\\t%%L\\n" "/"))\nclose(mcoOut)\nexit()\n' % ' '.join(sq(v) for v in identity), encoding='utf-8')
        env=os.environ.copy(); env['LC_ALL']='en_US.utf8'
        for key in list(env):
            if key.startswith('CDS_MPS_') or key in ('CDS_LIB_PATH','CDS_LOAD_PATH'): env.pop(key,None)
        if native_workdir:
            env['CAPLAB_NATIVE_CDS_LIB']=str(mapping.resolve())
        else:
            env.pop('CAPLAB_NATIVE_CDS_LIB',None)
        if native_environment:
            if not native_virtuoso: raise ValueError('Invoking Virtuoso executable is unavailable')
            env['CAPLAB_NATIVE_ENV_JSON']=json.dumps(native_environment)
            env['CAPLAB_NATIVE_VIRTUOSO']=native_virtuoso
        else:
            env.pop('CAPLAB_NATIVE_ENV_JSON',None)
            env.pop('CAPLAB_NATIVE_VIRTUOSO',None)
        try:
            proc=subprocess.run(['/bin/bash',str(ROOT/'cadence-child.sh'),str(script),str(log)],cwd=str(Path(native_workdir).resolve()) if native_workdir else workspace,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError('Native hierarchy scan timed out')
        if proc.returncode or not out.exists():
            detail=proc.stdout.decode('utf-8','replace')[-2000:]
            raise RuntimeError('Native hierarchy scan failed: '+detail)
        result=parse_records(out.read_text(encoding='utf-8',errors='replace'),source)
        if not result['root'] and not result['errors']:
            result['errors'].append({'kind':'CONFIG_TOP_ERROR','path':'/','detail':'No resolved config top returned'})
        return result


def placement_binding(hierarchy, row, instance_id):
    """Project a selected occurrence and its descendants onto a new canvas instance.

    Nested configs are flattened to their observed resolved implementations;
    the original occurrence and original config identities remain in each record.
    """
    if hierarchy.get('errors'):
        raise ValueError('Source hierarchy is incomplete; refresh and resolve its errors')
    if not instance_id or '/' in instance_id:
        raise ValueError('Invalid canvas instance name')
    rows = hierarchy.get('occurrences') or []
    by_path = {}
    for item in rows:
        path = item.get('path')
        if not path or path in by_path:
            raise ValueError('Missing or duplicate source occurrence path')
        by_path[path] = item
    path = row.get('path')
    if path not in by_path or by_path[path] != row:
        raise ValueError('Selected occurrence is not in the current hierarchy')
    if row.get('ignored'):
        raise ValueError('Ignored schematic marker cannot be placed')
    for key in ('raw_library','raw_cell','library','cell','view'):
        if not row.get(key): raise ValueError('Incomplete occurrence identity: ' + key)
    selected = []
    for item in rows:
        current = item
        seen = set()
        while current and current.get('path') != path:
            p = current.get('parent')
            if p in seen: raise ValueError('Cyclic source occurrence paths')
            seen.add(p)
            current = by_path.get(p)
        if current and not item.get('ignored'):
            selected.append(dict(item))
    return {'library':row['raw_library'], 'cell':row['raw_cell'], 'view':row['view'],
            'binding_library':row['library'], 'binding_cell':row['cell'],
            'source_occurrence':path, 'binding_records':selected}


def projected_binding_skill(project, library, cell):
    """Generate HDB edits and readback checks for selected private instances.

    Call only after the private schematic and config exist, before Maestro setup.
    """
    planned = []
    for inst in project.get('instances', []):
        records = inst.get('binding_records') or []
        if not records: continue
        rootpath = inst.get('source_occurrence')
        by_path = {r.get('path'):r for r in records}
        if len(by_path) != len(records) or rootpath not in by_path:
            raise ValueError('Invalid projected occurrence records')
        for r in records:
            chain=[]; cur=r; seen=set()
            while True:
                path=cur.get('path')
                if path in seen: raise ValueError('Cyclic projected occurrences')
                seen.add(path); chain.insert(0,cur)
                if path == rootpath: break
                cur=by_path.get(cur.get('parent'))
                if not cur: raise ValueError('Missing projected occurrence ancestor')
            if any(not c.get('traversable') for c in chain[:-1]):
                raise ValueError('Cannot project descendants below a stop or opaque view')
            for c in chain:
                if not all(c.get(k) for k in ('library','cell','view','name')):
                    raise ValueError('Incomplete projected binding')
            if r['view'] == 'spectre' and not r.get('stop'):
                raise ValueError('Non-stop spectre binding conflicts with private config stoplist')
            spec=[]
            for index, member in enumerate(chain):
                owner=(library,cell,'schematic') if index == 0 else tuple(chain[index-1][k] for k in ('library','cell','view'))
                name=inst['id'] if index == 0 else member['name']
                spec.append(owner+(name,))
            planned.append((spec,r,inst))
    if not planned: return ''
    lines=['calProjection=hdbOpen(%s %s "config" "a" "CDBA")' % (sq(library),sq(cell)),
           'unless(calProjection error("Cannot open private config for projected bindings\\n"))']
    for spec,r,inst in planned:
        arg='list(%s)' % ' '.join('list(%s)' % ' '.join(sq(v) for v in part) for part in spec)
        target=' '.join(sq(r[k]) for k in ('library','cell','view'))
        lines.append('unless(hdbSetObjBindRule(calProjection %s list(\'hdbcBindingRule list(%s))) error("Cannot set projected binding\\n"))' % (arg,target))
        if r.get('stop'):
            lines.append('unless(hdbSetObjBindRule(calProjection %s list(\'hdbcStopRule t)) error("Cannot set projected stop\\n"))' % arg)
    lines.extend(['unless(hdbSave(calProjection) error("Cannot save projected config\\n"))',
                  'hdbClose(calProjection)',
                  'calProjection=hdbOpen(%s %s "config" "r" "CDBA")' % (sq(library),sq(cell)),
                  'unless(calProjection error("Cannot reopen projected config\\n"))'])
    for spec,r,inst in planned:
        lines.append('calPath=hdbCreatePathVector(calProjection)')
        lines.append('unless(calPath error("Cannot verify projected path\\n"))')
        chain = [r]; by_path={v['path']:v for v in inst['binding_records']}
        while chain[0]['path'] != inst['source_occurrence']: chain.insert(0,by_path[chain[0]['parent']])
        for i,parent in enumerate(chain[:-1]):
            target=' '.join(sq(parent[k]) for k in ('library','cell','view'))
            lines.append('unless(hdbPushCell(calPath %s %s) error("Cannot enter projected parent\\n"))' % (sq(spec[i][3]),target))
        raw=([inst['library'],inst['cell'],'symbol'] if len(chain)==1 else [r[k] for k in ('raw_library','raw_cell','raw_view')])
        flags=('t t nil' if len(chain)==1 else ' '.join('t' if r.get(k) else 'nil' for k in ('lib_fixed','cell_fixed','view_fixed')))
        target=' '.join(sq(r[k]) for k in ('library','cell','view'))
        lines.append('calBound=hdbBind(calPath %s %s %s t nil)' % (' '.join(sq(v) for v in raw),sq(spec[-1][3]),flags))
        lines.append('unless(calBound && equal(list(car(calBound) cadr(calBound) caddr(calBound)) list(%s)) error("Projected binding readback mismatch at %s: %%L\\n" calBound))' % (target,str(r['path']).replace('\\','\\\\').replace('"','\\"')))
        lines.append('unless(hdbPushCell(calPath %s %s) error("Cannot verify projected stop\\n"))' % (sq(spec[-1][3]),target))
        lines.append('unless(equal(if(hdbIsAtStopPoint(calPath) then t else nil) %s) error("Projected stop readback mismatch\\n"))' % ('t' if r.get('stop') else 'nil'))
        lines.append('hdbDestroyPathVector(calPath)')
    lines.append('hdbClose(calProjection)')
    return '\n'.join(lines)+'\n'
