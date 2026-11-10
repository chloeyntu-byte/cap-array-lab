"""Derived resonance results; changing this view never launches a simulation."""
import csv
import hashlib
import json
import math
import tkinter as tk
from tkinter import ttk, filedialog

from .decomposition import result_names
from .resonance import find_first_resonance, _impedance, _KEYS
from .loss_native import evaluate_sample
from .plotview import CQPlot
from .tableview import ResultsTable


def resonance_data(project):
    private=project.get('private') or {}
    spectrum=private.get('spectrum') or {}
    raw=spectrum.get('rows') or []
    if not raw:
        return {'rows':[], 'names':[], 'losses':{}, 'message':'Run an SP sweep to identify resonance.'}
    expected=private.get('spectrum_data_sha256')
    if expected and hashlib.sha256(json.dumps(spectrum,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()!=expected:
        raise ValueError('Saved SP spectrum checksum does not match; reload intact data or run again.')
    expected=private.get('loss_data_sha256')
    if expected:
        payload={'plan':private.get('loss_plan'),'spectrum':private.get('loss_spectrum')}
        if hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()!=expected:
            raise ValueError('Saved native loss data checksum does not match; reload intact data or run again.')
    names=result_names(raw[0]);groups={}
    for sample in raw:
        key=sample.get('point',sample.get('code',0))
        groups.setdefault(key,[]).append(sample)
    rows=[];losses={};waveforms=[]
    for sample in raw:
        try:
            f=float(sample['frequency_Hz'])
            z=_impedance(f,[float(sample[k]) for k in _KEYS])
            if z is not None:
                item=dict(sample);item['frequency_GHz']=f/1e9
                item['z_magnitude']=abs(z);item['z_phase_deg']=math.degrees(math.atan2(z.imag,z.real))
                item['Point']=sample.get('point',sample.get('code',0));waveforms.append(item)
        except (KeyError,TypeError,ValueError,ZeroDivisionError):
            continue
    for key,samples in sorted(groups.items()):
        identity={name:samples[0][name] for name in names}
        identity.update({'point':key} if 'point' in samples[0] else {'code':key})
        try:found=find_first_resonance(samples)
        except (ValueError,KeyError) as ex:found={'available':False,'reason':'Full two-port sweep required: '+str(ex)}
        row=dict(identity,frequency_Hz=None,Z_real_ohm=None,Z_imag_ohm=None,Z_abs_ohm=None,kind='',status=found.get('reason',''))
        if found['available']:
            row.update(frequency_Hz=found['frequency_Hz'],Z_real_ohm=found['Z_real_ohm'],Z_imag_ohm=found['Z_imag_ohm'],
                       Z_abs_ohm=math.hypot(found['Z_real_ohm'],found['Z_imag_ohm']),kind=found['kind'],status='Estimated from saved sweep')
            loss_samples=(private.get('loss_spectrum') or {}).get(str(key))
            if loss_samples and private.get('loss_plan'):
                try:losses[key]=evaluate_sample(loss_samples,private['loss_plan'],found['frequency_Hz'])
                except (ValueError,KeyError,TypeError) as ex:losses[key]={'available':False,'reason':str(ex)}
            else:losses[key]={'available':False,'reason':'Run in Resonance mode to capture native module currents.'}
            row['bracket_Hz']=found.get('bracket_Hz')
        else:losses[key]={'available':False,'reason':found.get('reason','No resonance.')}
        rows.append(row)
    return {'rows':rows,'names':names,'losses':losses,'waveforms':waveforms,'message':'Z11 is the impedance across the floating P (+) / N (-) measurement port. First Im(Z11)=0 crossing in increasing frequency.'}


class ResonanceView(ttk.Frame):
    def __init__(self,parent,app):
        super().__init__(parent);self.app=app;self.data={'rows':[],'names':[],'losses':{}};self.axis=tk.StringVar()
        bar=ttk.Frame(self,padding=6);bar.pack(fill='x')
        ttk.Label(bar,text='X axis').pack(side='left')
        self.axisbox=ttk.Combobox(bar,textvariable=self.axis,state='readonly',width=16);self.axisbox.pack(side='left',padx=6)
        self.axisbox.bind('<<ComboboxSelected>>',lambda e:self.draw())
        ttk.Button(bar,text='Export resonance / loss CSV',command=self.export).pack(side='right')
        self.note=tk.StringVar();ttk.Label(self,textvariable=self.note,wraplength=650,style='Muted.TLabel',padding=6).pack(fill='x')
        self.plot_pages=ttk.Notebook(self);self.plot_pages.pack(fill='both',expand=True,padx=7,pady=4)
        summary_page=ttk.Frame(self.plot_pages);wave_page=ttk.Frame(self.plot_pages)
        self.plot_pages.add(summary_page,text='Resonance by code');self.plot_pages.add(wave_page,text='Z11 vs frequency')
        self.plot=CQPlot(summary_page,height=260);self.plot.empty_message='No resolved resonance data.';self.plot.pack(fill='both',expand=True)
        wave_bar=ttk.Frame(wave_page);wave_bar.pack(fill='x')
        ttk.Label(wave_bar,text='Scan point').pack(side='left')
        self.wave_choice=tk.StringVar(value='All');self.wave_box=ttk.Combobox(wave_bar,textvariable=self.wave_choice,state='readonly',width=16)
        self.wave_box.pack(side='left',padx=6);self.wave_box.bind('<<ComboboxSelected>>',lambda e:self.draw_wave())
        ttk.Label(wave_bar,text='Saved samples; Z11 = 1 / Y total',style='Muted.TLabel').pack(side='left',padx=4)
        self.wave_plot=CQPlot(wave_page,height=260);self.wave_plot.metric_minimums={'z_magnitude':0};self.wave_plot.empty_message='No full two-port saved frequency data.';self.wave_plot.pack(fill='both',expand=True)

        self.table=ResultsTable(self,on_select=self.select,on_log=self.open_log)
        self.table.pack(fill='x',padx=7,pady=3)
        self.loss_note=tk.StringVar(value='Select a scan point to inspect module losses.')
        ttk.Label(self,textvariable=self.loss_note,wraplength=690,padding=(8,5)).pack(fill='x')
        area=ttk.Frame(self);area.pack(fill='x',padx=7,pady=(0,6))
        self.loss_tree=ttk.Treeview(area,columns=('module','watts','share'),show='headings',height=4)
        for key,label,width in [('module','Module / coupled group',240),('watts','Absorbed power (W)',155),('share','Loss share (%)',125)]:
            self.loss_tree.heading(key,text=label);self.loss_tree.column(key,width=width,anchor='w' if key=='module' else 'e')
        sy=ttk.Scrollbar(area,orient='vertical',command=self.loss_tree.yview);sy.pack(side='right',fill='y')
        self.loss_tree.configure(yscrollcommand=sy.set);self.loss_tree.pack(fill='x',expand=True)

    def set_data(self,data,mode='cartesian',theme='black'):
        self.data=data;self.mode=mode;names=data['names'];rows=data['rows']
        self.note.set(data.get('message',''))
        axes=names+(['Scan point'] if rows and 'point' in rows[0] else ['code'])
        self.axisbox.configure(values=axes)
        if self.axis.get() not in axes:self.axis.set(next((n for n in names if len({r[n] for r in rows})>1),axes[0]))
        self.table.fixed_columns=(['point'] if rows and 'point' in rows[0] else ['code'])+names+['frequency_Hz','Z_real_ohm','Z_imag_ohm','Z_abs_ohm','kind','status']
        self.table.LABELS.update(frequency_Hz='First resonance (GHz)',Z_real_ohm='Re(Z11) (ohm)',Z_imag_ohm='Im(Z11) (ohm)',Z_abs_ohm='|Z11| (ohm)',kind='Crossing')
        self.table.set_rows(rows);self.plot.set_theme(theme);self.wave_plot.set_theme(theme)
        choices=['All']+[str(k) for k in sorted({r['Point'] for r in data.get('waveforms',[])})]
        self.wave_box.configure(values=choices)
        if self.wave_choice.get() not in choices:self.wave_choice.set('All')
        self.draw();self.draw_wave()

    def draw(self):
        axis='point' if self.axis.get()=='Scan point' else self.axis.get()
        rows=[r for r in self.data['rows'] if r['frequency_Hz'] is not None]
        self.plot.set_data(rows,axis,self.data['names'],getattr(self,'mode','cartesian'),
            metrics=[('frequency_Hz','First resonance (GHz)',1e-9,'GHz'),('Z_real_ohm','Re(Z11) at resonance (ohm)',1,'ohm')],include_frequency=False)

    def draw_wave(self):
        rows=self.data.get('waveforms',[])
        choice=self.wave_choice.get()
        if choice!='All':rows=[r for r in rows if str(r['Point'])==choice]
        self.wave_plot.set_data(rows,'frequency_GHz',['Point']+self.data['names'],'cartesian',
            metrics=[('z_magnitude','|Z11| (ohm)',1,'ohm'),('z_phase_deg','Phase of Z11 (deg)',1,'deg')],include_frequency=False)

    def select(self,row):
        self.loss_tree.delete(*self.loss_tree.get_children())
        if row is None:self.loss_note.set('No completed resonance data.');return
        loss=self.data['losses'].get(row.get('point',row.get('code',0)),{})
        if not loss.get('available'):
            self.loss_note.set('Loss shares unavailable: '+loss.get('reason','No native terminal-current data.'));return
        bracket=row.get('bracket_Hz') or [row['frequency_Hz']]*2
        self.loss_note.set('Loss estimate at {:.9g} GHz; native endpoint-power interpolation, bracket {:.6g} to {:.6g} GHz. DUT normalized to 1 A peak. Input {:.6g} W; balance residual {:.3g} W.{}'.format(
            row['frequency_Hz']/1e9,bracket[0]/1e9,bracket[1]/1e9,loss['input_power_W'],loss['residual_W'],
            ' Coupled mode-1 inductors use measured winding currents and modeled series resistance.' if loss.get('modeled_winding_losses') else ''))
        for name,share in sorted(loss['modules'].items()):
            self.loss_tree.insert('','end',values=(name,'{:.9g}'.format(loss['module_power_W'][name]),'{:.6g}'.format(share)))

    def open_log(self,row):
        from .pointlogs import open_row_log
        open_row_log(self.app,row)

    def export(self):
        rows=[]
        for row in self.table.filtered:
            loss=self.data['losses'].get(row.get('point',row.get('code',0)),{})
            base={k:v for k,v in row.items() if k!='bracket_Hz'}
            bracket=row.get('bracket_Hz') or [None,None]
            base.update(resonance_bracket_start_Hz=bracket[0],resonance_bracket_stop_Hz=bracket[1],
                        input_power_W=loss.get('input_power_W'),power_balance_residual_W=loss.get('residual_W'),
                        loss_interpolation_method=loss.get('method',''),excitation_condition=loss.get('excitation_condition',''))
            modules=loss.get('modules') or {'':None}
            for module,share in modules.items():
                item=dict(base,module=module,loss_percent=share,loss_W=loss.get('module_power_W',{}).get(module),loss_status='Available' if loss.get('available') else loss.get('reason','Unavailable'))
                rows.append(item)
        if not rows:return
        path=filedialog.asksaveasfilename(parent=self,defaultextension='.csv',initialfile='cap_array_resonance.csv',filetypes=[('CSV','*.csv')])
        if not path:return
        from pathlib import Path
        from datetime import datetime
        import shutil
        if Path(path).exists():shutil.copy2(path,path+datetime.now().strftime('.bak-%Y%m%d-%H%M%S'))
        with open(path,'w',newline='',encoding='utf-8-sig') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
