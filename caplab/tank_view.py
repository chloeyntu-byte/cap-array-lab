"""Tank scan view: choose what the canvas contains, assign roles, read startup results.

Changing roles or the 'contains' selection only re-analyzes saved native data;
it never launches a simulation.
"""
import csv
import math
import shutil
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk, filedialog

from . import tank_scan
from .plotview import CQPlot
from .tableview import ResultsTable

ROLE_CHOICES = [tank_scan.ROLE_LABELS[r] for r in tank_scan.ROLES]
ROLE_FROM_LABEL = {v: k for k, v in tank_scan.ROLE_LABELS.items()}

COLUMNS = [
    ('f0_GHz', 'f0 (GHz)'), ('f0_LC_GHz', 'f0 LC only (GHz)'),
    ('Rp_LC_ohm', 'Rp(LC) (ohm)'), ('Rp_loaded_ohm', 'Rp(LC+loading) (ohm)'),
    ('margin', 'Startup margin G_neg/G_loss'), ('gmRp', 'gm*Rp'),
    ('Rp_loaded_f0_ohm', 'Rp(LC+loading), gm@f0 split (ohm)'), ('margin_f0', 'Margin, gm@f0 split'),
    ('gm_ref_mS', 'gm @f_ref (mS)'), ('gds_ref_mS', 'gds @f_ref (mS)'),
    ('gm_f0_mS', 'gm @f0 (mS)'), ('gload_f0_mS', 'g_load @f0 (mS)'),
    ('G_neg_mS', 'G_neg (mS)'), ('G_LC_mS', 'G_LC (mS)'), ('G_core_mS', 'Core loading (mS)'),
    ('G_other_mS', 'Other loads (mS)'), ('G_loss_mS', 'G_loss (mS)'),
    ('Q_LC', 'Q LC'), ('Q_loaded', 'Q loaded'), ('C_eff_fF', 'C_eff (fF)'), ('L_eff_nH', 'L_eff (nH)'),
    ('bracket_pct', 'f0 bracket (%)'), ('status', 'Status')]
PASSIVE_HIDE = {'margin', 'gmRp', 'Rp_loaded_f0_ohm', 'margin_f0', 'gm_ref_mS', 'gds_ref_mS', 'gm_f0_mS', 'gload_f0_mS', 'G_neg_mS', 'G_core_mS'}


def _scaled(value, scale):
    return None if value is None else value * scale


def display_rows(data):
    out = []
    for row in data['rows']:
        r = row['tank']
        item = {k: v for k, v in row.items() if k != 'tank'}
        item.update(
            f0_GHz=_scaled(r.get('f0_Hz'), 1e-9), f0_LC_GHz=_scaled(r.get('f0_LC_Hz'), 1e-9),
            Rp_LC_ohm=r.get('Rp_LC_ohm'), Rp_loaded_ohm=r.get('Rp_loaded_ohm'),
            margin=r.get('margin'), gmRp=r.get('gmRp'),
            Rp_loaded_f0_ohm=r.get('Rp_loaded_f0_ohm'), margin_f0=r.get('margin_f0'),
            gm_ref_mS=_scaled(r.get('gm_ref_S'), 1e3), gds_ref_mS=_scaled(r.get('gds_ref_S'), 1e3),
            gm_f0_mS=_scaled(r.get('gm_f0_S'), 1e3), gload_f0_mS=_scaled(r.get('gload_f0_S'), 1e3),
            G_neg_mS=_scaled(r.get('G_neg_S'), 1e3), G_LC_mS=_scaled(r.get('G_LC_S'), 1e3),
            G_core_mS=_scaled(r.get('G_core_load_S'), 1e3), G_other_mS=_scaled(r.get('G_other_S'), 1e3),
            G_loss_mS=_scaled(r.get('G_loss_S'), 1e3), Q_LC=r.get('Q_LC'), Q_loaded=r.get('Q_loaded'),
            C_eff_fF=_scaled(r.get('C_eff_F'), 1e15), L_eff_nH=_scaled(r.get('L_eff_H'), 1e9),
            bracket_pct=_scaled(r.get('bracket_rel'), 100))
        if not r.get('available'):
            item['status'] = r.get('reason', 'Unavailable')
        else:
            parts = []
            if r.get('margin') is not None:
                parts.append('Starts' if r['margin'] > 1 else 'No start (margin < 1)')
            if r.get('partial'):
                parts.append(r.get('reason', ''))
            if r.get('warning'):
                parts.append(r['warning'])
            item['status'] = '; '.join(p for p in parts if p) or 'OK'
        for key, value in list(item.items()):
            if isinstance(value, float) and math.isfinite(value):
                item[key] = float('{:.6g}'.format(value))
        out.append(item)
    return out


