"""English Tk workbench; worker jobs never own or lock the editor UI."""
import copy
import csv
import hashlib
import json
import math
import os
import queue
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk,filedialog,messagebox,simpledialog,font as tkfont
from . import fieldwork_tk as fw
from .product_theme import apply_theme
from .backend import Backend,digest
from .canvas_tools import CanvasTools, node_rotation_transform
from .model import ROOT,new_project,add_instance,expand_pin,save_project,load_project,validate,frequencies
from .registers import all_nodes,add_register,edit_register,assign_net,register_bus_name,upgrade_project
from .workbook import workbook,current,add_page,rename_page,delete_page,cache,cached_rows,english_rows
from .native_viewer import ConfigViewer
from .tableview import ResultsTable
from .plotview import CQPlot
from .summaryview import DecompositionSummary
from .decomposition import result_names
from .maestro_panel import attach_context, source_description, source_design, runtime_dialog, hierarchy_dialog
from .config_state import capture_saved_config, record_config_baseline
from .rf_ui import RFControls
from .pointlogs import PointLogBrowser, open_row_log
from .resonance_view import ResonanceView, resonance_data
from .tank_view import TankView
from .tank_scan import tank_data, tank_settings, backend_mode

BG=fw.BG;PANEL=fw.PANEL;INK=fw.TEXT;MUTED=fw.DIM;ACCENT=fw.accent('cap')


