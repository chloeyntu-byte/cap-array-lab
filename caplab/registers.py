"""Named-net connectivity, explicit registers and deterministic code scans."""
import copy
import itertools
import math
import re
from .model import expand_pin, codes, frequencies, number
from .canvas_tools import node_rotation_transform, wire_points, on_segment


RESERVED = {'P', 'N', 'VDD', 'VSS'}
_NET_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:<\d+(?::\d+)?>)?$')


def all_nodes(project, include_disabled=False):
    nodes = project['instances'] + project.get('registers', [])
    return nodes if include_disabled else [n for n in nodes if n.get('enabled', True)]


def register_symbol(bits):
    return {'library': '', 'cell': 'DEC -> BIN', 'bbox': [-1.3,-.95,1.6,.8],
            'pins':[{'name':'B<{}:0>'.format(bits-1),'direction':'output','xy':[1.5,0]},
                    {'name':'VSS','direction':'inputOutput','xy':[0,-.8]}],
            'shapes':[{'kind':'rect','points':[-1.2,-.6,1.2,.6]},
                      {'kind':'line','points':[1.2,0,1.5,0]},
                      {'kind':'line','points':[0,-.6,0,-.8]}]}


def check_register(project, name, bits, text, ignore=None):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
        raise ValueError('Decimal variable names must start with a letter or underscore and contain only letters, digits and underscores')
    if name.upper().startswith('CAL_') or name.lower() in ('time','temp','tnom','pi','gnd','point','code','frequency_hz','c_f','q','g_s','b_s','status','q_signed','decomposition') or re.fullmatch(r'y(?:11|12|21|22)_[gb]_s',name.lower()):
        raise ValueError('This variable name is reserved; choose another name')
    if any(r['name']==name and r['id']!=ignore for r in project.get('registers', [])):
        raise ValueError('Duplicate decimal variable name: '+name)
    return codes(text, int(bits))


def add_register(project, name=None, bits=4, values='0', x=160, y=220):
    regs=project.setdefault('registers', [])
    n=0
    while any(r['id']=='R{}'.format(n) for r in regs): n+=1
    if name is None:
        index=n
        name='CODE{}'.format(index)
        while any(r['name']==name for r in regs):
            index+=1;name='CODE{}'.format(index)
    check_register(project,name,bits,values)
    reg={'id':'R{}'.format(n),'kind':'register','name':name,'bits':int(bits),'codes':values,
         'library':'','cell':'DEC -> BIN','view':'schematic','x':x,'y':y,
         'symbol':register_symbol(int(bits)),'roles':{'VSS':'VSS'}}
    for bit in range(int(bits)):
        reg['roles']['B<{}>'.format(bit)]='{}<{}>'.format(name,bit)
    regs.append(reg)
    project['private']=None
    return reg


