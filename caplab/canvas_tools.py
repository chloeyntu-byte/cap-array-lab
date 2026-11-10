"""Physical wire geometry and optional Tk canvas interaction."""
import math

SCALE = 190.0


def node_rotation_transform(node, x, y):
    """Map symbol coordinates to unzoomed screen/world coordinates."""
    b = node['symbol']['bbox']
    x -= (b[0]+b[2])/2.0
    y -= (b[1]+b[3])/2.0
    angle = int(node.get('rotation', 0)) % 360
    if angle == 90: x, y = -y, x
    elif angle == 180: x, y = -x, -y
    elif angle == 270: x, y = y, -x
    elif angle: raise ValueError('Rotation must be a multiple of 90 degrees')
    return node['x']+x*SCALE, node['y']-y*SCALE


def native_instance_origin(node):
    x, y = node_rotation_transform(node, 0, 0)
    return x/SCALE, -y/SCALE


def endpoint_point(project, endpoint):
    if 'point' in endpoint: return tuple(endpoint['point'])
    ident, sep, name = endpoint.get('pin', '').partition('.')
    for node in project.get('instances', []) + project.get('registers', []):
        if node['id'] == ident:
            for pin in node['symbol']['pins']:
                if pin['name'] == name:
                    return node_rotation_transform(node, *pin['xy'])
    raise ValueError('Wire references a missing pin: '+endpoint.get('pin', ''))


def wire_points(project, wire):
    return [endpoint_point(project, wire['a'])] + [tuple(p) for p in wire.get('points', [])] + [endpoint_point(project, wire['b'])]


def on_segment(point, a, b, tolerance=1e-6):
    dx, dy = b[0]-a[0], b[1]-a[1]
    length = dx*dx+dy*dy
    if length == 0: return math.hypot(point[0]-a[0], point[1]-a[1]) <= tolerance
    t = ((point[0]-a[0])*dx+(point[1]-a[1])*dy)/length
    return -tolerance <= t <= 1+tolerance and abs((point[0]-a[0])*dy-(point[1]-a[1])*dx) <= tolerance*max(1, math.sqrt(length))


def distance_segment(point, a, b):
    dx, dy = b[0]-a[0], b[1]-a[1]; length = dx*dx+dy*dy
    t = max(0, min(1, ((point[0]-a[0])*dx+(point[1]-a[1])*dy)/length)) if length else 0
    return math.hypot(point[0]-a[0]-t*dx, point[1]-a[1]-t*dy)