class App(tk.Tk):
    def __init__(self,project=None,launch_context=None,tank=False):
        super().__init__();self.tk.call('tk','scaling',1.2);self.tank_app=bool(tank)
        families=set(tkfont.families(self))
        self.font_family=next((x for x in ('Liberation Sans','Carlito','PT Sans','DejaVu Sans') if x in families),tkfont.nametofont('TkDefaultFont').actual('family'))
        for fontname in ('TkDefaultFont','TkTextFont'):
            tkfont.nametofont(fontname).configure(family=self.font_family,size=10)
        self.title(('Tank Scan Lab' if self.tank_app else 'Cap Array Lab')+' — Untitled workbook');self.configure(bg=BG)
        sw,sh=self.winfo_screenwidth(),self.winfo_screenheight()
        self.small_screen=sw<1200 or sh<870
        if self.small_screen:
            width,height=max(320,sw-40),max(260,sh-90)
            self.geometry('{}x{}+10+35'.format(width,height));self.minsize(min(640,width),min(380,height))
        else:self.geometry('{}x{}'.format(min(1540,sw-40),min(980,sh-90)));self.minsize(1160,780)
        if launch_context and project is None:project=attach_context(new_project(),launch_context)
        self.launch_context=copy.deepcopy(launch_context)
        self.book=workbook(project);self.project=current(self.book)['project'];self.path=None
        self.libs={};self.catalog_cache={};self.rows=[];self.result_identity=None;self.result_mode='cartesian';self.result_analysis='legacy AC'
        self.selected=None;self.zoom=1.;self.pan=[0.,0.];self.drag=None;self.undo_stack=[];self.switching_page=False
        self.events=queue.Queue();self.jobs={};self.sim_jobs={};self.sim_gate=threading.Semaphore(1);self.file_gate=threading.Lock();self.backend=None
        self.viewer=ConfigViewer();self.resonance_cache={};self.resonance_revision=0;self.closing=False;self.redraw_id=None;self._field_loading=False
        self.book.setdefault('appearance',{'canvas':'black','plot':'black'})
        self.canvas_tools=CanvasTools(self)
        self._style();self._screen_surface();self._layout();self.refresh_registers();self.restore_results()
        if not self.rows:self.update_measurement_view()
        for var in self.fields.values():var.trace_add('write',self.update_result_label)
        self.bind('<Control-s>',lambda e:self.save());self.bind('<Control-z>',lambda e:self.undo())
        self.bind('<Control-e>',lambda e:self.open_native_config());self.bind('<Control-b>',lambda e:self.binding_dialog())
        self.protocol('WM_DELETE_WINDOW',self.close)
        self.after(40,self.poll);self.after(150,self.redraw);self.after(200,self.load_libraries)
        if self.tank_app:self.after_idle(self.layout_side_panels)

    @property
    def busy(self):return bool(self.jobs)

    def _style(self):
        apply_theme(self,fw)
        fw.apply_matplotlib()
        self.font_family=tkfont.nametofont('TkDefaultFont').actual('family')
        style=ttk.Style(self)
        style.configure('Muted.TLabel',foreground=fw.DIM)
        if self.tank_app:
            from .tank_view import ensure_styles
            ensure_styles(self);CQPlot.FONT=self.font_family

    def _screen_surface(self):
        self.surface=self
        if not self.small_screen:return
        # A bounded viewport preserves usable labels and controls on a small
        # remote desktop without changing the desktop resolution or scaling.
        hint=ttk.Label(self,text='Small screen: scroll right for settings and down for measurement controls.',padding=4)
        hint.pack(side='bottom',fill='x')
        frame=ttk.Frame(self);frame.pack(fill='both',expand=True)
        viewport=tk.Canvas(frame,bg=BG,highlightthickness=0)
        horizontal=ttk.Scrollbar(frame,orient='horizontal',command=viewport.xview)
        vertical=ttk.Scrollbar(frame,orient='vertical',command=viewport.yview)
        horizontal.pack(side='bottom',fill='x');vertical.pack(side='right',fill='y');viewport.pack(fill='both',expand=True)
        viewport.configure(xscrollcommand=horizontal.set,yscrollcommand=vertical.set,scrollregion=(0,0,1280,900))
        self.surface=ttk.Frame(viewport,width=1280,height=900);self.surface.pack_propagate(False)
        viewport.create_window(0,0,window=self.surface,anchor='nw',width=1280,height=900)
        self.viewport=viewport

    def _layout(self):
        def global_actions(box):
            if self.tank_app:
                self.side_toggle=ttk.Button(box,text='Show setup panels',style='Ghost.TButton',command=self.toggle_side_panels)
                self.side_toggle.pack(side='left',padx=4)
            for label,fn in [('New workbook',self.new),('Open workbook',self.open),('Save workbook',self.save)]:
                ttk.Button(box,text=label,command=fn).pack(side='left',padx=4)
        if self.tank_app:fw.header(self.surface,'Tank Scan Lab','N-port + cap array + gm: f0, Rp, gm, startup gain',product='cap',right=global_actions).pack(fill='x')
        else:fw.header(self.surface,'Cap Array Lab','C/Q and resonance',product='cap',right=global_actions).pack(fill='x')
        top=self.surface
        pages=ttk.Frame(self.surface,padding=(12,0,12,5));pages.pack(fill='x')
        for label,fn in [('New page',self.new_page),('Rename',self.rename_current_page),('Duplicate',self.copy_page),('Delete',self.delete_current_page)]:ttk.Button(pages,text=label,command=fn,style='Danger.TButton' if label=='Delete' else 'TButton').pack(side='right',padx=3)
        self.pagebook=ttk.Notebook(pages,height=1);self.pagebook.pack(side='left',fill='x',expand=True);self.page_frames={}
        self.refresh_pages();self.pagebook.bind('<<NotebookTabChanged>>',self.page_selected)
        main=ttk.Panedwindow(self.surface,orient='horizontal');self.main_panes=main;main.pack(fill='both',expand=True,padx=12,pady=(0,5))
        left=ttk.Frame(main,padding=16,width=215,style='Panel.TFrame');center=ttk.Frame(main);right_outer=ttk.Frame(main,width=325)
        main.add(left,weight=0);main.add(center,weight=1);main.add(right_outer,weight=0)
        self.mainpane,self.leftpane,self.centerpane,self.rightpane=main,left,center,right_outer;self.side_override=None
        fw.section(left,'Design libraries').pack(anchor='w',pady=(0,8))
        ttk.Button(left,text='Load library...',command=self.library_dialog).pack(fill='x',pady=(0,7))
        self.libvar=tk.StringVar(value=self.project['library']);self.libcombo=ttk.Combobox(left,textvariable=self.libvar,state='readonly',width=21)
        self.libcombo.pack(fill='x');self.libcombo.bind('<<ComboboxSelected>>',lambda e:self.filter_cells())
        self.search=tk.StringVar();ttk.Entry(left,textvariable=self.search,width=22).pack(fill='x',pady=7)
        self.search.trace_add('write',lambda *a:self.filter_cells())
        self.cells=tk.Listbox(left,bg=BG,fg=INK,selectbackground='#34576a',highlightthickness=0,borderwidth=0,font=(self.font_family,10),width=23,exportselection=False)
        self.cells.pack(fill='both',expand=True);self.cells.bind('<Double-1>',lambda e:self.add_selected())
        self.addsymbolbutton=ttk.Button(left,text='+ Place symbol',command=self.add_selected);self.addsymbolbutton.pack(fill='x',pady=(10,6))
        self.rf_controls=RFControls(self);self.rf_controls.attach(top,left)
        self.addregbutton=ttk.Button(left,text='+ Add register',command=self.add_register_node);self.addregbutton.pack(fill='x')
        self.hierarchybutton=ttk.Button(left,text='Load Maestro hierarchy...',command=lambda:hierarchy_dialog(self));self.hierarchybutton.pack(fill='x',pady=(8,0))
        self.runtimebutton=ttk.Button(left,text='Maestro settings...',command=lambda:runtime_dialog(self));self.runtimebutton.pack(fill='x',pady=(5,0))
        self.source_label=tk.StringVar(value=source_description(self.project))
        ttk.Label(left,textvariable=self.source_label,style='Muted.TLabel',wraplength=195).pack(anchor='w',pady=6)
        ttk.Label(left,text='Drag symbols | Wheel: zoom\nMiddle drag: pan | Ctrl+Z: undo\nW: wire | J: junction | R: rotate\nEqual pin names also connect.',style='Muted.TLabel').pack(anchor='w',pady=10)
        ttk.Button(left,text='Advanced...',command=self.advanced).pack(fill='x')
        self.tabs=ttk.Notebook(center);self.tabs.pack(fill='both',expand=True)
        self.editor=ttk.Frame(self.tabs);self.resultpage=ttk.Frame(self.tabs)
        self.tabs.add(self.editor,text='Circuit canvas');self.tabs.add(self.resultpage,text='Measurement results')
        if self.tank_app:self.tabs.bind('<<NotebookTabChanged>>',lambda e:self.layout_side_panels())
        toolbar=ttk.Frame(self.editor,padding=5);toolbar.pack(fill='x')
        for label,fn in [('Fit',self.fit),('Undo',self.undo),('Delete',self.delete_selection),('Wire (W)',lambda:self.canvas_tools.activate('wire')),('Junction (J)',lambda:self.canvas_tools.activate('junction')),('Rotate (R)',lambda:self.canvas_tools.command('r'))]:ttk.Button(toolbar,text=label,command=fn,style='Danger.TButton' if label=='Delete' else 'Ghost.TButton').pack(side='left',padx=2)
        themebar=ttk.Frame(self.editor,padding=(6,0,6,3));themebar.pack(fill='x')
        self.canvas_theme=tk.StringVar(value=self.book['appearance'].get('canvas','black').title())
        cb=ttk.Combobox(themebar,textvariable=self.canvas_theme,values=['Black','White'],state='readonly',width=7);cb.pack(side='right');cb.bind('<<ComboboxSelected>>',lambda e:self.set_canvas_theme())
        ttk.Label(themebar,text='Canvas background ').pack(side='right')
        self.canvas=tk.Canvas(self.editor,bg='#ffffff' if self.canvas_theme.get()=='White' else fw.PLOT_BG,highlightthickness=0);self.canvas.pack(fill='both',expand=True)
        self.empty_state=ttk.Frame(self.editor,padding=24,style='Panel.TFrame')
        ttk.Label(self.empty_state,text='Explore an ideal switched capacitor array offline.',style='Panel.TLabel').pack(pady=(0,8))
        ttk.Button(self.empty_state,text='Load example',command=self.load_example).pack()
        self.empty_state.place(relx=.5,rely=.5,anchor='center')
        self.canvas.bind('<Configure>',lambda e:self.schedule_redraw());self.canvas.bind('<ButtonPress-1>',self.canvas_down)
        self.canvas.bind('<B1-Motion>',self.canvas_drag);self.canvas.bind('<Motion>',self.canvas_motion);self.canvas.bind('<ButtonRelease-1>',self.canvas_release)
        self.canvas.bind('<Key>',self.canvas_key);self.canvas.bind('<Double-Button-1>',self.canvas_double)
        self.canvas.bind('<ButtonPress-2>',lambda e:setattr(self,'drag',('pan',e.x,e.y)));self.canvas.bind('<B2-Motion>',self.canvas_drag)
        self.canvas.bind('<Button-4>',lambda e:self.scale_view(e,1.12));self.canvas.bind('<Button-5>',lambda e:self.scale_view(e,1/1.12))
        self.canvas.bind('<MouseWheel>',lambda e:self.scale_view(e,1.12 if e.delta>0 else 1/1.12))
        self.selection_label=tk.StringVar(value='Select an instance or pin to assign a net name.')
        ttk.Label(self.editor,textvariable=self.selection_label,style='Muted.TLabel',padding=7).pack(fill='x')
        actions=ttk.Frame(right_outer,padding=(12,5));actions.pack(side='bottom',fill='x')
        scroll=tk.Canvas(right_outer,bg=PANEL,highlightthickness=0,width=315)
        sy=ttk.Scrollbar(right_outer,orient='vertical',command=scroll.yview);sy.pack(side='right',fill='y');scroll.pack(fill='both',expand=True);scroll.configure(yscrollcommand=sy.set)
        right=ttk.Frame(scroll,padding=12);window=scroll.create_window(0,0,window=right,anchor='nw')
        right.bind('<Configure>',lambda e:scroll.configure(scrollregion=scroll.bbox('all')));scroll.bind('<Configure>',lambda e:scroll.itemconfigure(window,width=e.width))
        fw.section(right,'SP sweep').pack(anchor='w')
        self.decomp_enabled=tk.BooleanVar(value=bool(self.project['settings'].get('decomposition',True)))
        if not self.tank_app:
            ttk.Checkbutton(right,text='Separate coupling / ground',variable=self.decomp_enabled,command=self.update_result_label).pack(anchor='w',pady=(3,0))
            ttk.Label(right,text='Two-port SP; original DC bias preserved.',style='Muted.TLabel').pack(anchor='w',pady=(0,3))
        else:ttk.Label(right,text='Two-port SP with module currents; DC bias preserved.',style='Muted.TLabel').pack(anchor='w',pady=(3,3))
        self.fields={}
        for key,label,default in [('simulation_start','Start (Hz)','100M'),('simulation_stop','Stop (Hz)','50G'),('simulation_points','Frequency samples','301')]:
            row=ttk.Frame(right);row.pack(fill='x',pady=3);ttk.Label(row,text=label,width=19).pack(side='left')
            var=tk.StringVar(value=str(self.project['settings'].get(key,default)));self.fields[key]=var;ttk.Entry(row,textvariable=var,width=12).pack(side='right',fill='x',expand=True)
        self.fields['simulation_sweep']=tk.StringVar(value=self.project['settings'].get('simulation_sweep','log'))
        row=ttk.Frame(right);row.pack(fill='x',pady=3);ttk.Label(row,text='Spacing',width=19).pack(side='left')
        ttk.Combobox(row,textvariable=self.fields['simulation_sweep'],values=['log','linear'],state='readonly',width=12).pack(side='right',fill='x',expand=True)
        self.fields['frequencies']=tk.StringVar(value=self.project['settings']['frequencies'])
        observe=ttk.Frame(right);self.applybutton=ttk.Button(observe,text='Apply observation frequencies',command=self.apply_observations)
        if not self.tank_app:
            observe.pack(fill='x')
            ttk.Label(observe,text='Observe at (Hz)',style='Muted.TLabel').pack(anchor='w',pady=(8,3))
            ttk.Entry(observe,textvariable=self.fields['frequencies']).pack(fill='x')
            self.applybutton.pack(fill='x',pady=4)
        if not self.tank_app:ttk.Label(right,text='Apply reuses the saved SP spectrum.\nNo new simulation inside the saved range.',style='Muted.TLabel').pack(anchor='w')
        row=ttk.Frame(right);row.pack(fill='x',pady=(8,3));ttk.Label(row,text='VDD (V)',width=19).pack(side='left')
        self.fields['vdd']=tk.StringVar(value=str(self.project['settings']['vdd']));ttk.Entry(row,textvariable=self.fields['vdd'],width=12).pack(side='right',fill='x',expand=True)
        ttk.Label(right,text='Decimal registers',font=(self.font_family,11,'bold')).pack(anchor='w',pady=(12,5))
        self.reglist=ttk.Treeview(right,columns=('bits','codes'),show='tree headings',height=3,selectmode='browse')
        for key,label,width in [('#0','Variable',90),('bits','Bits',40),('codes','Decimal values',115)]:self.reglist.heading(key,text=label);self.reglist.column(key,width=width)
        self.reglist.pack(fill='x');self.reglist.bind('<<TreeviewSelect>>',self.select_register_row)
        form=ttk.Frame(right);form.pack(fill='x',pady=4);self.regfields={k:tk.StringVar() for k in ('name','bits','codes','bus')}
        for n,(key,label) in enumerate([('name','Variable'),('bits','Bits'),('codes','Codes'),('bus','Output net')]):
            ttk.Label(form,text=label,width=10).grid(row=n,column=0,sticky='w',pady=2)
            ttk.Entry(form,textvariable=self.regfields[key],width=22).grid(row=n,column=1,sticky='ew',pady=2)
        form.columnconfigure(1,weight=1);ttk.Button(right,text='Apply register',command=self.apply_register).pack(fill='x')
        self.modevar=tk.StringVar(value='Synchronous' if self.project['settings'].get('sweep_mode')=='synchronous' else 'Cartesian')
        cb=ttk.Combobox(right,textvariable=self.modevar,values=['Cartesian','Synchronous'],state='readonly');cb.pack(fill='x',pady=5);cb.bind('<<ComboboxSelected>>',self.set_sweep_mode)
        ttk.Label(right,text='Fixed: 7 | List: 0,3,7 | Range: 0:15\nSynchronous pairs lists; Cartesian uses all combinations.',style='Muted.TLabel',wraplength=282).pack(anchor='w')
        ttk.Separator(right).pack(fill='x',pady=9)
        ttk.Label(right,text='Pin roles / net names',font=(self.font_family,11,'bold')).pack(anchor='w')
        ttk.Label(right,text='P: positive measurement terminal\nN: negative measurement terminal\nTotal is referred to floating P (+) / N (-).\nEqual names and drawn wires connect.',style='Muted.TLabel').pack(anchor='w',pady=5)
        self.pins=ttk.Treeview(right,columns=('role',),show='tree headings',height=5,selectmode='browse')
        self.pins.heading('#0',text='Physical pin');self.pins.heading('role',text='Role / net name');self.pins.column('#0',width=112);self.pins.column('role',width=146)
        self.pins.pack(fill='x');self.pins.bind('<<TreeviewSelect>>',self.pin_selected)
        self.rolevar=tk.StringVar();self.rolebox=ttk.Combobox(right,textvariable=self.rolevar,values=['','P','N','VDD','VSS','DC:0','CTRL<3:0>'])
        self.rolebox.pack(fill='x',pady=5);self.rolebox.bind('<Return>',lambda e:self.set_role())
        ttk.Button(right,text='Apply pin name',command=self.set_role).pack(fill='x')
        self.runbutton=ttk.Button(actions,text='Run SP / Measure C + Q',style='Primary.TButton',command=self.run);self.runbutton.pack(fill='x',pady=(3,4))
        ttk.Button(actions,text='Scan points / Spectre logs...',command=lambda:PointLogBrowser(self,self.book['active_page'])).pack(fill='x',pady=3)
        self.stopbutton=ttk.Button(actions,text='Stop this page',command=self.stop);self.stopbutton.pack(fill='x')
        self.sim_label=tk.StringVar(value='No simulation running');ttk.Label(actions,textvariable=self.sim_label,style='Muted.TLabel').pack(anchor='w',pady=(7,2))
        self.sim_progress=ttk.Progressbar(actions,mode='determinate',maximum=1);self.sim_progress.pack(fill='x')
        self._result_layout()
        logbar=ttk.Frame(self.surface,padding=(14,3));logbar.pack(fill='x')
        self.log_open=tk.BooleanVar(value=False);ttk.Checkbutton(logbar,text='Show log',variable=self.log_open,command=self.toggle_log).pack(side='left')
        self.log_summary=tk.StringVar(value='Process, warning and error messages');ttk.Label(logbar,textvariable=self.log_summary,style='Muted.TLabel').pack(side='left',padx=12)
        self.logframe=ttk.Frame(self.surface,padding=(12,0,12,2))
        self.logtext=tk.Text(self.logframe,height=7,bg='#090d11',fg=INK,insertbackground=INK,font=('DejaVu Sans Mono',9),state='disabled',wrap='word',borderwidth=0)
        ys=ttk.Scrollbar(self.logframe,command=self.logtext.yview);ys.pack(side='right',fill='y');self.logtext.configure(yscrollcommand=ys.set);self.logtext.pack(fill='x')
        bottom=ttk.Frame(self.surface,padding=(15,5));bottom.pack(fill='x');self.bottom=bottom
        self.status=tk.StringVar(value='Load an example or select your Cadence workspace.')
        self.status_chip=fw.StatusChip(bottom);self.status_chip.pack(side='left')
        self._status_state='idle';self.status.trace_add('write',lambda *args:self.status_chip.set(self._status_state,self.status.get()))
        self.status_chip.set('idle',self.status.get())
        self.activityframe=ttk.Frame(bottom);self.activity_label=tk.StringVar()
        ttk.Label(self.activityframe,textvariable=self.activity_label,style='Muted.TLabel').pack(side='left',padx=7)
        self.progress=ttk.Progressbar(self.activityframe,length=150,mode='indeterminate');self.progress.pack(side='right')
        bottom.pack_configure(side='bottom',before=main)
        logbar.pack_configure(side='bottom',before=main)
        self.refresh_progress()

    def _result_layout(self):
        modebar=ttk.Frame(self.resultpage,padding=6);modebar.pack(fill='x')
        ttk.Label(modebar,text='Measurement mode').pack(side='left')
        self.measurement_view=tk.StringVar(value=self.page().get('measurement_view',self.default_view()))
        self.measurement_combo=ttk.Combobox(modebar,textvariable=self.measurement_view,values=['Capacitance','Resonance','Tank scan'],state='readonly',width=16)
        self.measurement_combo.pack(side='left',padx=7);self.measurement_combo.bind('<<ComboboxSelected>>',lambda e:self.update_measurement_view())
        self.cap_resultframe=ttk.Frame(self.resultpage);self.cap_resultframe.pack(fill='both',expand=True)
        top=ttk.Frame(self.cap_resultframe,padding=8);top.pack(fill='x')
        self.result_title=tk.StringVar(value='Run an SP sweep to see C / Q results.')
        heading=ttk.Label(top,textvariable=self.result_title,wraplength=470,width=1);heading.pack(side='left',fill='x',expand=True)
        top.bind('<Configure>',lambda e:heading.configure(wraplength=max(180,e.width-165)))
        ttk.Button(top,text='Export table CSV',command=self.export).pack(side='right')
        bar=ttk.Frame(self.cap_resultframe,padding=(8,1));bar.pack(fill='x')
        ttk.Label(bar,text='X axis').pack(side='left');self.axisvar=tk.StringVar()
        self.axiscombo=ttk.Combobox(bar,textvariable=self.axisvar,state='readonly',width=15);self.axiscombo.pack(side='left',padx=7);self.axiscombo.bind('<<ComboboxSelected>>',lambda e:self.draw_plots())
        ttk.Button(bar,text='Slice...',command=self.filter_plot).pack(side='left')
        self.plot_theme=tk.StringVar(value=self.book['appearance'].get('plot','black').title())
        cb=ttk.Combobox(modebar,textvariable=self.plot_theme,values=['Black','White'],state='readonly',width=7);cb.pack(side='right');cb.bind('<<ComboboxSelected>>',lambda e:self.set_plot_theme())
        ttk.Label(modebar,text='Plot ').pack(side='right');self.plot_filters={}
        self.summary=DecompositionSummary(self.cap_resultframe);self.summary.pack(fill='x',padx=7,pady=(3,1))
        componentbar=ttk.Frame(self.cap_resultframe,padding=(8,1));componentbar.pack(fill='x')
        ttk.Label(componentbar,text='Plot component').pack(side='left')
        self.componentvar=tk.StringVar(value='Total P-N')
        self.componentcombo=ttk.Combobox(componentbar,textvariable=self.componentvar,state='readonly',width=28,
            values=['Total P-N','Coupling P-N','Ground-path equivalent','P-to-ground shunt','N-to-ground shunt'])
        self.componentcombo.pack(side='left',padx=7);self.componentcombo.bind('<<ComboboxSelected>>',lambda e:self.draw_plots())
        self.plots=CQPlot(self.cap_resultframe,height=240);self.plots.set_theme(self.plot_theme.get().lower());self.plots.pack(fill='both',expand=True,padx=7,pady=5)
        ttk.Label(self.cap_resultframe,text='Click a curve or legend to highlight it; hover a highlighted data point to read its value.',style='Muted.TLabel',padding=(8,2)).pack(fill='x')
        self.table_panel=ResultsTable(self.cap_resultframe,on_select=lambda row:self.summary.set_row(row,self.result_names()),on_log=lambda row:open_row_log(self,row));self.table_panel.pack(fill='x',padx=7,pady=(1,5));self.table=self.table_panel.tree

        self.resonance_view=ResonanceView(self.resultpage,self)
        self.tank_view=TankView(self.resultpage,self)

    def default_view(self):return 'Tank scan' if self.tank_app else 'Capacitance'

    def update_measurement_view(self):
        mode=self.measurement_view.get();page=self.page();page['measurement_view']=mode
        self.runbutton.configure(text={'Resonance':'Run / Measure resonance + loss','Tank scan':'Run tank scan'}.get(mode,'Run SP / Measure C + Q'))
        self.resonance_revision+=1;revision=self.resonance_revision;pid=page['id']
        if mode=='Capacitance':
            self.resonance_view.pack_forget();self.tank_view.pack_forget();self.cap_resultframe.pack(fill='both',expand=True);return
        self.cap_resultframe.pack_forget()
        project=page.get('run_project') or page['project'];private=project.get('private') or {}
        if mode=='Tank scan':
            self.resonance_view.pack_forget();self.tank_view.pack(fill='both',expand=True)
            settings=tank_settings(page,project)
            def tank_done(data):
                if self.book['active_page']==pid and self.resonance_revision==revision and self.measurement_view.get()=='Tank scan':
                    self.tank_view.set_data(data,project['settings'].get('sweep_mode','cartesian'),self.plot_theme.get().lower())
            def tank_failed(message):
                if self.book['active_page']==pid and self.resonance_revision==revision:
                    self.tank_view.set_data({'rows':[],'names':[],'mode':backend_mode(settings),'issues':[],'message':'Tank analysis failed: '+message},theme=self.plot_theme.get().lower())
            if not (private.get('spectrum') or {}).get('rows'):tank_done(tank_data(project,settings));return
            self.tank_view.set_data({'rows':[],'names':[],'mode':backend_mode(settings),'issues':[],'message':'Analyzing saved sweep and native module currents...'},theme=self.plot_theme.get().lower())
            self.task(lambda b:tank_data(project,settings),tank_done,pid,label='Tank scan analysis',failed=tank_failed);return
        self.tank_view.pack_forget();self.resonance_view.pack(fill='both',expand=True)
        key=(private.get('folder'),private.get('history'),private.get('spectrum_sha256'))
        if not all(key):key=None
        def done(data):
            if key is not None:
                self.resonance_cache[key]=data
                if len(self.resonance_cache)>12:self.resonance_cache.pop(next(iter(self.resonance_cache)))
            if self.book['active_page']==pid and self.resonance_revision==revision and self.measurement_view.get()=='Resonance':
                self.resonance_view.set_data(data,project['settings'].get('sweep_mode','cartesian'),self.plot_theme.get().lower())
        if key is not None and key in self.resonance_cache:done(self.resonance_cache[key]);return
        if not (private.get('spectrum') or {}).get('rows'):done(resonance_data(project));return
        self.resonance_view.set_data({'rows':[],'names':[],'losses':{},'message':'Reading saved sweep and native loss data...'},theme=self.plot_theme.get().lower())
        self.task(lambda b:resonance_data(project),done,pid,label='Read resonance and module losses')

    def layout_side_panels(self):
        """Tank app: results tab uses the full width unless the user pinned the panels open."""
        if not self.tank_app:return
        show=self.side_override if self.side_override is not None else self.tabs.select()!=str(self.resultpage)
        panes=[str(x) for x in self.mainpane.panes()]
        if show and str(self.leftpane) not in panes:
            self.mainpane.insert(0,self.leftpane,weight=0);self.mainpane.add(self.rightpane,weight=0)
        elif not show and str(self.leftpane) in panes:
            self.mainpane.forget(self.leftpane);self.mainpane.forget(self.rightpane)
        self.side_toggle.configure(text='Hide setup panels' if show else 'Show setup panels')

    def toggle_side_panels(self):
        panes=[str(x) for x in self.mainpane.panes()]
        self.side_override=str(self.leftpane) not in panes;self.layout_side_panels()

    def page(self,ident=None):
        ident=ident or self.book['active_page'];return next((p for p in self.book['pages'] if p['id']==ident),None)

    def log(self,message,level='info',page_id=None):
        p=self.page(page_id)
        if not p:return
        line='{} [{}] {}'.format(datetime.now().strftime('%H:%M:%S'),level.upper(),message)
        p.setdefault('log',[]).append(line);p['log']=p['log'][-1500:]
        if p['id']==self.book['active_page']:
            self.log_summary.set('{} messages | {}'.format(len(p['log']),message[:100]))
            if self.log_open.get():
                self.logtext.configure(state='normal');self.logtext.insert('end',line+'\n');self.logtext.see('end');self.logtext.configure(state='disabled')

    def toggle_log(self):
        if self.log_open.get():
            self.logframe.pack(before=self.bottom,fill='x');self.refresh_log()
        else:self.logframe.pack_forget()

    def refresh_log(self):
        self.logtext.configure(state='normal');self.logtext.delete('1.0','end')
        self.logtext.insert('end','\n'.join(self.page().get('log',[]))+'\n');self.logtext.see('end');self.logtext.configure(state='disabled')
        self.log_summary.set('{} messages for this page'.format(len(self.page().get('log',[]))))

    def task(self,work,done,page_id=None,kind='activity',label='Loading',failed=None,scope='page',purpose=None):
        ident=uuid.uuid4().hex[:10];page_id=page_id or self.book['active_page']
        b=Backend(lambda event:self.events.put(('progress',ident,event)))
        runtime=(self.page(page_id) or {}).get('project',{}).get('maestro_runtime') or {}
        b.native_environment=copy.deepcopy(runtime.get('native_environment')) or None
        source=runtime.get('source') or {}
        if source.get('ocean'):b.native_virtuoso=os.path.join(os.path.dirname(source['ocean']),'virtuoso')
        if source:b.native_workdir=(((runtime.get('snapshot') or {}).get('storage') or {}).get('source_work_dir') or {}).get('value') or source.get('workspace')
        job=dict(id=ident,page=page_id,backend=b,kind=kind,label=label,purpose=purpose or label,scope=scope,book=self.book,done=done,failed=failed,activity=True,completed=0,total=1)
        self.jobs[ident]=job
        if kind=='simulation':self.sim_jobs[page_id]=ident;self.backend=b
        self._status_state='running';self.status.set(label)
        self.log(label,'info',page_id);self.refresh_progress()
        def worker():
            locked=False
            try:
                if kind=='simulation':
                    while not self.sim_gate.acquire(timeout=.1):
                        if b.cancelled.is_set():raise RuntimeError('Stopped before simulation started.')
                    locked=True
                if b.cancelled.is_set():raise RuntimeError('Operation stopped.')
                result=work(b);self.events.put(('done',ident,result))
            except Exception as ex:self.events.put(('error',ident,str(ex)))
            finally:
                if locked:self.sim_gate.release()
        threading.Thread(target=worker,daemon=True,name='caplab-'+ident).start();return ident

    def poll(self):
        if self.closing:return
        start=time.monotonic();count=0
        while count<100 and time.monotonic()-start<.018:
            try:event=self.events.get_nowait()
            except queue.Empty:break
            count+=1;typ,ident,payload=event;job=self.jobs.get(ident)
            if not job:continue
            p=self.page(job['page'])
            if typ=='progress':
                data=payload if isinstance(payload,dict) else dict(kind='log',message=str(payload),level='info')
                message=data.get('message','');event_kind=data.get('kind','log')
                if event_kind=='simulation':
                    if p:
                        p['progress']={k:data[k] for k in ('completed','total','message','active') if k in data}
                    job['activity']=False
                elif event_kind=='point_log':
                    if p:
                        index=int(data['point_index']);item=p.setdefault('run_points',{}).setdefault(str(index),dict(point=index,codes={}))
                        item.update({k:data[k] for k in ('path','state') if k in data})
                        if data.get('logs'):item.setdefault('logs',{}).update(data['logs'])
                elif event_kind=='activity':
                    job['activity']=data.get('active',True);job['label']=message or job['label']
                    job['completed']=data.get('completed',0);job['total']=data.get('total',0)
                if message:self.log(message,data.get('level','info'),job['page'])
                self.refresh_progress();continue
            self.jobs.pop(ident,None)
            if self.sim_jobs.get(job['page'])==ident:self.sim_jobs.pop(job['page'],None)
            if typ=='done':
                self._status_state='done';self.status.set(job['label']+' completed.')
                try:
                    if job['scope']=='context' or (job['book'] is self.book and (p or job['scope']=='workbook')):job['done'](payload)
                    self.log(job['label']+' completed.','info',job['page'])
                except Exception as ex:self.error(str(ex),job['page'])
            else:
                self._status_state='error'
                if p and job['kind']=='simulation':
                    p.setdefault('progress',{})['active']=False;p['progress']['message']='Failed / stopped - see log';p['last_error']=payload
                    for point in p.get('run_points',{}).values():
                        if point.get('state')!='done':point['state']='error'
                self.log(payload,'error',job['page'])
                if job['failed']:job['failed'](payload)
                else:self.status.set('Operation failed; expand the log for details.')
            self.refresh_progress()
        if not self.closing:self.after(40,self.poll)

    def refresh_progress(self):
        if not hasattr(self,'sim_label'):return
        pid=self.book['active_page'];running=pid in self.sim_jobs;p=self.page();progress=p.get('progress',{})
        for page in self.book['pages']:
            frame=self.page_frames.get(page['id'])
            if frame is not None:
                pr=page.get('progress',{});suffix=' [{}/{}]'.format(pr.get('completed',0),pr.get('total',0)) if page['id'] in self.sim_jobs else ' [error]' if page.get('last_error') else ''
                self.pagebook.tab(frame,text=page['name']+suffix)
        self.update_result_label()
        done=progress.get('completed',0);total=progress.get('total',0)
        self.runbutton.configure(state='disabled' if running else 'normal');self.stopbutton.configure(state='normal' if running else 'disabled')
        self.sim_progress.configure(maximum=max(1,total),value=done)
        self.sim_label.set('{} / {} code points | {}'.format(done,total,progress.get('message','Queued' if running else 'Ready')) if total or running else 'No simulation running')
        if not hasattr(self,'activityframe'):return
        activities=[j for j in self.jobs.values() if j.get('activity')]
        if activities:
            job=activities[-1];self.activityframe.pack(side='right');self.activity_label.set(job['label'][:58])
            total=job.get('total',0)
            if total:
                self.progress.stop();self.progress.configure(mode='determinate',maximum=total,value=job.get('completed',0))
            else:
                if str(self.progress.cget('mode'))!='indeterminate':self.progress.configure(mode='indeterminate')
                self.progress.start(40)
        else:self.progress.stop();self.activityframe.pack_forget()

    def error(self,text,page_id=None):
        self._status_state='error'
        self.log(text,'error',page_id);self.status.set('Operation could not be completed.')
        if not page_id or page_id==self.book['active_page']:messagebox.showerror('Cap Array Lab',text,parent=self)

    def refresh_pages(self):
        self.switching_page=True
        for frame in self.page_frames.values():frame.destroy()
        self.page_frames={}
        for p in self.book['pages']:
            frame=ttk.Frame(self.pagebook);self.page_frames[p['id']]=frame;self.pagebook.add(frame,text=p['name'])
        self.pagebook.select(self.page_frames[self.book['active_page']]);self.after_idle(lambda:setattr(self,'switching_page',False))

    def capture_fields(self,strict=True):
        if strict:self.commit_register()
        for key,var in self.fields.items():self.project['settings'][key]=var.get().strip()
        self.project['settings']['sweep_mode']='synchronous' if self.modevar.get()=='Synchronous' else 'cartesian'
        self.project['settings']['analysis']='sp'
        self.project['settings']['decomposition']=bool(self.decomp_enabled.get())

    def stash_page(self):
        self.capture_fields();p=self.page();p['project']=self.project
        result=cache(self.rows,self.result_identity,self.result_mode)
        if result:result['analysis']=self.result_analysis
        p['result']=result;p['axis']=self.axisvar.get();p['filters']=copy.deepcopy(self.plot_filters)
        p['component']=self.componentvar.get();p['measurement_view']=self.measurement_view.get()
        p['editor']={'selected':self.selected,'zoom':self.zoom,'pan':self.pan[:],'tab':'results' if self.tabs.select()==str(self.resultpage) else 'canvas'}

    def page_selected(self,event=None):
        if self.switching_page:return
        selected=self.pagebook.select();ident=next((k for k,v in self.page_frames.items() if str(v)==selected),None)
        if not ident or ident==self.book['active_page']:return
        try:self.stash_page()
        except ValueError as ex:self.error(str(ex));self.refresh_pages();return
        self.activate_page(self.page(ident))

    def activate_page(self,p):
        self._field_loading=True;self.book['active_page']=p['id'];self.project=p['project'];self.rows=[];self.result_identity=None;self.result_mode='cartesian'
        for key,var in self.fields.items():var.set(str(self.project['settings'].get(key,{'simulation_start':'100M','simulation_stop':'50G','simulation_points':301,'simulation_sweep':'log'}.get(key,''))))
        self.modevar.set('Synchronous' if self.project['settings'].get('sweep_mode')=='synchronous' else 'Cartesian')
        self.decomp_enabled.set(bool(self.project['settings'].get('decomposition',True)));self.componentvar.set(p.get('component','Total P-N'))
        self.libvar.set(self.project['library']);state=p.get('editor',{});self.selected=state.get('selected');self.zoom=state.get('zoom',1.);self.pan=state.get('pan',[0.,0.])[:];self.undo_stack=[]
        self.measurement_view.set(p.get('measurement_view',self.default_view()))
        self._field_loading=False;self.fill_pins();self.table_panel.set_rows([]);self.restore_results();self.draw_plots();self.redraw();self.refresh_progress();self.refresh_log();self.load_libraries()
        self.tabs.select(self.resultpage if state.get('tab')=='results' else self.editor)
        self.refresh_source_label();self.update_measurement_view()

    def refresh_source_label(self):
        self.source_label.set(source_description(self.project))

    def new_page(self):
        try:self.stash_page();p=add_page(self.book);self.refresh_pages();self.activate_page(p)
        except ValueError as ex:self.error(str(ex))

    def copy_page(self):
        try:
            self.stash_page();source=self.page();p=add_page(self.book,source['name']+' copy',source)
            # Result spectra remain independent snapshots; new runs never share a native setup.
            if source.get('run_project'):p['run_project']=copy.deepcopy(source['run_project'])
            if source.get('result_points'):p['result_points']=copy.deepcopy(source['result_points'])
            self.refresh_pages();self.activate_page(p)
        except ValueError as ex:self.error(str(ex))

    def rename_current_page(self):
        name=simpledialog.askstring('Rename page','Page name',initialvalue=self.page()['name'],parent=self)
        if name is not None:
            try:rename_page(self.book,self.book['active_page'],name);self.refresh_pages()
            except ValueError as ex:self.error(str(ex))

    def delete_current_page(self):
        if len(self.book['pages'])==1:self.error('Keep at least one page.');return
        if self.book['active_page'] in self.sim_jobs:self.error('Stop this page before deleting it. Other pages remain editable.');return
        if not messagebox.askyesno('Delete page','Delete "{}"? Native run histories are retained.'.format(self.page()['name']),parent=self):return
        delete_page(self.book,self.book['active_page']);self.refresh_pages();self.activate_page(self.page())

    def capture_config_edits(self):
        private=self.project.get('private') or {}
        if private.get('config_sha256') and capture_saved_config(self.project):
            self.log('Saved native config edits imported. Run again before refreshing measurement data.','warning')
            self.update_result_label()
            return True
        return False

    def sync(self):self.capture_fields();self.capture_config_edits();return validate(self.project)

    def snapshot(self):
        self.undo_stack.append(copy.deepcopy(self.project));self.undo_stack=self.undo_stack[-40:]

    def undo(self):
        if not self.undo_stack:return
        self.project=self.undo_stack.pop();self.page()['project']=self.project;self.selected=None;self.fill_pins();self.redraw()

    @staticmethod
    def context_key(project):
        runtime=project.get('maestro_runtime') or {};source=runtime.get('source') or {}
        return json.dumps([str(Path(project['workspace']).resolve()),project.get('extra_libraries',{}),source.get('cds_lib'),runtime.get('libraries',{})],sort_keys=True)

    def load_libraries(self,force=False):
        if not self.project.get('workspace'):
            self.libs={};self.libcombo.configure(values=[]);self.filter_cells();return
        p=copy.deepcopy(self.project);pid=self.book['active_page'];key=self.context_key(p)
        def done(libs):
            self.catalog_cache[key]=libs
            if self.context_key(self.project)==key:
                self.libs=libs;names=sorted(libs,key=lambda n:(not n.startswith('HH'),n));self.libcombo.configure(values=names)
                if self.libvar.get() not in libs:self.libvar.set(p['library'] if p['library'] in libs else names[0] if names else '')
                self.filter_cells();self.status.set('Libraries ready. Place a symbol or edit the current page.')
        if not force and key in self.catalog_cache:done(self.catalog_cache[key]);return
        if any(j['purpose']=='Load libraries' and j.get('context')==key for j in self.jobs.values()):return
        ident=self.task(lambda b:b.catalog(b.context(p)),done,pid,label='Load libraries',scope='context');self.jobs[ident]['context']=key

    def filter_cells(self):
        self.cells.delete(0,'end');lib=self.libs.get(self.libvar.get(),{});query=self.search.get().casefold()
        for name in sorted(lib.get('cells',[])):
            if not re.fullmatch(r'CAL_\d{8}_\d{6}_[0-9a-f]{6}(?:_R\d+)?',name) and query in name.casefold():self.cells.insert('end',name)

    def library_dialog(self):
        win=tk.Toplevel(self);win.title('Load library');win.transient(self);win.grab_set();box=ttk.Frame(win,padding=18);box.pack(fill='both',expand=True)
        ttk.Label(box,text='Select a design workspace or add an existing OA library.').pack(anchor='w',pady=(0,8))
        def workspace():win.destroy();self.choose_workspace()
        def extra():
            win.destroy();path=filedialog.askdirectory(parent=self,title='Select OA library directory',initialdir=str(ROOT))
            if not path:return
            if not (Path(path)/'cdsinfo.tag').is_file():self.error('Select an OA library containing cdsinfo.tag.');return
            name=simpledialog.askstring('Library name','Library name',initialvalue=Path(path).name,parent=self)
            if not name:return
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',name):self.error('Use letters, digits and underscores for a library name.');return
            if name in self.libs and Path(self.libs[name]['path']).resolve()!=Path(path).resolve():self.error('A different library already uses this name.');return
            self.snapshot();self.project.setdefault('extra_libraries',{})[name]=str(Path(path).resolve());self.libvar.set(name);self.load_libraries(force=True)
        ttk.Button(box,text='Select workspace cds.lib...',command=workspace).pack(fill='x',pady=4)
        ttk.Button(box,text='Add OA library directory...',command=extra).pack(fill='x',pady=4)
        ttk.Button(box,text='Refresh libraries',command=lambda:(win.destroy(),self.load_libraries(force=True))).pack(fill='x',pady=4)
        ttk.Label(box,text='Library mappings are private to this tool.',style='Muted.TLabel').pack(anchor='w',pady=(8,0))

    def choose_workspace(self):
        path=filedialog.askopenfilename(parent=self,title='Select workspace cds.lib',initialdir=self.project['workspace'],filetypes=[('Cadence libraries','cds.lib'),('All files','*')])
        if not path:return
        if self.project['instances'] and Path(path).parent!=Path(self.project['workspace']):
            if not messagebox.askyesno('Change workspace','Clear the current page circuit and change workspace?',parent=self):return
            self.snapshot();self.project['instances']=[];self.project['registers']=[];self.project['edges']=[];self.selected=None
        self.project['workspace']=str(Path(path).resolve().parent);self.project['extra_libraries']={};self.project['private']=None
        runtime=self.project.get('maestro_runtime') or {}
        if runtime.get('source') and Path(runtime['source']['workspace']).resolve()!=Path(self.project['workspace']).resolve():
            self.project.pop('maestro_runtime',None);self.refresh_source_label();self.log('Workspace changed; detached the previous source Maestro settings.')
        self.fill_pins();self.load_libraries(force=True);self.redraw()

    def add_selected(self):
        if not self.cells.curselection():return
        try:self.commit_register()
        except ValueError as ex:self.error(str(ex));return
        cell=self.cells.get(self.cells.curselection()[0]);lib=self.libvar.get();p=copy.deepcopy(self.project);pid=self.book['active_page']
        def done(symbol):
            page=self.page(pid)
            if not page:return
            project=page['project'];n=len(project['instances'])
            if pid==self.book['active_page']:self.snapshot()
            inst=add_instance(project,symbol,610+n%2*390,220+n//2*340)
            if n==0:project['library']=lib
            if pid==self.book['active_page']:
                self.selected=inst['id'];self.fill_pins();self.redraw();self.status.set('Symbol placed. Drag it and assign pin names; config is built only when needed.')
        self.task(lambda b:b.symbol(b.context(p),lib,cell),done,pid,label='Load symbol '+cell)

    def instance(self):return next((n for n in all_nodes(self.project,include_disabled=True) if n['id']==self.selected),None)

    def refresh_registers(self):
        self.reglist.delete(*self.reglist.get_children())
        for reg in self.project.get('registers',[]):self.reglist.insert('','end',iid=reg['id'],text=reg['name'],values=(reg['bits'],reg['codes']))
        reg=self.instance()
        for key,var in self.regfields.items():var.set(str(reg[key]) if reg and reg.get('kind')=='register' and key!='bus' else register_bus_name(reg) if key=='bus' and reg and reg.get('kind')=='register' else '')
        if reg and reg.get('kind')=='register':self.reglist.selection_set(reg['id'])

    def commit_register(self):
        reg=self.instance()
        if not reg or reg.get('kind')!='register':return
        name=self.regfields['name'].get().strip();bits=int(self.regfields['bits'].get());codes=self.regfields['codes'].get().strip();bus=self.regfields['bus'].get().strip()
        oldbits=reg['bits'];oldlabel=register_bus_name(reg)
        if name==reg['name'] and bits==reg['bits'] and codes==reg['codes'] and bus==register_bus_name(reg):return
        candidate={'version':3,'instances':self.project['instances'],'registers':[copy.deepcopy(r) if r['id']==reg['id'] else r for r in self.project['registers']],'edges':[]}
        target=next(r for r in candidate['registers'] if r['id']==reg['id'])
        edit_register(candidate,target,name,bits,codes)
        if bits!=oldbits and bus==oldlabel:bus=register_bus_name(target)
        if bus:assign_net(candidate,target['id']+'.B<{}:0>'.format(bits-1),bus)
        reg.clear();reg.update(target)

    def add_register_node(self):
        try:self.commit_register()
        except ValueError as ex:self.error(str(ex));return
        self.snapshot();n=len(self.project.get('registers',[]));reg=add_register(self.project,x=150,y=180+n*280)
        self.selected=reg['id'];self.fill_pins();self.redraw();self.status.set('Register added. Assign its output net name to the target array pins.')

    def apply_register(self):
        try:self.snapshot();self.commit_register();self.fill_pins();self.redraw()
        except (ValueError,KeyError) as ex:self.error(str(ex))

    def select_register_row(self,event=None):
        values=self.reglist.selection()
        if not values or values[0]==self.selected:return
        try:self.commit_register()
        except ValueError as ex:self.error(str(ex));return
        self.selected=values[0];self.fill_pins();self.redraw()

    def set_sweep_mode(self,event=None):
        self.project['settings']['sweep_mode']='synchronous' if self.modevar.get()=='Synchronous' else 'cartesian';self.update_result_label()

    @staticmethod
    def net_label(node,pin):
        labels=[node.get('roles',{}).get(s,'') for s in expand_pin(pin)]
        if len(labels)==1:return labels[0]
        if not any(labels):return ''
        parsed=[re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*)<(\d+)>',v) for v in labels]
        if all(parsed) and len({m[1] for m in parsed})==1:return '{}<{}:{}>'.format(parsed[0][1],parsed[0][2],parsed[-1][2])
        return '(individual names)'

    def fill_pins(self):
        self.pins.delete(*self.pins.get_children());self.refresh_registers();node=self.instance()
        if node:
            self.selection_label.set('{} / {}'.format(node['id'],node['name']) if node.get('kind')=='register' else '{} / {}.{} -> {}'.format(node['id'],node['library'],node['cell'],node['view']))
            for pin in node['symbol']['pins']:
                scalars=expand_pin(pin['name']);parent=''
                if pin['name']!=scalars[0]:
                    self.pins.insert('','end',iid=pin['name'],text=pin['name'],values=(self.net_label(node,pin['name']),),open=True);parent=pin['name']
                for scalar in scalars:self.pins.insert(parent,'end',iid=scalar,text=scalar,values=(node.get('roles',{}).get(scalar,''),))
        else:self.selection_label.set('Select a pin; equal role / net names connect automatically.')
        self.rolevar.set('')

    def pin_selected(self,event=None):
        node=self.instance()
        if node and self.pins.selection():self.rolevar.set(self.net_label(node,self.pins.selection()[0]))

    def set_role(self):
        if not self.instance() or not self.pins.selection():return
        ident=self.selected;pin=self.pins.selection()[0];value=self.rolevar.get().strip()
        try:
            self.snapshot();assign_net(self.project,ident+'.'+pin,value);self.fill_pins();self.pins.selection_set(pin);self.pin_selected();self.redraw()
            self.status.set('Assigned {}.{} = {}. Equal names are connected.'.format(ident,pin,value or '(unassigned)'))
        except ValueError as ex:self.error(str(ex))

    def delete_instance(self):
        if not self.instance():return
        self.snapshot();ident=self.selected;self.project['instances']=[i for i in self.project['instances'] if i['id']!=ident]
        self.project['registers']=[r for r in self.project.get('registers',[]) if r['id']!=ident]
        self.project['wires']=[w for w in self.project.get('wires',[]) if not any(e.get('pin','').split('.',1)[0]==ident for e in (w['a'],w['b']))]
        self.project['private']=None;self.selected=None;self.fill_pins();self.redraw()

    def set_canvas_theme(self):
        self.book['appearance']['canvas']=self.canvas_theme.get().lower();self.canvas.configure(bg='#ffffff' if self.canvas_theme.get()=='White' else fw.PLOT_BG);self.redraw()

    def set_plot_theme(self):
        self.book['appearance']['plot']=self.plot_theme.get().lower();self.plots.set_theme(self.plot_theme.get().lower())
        if hasattr(self,'resonance_view'):
            self.resonance_view.plot.set_theme(self.plot_theme.get().lower())
            self.resonance_view.wave_plot.set_theme(self.plot_theme.get().lower())
        if hasattr(self,'tank_view'):self.tank_view.plot.set_theme(self.plot_theme.get().lower())

    def transform(self,node,x,y):
        x,y=node_rotation_transform(node,x,y)
        return x*self.zoom+self.pan[0],y*self.zoom+self.pan[1]

    def node_bounds(self,node):
        b=node['symbol']['bbox']
        pts=[self.transform(node,x,y) for x in (b[0],b[2]) for y in (b[1],b[3])]
        return min(p[0] for p in pts),min(p[1] for p in pts),max(p[0] for p in pts),max(p[1] for p in pts)

    def canvas_key(self,event):
        if self.canvas_tools.keydown(event):return 'break'
        if event.keysym in ('Delete','BackSpace'):
            self.delete_instance();return 'break'

    def delete_selection(self):
        if not self.canvas_tools.command('Delete'):self.delete_instance()

    def canvas_motion(self,event):
        self.canvas_tools.motion(event)

    def canvas_release(self,event):
        self.canvas_tools.release(event);self.drag=None

    def canvas_double(self,event):
        if self.canvas_tools.mode=='select' and self.instance() and hasattr(self,'edit_selected_device'):
            self.edit_selected_device();return 'break'

    def schedule_redraw(self):
        if self.redraw_id:self.after_cancel(self.redraw_id)
        self.redraw_id=self.after(35,self.redraw)

    def redraw(self):
        if hasattr(self,'empty_state'):
            if self.project['instances']:self.empty_state.place_forget()
            else:self.empty_state.place(relx=.5,rely=.5,anchor='center')
        self.redraw_id=None;self.update_result_label();c=self.canvas;c.delete('all');white=self.canvas_theme.get()=='White'
        fg='#20262c' if white else '#d2dce5';muted='#4c626e' if white else '#8ea6b8';grid='#e6e9ec' if white else '#171b20';accent='#006989' if white else fw.accent('cap')
        w,h=c.winfo_width(),c.winfo_height();step=max(18,int(28*self.zoom))
        for x in range(int(self.pan[0]%step),w,step):c.create_line(x,0,x,h,fill=grid)
        for y in range(int(self.pan[1]%step),h,step):c.create_line(0,y,w,y,fill=grid)
        self.canvas_tools.render()
        if not all_nodes(self.project,include_disabled=True):
            c.create_text(w/2,h/2-20,text='Place a symbol or add a register',fill=fg,font=(self.font_family,17))
            c.create_text(w/2,h/2+15,text='Connect pins by assigning the same net name.',fill=muted,font=(self.font_family,12))
        for node in all_nodes(self.project,include_disabled=True):
            tag='inst:'+node['id'];color=muted if not node.get('enabled',True) else (accent if node['id']==self.selected else fg);b=node['symbol']['bbox'];x0,y0,x1,y1=self.node_bounds(node);p=(x0,y1);q=(x1,y0)
            if node['id']==self.selected:c.create_rectangle(x0-12,y0-20,x1+12,y1+32,outline=accent,dash=(5,4),tags=(tag,))
            for shape in node['symbol']['shapes']:
                points=shape['points'];coords=[v for i in range(0,len(points),2) for v in self.transform(node,points[i],points[i+1])]
                if len(coords)<4:continue
                kind=shape['kind']
                if kind=='rect':c.create_rectangle(*coords,outline=color,width=2,tags=(tag,))
                elif kind in ('ellipse','arc'):c.create_oval(*coords,outline=color,width=2,tags=(tag,))
                elif kind=='polygon' and len(coords)>=6:c.create_polygon(*coords,outline=color,fill='',width=2,tags=(tag,))
                else:c.create_line(*coords,fill=color,width=2,tags=(tag,))
            c.create_text((p[0]+q[0])/2,p[1]+22,text=node['id']+' / '+(node['name'] if node.get('kind')=='register' else node['cell']),fill=color,font=(self.font_family,max(9,int(11*self.zoom)),'bold'),tags=(tag,))
            cx,cy=(p[0]+q[0])/2,(p[1]+q[1])/2
            if node.get('kind')=='register':c.create_text(cx,cy,text='DEC / BIN\n{} bit'.format(node['bits']),fill=color,font=(self.font_family,max(9,int(12*self.zoom)),'bold'),tags=(tag,))
            for pin in node['symbol']['pins']:
                x,y=self.transform(node,*pin['xy']);tags=(tag,'pin:'+node['id']+'.'+pin['name']);name=self.net_label(node,pin['name']);label=pin['name']+(' = '+name if name else '')
                c.create_oval(x-4,y-4,x+4,y+4,fill=accent,outline=accent,tags=tags)
                ys=[v['xy'][1] for v in node['symbol']['pins']];py=pin['xy'][1]
                if max(ys)-min(ys)>.01 and (abs(py-max(ys))<.001 or abs(py-min(ys))<.001):
                    upper=abs(py-max(ys))<.001;tx,ty=x,y+(-14 if upper else 14);anchor='s' if upper else 'n'
                    siblings=sorted([v for v in node['symbol']['pins'] if abs(v['xy'][1]-py)<.001],key=lambda v:v['xy'][0])
                    if len(siblings)>2:ty+=(-1 if upper else 1)*siblings.index(pin)*max(18,int(16*self.zoom))
                else:tx,ty=x+(-9 if x<cx else 9),y;anchor='e' if x<cx else 'w'
                c.create_text(tx,ty,text=label,fill=color,anchor=anchor,font=(self.font_family,max(9,int(10*self.zoom))),tags=tags)

    def canvas_down(self,event):
        self.canvas.focus_set()
        if self.canvas_tools.down(event):return
        ident=pin=None
        for item in reversed(self.canvas.find_overlapping(event.x-3,event.y-3,event.x+3,event.y+3)):
            tags=self.canvas.gettags(item)
            ident=next((t[5:] for t in tags if t.startswith('inst:')),None)
            pin=next((t[4:].split('.',1)[1] for t in tags if t.startswith('pin:')),None)
            if ident:break
        if not ident:
            for node in reversed(all_nodes(self.project,include_disabled=True)):
                x0,y0,x1,y1=self.node_bounds(node)
                if x0-12<=event.x<=x1+12 and y0-20<=event.y<=y1+32:ident=node['id'];break
        if ident!=self.selected:
            try:self.commit_register()
            except ValueError as ex:self.error(str(ex));return
            self.selected=ident;self.fill_pins()
        if ident:
            if pin:self.pins.selection_set(pin);self.pins.see(pin);self.pin_selected()
            else:self.snapshot();self.drag=('inst',event.x,event.y)
        self.redraw()

    def canvas_drag(self,event):
        if self.canvas_tools.motion(event):return
        if not self.drag:return
        mode,x,y=self.drag;dx,dy=event.x-x,event.y-y
        if mode=='pan':self.pan[0]+=dx;self.pan[1]+=dy
        elif self.instance():self.instance()['x']+=dx/self.zoom;self.instance()['y']+=dy/self.zoom
        self.drag=(mode,event.x,event.y);self.schedule_redraw()

    def scale_view(self,event,factor):
        old=self.zoom;self.zoom=max(.15,min(3,self.zoom*factor));scale=self.zoom/old
        self.pan=[event.x-(event.x-self.pan[0])*scale,event.y-(event.y-self.pan[1])*scale];self.schedule_redraw()

    def fit(self):
        self.zoom=1.;self.pan=[0.,0.];points=[]
        for node in all_nodes(self.project,include_disabled=True):
            x0,y0,x1,y1=self.node_bounds(node);points.extend([(x0,y0),(x1,y1)])
        if points:
            x0=min(x for x,y in points)-70;x1=max(x for x,y in points)+70;y0=min(y for x,y in points)-60;y1=max(y for x,y in points)+70
            w,h=self.canvas.winfo_width(),self.canvas.winfo_height();self.zoom=min(1.5,max(.15,min(w/(x1-x0),h/(y1-y0))))
            self.pan=[(w-(x0+x1)*self.zoom)/2,(h-(y0+y1)*self.zoom)/2]
        self.redraw()

    def run(self):
        pid=self.book['active_page']
        if pid in self.sim_jobs:return
        if self.project.get('demo_model'):
            self.capture_fields(strict=False);self.replay_example();return
        if pid in self.sim_jobs:self.status.set('This page is already running. Other pages remain available.');return
        view=self.measurement_view.get()
        if view in ('Resonance','Tank scan'):
            self.project['settings']['capture_loss']=True;self.decomp_enabled.set(True)
        if view=='Tank scan':
            try:
                from .spectrum import simulation_grid
                from .model import number
                start,stop=number(self.fields['simulation_start'].get()),number(self.fields['simulation_stop'].get())
                if not all(start<=f<=stop for f in frequencies(self.fields['frequencies'].get())):
                    self.fields['frequencies'].set('{:.6g}'.format(math.sqrt(start*stop)))
            except ValueError:pass
            settings=tank_settings(self.page(pid),self.project);ref=self.tank_view.store_ref()
            if backend_mode(settings)=='active' and ref:self.project['settings']['tank_gm_ref']=ref
            else:self.project['settings'].pop('tank_gm_ref',None)
            from .tank_scan import check_roles
            for issue in check_roles(settings):self.log('Tank roles: '+issue,'warning',pid)
        else:self.project['settings'].pop('tank_gm_ref',None)
        try:cfg=self.sync()
        except Exception as ex:self.error(str(ex));return
        libs=self.catalog_cache.get(self.context_key(self.project),self.libs)
        if self.project['library'] not in libs:self.error('Libraries are still loading. Please wait for the load indicator.');return
        p=copy.deepcopy(self.project);p.pop('run_project',None);page=self.page(pid);page.pop('last_error',None)
        page['observation_revision']=page.get('observation_revision',0)+1
        page['progress']=dict(completed=0,total=len(cfg['codes']),active=True,message='Queued / preparing')
        page['run_points']={str(i):dict(point=i,codes=point,state='pending',path='') for i,point in enumerate(cfg.get('scan_points') or [dict(code=c) for c in cfg['codes']])}
        self.log('Two-port separation preserves the original floating P/N DC bias.' if p['settings'].get('decomposition') else 'SP port is connected from measurement P to N (50 ohm); no P/N common-mode clamps are added.','info',pid)
        def work(b):return p,b.run(p,libs)
        def done(result):
            completed,rows=result;page=self.page(pid)
            if not page:return
            page['observation_revision']=page.get('observation_revision',0)+1
            if completed['settings'].get('decomposition'):
                strict=sum(bool((row.get('decomposition') or {}).get('valid')) for row in rows)
                fitted=sum(bool((row.get('decomposition') or {}).get('approximate')) for row in rows)
                self.log('{} strict splits, {} approximate pi fits, {} unavailable; exact totals retained.'.format(strict,fitted,len(rows)-strict-fitted),
                         'warning' if strict<len(rows) else 'info',pid)
            identity=digest(completed);page['run_project']=completed
            for index,native in (completed['private'].get('point_logs') or {}).items():
                item=page.setdefault('run_points',{}).setdefault(str(index),dict(point=int(index),codes={}))
                item.update({k:native[k] for k in ('path','state') if k in native})
                if native.get('logs'):item.setdefault('logs',{}).update(native['logs'])
            page['result_points']=copy.deepcopy(page.get('run_points',{}))
            descriptor={k:v for k,v in completed['private'].items() if k not in ('spectrum','loss_spectrum')}
            if digest(page['project'])==identity:page['project']['private']=descriptor
            page['result']=cache(rows,identity,completed['settings'].get('sweep_mode','cartesian'));page['result']['analysis']='sp'
            page['progress']=dict(completed=len(cfg['codes']),total=len(cfg['codes']),active=False,message='Complete')
            if pid==self.book['active_page']:
                self.rows=english_rows(rows);self.result_identity=identity;self.result_mode=completed['settings'].get('sweep_mode','cartesian');self.result_analysis='sp'
                self.show_results();self.status.set('SP measurement complete: {} code points, {} observation frequencies.'.format(len(cfg['codes']),len(cfg['frequencies'])))
            if self.path:self.save_workbook(self.path,automatic=True)
        def failed(message):
            # Keep a fully established native setup for a manual retry. A failed
            # history never replaces a successful result or its saved spectrum.
            page=self.page(pid);private=p.get('private')
            if page and private and private.get('spectrum_schema') in (1,2) and digest(page['project'])==digest(p):
                page['project']['private']={k:v for k,v in private.items() if k not in ('spectrum','loss_spectrum')}
                self.log('Native setup retained. Run again to start a separate history.','info',pid)
            self.status.set('Measurement failed / stopped. Expand the log for details; Run starts a new history.')
        self.task(work,done,pid,kind='simulation',label='Build / run SP measurement',failed=failed)

    def stop(self):
        ident=self.sim_jobs.get(self.book['active_page'])
        if ident and ident in self.jobs:
            self.jobs[ident]['backend'].stop();self.log('Stop requested for this page.','warning');self.status.set('Stopping the current page...')

    def apply_observations(self):
        try:
            if self.capture_config_edits():raise ValueError('Saved native config bindings changed. Run a new measurement first.')
            if self.project.get('config_template') and self.page().get('run_project') and self.project.get('config_template')!=self.page()['run_project'].get('config_template'):
                raise ValueError('Saved native config bindings changed. Run a new measurement first.')
            requested=self.fields['frequencies'].get().strip();frequencies(requested)
            page=self.page();pid=page['id'];source=page.get('run_project')
            if not source:
                if (self.project.get('private') or {}).get('spectrum_schema') in (1,2):source=self.project
                else:raise ValueError('Run an SP sweep first. Legacy AC results do not contain a reusable SP spectrum.')
            p=dict(source);p['settings']=dict(source['settings']);p['settings']['frequencies']=requested
            page['observation_revision']=page.get('observation_revision',0)+1;revision=page['observation_revision']
        except Exception as ex:self.error(str(ex));return
        def done(rows):
            page=self.page(pid)
            if not page or page.get('observation_revision')!=revision:return
            page['project']['settings']['frequencies']=requested
            page['run_project']=p
            result=cache(rows,digest(p),p['settings'].get('sweep_mode','cartesian'));result['analysis']='sp';page['result']=result
            if pid==self.book['active_page']:
                self.rows=english_rows(rows);self.result_identity=digest(p);self.result_mode=p['settings'].get('sweep_mode','cartesian');self.result_analysis='sp';self.show_results()
                self.status.set('Observation data refreshed from the saved SP sweep. No simulation was started.')
        self.task(lambda b:b.refresh(p),done,pid,label='Apply observation frequencies',failed=lambda text:self.error(text,pid))

    def restore_results(self):
        page=self.page();saved=page.get('result');rows=cached_rows(saved)
        if rows:
            self.rows=rows;self.result_identity=saved['digest'];self.result_mode=saved.get('mode','cartesian');self.result_analysis=saved.get('analysis','legacy AC')
            self.show_results(switch=False);self.axisvar.set(page.get('axis',self.axisvar.get()));self.plot_filters=page.get('filters',{}).copy();self.draw_plots();return
        m=self.project.get('private') or {}
        if not m.get('result_sha256'):return
        path=Path(m.get('folder',''))/'cq.csv'
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=m['result_sha256']:return
        try:
            with path.open(encoding='utf-8',newline='') as stream:rows=list(csv.DictReader(stream))
            for row in rows:
                for key in row:
                    if key=='status':continue
                    if key=='decomposition':row[key]=json.loads(row[key]) if row[key] else None
                    elif key=='Q':row[key]=float(row[key]) if row[key] else None
                    elif key in ('frequency_Hz','C_F','G_S','B_S'):row[key]=float(row[key])
                    else:row[key]=int(row[key])
            self.rows=english_rows(rows);self.result_identity=m['digest'];self.result_mode=self.project['settings'].get('sweep_mode','cartesian');self.result_analysis='sp' if m.get('spectrum_schema') in (1,2) else 'legacy AC'
            self.show_results(switch=False)
        except (OSError,ValueError,KeyError):self.rows=[]

    def result_names(self):return result_names(self.rows[0]) if self.rows else []

    def show_results(self,switch=True):
        self.table_panel.set_rows(self.rows);names=self.result_names();axes=names+['Scan point'] if self.rows and 'point' in self.rows[0] else ['code']
        self.axiscombo.configure(values=axes)
        if self.axisvar.get() not in axes:
            varying=[n for n in names if len({r[n] for r in self.rows})>1];self.axisvar.set(varying[0] if varying else axes[0])
        if switch:self.tabs.select(self.resultpage)
        self.draw_plots();self.update_result_label();self.update_measurement_view()

    def update_result_label(self,*args):
        if self._field_loading or not hasattr(self,'result_title'):return
        if self.book['active_page'] in self.sim_jobs:
            self.result_title.set('SP run in progress - previous results shown.' if self.rows else 'SP run in progress. No completed data yet.');return
        if self.page().get('last_error'):
            self.result_title.set('Last run failed / stopped - see log.'+(' Previous results shown.' if self.rows else ''));return
        if not self.rows:self.result_title.set('Run an SP sweep to see C / Q results.');return
        if self.result_analysis!='sp':self.result_title.set('Legacy AC results - run SP for the current port setup.');return
        candidate=dict(self.project);candidate['settings']=dict(self.project['settings'])
        for key,var in self.fields.items():candidate['settings'][key]=var.get().strip()
        candidate['settings']['decomposition']=bool(self.decomp_enabled.get())
        if self.result_identity!=digest(candidate):self.result_title.set('Setup changed - showing results from the previous run.');return
        try:
            asked=frequencies(self.fields['frequencies'].get());shown=sorted({r['frequency_Hz'] for r in self.rows})
            if asked!=shown:self.result_title.set('Observation frequencies changed - click Apply.');return
        except ValueError:self.result_title.set('Enter valid observation frequencies, then click Apply.');return
        self.result_title.set('{} observed values | SP differential C / Q'.format(len(self.rows)))

    def draw_plots(self):
        if not hasattr(self,'plots'):return
        axis=self.axisvar.get();axis='point' if axis=='Scan point' else axis
        choice=self.componentvar.get();key={'Coupling P-N':'coupling','Ground-path equivalent':'ground','P-to-ground shunt':'p_ground','N-to-ground shunt':'n_ground'}.get(choice)
        rows=self.rows;titles=None
        if key:
            rows=[]
            for row in self.rows:
                shown=dict(row);split=row.get('decomposition') or {};metric=split.get(key)
                if metric is None:metric=(split.get('approximate') or {}).get(key)
                shown.update({name:metric.get(name) for name in ('C_F','Q','G_S','B_S','status')} if metric else {'C_F':None,'Q':None,'G_S':None,'B_S':None,'status':'Unavailable'})
                rows.append(shown)
            short={'coupling':'Coupling','ground':'Ground path','p_ground':'P-ground','n_ground':'N-ground'}[key]
            approximate=any(bool((row.get('decomposition') or {}).get('approximate')) for row in self.rows)
            titles=(short+' C (fF)'+(' - approx.' if approximate else ''),short+' Q'+(' - approx.' if approximate else ''))
        self.plots.set_data(rows,axis,self.result_names(),self.result_mode,self.plot_filters,titles=titles)

    def filter_plot(self):
        if not self.rows:return
        names=[n for n in self.result_names() if n!=self.axisvar.get()]
        win=tk.Toplevel(self);win.title('Select curve slices');win.transient(self);win.grab_set();box=ttk.Frame(win,padding=18);box.pack(fill='both',expand=True);fields={}
        for name in names:
            ttk.Label(box,text=name).pack(anchor='w');var=tk.StringVar(value=str(self.plot_filters.get(name,'All')));fields[name]=var
            ttk.Combobox(box,textvariable=var,values=['All']+[str(v) for v in sorted({r[name] for r in self.rows})],state='readonly').pack(fill='x',pady=4)
        if not names:ttk.Label(box,text='No other registers to filter.').pack()
        def apply():self.plot_filters={k:v.get() for k,v in fields.items() if v.get()!='All'};self.draw_plots();win.destroy()
        ttk.Button(box,text='Apply',command=apply).pack(fill='x',pady=8)

    def export(self):
        rows=[]
        for source in self.table_panel.filtered:
            row={k:v for k,v in source.items() if k!='decomposition'};split=source.get('decomposition') or {}
            row['decomposition.valid']=split.get('valid');row['decomposition.note']=split.get('message','Unavailable')
            for component in ('coupling','ground','total','p_ground','n_ground','p_input','n_input'):
                metric=split.get(component) or {}
                for name in ('C_F','Q','Q_signed','G_S','B_S','status'):row['decomposition.'+component+'.'+name]=metric.get(name)
            approximate=split.get('approximate') or {}
            row['decomposition.approximate']=bool(approximate)
            row['decomposition.approximate.reconstruction_error']=approximate.get('reconstruction_error')
            row['decomposition.approximate.c_sum_residual_F']=approximate.get('c_sum_residual_F')
            for component in ('coupling','ground','total','p_ground','n_ground','correction'):
                metric=approximate.get(component) or {}
                for name in ('C_F','Q','Q_signed','G_S','B_S','status'):row['decomposition.approximate.'+component+'.'+name]=metric.get(name)
            rows.append(row)
        if not rows:return
        path=filedialog.asksaveasfilename(parent=self,title='Export filtered / sorted table',defaultextension='.csv',initialfile='cap_array_CQ.csv',filetypes=[('CSV','*.csv')])
        if not path:return
        if Path(path).exists():shutil.copy2(path,path+datetime.now().strftime('.bak-%Y%m%d-%H%M%S'))
        with open(path,'w',newline='',encoding='utf-8-sig') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
        self.status.set('Exported {} filtered/sorted rows.'.format(len(rows)))

    def book_snapshot(self):
        if self.book['active_page'] not in self.sim_jobs:self.capture_config_edits()
        # Result rows and spectra are published as immutable snapshots. Copy the
        # editable circuit/controls without cloning every raw frequency sample.
        book={k:copy.deepcopy(v) for k,v in self.book.items() if k!='pages'};book['pages']=[]
        for page in self.book['pages']:
            item={k:copy.deepcopy(v) for k,v in page.items() if k not in ('project','run_project','result')}
            project=page['project'];item['project']=copy.deepcopy({k:v for k,v in project.items() if k!='private'})
            item['project']['private']=project.get('private')
            for key in ('run_project','result'):
                if key in page:item[key]=page[key]
            book['pages'].append(item)
        return book

    def save_workbook(self,path,automatic=False):
        snapshot=self.book_snapshot();page_id=self.book['active_page']
        def work(backend):
            backend.activity('Saving all pages and measurement data',0,1)
            with self.file_gate:save_project(path,snapshot)
            backend.activity('Workbook saved',1,1,False)
        def done(value):
            self.path=str(path);self.title('Cap Array Lab — '+Path(path).name)
            if not automatic:self.status.set('Saved all {} pages and their data.'.format(len(snapshot['pages'])))
        self.task(work,done,page_id,label='Save workbook',scope='workbook')

    def save(self):
        try:
            self.stash_page();path=self.path or filedialog.asksaveasfilename(parent=self,title='Save all pages and results',defaultextension='.json',initialfile='cap_array_workbook.json',filetypes=[('Cap Array Lab','*.json')])
            if path:self.save_workbook(path)
        except Exception as ex:self.error(str(ex))

    def replace_project(self,p,prepared=False):
        if self.sim_jobs:self.error('Stop active simulations before replacing the workbook. Page editing and saving remain available.');return
        self.book=p if prepared else workbook(p);self.book.setdefault('appearance',{'canvas':'black','plot':'black'})
        self.canvas_theme.set(self.book['appearance']['canvas'].title());self.set_canvas_theme()
        self.plot_theme.set(self.book['appearance']['plot'].title());self.set_plot_theme()
        self.refresh_pages();self.activate_page(self.page())

    def load_workbook_path(self,path,session=False):
        if self.sim_jobs:self.error('Stop active simulations before loading a different workbook.');return
        def work(backend):
            backend.activity('Loading pages and saved measurement data',0,2)
            # load_project already validates/upgrades workbook files.
            with self.file_gate:data=load_project(path)
            prepared=data if data.get('kind')=='caplab-workbook' else workbook(data)
            backend.activity('Pages and measurement data loaded',2,2,False);return prepared
        def done(book):
            if self.sim_jobs:self.error('A simulation started while loading. Stop it before opening this workbook.');return
            self.replace_project(book,prepared=True);self.path=None if session else str(path);self.title('Cap Array Lab — '+('Untitled workbook' if session else Path(path).name));self.status.set('Loaded all {} pages and their data.'.format(len(book['pages'])))
        self.task(work,done,label='Load workbook',scope='workbook')

    def load_example(self):
        self.load_workbook_path(ROOT/'examples'/'ideal_array.json',session=True)

    def replay_example(self):
        from .demo import replay
        replay(self)

    def open(self):
        if self.sim_jobs:self.error('Stop active simulations before loading a different workbook.');return
        path=filedialog.askopenfilename(parent=self,title='Load all pages',filetypes=[('Cap Array Lab','*.json')])
        if path:self.load_workbook_path(path)

    def new(self):
        if self.sim_jobs:self.error('Stop active simulations before starting a new workbook.');return
        if any(p['project']['instances'] or p['project'].get('registers') for p in self.book['pages']):
            if not messagebox.askyesno('New workbook','Save your current workbook first. Continue with a new workbook?',parent=self):return
        self.path=None;self.title('Cap Array Lab — Untitled workbook');self.replace_project(attach_context(new_project(),self.launch_context) if self.launch_context else new_project())

    def set_instance_view(self,ident,view):
        inst=next(i for i in self.project['instances'] if i['id']==ident)
        if not re.fullmatch(r'[A-Za-z0-9_]+',view):raise ValueError('Select a valid existing cellview.')
        if view!=inst['view']:
            self.snapshot();self.override_instance_binding(inst,view);self.project.pop('config_template',None);self.fill_pins();self.redraw()

    def override_instance_binding(self,inst,view):
        inst['view']=view
        if inst.get('binding_records'):
            for key in ('binding_records','binding_library','binding_cell'):inst.pop(key,None)
            self.log('Manual view selection replaces imported config bindings for '+inst['id']+'.')

    def binding_dialog(self):
        node=self.instance()
        if not node or node.get('kind')=='register':self.status.set('Select an array symbol before changing its cellview.');return
        pid=self.book['active_page'];ident=node['id'];lib=node['library'];cell=node['cell'];old=node['view'];p=copy.deepcopy(self.project)
        def done(views):
            if pid!=self.book['active_page']:self.log('View choices loaded; return to this page and choose Change view.','info',pid);return
            win=tk.Toplevel(self);win.title('Instance cellview binding');win.transient(self);win.grab_set();box=ttk.Frame(win,padding=18);box.pack(fill='both',expand=True)
            ttk.Label(box,text=ident+' / '+lib+'/'+cell).pack(anchor='w');var=tk.StringVar(value=old);choices=[v for v in views if v not in ('symbol','maestro','config')]
            ttk.Combobox(box,textvariable=var,values=choices,state='readonly',width=36).pack(fill='x',pady=10)
            ttk.Label(box,text='Select an existing netlistable schematic / extracted view.\nRaw PEX text must first be imported as a native cellview.',style='Muted.TLabel').pack(anchor='w')
            def apply():
                if var.get() not in choices:self.error('Select an existing cellview.');return
                self.set_instance_view(ident,var.get());win.destroy()
            self.binding_apply=apply;self.binding_var=var;self.binding_choices=choices;self.binding_window=win
            ttk.Button(box,text='Apply binding',command=apply).pack(fill='x',pady=8)
        self.task(lambda b:b.cell_views(b.context(p),lib,cell),done,pid,label='Read cellviews')

    def open_native_config(self):
        pid=self.book['active_page']
        if pid in self.sim_jobs:self.status.set('Config inspection is available after this page finishes. Other pages remain editable.');return
        try:self.sync()
        except Exception as ex:self.error(str(ex));return
        p=copy.deepcopy(self.project);libs=self.catalog_cache.get(self.context_key(p),self.libs)
        def work(b):
            m=p.get('private')
            if not m or m['digest']!=digest(p) or not Path(m['cellpath']).is_dir():m=b.build(p,libs)
            record_config_baseline(p)
            self.viewer.open(b.context(p),m['library'],m['cell'],native_environment=b.native_environment,native_virtuoso=b.native_virtuoso,native_workdir=b.native_workdir);return p
        def done(p):
            page=self.page(pid)
            if page and digest(page['project'])==digest(p):page['project']['private']={k:v for k,v in p['private'].items() if k not in ('spectrum','loss_spectrum')}
            if pid==self.book['active_page']:self.status.set('Private measurement config is open for editing in the native Hierarchy Editor. Save there before Run.')
        self.task(work,done,pid,label='Open native config')

    def open_source_config(self):
        try:
            design=source_design(self.project)
        except ValueError as ex:self.error(str(ex));return
        pid=self.book['active_page'];p=copy.deepcopy(self.project)
        def work(b):
            runtime=p['maestro_runtime'];source=runtime['source']
            self.viewer.open(b.context(p),design['design_library'],design['design_cell'],design['design_view'],mode='a',native_environment=b.native_environment,native_virtuoso=b.native_virtuoso,native_workdir=b.native_workdir)
        self.task(work,lambda value:self.status.set('Source config is open for manual editing in Virtuoso.'),pid,label='Open source config')

    def advanced(self):
        win=tk.Toplevel(self);win.title('Advanced settings');win.transient(self);win.grab_set();box=ttk.Frame(win,padding=18);box.pack(fill='both',expand=True);fields={}
        pairs=[('library','Private TB library',self.project['library']),('reference_lib','Model reference library',self.project['reference']['library']),('reference_cell','Model reference Maestro cell',self.project['reference']['cell']),('temperature','Temperature (C)',self.project['settings']['temperature'])]
        for key,label,value in pairs:
            ttk.Label(box,text=label).pack(anchor='w',pady=(6,2));var=tk.StringVar(value=value);fields[key]=var;ttk.Entry(box,textvariable=var,width=46).pack(fill='x')
        ttk.Label(box,text='The default SP port is floating between P and N.\nThe old AC common-mode bias is not applied to SP.',style='Muted.TLabel').pack(anchor='w',pady=10)
        ttk.Label(box,text='Instance view bindings',font=(self.font_family,11,'bold')).pack(anchor='w',pady=(8,4));views={}
        for inst in self.project['instances']:
            row=ttk.Frame(box);row.pack(fill='x',pady=2);ttk.Label(row,text=inst['id']+' / '+inst['cell'],width=29).pack(side='left')
            var=tk.StringVar(value=inst['view']);views[inst['id']]=var;ttk.Entry(row,textvariable=var,width=18).pack(side='left')
        def apply():
            self.snapshot();self.project['library']=fields['library'].get().strip();self.project['reference']={'library':fields['reference_lib'].get().strip(),'cell':fields['reference_cell'].get().strip()};self.project['settings']['temperature']=fields['temperature'].get().strip()
            changed=any(inst['view']!=views[inst['id']].get().strip() for inst in self.project['instances'])
            for inst in self.project['instances']:
                view=views[inst['id']].get().strip()
                if inst['view']!=view:self.override_instance_binding(inst,view)
            if changed:self.project.pop('config_template',None)
            self.fill_pins();self.redraw();win.destroy()
        ttk.Button(box,text='Apply settings',command=apply,style='Primary.TButton').pack(fill='x',pady=8)
        ttk.Button(box,text='Maestro settings...',command=lambda:runtime_dialog(self)).pack(fill='x',pady=3)
        ttk.Button(box,text='Open source config in Virtuoso...',command=self.open_source_config).pack(fill='x',pady=3)
        private=self.project.get('private')
        if private:
            def edit_config():
                editor=tk.Toplevel(win);editor.title('Private config bindings');text=tk.Text(editor,width=90,height=24,bg=BG,fg=INK,insertbackground=INK,font=('DejaVu Sans Mono',10));text.pack(fill='both',expand=True)
                path=Path(private['cellpath'])/'config/expand.cfg';text.insert('1.0',path.read_text())
                def store():
                    content=text.get('1.0','end-1c');shutil.copy2(str(path),str(path)+datetime.now().strftime('.bak-%Y%m%d-%H%M%S'));path.write_text(content,encoding='utf-8')
                    self.project['config_template']=content.replace(private['library']+'.'+private['cell'],'@CAL_TOP@').replace(private['cell'],'@CAL_CELL@');self.update_result_label();editor.destroy()
                ttk.Button(editor,text='Save bindings',command=store).pack(pady=5)
            ttk.Button(box,text='Edit generated config text...',command=edit_config).pack(fill='x')

    def close(self):
        if self.jobs:
            if not messagebox.askyesno('Close workbench','Stop pending work and close the workbench?',parent=self):return
            for job in list(self.jobs.values()):job['backend'].stop()
        try:self.capture_config_edits();self.stash_page();save_project(ROOT/'.work'/('last_session_tank.json' if self.tank_app else 'last_session.json'),self.book)
        except (OSError,ValueError):pass
        self.closing=True
        try:
            from .native_metadata import shutdown_all
            shutdown_all()
        except ImportError:pass
        self.destroy()

    def destroy(self):
        self.closing=True
        for job in list(getattr(self,'jobs',{}).values()):job['backend'].stop()
        try:
            self.update_idletasks()
            if hasattr(self,'progress'):self.progress.stop()
            for timer in self.tk.splitlist(self.tk.call('after','info')):self.tk.call('after','cancel',timer)
        except tk.TclError:pass
        super().destroy()