def palette():
    """Tank view colors from the shared RF-FIELDWORK theme."""
    from . import fieldwork_tk as fw
    acc = fw.accent('cap')
    return {'BG': fw.BG, 'PANEL': fw.PANEL, 'CARD': fw.PANEL, 'LINE': fw.LINE, 'INK': fw.TEXT, 'MUTED': fw.DIM,
            'ACCENT': acc, 'SELECT': fw._mix(fw.BG, acc, .35), 'GOOD': fw.OK, 'BAD': fw.ERR}


def ensure_styles(app):
    """Card, badge and checkbox styles used by the tank view (idempotent)."""
    c = palette()
    style = ttk.Style(app)
    font = getattr(app, 'font_family', 'TkDefaultFont')
    style.configure('Card.TFrame', background=c['CARD'])
    style.configure('Card.TLabel', background=c['CARD'], foreground=c['INK'])
    style.configure('CardMuted.TLabel', background=c['CARD'], foreground=c['MUTED'], font=(font, 9))
    style.configure('Badge.TLabel', background=c['SELECT'], foreground=c['ACCENT'], font=(font, 9, 'bold'), padding=(8, 3))
    style.configure('Card.TCheckbutton', background=c['CARD'])
    style.map('Card.TCheckbutton', background=[('active', c['CARD'])])