class CanvasTools(object):
    """Hooks: down/motion/release/keydown return True when event consumed."""
    def __init__(self, app):
        self.app = app; self.mode = 'select'; self.start = None
        self.points = []; self.cursor = None; self.selected_wire = None

    def command(self, key):
        class Event(object): pass
        event=Event();event.keysym=key
        return self.keydown(event)

    def activate(self, mode):
        self.app.canvas.focus_set()
        return self.command('w' if mode == 'wire' else 'j')

    def _world(self, event):
        return ((event.x-self.app.pan[0])/self.app.zoom, (event.y-self.app.pan[1])/self.app.zoom)

    def _screen(self, point):
        return (point[0]*self.app.zoom+self.app.pan[0], point[1]*self.app.zoom+self.app.pan[1])

    def _pin(self, event):
        for item in reversed(self.app.canvas.find_overlapping(event.x-6,event.y-6,event.x+6,event.y+6)):
            for tag in self.app.canvas.gettags(item):
                if tag.startswith('pin:'): return {'pin':tag[4:]}
        return None

    def _wire_hit(self, point):
        for wire in reversed(self.app.project.get('wires', [])):
            path = wire_points(self.app.project, wire)
            for a,b in zip(path,path[1:]):
                if distance_segment(point,a,b) <= 6/self.app.zoom: return wire, a, b
        return None

    def _anchor(self, event):
        pin = self._pin(event)
        if pin: return pin
        p = self._world(event); hit = self._wire_hit(p)
        if hit:
            _, a, b = hit; dx,dy=b[0]-a[0],b[1]-a[1]; den=dx*dx+dy*dy
            t=max(0,min(1,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/den)) if den else 0
            return {'point':[a[0]+t*dx,a[1]+t*dy]}
        return None

    def keydown(self, event):
        key = event.keysym.lower()
        if key == 'w':
            self.mode='wire'; self.start=None; self.points=[]; self.app.status.set('Wire: click a pin or wire, add bends, then click another pin or wire.'); return True
        if key == 'j':
            self.mode='junction'; self.start=None; self.points=[]; self.app.status.set('Junction: click a wire crossing to add or remove a connection.'); return True
        if key == 'escape' and (self.mode != 'select' or self.start):
            self.mode='select'; self.start=None; self.points=[]; self.app.redraw(); return True
        if key == 'r':
            node=self.app.instance()
            if node and node.get('kind') != 'register':
                self.app.snapshot(); node['rotation']=(int(node.get('rotation',0))+90)%360; self.app.project['private']=None; self.app.redraw(); return True
        if key in ('delete','backspace') and self.selected_wire:
            self.app.snapshot(); self.app.project['wires']=[w for w in self.app.project.get('wires',[]) if w['id'] != self.selected_wire]
            self.selected_wire=None; self.app.project['private']=None; self.app.redraw(); return True
        return False

    def down(self, event):
        self.app.canvas.focus_set(); p=self._world(event)
        if self.mode == 'junction':
            wires=self.app.project.get('wires', []); paths=[wire_points(self.app.project,w) for w in wires]
            best=None; distance=8/self.app.zoom
            for i,path in enumerate(paths):
                for other in paths[:i]:
                    for a,b in zip(path,path[1:]):
                        for c,d in zip(other,other[1:]):
                            dx,dy=b[0]-a[0],b[1]-a[1]; ex,ey=d[0]-c[0],d[1]-c[1]; den=dx*ey-dy*ex
                            if abs(den)<1e-12: continue
                            t=((c[0]-a[0])*ey-(c[1]-a[1])*ex)/den
                            u=((c[0]-a[0])*dy-(c[1]-a[1])*dx)/den
                            if 0<=t<=1 and 0<=u<=1:
                                q=(a[0]+t*dx,a[1]+t*dy); dist=math.hypot(p[0]-q[0],p[1]-q[1])
                                if dist<=distance: best=q;distance=dist
            if best is not None:
                self.app.snapshot(); junctions=self.app.project.setdefault('junctions',[])
                old=next((q for q in junctions if math.hypot(q[0]-best[0],q[1]-best[1])<1e-6),None)
                if old is not None: junctions.remove(old)
                else: junctions.append(list(best))
                self.app.project['private']=None; self.app.redraw()
            return True
        if self.mode == 'wire':
            anchor=self._anchor(event)
            if self.start is None:
                if anchor: self.start=anchor; self.points=[]
            elif anchor:
                if anchor != self.start:
                    self.app.snapshot(); wires=self.app.project.setdefault('wires',[]); used={w['id'] for w in wires}; i=1
                    while 'W'+str(i) in used: i+=1
                    wires.append({'id':'W'+str(i),'a':self.start,'b':anchor,'points':self.points[:]})
                    self.app.project['private']=None; self.start=None; self.points=[]
            else: self.points.append(list(p))
            self.app.redraw(); return True
        hit=self._wire_hit(p)
        if hit and not self._pin(event):
            self.selected_wire=hit[0]['id']; self.app.selected=None; self.app.fill_pins(); self.app.redraw(); return True
        self.selected_wire=None
        return False

    def motion(self, event):
        if self.mode == 'wire' and self.start:
            self.cursor=self._world(event); self.app.redraw(); return True
        return False

    def release(self, event):
        return self.mode == 'wire'

    def render(self):
        canvas=self.app.canvas
        for wire in self.app.project.get('wires', []):
            try: points=wire_points(self.app.project,wire)
            except ValueError: continue
            coords=[v for point in points for v in self._screen(point)]
            canvas.create_line(*coords,fill='#f4bd45' if wire['id']==self.selected_wire else '#72d992',width=3,tags=('wire:'+wire['id'],))
        for point in self.app.project.get('junctions',[]):
            x,y=self._screen(point);canvas.create_oval(x-4,y-4,x+4,y+4,fill='#72d992',outline='')
        if self.start and self.cursor:
            points=[endpoint_point(self.app.project,self.start)]+self.points+[self.cursor]
            canvas.create_line(*[v for p in points for v in self._screen(p)],fill='#f4bd45',dash=(4,3),width=2)


