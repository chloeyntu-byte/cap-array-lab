"""Small source-Maestro dialogs; native reads stay outside the Tk thread."""
import copy
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from .maestro_runtime import effective_runtime, normalize_overrides, selected_test
from .source_hierarchy import scan_source_hierarchy, placement_binding
from .model import add_instance


def attach_context(project, context):
    source = copy.deepcopy(context['source'])
    project['workspace'] = source['workspace']
    project['library'] = source['library']
    project['reference'] = {key: source[key] for key in ('library', 'cell')}
    tests = source.get('tests') or []
    project['maestro_runtime'] = {
        'source': source, 'snapshot': copy.deepcopy(context.get('snapshot', {})),
        'native_environment': copy.deepcopy(context.get('native_environment', {})),
        'libraries': copy.deepcopy(context.get('libraries', {})),
        'source_test': tests[0]['name'] if len(tests) == 1 else None, 'overrides': {}}
    project['private'] = None
    return project


def source_description(project):
    runtime = project.get('maestro_runtime') or {}
    source = runtime.get('source') or {}
    if not source:
        return 'Standalone: no invoking Maestro'
    name = '/'.join(str(source.get(k, '?')) for k in ('library', 'cell', 'view'))
    test = runtime.get('source_test')
    return name + (' | ' + test if test else ' | select a source test')


def source_design(project):
    runtime = project.get('maestro_runtime') or {}
    name = selected_test(runtime)
    tests = (runtime.get('source') or {}).get('tests') or []
    row = next((copy.deepcopy(t) for t in tests if t.get('name') == name), None)
    if row is None:
        raise ValueError('Open Cap Array Lab from the source Maestro menu and select its test.')
    for key in ('library', 'cell', 'view'):
        row.setdefault('design_' + key, row.get(key))
    return row


def _display(value):
    return 'Unknown' if value is None else json.dumps(value) if isinstance(value, (dict, list, bool)) else str(value)


