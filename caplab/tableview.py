"""Sortable/filterable Tk results table with bounded rendering cost."""
import math
import re
import tkinter as tk
from tkinter import ttk
from .decomposition import MEASUREMENT_KEYS

NUMERIC=r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
METRICS=set(MEASUREMENT_KEYS)


def columns_for(rows):
    names=[k for k in rows[0] if k not in METRICS] if rows else []
    return (['point']+names if rows and 'point' in rows[0] else ['code'])+['frequency_Hz','C_F','Q','status']


def cell_value(row,key):
    value=row.get(key)
    if key=='point' and value is not None:return value+1
    if key=='frequency_Hz' and value is not None:return value/1e9
    if key=='C_F' and value is not None:return value*1e15
    return value


def display_value(value):
    if value is None:return 'Inf / undefined'
    if isinstance(value,float):return '{:.9g}'.format(value)
    return str(value)


def matches(value,query):
    query=query.strip()
    if not query:return True
    m=re.fullmatch(r'(>=|<=|!=|==|>|<|=)\s*('+NUMERIC+r')',query)
    if m:
        try:a,b=float(value),float(m[2])
        except (TypeError,ValueError):return False
        return {'>=':a>=b,'<=':a<=b,'!=':a!=b,'==':a==b,'>':a>b,'<':a<b,'=':a==b}[m[1]]
    m=re.fullmatch(r'('+NUMERIC+r')\s*\.\.\s*('+NUMERIC+r')',query)
    if m:
        try:return float(m[1])<=float(value)<=float(m[2])
        except (TypeError,ValueError):return False
    return query.casefold() in display_value(value).casefold()