def register_bus_name(reg):
    """Return a coherent user net label, independent of the native B<> pins."""
    labels=[reg.get('roles',{}).get('B<{}>'.format(bit),'') for bit in reversed(range(int(reg['bits'])))]
    if not labels:return ''
    first=re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*)<(\d+)>',labels[0])
    if first:
        indices=[int(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*<(\d+)>',v)[1])
                 if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*<(\d+)>',v) else None for v in labels]
        if all(v is not None for v in indices) and all(v.startswith(first[1]+'<') for v in labels):
            if len(indices)==1:return '{}<{}:{}'.format(first[1],indices[0],indices[0])+ '>'
            step=indices[1]-indices[0]
            if abs(step)==1 and all(v==indices[0]+i*step for i,v in enumerate(indices)):
                return '{}<{}:{}>'.format(first[1],indices[0],indices[-1])
    return ''


def edit_register(project, reg, name, bits, text):
    check_register(project,name,bits,text,reg['id'])
    oldbits=int(reg['bits']);bits=int(bits)
    old='B<{}:0>'.format(oldbits-1);new='B<{}:0>'.format(bits-1)
    if old!=new:
        for edge in project.get('edges',[]):
            for idx,key in enumerate(edge):
                if key==reg['id']+'.'+old:edge[idx]=reg['id']+'.'+new
        label=register_bus_name(reg)
        stem=re.match(r'([A-Za-z_][A-Za-z0-9_]*)<',label)
        stem=stem[1] if stem else name
        reg['roles']={k:v for k,v in reg['roles'].items() if not k.startswith('B<')}
        for bit in range(bits):reg['roles']['B<{}>'.format(bit)]='{}<{}>'.format(stem,bit)
        reg['symbol']=register_symbol(bits)
    reg.update(name=name,bits=bits,codes=text)


def endpoint_pins(project, key):
    ident, sep, pin=key.partition('.')
    node=next((n for n in all_nodes(project, include_disabled=True) if n['id']==ident),None)
    if not node or not sep:raise ValueError('Connection references a missing instance: '+key)
    grouped={p['name']:expand_pin(p['name']) for p in node['symbol']['pins']}
    if pin in grouped:return [ident+'.'+p for p in grouped[pin]]
    if any(pin in pins for pins in grouped.values()):return [key]
    raise ValueError('Pin is unavailable or its bus width has changed: '+key)


def _net_labels(name, width, pin):
    name=str(name).strip()
    if not name:return ['']*width
    if name in RESERVED or name.startswith('DC:'):
        if width!=1:raise ValueError('P, N, VDD, VSS and DC roles can only be assigned to scalar pins')
        if name.startswith('DC:'):number(name[3:])
        return [name]
    if not _NET_RE.fullmatch(name):
        raise ValueError('Net names must contain letters, digits or underscores, with an optional bus index/range')
    if name.upper().startswith('CAL_'):
        raise ValueError('Net names beginning CAL_ are reserved for private test fixtures')
    labels=expand_pin(name)
    if len(labels)==1 and width>1 and '<' not in name:
        indices=[re.search(r'<(\d+)>$',v) for v in pin]
        indices=[int(m[1]) for m in indices] if all(indices) else list(reversed(range(width)))
        labels=['{}<{}>'.format(name,i) for i in indices]
    if len(labels)!=width:
        raise ValueError('Bus width mismatch: pin has {} bits, net label has {} bits'.format(width,len(labels)))
    return labels


def assign_net(project, endpoint, name):
    """Atomically assign a scalar/bus role; equal labels connect without wires."""
    if project.get('version',1)<3 or project.get('edges'):
        migrated=upgrade_project(project);project.clear();project.update(migrated)
    pins=endpoint_pins(project,endpoint)
    labels=_net_labels(name,len(pins),pins)
    for key,label in zip(pins,labels):
        ident,pin=key.split('.',1)
        node=next(n for n in all_nodes(project, include_disabled=True) if n['id']==ident)
        node.setdefault('roles',{})[pin]=label
    project['version']=3
    project['edges']=[]
    return labels


def connect(project, a, b):
    """Compatibility helper: copy a named connection; no stored wire is created."""
    aa,bb=endpoint_pins(project,a),endpoint_pins(project,b)
    if len(aa)!=len(bb):
        raise ValueError('Bus width mismatch: {} bits vs {} bits'.format(len(aa),len(bb)))
    if a==b:raise ValueError('Choose two different pins')
    nodes={n['id']:n for n in all_nodes(project)}
    for pa,pb in zip(aa,bb):
        na,sa=pa.split('.',1);nb,sb=pb.split('.',1)
        ra=nodes[na]['roles'].get(sa,'');rb=nodes[nb]['roles'].get(sb,'')
        chosen=ra or rb
        if not chosen:
            used={v for n in all_nodes(project) for v in n.get('roles',{}).values()}
            i=1
            while 'NET'+str(i) in used:i+=1
            chosen='NET'+str(i)
        if ra and rb and ra!=rb:
            # Preserve all existing fanouts when joining two already named nets.
            for node in all_nodes(project):
                for pin,label in list(node['roles'].items()):
                    if label==rb:node['roles'][pin]=ra
        nodes[na]['roles'][sa]=chosen;nodes[nb]['roles'][sb]=chosen
    project['version']=3;project['edges']=[]


def scan_plan(project):
    regs=[]
    for r in project.get('registers',[]):
        if not r.get('enabled', True): continue
        values=check_register(project,r['name'],r['bits'],r['codes'],r['id'])
        regs.append({'id':r['id'],'name':r['name'],'bits':int(r['bits']),'values':values})
    mode=project['settings'].get('sweep_mode','cartesian')
    if mode not in ('cartesian','synchronous'):raise ValueError('Unknown scan mode')
    if not regs:points=[{}]
    elif mode=='cartesian':
        count=1
        for r in regs:count*=len(r['values'])
        if count>4096:raise ValueError('Cartesian scan has {} points and exceeds 4096; fix some registers or use a synchronous scan'.format(count))
        points=[dict(zip([r['name'] for r in regs],values)) for values in itertools.product(*(r['values'] for r in regs))]
    else:
        count=max(len(r['values']) for r in regs)
        if any(len(r['values']) not in (1,count) for r in regs):
            raise ValueError('Synchronous scan lists must have equal lengths; single-value registers stay fixed')
        points=[{r['name']:r['values'][0 if len(r['values'])==1 else i] for r in regs} for i in range(count)]
    return regs,points


def code_expression(reg, registers, mode):
    """Spectre expression for this named code at hidden scan index CAL_POINT."""
    values=reg['values'];n=len(values)
    if n==1:return str(values[0])
    stride=1
    if mode=='cartesian':
        pos=registers.index(reg)
        for r in registers[pos+1:]:stride*=len(r['values'])
        index='(floor(CAL_POINT/{})-{}*floor(CAL_POINT/{}))'.format(stride,n,stride*n)
    else:index='CAL_POINT'
    step=values[1]-values[0]
    if all(v==values[0]+i*step for i,v in enumerate(values)):
        return '({}+{}*{})'.format(values[0],step,index)
    # Ternary lookup only depends on a bounded integer index; never evaluate user text.
    expr=str(values[-1])
    for i in reversed(range(n-1)):
        expr='({}=={} ? {} : {})'.format(index,i,values[i],expr)
    return expr


def resolve_connectivity(project):
    """Resolve explicit labels and physical wire geometry into scalar pin groups.

    Interior crossings are separate unless an explicit junction is present.
    Returns groups with scalar members and labels; raises on bus width mismatch.
    """
    nodes=all_nodes(project); parents={}; pin_groups=[]; labels={}; attached=set()
    def add(k):
        if k not in parents: parents[k]=k
    def find(k):
        while parents[k]!=k:
            parents[k]=parents[parents[k]];k=parents[k]
        return k
    def merge(a,b):
        add(a);add(b); parents[find(b)]=find(a)
    bylabel={}
    for node in nodes:
        valid=set()
        for pin in node['symbol']['pins']:
            keys=[node['id']+'.'+v for v in expand_pin(pin['name'])]
            valid.update(k.split('.',1)[1] for k in keys)
            point=node_rotation_transform(node,*pin['xy'])
            pin_groups.append((keys,point))
            for key in keys:
                add(key); scalar=key.split('.',1)[1]
                label=node.get('roles',{}).get(scalar,'').strip()
                if label:
                    label=_net_labels(label,1,[scalar])[0]
                    if label.startswith('DC:'): label='DC:{:.17g}'.format(number(label[3:]))
                    labels[key]=label
                    if label in bylabel: merge(key,bylabel[label])
                    else: bylabel[label]=key
        stale=[p for p,v in node.get('roles',{}).items() if v and p not in valid]
        if stale: raise ValueError('Pin is unavailable or its bus width has changed: '+node['id']+'.'+stale[0])
    wires=project.get('wires',[]); paths=[wire_points(project,w) for w in wires]
    wp=list(range(len(wires)))
    def wf(i):
        while wp[i]!=i: wp[i]=wp[wp[i]];i=wp[i]
        return i
    def wm(i,j): wp[wf(j)]=wf(i)
    def contains(path,p): return any(on_segment(p,a,b) for a,b in zip(path,path[1:]))
    for i,path in enumerate(paths):
        for j in range(i):
            other=paths[j]
            if any(contains(other,p) for p in (path[0],path[-1])) or any(contains(path,p) for p in (other[0],other[-1])) or any(contains(path,p) and contains(other,p) for p in project.get('junctions',[])):
                wm(i,j)
    components={}
    for i in range(len(wires)): components.setdefault(wf(i),[]).append(i)
    # Physical pins on a wire and coincident pins are electrical contacts.
    for i,(keys,p) in enumerate(pin_groups):
        for other,q in pin_groups[:i]:
            if wires and keys[0].split('.',1)[0] != other[0].split('.',1)[0] and on_segment(p,q,q):
                if len(keys)!=len(other): raise ValueError('Bus width mismatch at coincident pins')
                for a,b in zip(keys,other): merge(a,b);attached.add(a);attached.add(b)
    for indices in components.values():
        touches=[keys for keys,p in pin_groups if any(contains(paths[i],p) for i in indices)]
        # Explicit endpoint references survive small numerical discrepancies.
        for i in indices:
            for endpoint in (wires[i]['a'],wires[i]['b']):
                if 'pin' in endpoint:
                    keys=endpoint_pins(project,endpoint['pin'])
                    if all(k in parents for k in keys) and keys not in touches: touches.append(keys)
        if not touches: continue
        width=len(touches[0])
        if any(len(keys)!=width for keys in touches): raise ValueError('Bus width mismatch on physical wire')
        for keys in touches:
            attached.update(keys)
            for a,b in zip(touches[0],keys): merge(a,b)
    groups={}
    for key in parents: groups.setdefault(find(key),[]).append(key)
    return [(members, sorted(set(labels[k] for k in members if k in labels)), attached) for members in groups.values()]


def validate_explicit(project):
    if project.get('version',1)<3 or project.get('edges'):
        project=upgrade_project(project)
    s=project['settings']; fs=frequencies(s['frequencies'])
    vdd,vcm,temp=number(s['vdd']),number(s['vcm']),number(s['temperature'])
    if not 0<vdd<=100:raise ValueError('VDD must be greater than 0 V and no greater than 100 V')
    if not any(n.get('enabled',True) for n in project['instances']):raise ValueError('Place an array symbol on the canvas first')
    regs,points=scan_plan(project); nodes=all_nodes(project); ids=set();drivers={}
    for node in nodes:
        if node['id'] in ids:raise ValueError('Duplicate instance identifier: '+node['id'])
        ids.add(node['id'])
        for pin in node['symbol']['pins']:
            for scalar in expand_pin(pin['name']):
                if node.get('kind')=='register' and re.fullmatch(r'B<\d+>',scalar):
                    drivers[node['id']+'.'+scalar]=(node['id'],int(re.search(r'<(\d+)>',scalar)[1]))
    groups=resolve_connectivity(project)
    # Diagnose physical shorts before missing or floating pins.
    for members,labels,attached in groups:
        sources=[drivers[k] for k in members if k in drivers]
        special=[v for v in labels if v in RESERVED or v.startswith('DC:')]
        if len(sources)>1:raise ValueError('Different register outputs are shorted on net '+', '.join(labels)+': '+', '.join(members))
        if len(special)>1:raise ValueError('Conflicting stimulus roles are physically shorted: '+', '.join(special))
        if sources and special:raise ValueError('Register output conflicts with reserved stimulus role '+special[0])
    nets,fixed,seen,used_regs={},{},set(),set()
    array_ids={n['id'] for n in project['instances'] if n.get('enabled',True)}
    for index,(members,labels,attached) in enumerate(groups):
        sources=[drivers[k] for k in members if k in drivers]
        special=[v for v in labels if v in RESERVED or v.startswith('DC:')]
        label=special[0] if special else (labels[0] if labels else '')
        if not label and len(members)==1:
            raise ValueError('Pin has no role or net name: '+members[0])
        if sources:
            rid,bit=sources[0];net='CAL_{}_B<{}>'.format(rid,bit)
            if any(k.split('.',1)[0] in array_ids for k in members):used_regs.add(rid)
        elif label in RESERVED:net='CAL_'+label
        elif label.startswith('DC:'):
            net='CAL_DC{}'.format(len(fixed));fixed[net]=number(label[3:])
        else:
            if len(members)<2:raise ValueError('Floating named net '+label+': '+members[0]+'; assign the same net name to its other pin')
            net='CAL_NET_'+label if label else 'CAL_WIRE_{}'.format(index)
        seen.update(labels)
        for key in members:nets[key]=net
    if not {'P','N'}.issubset(seen):raise ValueError('Assign measurement terminals P and N; the differential port is connected across P and N')
    for r in regs:
        if r['id'] not in used_regs:raise ValueError('Register '+r['name']+' is not connected to an array')
    return {'frequencies':fs,'vdd':vdd,'vcm':vcm,'temperature':temp,'nets':nets,'fixed':fixed,
            'registers':regs,'scan_points':points,'codes':list(range(len(points))),
            'sweep_mode':s.get('sweep_mode','cartesian'),'explicit':True}


def _legacy_to_explicit(project):
    p=copy.deepcopy(project)
    if p.get('version',1)>=2:return p
    p['version']=2;p['registers']=[];p['private']=None
    p['settings']['sweep_mode']='cartesian'
    r=add_register(p,'CODE0',int(p['settings']['bits']),p['settings']['codes'],120,250)
    for i in p['instances']:
        for pin,role in list(i['roles'].items()):
            if role.startswith('BIT:'):
                bit=int(role[4:]);i['roles'][pin]='';p.setdefault('edges',[]).append([r['id']+'.B<{}>'.format(bit),i['id']+'.'+pin])
    return p


def upgrade_project(project):
    """Migrate stored wires to names while preserving their electrical groups."""
    if project.get('version',1)>=3 and not project.get('edges'):return project
    p=_legacy_to_explicit(project)
    parents={};labels={};outputs={};nodes={n['id']:n for n in all_nodes(p)}
    for n in all_nodes(p):
        for pin in n['symbol']['pins']:
            for scalar in expand_pin(pin['name']):
                key=n['id']+'.'+scalar;parents[key]=key
                role=n.get('roles',{}).get(scalar,'')
                if n.get('kind')=='register' and re.fullmatch(r'B<\d+>',scalar):
                    bit=int(re.search(r'<(\d+)>',scalar)[1])
                    outputs[key]='{}<{}>'.format(n['name'],bit)
                    # v2 register role labels were not used for electrical connectivity.
                    role=''
                labels[key]=role
    def find(key):
        while parents[key]!=key:
            parents[key]=parents[parents[key]];key=parents[key]
        return key
    for a,b in p.get('edges',[]):
        aa,bb=endpoint_pins(p,a),endpoint_pins(p,b)
        if len(aa)!=len(bb):raise ValueError('Bus width mismatch in saved connection: '+a+' vs '+b)
        for pa,pb in zip(aa,bb):parents[find(pb)]=find(pa)
    groups={}
    for key in parents:groups.setdefault(find(key),[]).append(key)
    used=set(labels.values())|set(outputs.values())
    index=1
    for members in groups.values():
        old={labels[k] for k in members if labels[k]}
        if len(old)>1:raise ValueError('Different roles are shorted in the saved circuit: '+', '.join(members))
        driver=[outputs[k] for k in members if k in outputs]
        if len(driver)>1:raise ValueError('Different register outputs are shorted in the saved circuit: '+', '.join(members))
        if driver and old:raise ValueError('Register output is shorted to a stimulus role in the saved circuit: '+', '.join(members))
        name=next(iter(old),'') or (driver[0] if driver else '')
        if not name and len(members)>1:
            while 'NET'+str(index) in used:index+=1
            name='NET'+str(index);used.add(name);index+=1
        for key in members:
            ident,pin=key.split('.',1);nodes[ident].setdefault('roles',{})[pin]=name
    p['version']=3;p['edges']=[]
    for r in p.get('registers',[]):
        r['cell']='DEC -> BIN';r['symbol']['cell']='DEC -> BIN'
    # Preserve old custom ranges, while giving pre-range projects usable defaults.
    fs=frequencies(p['settings'].get('frequencies','1G'))
    defaults={'analysis':'sp','simulation_start':str(min(1e8,min(fs))),
              'simulation_stop':str(max(5e10,max(fs))),'simulation_points':301,'simulation_sweep':'log'}
    for key,value in defaults.items():p['settings'].setdefault(key,value)
    return p