def runtime_dialog(app):
    pid = app.book['active_page']; book = app.book; project = app.project
    runtime = copy.deepcopy(project.get('maestro_runtime') or {})
    win = tk.Toplevel(app); win.title('Maestro settings'); win.transient(app)
    width=min(850,app.winfo_screenwidth()-40);height=min(650,app.winfo_screenheight()-80)
    win.geometry('{}x{}'.format(width,height)); win.minsize(min(600,width),min(400,height))
    outer = ttk.Frame(win, padding=12); outer.pack(fill='both', expand=True)
    ttk.Label(outer, text=source_description(project), wraplength=780).pack(anchor='w')
    source = runtime.get('source') or {}
    if source.get('modified') is not False and source:
        ttk.Label(outer, text='The saved source setup is copied. Unsaved window edits are not copied.',
                  style='Muted.TLabel').pack(anchor='w', pady=4)
    tests = [t['name'] for t in source.get('tests', [])]
    selected = tk.StringVar(value=runtime.get('source_test') or (tests[0] if len(tests) == 1 else ''))
    testrow = ttk.Frame(outer); testrow.pack(fill='x', pady=7)
    ttk.Label(testrow, text='Source test', width=18).pack(side='left')
    cb = ttk.Combobox(testrow, textvariable=selected, values=tests, state='readonly')
    cb.pack(side='left', fill='x', expand=True)
    ttk.Label(outer, text='Unchecked fields inherit the source. Checked fields override the private Maestro only.',
              style='Muted.TLabel', wraplength=790).pack(anchor='w', pady=(0, 8))
    notebook = ttk.Notebook(outer); notebook.pack(fill='both', expand=True)
    fields, originals = {}, {}
    groups = [
        ('Jobs', [('command', 'Submission command'), ('distributionmethod', 'Distribution method'),
                  ('interfacename', 'Interface'), ('maxjobs', 'Max jobs'),
                  ('starttimeout', 'Start timeout (s)'), ('configuretimeout', 'Configure timeout (s)'),
                  ('runtimeout', 'Run timeout (s)'), ('lingertimeout', 'Linger timeout (s)'),
                  ('RunIn', 'Run in'), ('DivideJobs', 'Divide jobs'), ('run_mode', 'Run mode')]),
        ('Storage', [('project_dir', 'Project directory'), ('save_res_dir', 'Simulation results root'),
                     ('save_dir', 'Database results root'), ('use_local_psf_dir', 'Use local PSF'),
                     ('local_psf_dir', 'Local PSF directory'), ('use_local_rdb_dir', 'Use local RDB'),
                     ('local_rdb_dir', 'Local RDB directory'), ('fallback', 'Fallback directory mode')])]
    overrides = runtime.get('overrides') or {}
    for title, rows in groups:
        tab = ttk.Frame(notebook); notebook.add(tab, text=title)
        canvas=tk.Canvas(tab,bg='#19252f',highlightthickness=0)
        sy=ttk.Scrollbar(tab,orient='vertical',command=canvas.yview);sy.pack(side='right',fill='y')
        canvas.pack(fill='both',expand=True);canvas.configure(yscrollcommand=sy.set)
        frame=ttk.Frame(canvas,padding=8);window=canvas.create_window(0,0,window=frame,anchor='nw')
        last=[None,None]
        def content_size(event,viewport=canvas,state=last):
            size=(event.width,event.height)
            if state[0]!=size:state[0]=size;viewport.configure(scrollregion=viewport.bbox('all'))
        def viewport_size(event,viewport=canvas,item=window,state=last):
            if state[1]!=event.width:state[1]=event.width;viewport.itemconfigure(item,width=event.width)
        frame.bind('<Configure>',content_size);canvas.bind('<Configure>',viewport_size)
        frame.columnconfigure(2, weight=1); frame.columnconfigure(3, weight=1)
        for col, text in enumerate(('Override', 'Setting', 'Inherited value', 'Override value')):
            ttk.Label(frame, text=text).grid(row=0, column=col, sticky='w', padx=4, pady=5)
        for n, (key, label) in enumerate(rows, 1):
            enabled = tk.BooleanVar(value=key in overrides); value = tk.StringVar(value=_display(overrides[key]) if key in overrides else '')
            original = tk.StringVar(value='Unknown'); originals[key] = original
            ttk.Checkbutton(frame, variable=enabled).grid(row=n, column=0)
            ttk.Label(frame, text=label).grid(row=n, column=1, sticky='w', padx=4, pady=3)
            ttk.Entry(frame, textvariable=original, state='readonly', width=22).grid(row=n, column=2, sticky='ew', padx=4)
            entry = ttk.Entry(frame, textvariable=value, width=24)
            entry.grid(row=n, column=3, sticky='ew', padx=4)
            def toggle(*args, var=enabled, widget=entry):
                widget.configure(state='normal' if var.get() else 'disabled')
            enabled.trace_add('write', toggle); toggle(); fields[key] = (enabled, value)
    envframe = ttk.Frame(notebook, padding=10); notebook.add(envframe, text='Environment')
    ttk.Label(envframe, text='Other native test Environment options are inherited from the selected source test.',
              wraplength=760).pack(anchor='w')
    ttk.Label(envframe, text='Optional overrides as JSON, e.g. {"switchViewList": ["spectre", "schematic"]}.\n'
              'Model files and batch control are managed by the measurement tool.',
              style='Muted.TLabel', wraplength=760).pack(anchor='w', pady=8)
    envtext = tk.Text(envframe, height=8, wrap='word', bg='#101820', fg='#dce6ed', insertbackground='#dce6ed')
    envtext.pack(fill='both', expand=True); envtext.insert('1.0', json.dumps(overrides.get('env_options', {}), indent=2))
    ttk.Label(envframe, text='Native process environment is captured from the invoking Maestro; it is separate from test Environment options.',
              style='Muted.TLabel', wraplength=760).pack(anchor='w', pady=8)
    def preview(*args):
        runtime['source_test'] = selected.get() or None
        try:
            values = effective_runtime(runtime)
            for key, var in originals.items(): var.set(_display(values[key]['original']) if values[key]['available'] else 'Unknown')
        except ValueError:
            for var in originals.values(): var.set('Select source test')
    cb.bind('<<ComboboxSelected>>', preview); preview()
    def apply():
        try:
            if app.book is not book or app.page(pid) is None: raise ValueError('This workbook has changed; reopen Maestro settings.')
            values = {}
            for key, (enabled, value) in fields.items():
                if enabled.get():
                    text = value.get().strip()
                    if key in ('use_local_psf_dir', 'use_local_rdb_dir'):
                        if text.lower() not in ('true', 'false'): raise ValueError(key + ': enter true or false')
                        values[key] = text.lower() == 'true'
                    else: values[key] = text
            env = json.loads(envtext.get('1.0', 'end-1c').strip() or '{}')
            if env: values['env_options'] = env
            runtime['overrides'] = normalize_overrides(values)
            runtime['source_test'] = selected.get() or None
            if source: selected_test(runtime)
            target = app.page(pid)['project']
            if (target.get('maestro_runtime') or {}).get('source',{})!=source:
                raise ValueError('The source Maestro changed; reopen settings.')
            if pid == app.book['active_page']: app.snapshot()
            target['maestro_runtime'] = copy.deepcopy(runtime)
            app.log('Maestro settings updated; unchecked fields inherit their source.', page_id=pid)
            if pid == app.book['active_page']: app.refresh_source_label(); app.update_result_label()
            win.destroy()
        except (ValueError, TypeError) as exc: app.error(str(exc), page_id=pid)
    ttk.Button(outer, text='Apply to this page', command=apply, style='Accent.TButton').pack(side='bottom', fill='x', pady=(8, 0))
    app.runtime_window = win; app.runtime_fields = fields; app.runtime_apply = apply


