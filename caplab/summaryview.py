"""Selected-point C/Q decomposition summary; no result calculation or native I/O."""
import math
import tkinter as tk
from tkinter import ttk


UNAVAILABLE='Only total is available - enable separation and run'
NOTE='Ground shunts form a series equivalent between P and N; Q does not add.'
APPROX_NOTE='Ground shunts form a series equivalent; fitted branches are approximate. Q does not add.'
APPROX_MESSAGE='Approximate reciprocal pi fit - exact total retained.'
COMPONENTS=(('coupling','Coupling P-N'),('ground','Ground-path equivalent'),('total','Total P-N'))


def finite(value):
    return isinstance(value,(float,int)) and math.isfinite(value)


def metric_values(metric,approximate=False):
    """Format missing quantities explicitly instead of inventing zero."""
    if not isinstance(metric,dict):return 'Unavailable','Unavailable'
    capacitance=metric.get('C_F');quality=metric.get('Q')
    c='{:.9g} fF'.format(capacitance*1e15) if finite(capacitance) else 'Unavailable'
    q='{:.9g}'.format(quality) if finite(quality) else 'Undefined' if quality is None and finite(capacitance) else 'Unavailable'
    conductance=metric.get('G_S');susceptance=metric.get('B_S')
    tolerance=max(1e-30,max(abs(conductance) if finite(conductance) else 0,abs(susceptance) if finite(susceptance) else 0)*1e-14)
    if quality is None and finite(capacitance) and finite(conductance) and conductance < -tolerance:q='Undefined (negative G)'
    if approximate:
        if finite(capacitance):c='~ '+c
        if finite(quality):q='~ '+q
    return c,q


def branch_text(label,metric,approximate=False):
    if not isinstance(metric,dict):return label+': Unavailable'
    c,q=metric_values(metric,approximate)
    g,b=metric.get('G_S'),metric.get('B_S')
    admittance=' | G={:.9g} S, B={:.9g} S'.format(g,b) if finite(g) and finite(b) else ''
    status=str(metric.get('status','')).strip()
    signed=metric.get('Q_signed')
    diagnostic=' | Signed B/G (not passive Q)={:.9g}'.format(signed) if finite(signed) else ''
    return '{}: C={}, Q={}{}{}{}'.format(label,c,q,admittance,' | '+status if status else '',diagnostic)