def native_wire_plan(project, cfg, isolate_instances=False):
    """Return OA segments with resolved scalar nets; gap unrelated crossings.

    Each segment needs its own OA net label. A gap intentionally breaks the
    drawn geometry, while matching labels restore its electrical continuity.
    """
    from .model import expand_pin
    wires=project.get('wires',[]);paths=[wire_points(project,w) for w in wires]
    parents=list(range(len(paths)))
    def find(i):
        while parents[i]!=i:
            parents[i]=parents[parents[i]];i=parents[i]
        return i
    def merge(i,j):parents[find(j)]=find(i)
    def contains(path,p):return any(on_segment(p,a,b) for a,b in zip(path,path[1:]))
    for i,path in enumerate(paths):
        for j in range(i):
            other=paths[j]
            if any(contains(other,p) for p in (path[0],path[-1])) or any(contains(path,p) for p in (other[0],other[-1])) or any(contains(path,p) and contains(other,p) for p in project.get('junctions',[])):
                merge(i,j)
    groups={}
    for i in range(len(paths)):groups.setdefault(find(i),[]).append(i)
    pin_groups=[]
    for node in project.get('instances',[])+project.get('registers',[]):
        if not node.get('enabled',True):continue
        for pin in node['symbol']['pins']:
            names=[cfg['nets'].get(node['id']+'.'+name) for name in expand_pin(pin['name'])]
            if all(names):pin_groups.append((node_rotation_transform(node,*pin['xy']),names))
    netlist={}
    for root,indices in groups.items():
        choices=[tuple(names) for point,names in pin_groups if any(contains(paths[i],point) for i in indices)]
        if choices and any(v!=choices[0] for v in choices):raise ValueError('Native wire net mapping disagrees with resolved connectivity')
        names=list(choices[0]) if choices else ['CAL_FLOAT_WIRE_{}'.format(root)]
        for i in indices:netlist[i]=names
    segments=[]
    for i,path in enumerate(paths):
        for a,b in zip(path,path[1:]):
            if a!=b:segments.append((a,b,netlist[i]))
    cuts=[[] for segment in segments]
    if isolate_instances:
        # Leave the original wire on its net, away from each sensed terminal.
        # The zero-volt probe bridges that gap electrically.
        pin_points=[node_rotation_transform(node,*pin['xy'])
                    for node in project.get('instances',[]) if node.get('enabled',True)
                    for pin in node['symbol']['pins']]
        for index,(a,b,nets) in enumerate(segments):
            dx,dy=b[0]-a[0],b[1]-a[1]; length=math.hypot(dx,dy)
            for point in pin_points:
                t=((point[0]-a[0])*dx+(point[1]-a[1])*dy)/(length*length)
                closest=(a[0]+t*dx,a[1]+t*dy)
                distance=math.hypot(point[0]-closest[0],point[1]-closest[1])
                radius=0.5*SCALE
                if distance < radius and -radius/length <= t <= 1+radius/length:
                    half=math.sqrt(radius*radius-distance*distance)/length
                    cuts[index].append((t-half,t+half))
    for i,(a,b,nets) in enumerate(segments):
        dx,dy=b[0]-a[0],b[1]-a[1];length=math.hypot(dx,dy)
        for c,d,other in segments[:i]:
            if nets==other:continue
            ex,ey=d[0]-c[0],d[1]-c[1];den=dx*ey-dy*ex
            if abs(den)>1e-9:
                t=((c[0]-a[0])*ey-(c[1]-a[1])*ex)/den
                u=((c[0]-a[0])*dy-(c[1]-a[1])*dx)/den
                if 0<=t<=1 and 0<=u<=1:cuts[i].append((t-5/length,t+5/length))
            elif abs((c[0]-a[0])*dy-(c[1]-a[1])*dx)<1e-6:
                den2=length*length
                t0=((c[0]-a[0])*dx+(c[1]-a[1])*dy)/den2;t1=((d[0]-a[0])*dx+(d[1]-a[1])*dy)/den2
                lo=max(0,min(t0,t1));hi=min(1,max(t0,t1))
                if lo<=hi:cuts[i].append((lo-5/length,hi+5/length))
    result=[]
    for i,(a,b,nets) in enumerate(segments):
        start=0.0
        for lo,hi in sorted(cuts[i]+[(1.0,1.0)]):
            lo=max(0,min(1,lo));hi=max(0,min(1,hi))
            if lo>start+1e-9:
                points=[]
                for t in (start,lo):
                    x=a[0]+(b[0]-a[0])*t;y=a[1]+(b[1]-a[1])*t
                    points.append([x/SCALE,-y/SCALE])
                result.append({'nets':nets[:], 'points':points})
            start=max(start,hi)
    return result