def hierarchy_dialog(app):
    pid = app.book['active_page']; project = copy.deepcopy(app.project)
    try: design = source_design(project)
    except ValueError as exc: app.error(str(exc)); return
    runtime = project['maestro_runtime']; source = runtime['source']
    workspace = source['workspace']
    def work(backend):
        backend.activity('Reading source config hierarchy', 0, 1)
        tree = scan_source_hierarchy(backend.context(project), design,
            native_environment=backend.native_environment, native_virtuoso=backend.native_virtuoso,native_workdir=backend.native_workdir)
        if tree['errors']:
            raise ValueError('Config hierarchy is incomplete: ' + '; '.join(str(e['kind']) + ': ' + str(e.get('detail', '')) for e in tree['errors'][:4]))
        backend.activity('Source config hierarchy ready', 1, 1, False)
        return tree
    def done(data):
        if pid != app.book['active_page']: app.log('Hierarchy is ready. Return to this page and open Load hierarchy again.', page_id=pid); return
        if app.project.get('maestro_runtime') != runtime: app.log('Discarded hierarchy from an earlier source test.', page_id=pid); return
        show_hierarchy(app, data, project, pid)
    app.task(work, done, pid, label='Load source hierarchy')


def show_hierarchy(app, hierarchy, project, pid):
    win = tk.Toplevel(app); win.title('Source config hierarchy'); win.transient(app)
    width=min(960,app.winfo_screenwidth()-40);height=min(600,app.winfo_screenheight()-80)
    win.geometry('{}x{}'.format(width,height));win.minsize(min(600,width),min(360,height))
    box = ttk.Frame(win, padding=12); box.pack(fill='both', expand=True)
    ttk.Label(box, text=source_description(project), wraplength=900).pack(anchor='w')
    ttk.Label(box, text='Select an occurrence. Placement preserves its resolved implementation and descendant bindings.',
              style='Muted.TLabel', wraplength=900).pack(anchor='w', pady=5)
    query = tk.StringVar(); ttk.Entry(box, textvariable=query).pack(fill='x', pady=5)
    frame = ttk.Frame(box); frame.pack(fill='both', expand=True)
    tree = ttk.Treeview(frame, columns=('library', 'cell', 'view', 'kind'), show='tree headings', selectmode='browse')
    for key, text, width in [('#0', 'Instance path', 290), ('library', 'Library', 130), ('cell', 'Cell', 160), ('view', 'Implementation', 125), ('kind', 'State', 100)]:
        tree.heading(key, text=text); tree.column(key, width=width, minwidth=60)
    ys = ttk.Scrollbar(frame, command=tree.yview); ys.pack(side='right', fill='y'); xs = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview); xs.pack(side='bottom', fill='x')
    tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set); tree.pack(fill='both', expand=True)
    records = {str(i): row for i, row in enumerate(hierarchy['occurrences'])}; epoch = [0]
    def fill(*args):
        epoch[0] += 1; token = epoch[0]; tree.delete(*tree.get_children())
        q = query.get().casefold(); rows = [(i, r) for i, r in records.items() if q in ' '.join(str(r.get(k, '')) for k in ('path', 'library', 'cell', 'view')).casefold()]
        def batch(index=0):
            if not win.winfo_exists() or token != epoch[0]: return
            for ident, row in rows[index:index+200]:
                kind = 'Ignored' if row.get('ignored') else 'Stop' if row.get('stop') else 'Module' if row.get('traversable') else 'Leaf'
                tree.insert('', 'end', iid=ident, text=row['path'], values=(row['library'], row['cell'], row['view'], kind))
            if index + 200 < len(rows): win.after(1, lambda: batch(index+200))
        batch()
    query.trace_add('write', fill); fill()
    def place():
        if not tree.selection(): return
        row = records[tree.selection()[0]]
        if row.get('ignored') or row.get('symbol_masked'): app.error('This occurrence is excluded from netlisting.'); return
        if pid != app.book['active_page'] or app.project.get('maestro_runtime') != project.get('maestro_runtime'):
            app.error('Source page changed; reopen the hierarchy.'); return
        current = copy.deepcopy(app.project)
        def loaded(symbol):
            if pid != app.book['active_page'] or app.project.get('maestro_runtime') != project.get('maestro_runtime') or app.context_key(app.project)!=app.context_key(current): return
            app.snapshot(); n = len(app.project['instances'])
            inst = add_instance(app.project, symbol, 610+n%2*390, 220+n//2*340)
            inst.update(placement_binding(hierarchy, row, inst['id']))
            app.project.pop('config_template', None); app.selected = inst['id']; app.fill_pins(); app.redraw()
            app.status.set('Placed ' + row['path'] + ' with source config bindings.'); app.log('Placed source occurrence ' + row['path'])
        app.task(lambda b: b.symbol(b.context(current), row['raw_library'], row['raw_cell']), loaded, pid, label='Load hierarchy symbol ' + row['raw_cell'])
    bar = ttk.Frame(box); bar.pack(fill='x', pady=(8, 0))
    ttk.Button(bar, text='Place selected symbol', command=place, style='Accent.TButton').pack(side='right')
    ttk.Button(bar, text='Open source config in Virtuoso', command=app.open_source_config).pack(side='left')
    tree.bind('<Double-1>', lambda e: place())
    app.hierarchy_window = win; app.hierarchy_tree = tree; app.hierarchy_place = place; app.hierarchy_records = records