class ResultsTable(ttk.Frame):
    PAGE_SIZE=300
    LABELS={'point':'Point','code':'Code','frequency_Hz':'Frequency (GHz)','C_F':'Total C (fF)','Q':'Total Q','status':'Status'}
    def __init__(self,parent,on_select=None,on_log=None,columns=None,labels=None):
        super().__init__(parent)
        self.on_select=on_select;self.on_log=on_log;self.fixed_columns=columns;self.LABELS=dict(self.LABELS,**(labels or {}));self.item_rows={};self._last_notified=object()
        self.rows=[];self.filtered=[];self.columns=[];self.sort_key=None;self.descending=False;self.page=0
        bar=ttk.Frame(self);bar.pack(fill='x',pady=(3,5))
        ttk.Label(bar,text='Table column').pack(side='left')
        self.column_var=tk.StringVar(value='All columns')
        self.column_combo=ttk.Combobox(bar,textvariable=self.column_var,state='readonly',width=17)
        self.column_combo.pack(side='left',padx=5)
        self.query=tk.StringVar()
        entry=ttk.Entry(bar,textvariable=self.query,width=18);entry.pack(side='left',fill='x',expand=True)
        entry.bind('<Return>',lambda e:self.apply())
        ttk.Button(bar,text='Filter',command=self.apply).pack(side='left',padx=4)
        ttk.Button(bar,text='Clear',command=self.clear).pack(side='left')
        area=ttk.Frame(self);area.pack(fill='both',expand=True)
        self.tree=ttk.Treeview(area,show='headings',height=6)
        vy=ttk.Scrollbar(area,orient='vertical',command=self.tree.yview)
        hx=ttk.Scrollbar(area,orient='horizontal',command=self.tree.xview)
        self.tree.configure(yscrollcommand=vy.set,xscrollcommand=hx.set)
        vy.pack(side='right',fill='y');hx.pack(side='bottom',fill='x');self.tree.pack(fill='both',expand=True)
        self.tree.bind('<Button-3>',self.context_menu)
        self.tree.bind('<<TreeviewSelect>>',self._selection_changed)
        foot=ttk.Frame(self);foot.pack(fill='x',pady=(4,0))
        self.count=tk.StringVar();ttk.Label(foot,textvariable=self.count,style='Muted.TLabel').pack(side='left')
        self.next_button=ttk.Button(foot,text='Next',command=lambda:self.move(1));self.next_button.pack(side='right')
        self.prev_button=ttk.Button(foot,text='Previous',command=lambda:self.move(-1));self.prev_button.pack(side='right',padx=4)
        ttk.Label(foot,text='Click a heading to sort; filters accept >=700 or 600..900.',style='Muted.TLabel').pack(side='left',padx=12)

    def set_rows(self,rows):
        self.rows=rows;self.columns=self.fixed_columns or columns_for(rows)
        self.tree.configure(columns=self.columns)
        choices=['All columns']+[self.LABELS.get(k,k) for k in self.columns]
        self.column_combo.configure(values=choices)
        if self.column_var.get() not in choices:self.column_var.set('All columns')
        if self.sort_key not in self.columns:self.sort_key=None
        for key in self.columns:
            self.tree.column(key,width=125 if key in ('frequency_Hz','C_F') else 105,minwidth=75,anchor='center',stretch=True)
        self.apply()

    def selected_column(self):
        label=self.column_var.get()
        return next((k for k in self.columns if self.LABELS.get(k,k)==label),None)

    def apply(self):
        key=self.selected_column();query=self.query.get()
        self.filtered=[r for r in self.rows if matches(cell_value(r,key),query)] if key else [r for r in self.rows if any(matches(cell_value(r,k),query) for k in self.columns)]
        if self.sort_key:
            key=self.sort_key
            def order(row):
                value=cell_value(row,key)
                if value is None:return (2,0)
                if isinstance(value,(float,int)):return (0,float(value))
                return (1,str(value).casefold())
            self.filtered.sort(key=order,reverse=self.descending)
        self.page=0;self.render()

    def sort(self,key,descending=None):
        self.descending=(not self.descending if self.sort_key==key else False) if descending is None else descending
        self.sort_key=key;self.apply()

    def clear(self):
        self.query.set('');self.apply()

    def move(self,delta):
        self.page=max(0,min(self.page+delta,max(0,(len(self.filtered)-1)//self.PAGE_SIZE)));self.render()

    def render(self):
        for key in self.columns:
            arrow=(' [desc]' if self.descending else ' [asc]') if key==self.sort_key else ''
            self.tree.heading(key,text=self.LABELS.get(key,key)+arrow,command=lambda k=key:self.sort(k))
        self.tree.delete(*self.tree.get_children());self.item_rows={}
        start=self.page*self.PAGE_SIZE
        for row in self.filtered[start:start+self.PAGE_SIZE]:
            ident=self.tree.insert('','end',values=[display_value(cell_value(row,k)) for k in self.columns])
            self.item_rows[ident]=row
        first=next(iter(self.item_rows),None)
        if first:self.tree.selection_set(first);self.tree.focus(first)
        self._selection_changed()
        last=min(start+self.PAGE_SIZE,len(self.filtered))
        self.count.set('{} / {} rows{}'.format(len(self.filtered),len(self.rows),' | {}-{}'.format(start+1,last) if self.filtered else ''))
        self.prev_button.configure(state='normal' if self.page else 'disabled')
        self.next_button.configure(state='normal' if last<len(self.filtered) else 'disabled')

    def selected_row(self):
        selection=self.tree.selection()
        return self.item_rows.get(selection[0]) if selection else None

    def _selection_changed(self,event=None):
        row=self.selected_row()
        if row is self._last_notified:return
        self._last_notified=row
        if self.on_select:self.on_select(row)

    def context_menu(self,event):
        column=self.tree.identify_column(event.x)
        if not column:return
        index=int(column[1:])-1
        if not 0<=index<len(self.columns):return
        key=self.columns[index]
        menu=tk.Menu(self,tearoff=False)
        ident=self.tree.identify_row(event.y);row=self.item_rows.get(ident)
        if row is not None and self.on_log:
            self.tree.selection_set(ident)
            menu.add_command(label='View Spectre log for this point',command=lambda:self.on_log(row))
            menu.add_separator()
        menu.add_command(label='Sort ascending',command=lambda:self.sort(key,False))
        menu.add_command(label='Sort descending',command=lambda:self.sort(key,True))
        menu.add_command(label='Filter this column',command=lambda:self.column_var.set(self.LABELS.get(key,key)))
        menu.tk_popup(event.x_root,event.y_root)
