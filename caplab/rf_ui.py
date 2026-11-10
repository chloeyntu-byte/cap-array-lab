"""Nonblocking RF device controls for the circuit canvas."""
import copy
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog
from . import devices, touchstone
from . import fieldwork_tk as fw
from .backend import Backend
from .z11_plugin import launch_z11, launch_standalone


class RFControls(object):
    def __init__(self, app):
        self.app=app;self.window=None

    def attach(self, top, left):
        ttk.Button(left,text='Z11 Studio',command=self.open_z11).pack(fill='x',pady=4)
        ttk.Button(left,text='Load example',command=self.app.load_example).pack(fill='x',pady=4)
        ttk.Button(left,text='RF devices / SNP...',command=self.open).pack(fill='x',pady=(5,0))
        self.app.edit_selected_device=self.edit_canvas_selected

    def _task(self,work,done,label,page_id=None):
        self.app.task(work,done,page_id=page_id,label=label,failed=lambda text:self.app.error(text))

    def open_z11(self):
        page=self.app.page();project=copy.deepcopy(page['project']);page_id=page['id']
        if not project.get('workspace'):
            self._task(lambda backend:launch_standalone(),lambda proc:self.app.status.set('Z11 Studio opened in offline mode.'),'Opening offline Z11 Studio',page_id)
            return
        def work(backend):
            proc=launch_z11(project);folder=Path(proc.caplab_z11_dir);start=time.monotonic()
            while time.monotonic()-start<180:
                error=folder/'error.txt'
                if error.exists():raise RuntimeError(error.read_text(encoding='utf-8'))
                if proc.poll() is not None:raise RuntimeError('Z11 host exited; see '+str(folder/'host.log'))
                if (folder/'gui.pid').is_file():return str(folder)
                time.sleep(.2)
            raise RuntimeError('Z11 did not start within 180 seconds; see '+str(folder/'host.log'))
        self._task(work,lambda folder:self.app.status.set('Z11 Studio opened.'),'Opening Z11 Studio',page_id)

    def open(self):
        if self.window is not None and self.window.winfo_exists():
            if self.book is self.app.book and self.page_id==self.app.book['active_page'] and self.app.page(self.page_id)['project'] is self.project_ref:
                self.window.lift();return
            self.window.destroy()
        self.book=self.app.book;self.page_id=self.app.book['active_page'];self.project_ref=self.app.page(self.page_id)['project']
        self.window=tk.Toplevel(self.app);self.window.title('RF devices and Touchstone files')
        win=self.window;win.geometry('800x480');win.transient(self.app)
        fw.header(win,'RF devices','Touchstone and ideal components',product='cap').pack(fill='x')
        self.tree=ttk.Treeview(win,columns=('kind','details','enabled'),show='tree headings',selectmode='browse')
        for col,title,width in [('#0','Instance',90),('kind','Device',95),('details','Parameters / file',360),('enabled','Enabled',75)]:
            self.tree.heading(col,text=title);self.tree.column(col,width=width)
        self.tree.pack(fill='both',expand=True,padx=10,pady=8);self.tree.bind('<Double-1>',lambda e:self.edit_selected())
        row=ttk.Frame(win);row.pack(fill='x',padx=8,pady=5)
        for name,fn in [('Import SNP...',self.import_snp),('Add resistor',lambda:self.add('res')),('Add inductor Q',lambda:self.add('indq')),('Add capacitor Q',lambda:self.add('capq')),('Add coupling',lambda:self.add('mind'))]:ttk.Button(row,text=name,command=fn).pack(side='left',padx=2)
        row=ttk.Frame(win);row.pack(fill='x',padx=8,pady=5)
        for name,fn in [('Edit',self.edit_selected),('Enable / disable',self.toggle),('Delete',self.delete)]:ttk.Button(row,text=name,command=fn).pack(side='left',padx=2)
        self.show_paths=tk.BooleanVar(value=False)
        ttk.Checkbutton(row,text='Show full file locations',variable=self.show_paths,command=self.refresh).pack(side='right',padx=2)
        self.refresh()

    def _page(self):
        page=self.app.page(self.page_id)
        if self.app.book is not self.book or not page or self.app.book['active_page']!=self.page_id or page['project'] is not self.project_ref:raise ValueError('The source page changed; reopen RF devices before editing.')
        return page

    def _selected(self):
        ident=self.tree.selection()[0] if self.window and self.window.winfo_exists() and self.tree.selection() else self.app.selected
        page=self.app.page(self.page_id if self.window and self.window.winfo_exists() else None)
        return next((n for n in page['project']['instances'] if n['id']==ident and n.get('kind') in ('snp','passive')),None) if page else None

    def refresh(self):
        if not self.window or not self.window.winfo_exists():return
        self.tree.delete(*self.tree.get_children());page=self.app.page(self.page_id)
        if not page:return
        for n in page['project']['instances']:
            if n.get('kind') not in ('snp','passive'):continue
            detail=(n.get('touchstone',{}).get('path','') if self.show_paths.get() else Path(n.get('touchstone',{}).get('path','')).name) if n['kind']=='snp' else ', '.join('{}={}'.format(k,v) for k,v in n.get('props',{}).items())
            self.tree.insert('','end',iid=n['id'],text=n['id'],values=(n['cell'],detail,'Yes' if n.get('enabled',True) else 'No'))

    @staticmethod
    def _position(project):
        n=len(project.get('instances',[]));return 350+430*(n%3),240+300*(n//3)

    def _changed(self):
        page=self._page();page['project']['private']=None;self.app.fill_pins();self.app.redraw();self.refresh()

    def import_snp(self):
        paths=filedialog.askopenfilenames(parent=self.window,title='Select Touchstone files',filetypes=[('Touchstone','*.s*p *.S*P'),('All files','*')])
        if not paths:return
        page=self._page();page_id=page['id'];project_ref=page['project'];book=self.book;project=copy.deepcopy(project_ref)
        def work(backend):
            workspace=Backend.context(project);items=[]
            for path in paths:
                meta=touchstone.inspect(path);symbol=devices.native_symbol(workspace,'nport',meta['ports'],backend)
                items.append((path,meta,symbol))
            return items
        def done(items):
            page=self.app.page(page_id) if self.app.book is book else None
            if not page or self.app.book['active_page']!=page_id or page['project'] is not project_ref:
                self.app.status.set('Discarded SNP import: source page changed.');return
            self.app.snapshot()
            for path,meta,symbol in items:
                x,y=self._position(page['project']);node=devices.create_snp(page['project'],path,x=x,y=y,meta=meta,symbol=symbol)
            self.app.selected=node['id'];page['project']['private']=None;self.app.fill_pins();self.app.fit()
            self.app.status.set('Imported {} SNP device(s); last instance selected.'.format(len(items)))
            if self.window and self.window.winfo_exists() and self.book is book and self.project_ref is project_ref:self.refresh()
        self._task(work,done,'Importing SNP symbols',page_id)

    def add(self,cell):
        page=self._page();page_id=page['id'];project_ref=page['project'];book=self.book;project=copy.deepcopy(project_ref)
        def work(backend):return devices.native_symbol(Backend.context(project),cell,backend=backend)
        def done(symbol):
            page=self.app.page(page_id) if self.app.book is book else None
            if not page or self.app.book['active_page']!=page_id or page['project'] is not project_ref:
                self.app.status.set('Discarded device placement: source page changed.');return
            self.app.snapshot();x,y=self._position(page['project']);node=devices.create_passive(page['project'],cell,x=x,y=y,symbol=symbol)
            self.app.selected=node['id'];page['project']['private']=None;self.app.fill_pins();self.app.fit()
            self.app.status.set('Placed {}; selected on canvas.'.format(node['id']))
            if self.window and self.window.winfo_exists() and self.book is book and self.project_ref is project_ref:
                self.refresh();self.tree.selection_set(node['id']);self.edit_selected()
        self._task(work,done,'Loading '+cell+' symbol',page_id)

    def edit_canvas_selected(self):
        ident=self.app.selected
        node=self.app.instance()
        if not node or node.get('kind') not in ('snp','passive'):return
        self.open()
        if ident and self.tree.exists(ident):
            self.tree.selection_set(ident);self.edit_selected()

    def edit_selected(self):
        if not self.window or not self.window.winfo_exists():
            self.open()
            if self.app.selected and self.tree.exists(self.app.selected):self.tree.selection_set(self.app.selected)
        node=self._selected()
        if not node:return
        self._page();win=tk.Toplevel(self.window);win.title('Edit '+node['id']);win.transient(self.window)
        variables={}
        for i,(key,value) in enumerate(node.get('props',{}).items()):
            label={'r':'Resistance (ohm)','l':'Inductance (H)','c':'Capacitance (F)','q':'Quality factor Q','fq':'Q reference frequency (Hz)','rdc':'DC resistance (ohm)','k':'Signed coupling coefficient k','ind1':'First inductor','ind2':'Second inductor','mode':'Q model mode'}.get(key,key)
            ttk.Label(win,text=label).grid(row=i,column=0,sticky='w',padx=8,pady=4)
            var=tk.StringVar(value=str(value));variables[key]=var
            choices=devices.Q_MODES.get(node['cell'],{}) if key=='mode' else None
            if choices:
                widget=ttk.Combobox(win,textvariable=var,values=list(choices),state='readonly')
            elif key in ('ind1','ind2'):
                widget=ttk.Combobox(win,textvariable=var,values=[n['id'] for n in self._page()['project']['instances'] if n.get('cell')=='indq' and n.get('enabled',True)],state='readonly')
            else:widget=ttk.Entry(win,textvariable=var)
            widget.grid(row=i,column=1,sticky='ew',padx=8,pady=4)
            if choices:ttk.Label(win,text='; '.join('{}: {}'.format(k,v) for k,v in choices.items()),wraplength=300).grid(row=i,column=2,padx=5)
        if node['kind']=='snp':
            meta=node['touchstone']
            text='{}\n{} ports | {:.9g} to {:.9g} GHz | {} frequency samples\nEach port has independent positive / negative terminals.'.format(
                Path(meta['path']).name,meta['ports'],meta['frequency_min_hz']/1e9,meta['frequency_max_hz']/1e9,meta['points'])
            ttk.Label(win,text=text,wraplength=500).grid(row=0,column=0,columnspan=3,padx=10,pady=10)
            show_path=tk.BooleanVar(value=False);path_label=ttk.Label(win,text=meta['path'],wraplength=500)
            def toggle_path():
                if show_path.get():path_label.grid(row=2,column=0,columnspan=3,padx=10,pady=4)
                else:path_label.grid_remove()
            ttk.Checkbutton(win,text='Show full file location',variable=show_path,command=toggle_path).grid(row=1,column=0,sticky='w',padx=10)

        def apply():
            try:
                page=self._page();candidate=copy.deepcopy(node);candidate['props']={k:v.get().strip() for k,v in variables.items()}
                if node['kind']=='passive':devices.validate_device(candidate,page['project']['instances'])
                self.app.snapshot();node['props']=candidate['props'];self._changed();win.destroy()
            except Exception as ex:self.app.error(str(ex))
        ttk.Button(win,text='Apply',command=apply).grid(row=max(len(variables)+1,3 if node['kind']=='snp' else 0),column=1,pady=8)

    def toggle(self):
        node=self._selected()
        if not node:return
        self._page();self.app.snapshot();node['enabled']=not node.get('enabled',True);self._changed()

    def delete(self):
        node=self._selected()
        if not node:return
        page=self._page();self.app.selected=node['id'];self.app.delete_instance();page['project']['private']=None;self.refresh()