class TankView(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.data = {'rows': [], 'names': [], 'mode': 'passive', 'issues': []}
        self.rows = []
        self.colors = palette()
        ensure_styles(app)
        font = getattr(app, 'font_family', 'DejaVu Sans')
        self.mono = getattr(app, 'mono_family', font)
        c = self.colors
        # --- toolbar: run, what the canvas contains, actions
        bar = ttk.Frame(self, style='Card.TFrame', padding=(10, 8))
        bar.pack(fill='x', padx=8, pady=(6, 4))
        ttk.Button(bar, text='Run tank scan', style='Primary.TButton', command=app.run).pack(side='left')
        ttk.Button(bar, text='Stop', command=app.stop).pack(side='left', padx=(6, 14))
        ttk.Label(bar, text='Canvas contains', style='CardMuted.TLabel').pack(side='left', padx=(0, 8))
        self.contains = {}
        for key, label in (('inductor', 'Inductor / N-port'), ('array', 'Cap array'), ('gm', 'gm core')):
            var = tk.BooleanVar(value=False)
            ttk.Checkbutton(bar, text=label, variable=var, style='Card.TCheckbutton',
                            command=self.settings_changed).pack(side='left', padx=(0, 10))
            self.contains[key] = var
        self.mode_label = tk.StringVar()
        ttk.Label(bar, textvariable=self.mode_label, style='Badge.TLabel').pack(side='left', padx=(6, 0))
        ttk.Button(bar, text='Export CSV', style='Ghost.TButton', command=self.export).pack(side='right')
        ttk.Button(bar, text='Narrow sweep to f0', style='Ghost.TButton', command=self.narrow_sweep).pack(side='right', padx=4)
        self.setup_button = ttk.Button(bar, text='Roles & options ▾', style='Ghost.TButton', command=self.toggle_setup)
        self.setup_button.pack(side='right', padx=4)
        self.run_state = tk.StringVar()
        if hasattr(app, 'sim_label'):
            ttk.Label(bar, textvariable=app.sim_label, style='CardMuted.TLabel').pack(side='right', padx=10)
        # --- collapsible roles / options
        self.setup = ttk.Frame(self, style='Card.TFrame', padding=(10, 6))
        self.setup_open = False
        roles = ttk.Frame(self.setup, style='Card.TFrame')
        roles.pack(side='left', fill='x', expand=True)
        self.role_tree = ttk.Treeview(roles, columns=('cell', 'role'), show='tree headings', height=4, selectmode='browse')
        self.role_tree.heading('#0', text='INSTANCE')
        self.role_tree.heading('cell', text='CELL / DEVICE')
        self.role_tree.heading('role', text='TANK ROLE')
        self.role_tree.column('#0', width=90)
        self.role_tree.column('cell', width=280)
        self.role_tree.column('role', width=160)
        self.role_tree.pack(side='left', fill='x', expand=True)
        self.role_tree.bind('<<TreeviewSelect>>', self.role_selected)
        side = ttk.Frame(self.setup, style='Card.TFrame', padding=(12, 0, 0, 0))
        side.pack(side='left', fill='y')
        ttk.Label(side, text='Role of selected instance', style='CardMuted.TLabel').pack(anchor='w')
        self.role_var = tk.StringVar()
        self.role_box = ttk.Combobox(side, textvariable=self.role_var, values=ROLE_CHOICES, state='readonly', width=22)
        self.role_box.pack(fill='x', pady=(2, 4))
        self.role_box.bind('<<ComboboxSelected>>', lambda e: self.apply_role())
        ttk.Button(side, text='Auto-detect roles', command=self.auto_roles).pack(fill='x')
        row = ttk.Frame(side, style='Card.TFrame')
        row.pack(fill='x', pady=(8, 0))
        ttk.Label(row, text='gm reference freq.', style='CardMuted.TLabel').pack(side='left')
        self.ref_var = tk.StringVar(value='10M')
        ttk.Entry(row, textvariable=self.ref_var, width=8).pack(side='right')
        self.note = tk.StringVar()
        ttk.Label(self, textvariable=self.note, wraplength=1100, style='Muted.TLabel', padding=(10, 0, 10, 2)).pack(fill='x')
        # --- KPI tiles for the selected point
        self.kpi = tk.Frame(self, bg=c['PANEL'])
        self.kpi.pack(fill='x', padx=8, pady=(2, 4))
        self.tiles = {}
        for key, caption in (('f0', 'f0'), ('f0lc', 'f0 · LC only'), ('rplc', 'Rp(LC)'), ('rpload', 'Rp(LC+loading)'),
                             ('margin', 'Startup margin'), ('gmrp', 'gm·Rp'), ('gm', 'gm / gds  (per side)')):
            tile = tk.Frame(self.kpi, bg=c['CARD'], highlightthickness=1, highlightbackground=c['LINE'], padx=12, pady=6)
            tile.pack(side='left', fill='both', expand=True, padx=(0, 6))
            tk.Label(tile, text=caption.upper(), bg=c['CARD'], fg=c['MUTED'], font=(font, 8, 'bold'), anchor='w').pack(fill='x')
            value = tk.Label(tile, text='—', bg=c['CARD'], fg=c['INK'], font=(font, 16, 'bold'), anchor='w')
            value.pack(fill='x')
            unit = tk.Label(tile, text='', bg=c['CARD'], fg=c['MUTED'], font=(font, 9), anchor='w')
            unit.pack(fill='x')
            self.tiles[key] = (value, unit)
        # --- resizable plot / table / loss areas
        self.panes = ttk.Panedwindow(self, orient='vertical')
        self.panes.pack(fill='both', expand=True, padx=8, pady=(0, 6))
        plotframe = ttk.Frame(self.panes)
        tableframe = ttk.Frame(self.panes)
        lossframe = ttk.Frame(self.panes)
        self.panes.add(plotframe, weight=6)
        self.panes.add(tableframe, weight=2)
        self.panes.add(lossframe, weight=2)
        self._sashes_set = False
        self.panes.bind('<Configure>', self._initial_sashes, add='+')
        axisbar = ttk.Frame(plotframe, padding=(2, 2))
        axisbar.pack(fill='x')
        ttk.Label(axisbar, text='X axis', style='Muted.TLabel').pack(side='left')
        self.axis = tk.StringVar()
        self.axisbox = ttk.Combobox(axisbar, textvariable=self.axis, state='readonly', width=16)
        self.axisbox.pack(side='left', padx=6)
        self.axisbox.bind('<<ComboboxSelected>>', lambda e: self.draw())
        ttk.Label(axisbar, text='Click a curve to highlight; hover a point to read it.', style='Muted.TLabel').pack(side='left', padx=10)
        self.plot = CQPlot(plotframe, height=300)
        self.plot.hide_single_legend = True
        self.plot.empty_message = 'Run the tank scan to see f0 and startup margin.'
        self.plot.pack(fill='both', expand=True)
        self.table = ResultsTable(tableframe, on_select=self.select, on_log=self.open_log)
        self.table.tree.configure(height=7)
        self.table.pack(fill='both', expand=True)
        self.detail = tk.StringVar(value='Select a scan point to see the loss contributions at f0.')
        ttk.Label(lossframe, textvariable=self.detail, wraplength=1100, style='Muted.TLabel', padding=(2, 4)).pack(fill='x')
        area = ttk.Frame(lossframe)
        area.pack(fill='both', expand=True)
        cols = ('module', 'role', 'g', 'share', 'gneg', 'gm', 'gload')
        self.loss_tree = ttk.Treeview(area, columns=cols, show='headings', height=5)
        for key, label, width in (('module', 'MODULE / GROUP', 220), ('role', 'ROLE', 130), ('g', 'LOSS G (mS)', 110),
                                  ('share', 'SHARE OF G_LOSS', 130), ('gneg', 'G_NEG (mS)', 100),
                                  ('gm', 'gm @f0 (mS)', 100), ('gload', 'g_load @f0 (mS)', 120)):
            self.loss_tree.heading(key, text=label)
            self.loss_tree.column(key, width=width, anchor='w' if key in ('module', 'role') else 'e')
        sy = ttk.Scrollbar(area, orient='vertical', command=self.loss_tree.yview)
        sy.pack(side='right', fill='y')
        self.loss_tree.configure(yscrollcommand=sy.set)
        self.loss_tree.pack(fill='both', expand=True)
        for tree in (self.loss_tree, self.table.tree, self.role_tree):
            tree.tag_configure('odd', background=c['PANEL'])

    def _initial_sashes(self, event=None):
        """Give the plot about half of the results height on first layout."""
        h = self.panes.winfo_height()
        if self._sashes_set or h < 300:
            return
        self._sashes_set = True
        try:
            self.panes.sashpos(0, int(h * 0.50))
            self.panes.sashpos(1, int(h * 0.74))
        except tk.TclError:
            pass

    def toggle_setup(self, show=None):
        if show is None:
            self._setup_touched = True
        self.setup_open = (not self.setup_open) if show is None else show
        if self.setup_open:
            self.setup.pack(fill='x', padx=8, pady=(0, 4), before=self.kpi)
        else:
            self.setup.pack_forget()
        self.setup_button.configure(text='Roles & options ▴' if self.setup_open else 'Roles & options ▾')

    def set_tile(self, key, value, unit='', color=None):
        label, unit_label = self.tiles[key]
        label.configure(text=value, fg=color or self.colors['INK'])
        unit_label.configure(text=unit)

    # ------------------------------------------------------------ settings
    def page_settings(self):
        page = self.app.page()
        project = page.get('run_project') or page['project']
        return tank_scan.tank_settings(page, project), page, project

    def load_controls(self):
        settings, page, project = self.page_settings()
        for key, var in self.contains.items():
            var.set(bool(settings['contains'].get(key)))
        ref = page['tank'].get('gm_ref')
        self.ref_var.set('10M' if ref is None else ref)
        self.role_tree.delete(*self.role_tree.get_children())
        nodes = {n['id']: n for n in page['project'].get('instances', []) if n.get('enabled', True)}
        for ident, role in sorted(settings['roles'].items(), key=lambda x: (len(x[0]), x[0])):
            node = nodes.get(ident) or {}
            cell = node.get('cell') or node.get('kind') or ''
            if node.get('kind') == 'snp':
                cell = 'N-port ' + Path((node.get('touchstone') or {}).get('path', '')).name
            elif node.get('library'):
                cell = '{}/{}'.format(node['library'], node['cell'])
            self.role_tree.insert('', 'end', iid=ident, text=ident, values=(cell, tank_scan.ROLE_LABELS[role]),
                                  tags=('odd',) if len(self.role_tree.get_children()) % 2 else ())
        self.mode_label.set('ACTIVE · gm split' if tank_scan.backend_mode(settings) == 'active' else 'PASSIVE · resonance + loss')

    def store_ref(self):
        page = self.app.page()
        page.setdefault('tank', {})['gm_ref'] = self.ref_var.get().strip()
        return page['tank']['gm_ref']

    def settings_changed(self):
        settings, page, project = self.page_settings()
        page['tank']['contains'] = {k: bool(v.get()) for k, v in self.contains.items()}
        self.app.update_measurement_view()

    def role_selected(self, event=None):
        sel = self.role_tree.selection()
        if sel:
            self.role_var.set(self.role_tree.set(sel[0], 'role'))

    def apply_role(self):
        sel = self.role_tree.selection()
        if not sel:
            return
        settings, page, project = self.page_settings()
        page['tank']['roles'][sel[0]] = ROLE_FROM_LABEL[self.role_var.get()]
        self.app.update_measurement_view()

    def auto_roles(self):
        page = self.app.page()
        page.setdefault('tank', {}).pop('roles', None)
        page['tank'].pop('contains', None)
        self.app.update_measurement_view()

    def narrow_sweep(self):
        sweep = tank_scan.suggested_sweep(self.data.get('rows', []))
        if not sweep:
            self.app.status.set('Run a tank scan first; no resolved f0 to narrow around.')
            return
        for key in ('simulation_start', 'simulation_stop', 'simulation_points', 'simulation_sweep'):
            value = sweep[key]
            self.app.fields[key].set('{:.6g}'.format(value) if isinstance(value, float) else str(value))
        obs = self.app.fields['frequencies']
        try:
            from .model import frequencies
            inside = [f for f in frequencies(obs.get()) if sweep['simulation_start'] <= f <= sweep['simulation_stop']]
        except ValueError:
            inside = []
        if not inside:
            obs.set('{:.6g}'.format((sweep['simulation_start'] + sweep['simulation_stop']) / 2))
        self.app.status.set('Sweep narrowed to {:.4g}-{:.4g} GHz ({} linear points). Run again for bracket-accurate values.'.format(
            sweep['simulation_start'] / 1e9, sweep['simulation_stop'] / 1e9, sweep['simulation_points']))

    # ------------------------------------------------------------- results
    def set_data(self, data, mode='cartesian', theme='white'):
        self.data = data
        self.mode = mode
        self.load_controls()
        rows = display_rows(data)
        self.rows = rows
        issues = data.get('issues') or []
        self.note.set(('Check roles: ' + ' '.join(issues) + ' ') if issues else data.get('message', ''))
        names = data.get('names', [])
        ident = ['point'] if rows and 'point' in rows[0] else ['code']
        keys = [k for k, _ in COLUMNS if not (data.get('mode') == 'passive' and k in PASSIVE_HIDE)]
        self.table.fixed_columns = ident + names + keys
        self.table.LABELS.update({k: v for k, v in COLUMNS})
        self.table.set_rows(rows)
        axes = names + (['Scan point'] if rows and 'point' in rows[0] else ['code'])
        self.axisbox.configure(values=axes)
        if self.axis.get() not in axes:
            self.axis.set(next((n for n in names if len({r[n] for r in rows}) > 1), axes[0]))
        self.plot.set_theme(theme)
        self.draw()
        if not rows or issues:
            self.toggle_setup(True)
        elif not getattr(self, '_setup_touched', False):
            self.toggle_setup(False)
        children = self.table.tree.get_children()
        if children:
            self.table.tree.selection_set(children[0])
            self.select(self.table.item_rows.get(children[0], rows[0]) if hasattr(self.table, 'item_rows') else rows[0])
        else:
            self.select(None)

    def draw(self):
        axis = 'point' if self.axis.get() == 'Scan point' else self.axis.get()
        rows = [r for r in self.rows if r.get('f0_GHz') is not None]
        second = (('margin', 'Startup margin G_neg/G_loss', 1, '') if self.data.get('mode') == 'active'
                  else ('Rp_loaded_ohm', 'Rp at f0 (ohm)', 1, 'ohm'))
        self.plot.set_data(rows, axis, self.data.get('names', []), getattr(self, 'mode', 'cartesian'),
                           metrics=[('f0_GHz', 'f0 (GHz)', 1, 'GHz'), second], include_frequency=False)

    def _raw(self, row):
        key = row.get('point', row.get('code'))
        return next((r['tank'] for r in self.data['rows'] if r.get('point', r.get('code')) == key), {})

    def fill_tiles(self, r):
        def fmt(v, scale=1, digits=4):
            return '—' if v is None else '{:.{}g}'.format(v * scale, digits)
        c = self.colors
        self.set_tile('f0', fmt(r.get('f0_Hz'), 1e-9, 5), 'GHz')
        self.set_tile('f0lc', fmt(r.get('f0_LC_Hz'), 1e-9, 5), 'GHz  (core C removed)' if r.get('f0_LC_Hz') else '')
        self.set_tile('rplc', fmt(r.get('Rp_LC_ohm')), 'Ω  inductor + array')
        self.set_tile('rpload', fmt(r.get('Rp_loaded_ohm')), 'Ω  incl. core & other loading')
        m = r.get('margin')
        self.set_tile('margin', fmt(m, 1, 3), ('starts (> 1)' if m > 1 else 'does NOT start') if m is not None else 'needs gm core',
                      None if m is None else (c['GOOD'] if m > 1 else c['BAD']))
        self.set_tile('gmrp', fmt(r.get('gmRp'), 1, 3), 'gm(f_ref) × Rp(LC+loading)' if r.get('gmRp') is not None else '')
        if r.get('gm_ref_S') is not None:
            self.set_tile('gm', '{} / {}'.format(fmt(r['gm_ref_S'], 1e3, 3), fmt(r['gds_ref_S'], 1e3, 3)),
                          'mS @ {:.3g} MHz'.format(r['f_ref_Hz'] / 1e6))
        else:
            self.set_tile('gm', '—', '')

    def select(self, row):
        self.loss_tree.delete(*self.loss_tree.get_children())
        if row is None:
            self.fill_tiles({})
            return
        r = self._raw(row)
        self.fill_tiles(r if r.get('available') else {})
        if not r.get('available'):
            self.detail.set('Unavailable: ' + r.get('reason', ''))
            return
        if r.get('partial'):
            self.detail.set('f0 {:.6g} GHz, Rp = Re(Z) {:.6g} ohm. {}'.format(r['f0_Hz'] / 1e9, r['Z_real_ohm'], r.get('reason', '')))
            return
        text = 'At f0 = {:.6g} GHz (bracket {:.6g}-{:.6g} GHz): G_loss {:.4g} mS = LC {:.4g} + other {:.4g} + core loading {:.4g} mS'.format(
            r['f0_Hz'] / 1e9, r['bracket_Hz'][0] / 1e9, r['bracket_Hz'][1] / 1e9, r['G_loss_S'] * 1e3,
            r['G_LC_S'] * 1e3, r['G_other_S'] * 1e3, r['G_core_load_S'] * 1e3)
        if r.get('margin') is not None:
            text += '; G_neg {:.4g} mS, margin {:.4g}, gm*Rp {:.4g} (gm {:.4g} mS @ {:.4g} MHz).'.format(
                r['G_neg_S'] * 1e3, r['margin'], r['gmRp'], r['gm_ref_S'] * 1e3, r['f_ref_Hz'] / 1e6)
        text += ' Module sum check {:.1e}; Re(Y) vs SP {:.2e} S.'.format(r.get('module_sum_error', 0), r.get('net_check_S', 0))
        if not r.get('shares_valid'):
            text += ' A passive module shows negative loss (coupling transfer); shares are indicative only.'
        self.detail.set(text)

        def ms(v):
            return '' if v is None else '{:.5g}'.format(v * 1e3)
        for n, c in enumerate(sorted(r['contributions'], key=lambda c: -c['G_S'])):
            self.loss_tree.insert('', 'end', tags=('odd',) if n % 2 else (), values=(c['module'], tank_scan.ROLE_LABELS.get(c['role'], c['role']), ms(c['G_S']),
                                                     '' if c['share_pct'] is None else '{:.4g}'.format(c['share_pct']),
                                                     ms(c.get('G_neg_S')), ms(c.get('gm_S')), ms(c.get('gload_S'))))

    def open_log(self, row):
        from .pointlogs import open_row_log
        open_row_log(self.app, row)

    def export(self):
        if not self.rows:
            return
        out = []
        for row in self.table.filtered or self.rows:
            r = self._raw(row)
            base = dict(row)
            for c in r.get('contributions') or [{}]:
                item = dict(base)
                item.update(module=c.get('module', ''), module_role=c.get('role', ''), module_G_S=c.get('G_S'),
                            module_share_pct=c.get('share_pct'), module_G_neg_S=c.get('G_neg_S'),
                            module_gm_S=c.get('gm_S'), module_gload_S=c.get('gload_S'))
                out.append(item)
        path = filedialog.asksaveasfilename(parent=self, defaultextension='.csv', initialfile='tank_scan.csv',
                                            filetypes=[('CSV', '*.csv')])
        if not path:
            return
        if Path(path).exists():
            shutil.copy2(path, path + datetime.now().strftime('.bak-%Y%m%d-%H%M%S'))
        fields = list(dict.fromkeys(k for item in out for k in item))
        with open(path, 'w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(out)
        self.app.status.set('Exported {} tank scan rows.'.format(len(out)))