class DecompositionSummary(ttk.Frame):
    """Three compact cards linked to one actual table row.

    Ground-path equivalent is a differential contribution from the series ground
    path. The P/N-to-AC-ground branch values are separate detail quantities.
    """
    def __init__(self,parent,**kwargs):
        super().__init__(parent,**kwargs)
        self.current_row=None;self._last_width=None;self.cards={};self.values={}
        self.context=tk.StringVar();self.message=tk.StringVar();self.details_text=tk.StringVar()
        self.context_label=ttk.Label(self,textvariable=self.context,style='Muted.TLabel',width=1)
        self.context_label.grid(row=0,column=0,columnspan=3,sticky='ew',padx=5,pady=(2,3))
        for column,(key,title) in enumerate(COMPONENTS):
            self.columnconfigure(column,weight=1,uniform='component')
            frame=ttk.Frame(self,padding=(7,3));frame.grid(row=1,column=column,sticky='nsew',padx=2)
            title_label=ttk.Label(frame,text=title,font='TkDefaultFont',width=1)
            title_label.pack(fill='x')
            c=tk.StringVar();q=tk.StringVar()
            c_label=ttk.Label(frame,textvariable=c,font='TkDefaultFont',width=1);c_label.pack(fill='x',pady=(2,0))
            q_label=ttk.Label(frame,textvariable=q,font='TkDefaultFont',width=1);q_label.pack(fill='x')
            self.cards[key]=dict(frame=frame,title=title_label,c=c,q=q,labels=(title_label,c_label,q_label))
        self.message_label=ttk.Label(self,textvariable=self.message,style='Muted.TLabel',width=1)
        self.message_label.grid(row=2,column=0,columnspan=3,sticky='ew',padx=5,pady=(2,0))
        footer=ttk.Frame(self);footer.grid(row=3,column=0,columnspan=3,sticky='ew',padx=5,pady=(2,3));footer.columnconfigure(0,weight=1)
        self.note=ttk.Label(footer,text=NOTE,style='Muted.TLabel',width=1);self.note.grid(row=0,column=0,sticky='ew')
        self.details_open=tk.BooleanVar(value=False)
        ttk.Checkbutton(footer,text='Details',variable=self.details_open,command=self.toggle_details).grid(row=0,column=1,sticky='e',padx=(7,0))
        self.details_window=None;self.details_frame=None;self.details_widget=None;self.details_scrollbar=None;self._details_content=None
        self.details_text.trace_add('write',self._sync_details)
        self.bind('<Configure>',self._resize)
        self.set_row(None)

    def toggle_details(self):
        if not self.details_open.get():self._close_details();return
        if self.details_window is not None and self.details_window.winfo_exists():
            self.details_window.deiconify();self.details_window.lift();self._sync_details();return
        parent=self.winfo_toplevel();window=tk.Toplevel(parent);self.details_window=window
        window.title('Capacitance / Q details');window.transient(parent)
        sw,sh=window.winfo_screenwidth(),window.winfo_screenheight()
        width,height=min(720,max(260,sw-60)),min(500,max(220,sh-90))
        x=max(0,min(parent.winfo_rootx()+40,sw-width-10));y=max(0,min(parent.winfo_rooty()+35,sh-height-35))
        window.geometry('{}x{}+{}+{}'.format(width,height,x,y));window.minsize(min(360,width),min(220,height))
        window.protocol('WM_DELETE_WINDOW',self._close_details);window.bind('<Escape>',self._close_details)
        window.bind('<Destroy>',self._details_destroyed)
        frame=ttk.Frame(window,padding=8);frame.pack(fill='both',expand=True);self.details_frame=frame
        canvas=tk.Canvas(frame,background='#101e25',highlightthickness=0)
        scrollbar=ttk.Scrollbar(frame,orient='vertical',command=canvas.yview);scrollbar.pack(side='right',fill='y')
        canvas.configure(yscrollcommand=scrollbar.set);canvas.pack(fill='both',expand=True)
        body=tk.Frame(canvas,bg='#101e25',padx=14,pady=12);item=canvas.create_window((0,0),window=body,anchor='nw')
        body.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda e:canvas.itemconfigure(item,width=e.width))
        self.detail_context=tk.StringVar();self.detail_status=tk.StringVar()
        def label(text='',variable=None,color='#d9e8e5'):
            return tk.Label(body,text=text,textvariable=variable,anchor='w',justify='left',bg='#101e25',fg=color,font='TkDefaultFont',wraplength=max(230,width-95))
        label('FLOATING P-N  /  COMPONENTS',color='#7cdcc4').pack(fill='x')
        label(variable=self.detail_context,color='#b1c6c3').pack(fill='x',pady=(4,8))
        label(variable=self.detail_status,color='#f0ce86').pack(fill='x',pady=(0,8))
        label('P -- Coupling -- N    +    P -- Ground P -- Ground N -- N',color='#8de2ce').pack(fill='x')
        label('The two ground shunts form a series equivalent across floating P-N. Total combines complex admittances; Q does not add.',color='#b1c6c3').pack(fill='x',pady=(5,12))
        tableframe=ttk.Frame(body);tableframe.pack(fill='x')
        self.detail_table=ttk.Treeview(tableframe,columns=('c','q','g','b'),show='tree headings',height=7)
        for col,title,w in [('#0','Component',180),('c','C (fF)',120),('q','Q',155),('g','G (S)',120),('b','B (S)',120)]:
            self.detail_table.heading(col,text=title);self.detail_table.column(col,width=w,minwidth=80,stretch=True,anchor='w' if col=='#0' else 'e')
        hs=ttk.Scrollbar(tableframe,orient='horizontal',command=self.detail_table.xview);self.detail_table.configure(xscrollcommand=hs.set);self.detail_table.pack(fill='x');hs.pack(fill='x')
        self.diagnostic_open=tk.BooleanVar(value=False)
        ttk.Checkbutton(body,text='Technical diagnostics / complex values',variable=self.diagnostic_open,command=self._toggle_diagnostic).pack(anchor='w',pady=(10,3))
        self.diagnostic_frame=ttk.Frame(body)
        text=tk.Text(self.diagnostic_frame,wrap='word',height=12,width=1,bg='#152a32',fg='#dcebe8',insertbackground='#dcebe8',selectbackground='#34576a',font='TkFixedFont',state='disabled',exportselection=False)
        ds=ttk.Scrollbar(self.diagnostic_frame,orient='vertical',command=text.yview);ds.pack(side='right',fill='y');text.configure(yscrollcommand=ds.set);text.pack(fill='both',expand=True)
        text.bind('<Control-a>',self._select_details);text.bind('<Control-A>',self._select_details)
        text.bind('<Control-c>',self._copy_details);text.bind('<Control-C>',self._copy_details)
        self.details_widget=text;self.details_scrollbar=scrollbar;self._details_content=None
        self._sync_details();self.detail_table.focus_set()

    def _toggle_diagnostic(self):
        if self.diagnostic_open.get():self.diagnostic_frame.pack(fill='both',expand=True)
        else:self.diagnostic_frame.pack_forget()

    def _sync_details(self,*args):
        text=self.details_widget
        if text is None:return
        try:
            if not text.winfo_exists():return
            content=self.context.get()+'\n\n'+self.details_text.get()
            if content==self._details_content:return
            self._details_content=content
            text.configure(state='normal');text.delete('1.0','end');text.insert('1.0',content);text.configure(state='disabled');text.yview_moveto(0)
            self.detail_context.set(self.context.get())
            row=self.current_row or {}; d=row.get('decomposition') or {}; fit=d.get('approximate') or {}
            approximate=bool(getattr(self,'approximate',False)); valid=bool(d.get('valid'))
            self.detail_status.set('APPROXIMATE FIT - exact total retained' if approximate else 'EXACT - reciprocal decomposition' if valid else 'UNAVAILABLE - '+(self.message.get() or 'Select a result row'))
            self.detail_table.delete(*self.detail_table.get_children())
            for key,title in [('coupling','Coupling P-N'),('ground','Ground series equivalent'),('total','Total P-N'),('p_ground','P-to-ground shunt'),('n_ground','N-to-ground shunt'),('p_input','P input (N grounded)'),('n_input','N input (P grounded)')]:
                metric=(fit.get(key) if approximate and key in ('coupling','ground','p_ground','n_ground') else d.get(key))
                if key=='total' and not d:metric=row or None
                c,q=metric_values(metric,approximate and key in ('coupling','ground','p_ground','n_ground'))
                c=c.replace(' fF','')
                def fmt(k):
                    v=metric.get(k) if isinstance(metric,dict) else None
                    return '{:.9g}'.format(v) if finite(v) else 'Unavailable'
                title += ' ~' if approximate and key in ('coupling','ground','p_ground','n_ground') else ''
                self.detail_table.insert('','end',text=title,values=(c,q,fmt('G_S'),fmt('B_S')))
        except tk.TclError:pass

    def _select_details(self,event=None):
        if self.details_widget is not None:self.details_widget.tag_add('sel','1.0','end-1c')
        return 'break'

    def _copy_details(self,event=None):
        try:
            value=self.details_widget.get('sel.first','sel.last');self.clipboard_clear();self.clipboard_append(value)
        except (tk.TclError,AttributeError):pass
        return 'break'

    def _close_details(self,event=None):
        self.details_open.set(False);window=self.details_window
        self.details_window=None;self.details_widget=None;self.details_frame=None;self.details_scrollbar=None;self._details_content=None
        if window is not None:
            try:window.destroy()
            except tk.TclError:pass
        return 'break'

    def _details_destroyed(self,event):
        if event.widget is self.details_window:
            self.details_window=None;self.details_widget=None;self.details_frame=None;self.details_scrollbar=None;self._details_content=None
            try:self.details_open.set(False)
            except tk.TclError:pass

    def _resize(self,event):
        width=event.width
        if width<20 or width==self._last_width:return
        self._last_width=width
        for card in self.cards.values():
            for label in card['labels']:label.configure(wraplength=max(28,width//3-22))
        for label in (self.context_label,self.message_label):label.configure(wraplength=max(60,width-18))
        self.note.configure(wraplength=max(60,width-105))

    def set_row(self,row,names=()):
        self.current_row=row
        if not row:
            self.context.set('Select a result row to inspect C / Q components.')
        else:
            identity=['{}={}'.format(name,row[name]) for name in names if name in row]
            if not identity and 'code' in row:identity.append('Code={}'.format(row['code']))
            if 'point' in row:identity.insert(0,'Point {}'.format(row['point']+1))
            frequency=row.get('frequency_Hz')
            if finite(frequency):identity.append('f={:.9g} GHz'.format(frequency/1e9))
            self.context.set('Selected point: '+' | '.join(identity))
        decomposition=row.get('decomposition') if isinstance(row,dict) else None
        if not isinstance(decomposition,dict) or decomposition.get('schema')!=1:
            decomposition=None
        fit=decomposition.get('approximate') if decomposition else None
        approximate=bool(decomposition and not decomposition.get('valid') and isinstance(fit,dict)
                         and all(isinstance(fit.get(key),dict) for key in ('coupling','ground','total')))
        self.approximate=approximate
        self.note.configure(text=APPROX_NOTE if approximate else NOTE)
        for key,_ in COMPONENTS:
            if decomposition:component=fit.get(key) if approximate and key!='total' else decomposition.get(key)
            else:component=row if key=='total' and isinstance(row,dict) else None
            c,q=metric_values(component,approximate and key!='total')
            self.values[key]=dict(C=c,Q=q)
            self.cards[key]['c'].set('C = '+c);self.cards[key]['q'].set('Q = '+q)
        if not decomposition:
            self.message.set(UNAVAILABLE if row else 'No selected measurement.')
            self.message_label.grid()
            self.details_text.set('Component separation unavailable. The existing single-port measurement provides only total P/N C and Q; enable separation and run for the branch summary.')
            return
        message=str(decomposition.get('message','')).strip()
        self.message.set(APPROX_MESSAGE if approximate else '' if decomposition.get('valid') else message or 'Decomposition unavailable at this measurement point.')
        if self.message.get():self.message_label.grid()
        else:self.message_label.grid_remove()
        components=fit if approximate else decomposition
        qualifier=' (approx.)' if approximate else ''
        lines=[branch_text('Coupling P-N'+qualifier,components.get('coupling'),approximate),
               branch_text('Ground-path equivalent'+qualifier,components.get('ground'),approximate),
               branch_text('Exact total P-N',decomposition.get('total')),
               branch_text('P-to-AC-ground'+qualifier,components.get('p_ground'),approximate),
               branch_text('N-to-AC-ground'+qualifier,components.get('n_ground'),approximate),
               branch_text('Single-ended P input (N AC-grounded)',decomposition.get('p_input')),
               branch_text('Single-ended N input (P AC-grounded)',decomposition.get('n_input'))]
        error=decomposition.get('reciprocity_error')
        if finite(error):lines.append('Reciprocity residual (relative): {:.6g}'.format(error))
        if approximate:
            lines.append(branch_text('Fitted total P-N (approx.)',fit.get('total'),True))
            correction=fit.get('correction')
            if isinstance(correction,dict):
                values=[]
                for key,label,scale,unit in (('C_F','C',1e15,'fF'),('G_S','G',1,'S'),('B_S','B',1,'S')):
                    value=correction.get(key)
                    values.append('{}={:.9g} {}'.format(label,value*scale,unit) if finite(value) else label+'=Unavailable')
                lines.append('Exact-minus-fit correction: '+', '.join(values))
            residual=fit.get('reconstruction_error')
            if finite(residual):lines.append('Fit total Y reconstruction residual (relative): {:.9g}'.format(residual))
            residual=fit.get('c_sum_residual_F')
            if finite(residual):lines.append('C coupling + C ground - C exact total: {:.9g} fF'.format(residual*1e15))
            method=str(fit.get('method','')).strip()
            if method:lines.append('Fit method: '+method)
            fit_message=str(fit.get('message','')).strip()
            if fit_message:lines.append('Fit status: '+fit_message)
            lines.append('Approximate equivalent fit; not a physical capacitor inventory.')
        else:
            metrics=[decomposition.get(key) for key in ('coupling','ground','total')]
            if all(isinstance(metric,dict) and finite(metric.get('C_F')) for metric in metrics):
                residual=(metrics[0]['C_F']+metrics[1]['C_F']-metrics[2]['C_F'])*1e15
                lines.append('C sum residual: {:.6g} fF'.format(residual))
        lines.append('Definition: floating common mode. The P/N ground branches form a series path between P and N; their equivalent contribution is shown above.')
        lines.append('P/N-to-ground values are shunt branches of the reduced two-port network, not their raw single-ended input capacitances.')
        if message:lines.append('Strict decomposition status: '+message)
        self.details_text.set('\n'.join(lines))
