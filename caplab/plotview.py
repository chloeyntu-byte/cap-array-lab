"""Interactive C/Q plots using the Tk Canvas only."""
import math
import tkinter as tk
from .plotting import curve_groups
from . import fieldwork_tk as fw

THEMES={
 'black':dict(bg=fw.tk_plot_colors()['bg'],ink=fw.TEXT,muted=fw.DIM,grid=fw.tk_plot_colors()['grid'],box=fw.PANEL,dim=fw.MUTED,colors=fw.SERIES),
 'white':{'bg':'#ffffff','ink':'#15191e','muted':'#49535e','grid':'#e0e4e8','box':'#eef3f7','dim':'#bac1c9',
          'colors':['#006c8f','#73529d','#427b46','#9f6d16','#a64963','#415c97']}}


class CQPlot(tk.Canvas):
    FONT='DejaVu Sans'
    hide_single_legend=False
    def __init__(self,parent,**kwargs):
        kwargs.setdefault('highlightthickness',0);kwargs.setdefault('background',fw.PLOT_BG);kwargs.setdefault('height',350)
        super().__init__(parent,**kwargs)
        self.rows=[];self.names=[];self.axis='point';self.mode='cartesian';self.filters={};self.theme='black'
        self.selected=None;self.points=[];self.tooltip_text='';self.pending=None;self.titles=None;self.metrics=None;self.include_frequency=True;self.metric_minimums={}
        self.bind('<Configure>',lambda e:self.schedule_draw())
        self.bind('<Button-1>',self.select_at)
        self.bind('<Motion>',self.hover)
        self.bind('<Leave>',lambda e:self.clear_tooltip())

    def set_data(self,rows,axis,names,mode,filters=None,titles=None,metrics=None,include_frequency=True):
        self.rows=rows;self.axis=axis;self.names=names;self.mode=mode;self.filters=filters or {};self.titles=titles
        self.metrics=metrics;self.include_frequency=include_frequency;self.draw()

    def set_theme(self,theme):
        self.theme=theme.lower();self.configure(background=THEMES[self.theme]['bg']);self.draw()

    def schedule_draw(self):
        if self.pending:self.after_cancel(self.pending)
        self.pending=self.after(60,self.draw)

    def draw(self):
        self.pending=None;self.delete('all');self.points=[];self.tooltip_text=''
        w,h=self.winfo_width(),self.winfo_height();theme=THEMES[self.theme]
        if w<30 or h<30:return
        if not self.rows:
            self.create_text(w/2,h/2,text=getattr(self,'empty_message','Run an SP sweep to see C / Q data.'),fill=theme['muted'],font=(self.FONT,14));return
        axis=self.axis if self.axis in self.rows[0] else ('point' if 'point' in self.rows[0] else 'code')
        groups=curve_groups(self.rows,axis,self.names,self.mode,self.filters,self.include_frequency)
        keys=[label for label,_ in groups]
        if self.selected not in keys:self.selected=None
        # A compact legend above each plot remains clickable at larger font sizes.
        titles=self.titles or ('Differential C (fF)','Quality factor Q')
        metrics=self.metrics or [('C_F',titles[0],1e15,'fF'),('Q',titles[1],1,'')]
        for panel,(metric,title,scale,unit) in enumerate(metrics):
            half=w/2;left=panel*half;x0=left+82;x1=left+half-20;y1=h-65
            single=self.hide_single_legend and len(groups)==1
            legend_lines=0 if single else min(4,len(groups));y0=min(50 if single else 95+legend_lines*17,max(50 if single else 100,y1-100))
            self.create_text(left+half/2,22,text=title,fill=theme['ink'],font=(self.FONT,15,'bold'))
            data=[row for _,series in groups for row in series if row.get(metric) is not None and math.isfinite(row[metric])]
            if not data:
                self.create_text(left+half/2,h/2,text='No matching data / Q undefined',fill=theme['muted'],font=(self.FONT,12));continue
            xmin=min(r[axis] for r in data);xmax=max(r[axis] for r in data)
            ymin=min(r[metric]*scale for r in data);ymax=max(r[metric]*scale for r in data)
            yr=ymax-ymin
            if yr<=max(abs(ymin),abs(ymax))*1e-10:yr=max(abs(ymax)*.1,1e-6)
            ymin-=yr*.1;ymax+=yr*.1
            if metric in self.metric_minimums:ymin=max(ymin,self.metric_minimums[metric])
            if xmax==xmin:xmin-=.5;xmax+=.5
            def xy(row):return x0+(row[axis]-xmin)/(xmax-xmin)*(x1-x0),y1-(row[metric]*scale-ymin)/(ymax-ymin)*(y1-y0)
            for tick in range(5):
                y=y1-(y1-y0)*tick/4;x=x0+(x1-x0)*tick/4
                self.create_line(x0,y,x1,y,fill=theme['grid'])
                self.create_text(x0-9,y,text='{:.4g}'.format(ymin+(ymax-ymin)*tick/4),anchor='e',fill=theme['muted'],font=(self.FONT,11))
                self.create_text(x,y1+20,text='{:.4g}'.format(xmin+(xmax-xmin)*tick/4),fill=theme['muted'],font=(self.FONT,11))
            self.create_line(x0,y0,x0,y1,x1,y1,fill=theme['ink'])
            xlabel='Frequency (GHz)' if axis=='frequency_GHz' else 'Scan point (index)' if axis=='point' else 'Decimal code' if axis=='code' else axis+' (decimal)'
            self.create_text((x0+x1)/2,h-20,text=xlabel,fill=theme['ink'],font=(self.FONT,12))
            for index,(label,series) in enumerate(groups):
                tag='curve:'+str(index);chosen=self.selected==label
                color=fw.accent('cap') if chosen else theme['colors'][index%len(theme['colors'])]
                if self.selected and not chosen:color=theme['dim']
                series=[r for r in series if r.get(metric) is not None and math.isfinite(r[metric])]
                points=[v for r in series for v in xy(r)]
                if len(points)>=4:self.create_line(*points,fill=color,width=4 if chosen else 2,tags=(tag,'curve'),dash=() if index<6 else (6,3))
                for row in series:
                    x,y=xy(row);radius=4.5 if chosen else 3
                    self.create_oval(x-radius,y-radius,x+radius,y+radius,fill=color,outline=color,tags=(tag,'curve'))
                    self.points.append(dict(curve=label,row=row,metric=metric,x=x,y=y,scale=scale,unit=unit,title=title))
                if index<4 and not single:
                    y=49+index*17
                    self.create_line(x0,y,x0+21,y,fill=color,width=4 if chosen else 2,tags=(tag,'curve'))
                    self.create_text(x0+28,y,text=label,fill=color,anchor='w',font=(self.FONT,11,'bold' if chosen else 'normal'),tags=(tag,'curve'))
            if len(groups)>4:self.create_text(x1,49,text='{} curves - use Slice'.format(len(groups)),anchor='e',fill=theme['muted'],font=(self.FONT,11))
        self.curve_labels=keys

    def select_at(self,event):
        ident=None
        for item in reversed(self.find_overlapping(event.x-6,event.y-6,event.x+6,event.y+6)):
            tag=next((tag for tag in self.gettags(item) if tag.startswith('curve:')),None)
            if tag:
                ident=int(tag.split(':')[1]);break
        label=self.curve_labels[ident] if ident is not None and ident<len(getattr(self,'curve_labels',[])) else None
        self.selected=None if label==self.selected else label;self.draw()

    def clear_tooltip(self):
        self.delete('tooltip');self.tooltip_text=''

    def hover(self,event):
        self.clear_tooltip()
        if not self.selected:return
        points=[p for p in self.points if p['curve']==self.selected]
        if not points:return
        p=min(points,key=lambda p:(p['x']-event.x)**2+(p['y']-event.y)**2)
        if (p['x']-event.x)**2+(p['y']-event.y)**2>14**2:return
        row=p['row'];names='  '.join('{}={}'.format(n,row[n]) for n in self.names if n in row)
        value=row[p['metric']]*p['scale'];unit=p['unit'];metric=('C' if p['metric']=='C_F' else 'Q') if self.metrics is None else p['title']
        self.tooltip_text='{}\nf = {:.9g} GHz   {} = {:.9g} {}\n{}'.format(self.selected,row['frequency_Hz']/1e9,metric,value,unit,names)
        theme=THEMES[self.theme]
        text=self.create_text(0,0,text=self.tooltip_text,anchor='nw',fill=theme['ink'],font=(self.FONT,11),
                              width=max(80,min(430,self.winfo_width()-30)),tags=('tooltip',))
        bounds=self.bbox(text);width,height=bounds[2]-bounds[0],bounds[3]-bounds[1]
        x=max(8,min(event.x+17,self.winfo_width()-width-10));y=max(8,min(event.y-80,self.winfo_height()-height-10))
        self.coords(text,x,y);box=self.bbox(text)
        rect=self.create_rectangle(box[0]-6,box[1]-5,box[2]+6,box[3]+5,fill=theme['box'],outline=theme['ink'],tags=('tooltip',))
        self.tag_raise(text,rect)
        self.create_oval(p['x']-7,p['y']-7,p['x']+7,p['y']+7,outline=theme['ink'],width=2,tags=('tooltip',))
