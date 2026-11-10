"""Exact scan-point log browser with background file reading."""
import queue
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk


def point_for_row(points,row):
    """Results carry zero-based point; never infer from a directory name."""
    if 'point' not in row:return None
    try:index=int(row['point'])
    except (TypeError,ValueError):return None
    item=points.get(str(index)) if isinstance(points,dict) else None
    return item if item and int(item.get('point',-1))==index else None


def _read_update(path,offset,signature):
    """Read complete initial content and subsequent appended bytes off Tk."""
    if not path:return None,0,None,'Pending: no Spectre log path yet.'
    source=Path(path)
    if source.is_dir():
        first=source/'psf'/'spectre.out';second=source/'spectre.out'
        source=first if first.is_file() or not second.is_file() else second
    if not source.is_file():return None,0,None,'Pending: '+str(source)
    stat=source.stat();current=(stat.st_dev,stat.st_ino)
    reset=signature!=current or stat.st_size<offset
    if reset:offset=0
    with source.open('rb') as stream:
        stream.seek(offset);data=stream.read();end=stream.tell()
    return data.decode('utf-8',errors='replace'),end,current,('reset' if reset else 'append')


class PointLogViewer(object):
    def __init__(self,app,page_id,point,source='run_points',points=None):
        self.app=app;self.page_id=page_id;self.point=int(point);self.source=source
        page=app.page(page_id);self.points=points if points is not None else (page.get(source,{}) if page else {})
        self.win=tk.Toplevel(app);self.win.title('Spectre log: point {}'.format(self.point+1));self.win.geometry('900x600')
        header=ttk.Frame(self.win);header.pack(fill='x',padx=8,pady=4)
        ttk.Label(header,text='Test').pack(side='left');self.test_var=tk.StringVar(value='CAP_SP')
        self.test_combo=ttk.Combobox(header,textvariable=self.test_var,values=['CAP_SP'],state='readonly',width=16);self.test_combo.pack(side='left',padx=6)
        self.status=tk.StringVar(value='Waiting for point log...');ttk.Label(header,textvariable=self.status).pack(side='left',fill='x',expand=True)
        frame=ttk.Frame(self.win);frame.pack(fill='both',expand=True)
        self.text=tk.Text(frame,wrap='none');ys=ttk.Scrollbar(frame,command=self.text.yview);xs=ttk.Scrollbar(self.win,orient='horizontal',command=self.text.xview)
        self.text.configure(yscrollcommand=ys.set,xscrollcommand=xs.set,state='disabled');ys.pack(side='right',fill='y');self.text.pack(fill='both',expand=True);xs.pack(fill='x')
        self.queue=queue.Queue();self.busy=False;self.offset=0;self.signature=None;self.current_path=None
        self.after_id=None;self.win.bind('<Destroy>',self._destroyed,add='+')
        self.win.protocol('WM_DELETE_WINDOW',self.win.destroy);self.after_id=self.win.after(50,self._poll)

    def _destroyed(self,event):
        if event.widget is self.win and self.after_id is not None:
            try:self.win.after_cancel(self.after_id)
            except tk.TclError:pass
            self.after_id=None

    def _poll(self):
        self.after_id=None
        if not self.win.winfo_exists():return
        try:
            while True:
                path,result,error=self.queue.get_nowait();self.busy=False
                if path!=self.current_path:continue
                if error:self.status.set('Log error: '+error);continue
                text,self.offset,self.signature,state=result
                if text is None:self.status.set(state);continue
                at_end=self.text.yview()[1]>.99
                if text or state=='reset':
                    self.text.configure(state='normal')
                    if state=='reset':self.text.delete('1.0','end')
                    self.text.insert('end',text);self.text.configure(state='disabled')
                    if at_end:self.text.see('end')
                self.status.set('Point {} | {}'.format(self.point+1,path))
        except queue.Empty:pass
        item=self.points.get(str(self.point),{});logs=item.get('logs') or {}
        tests=sorted(logs,key=lambda name:(name not in ('CAP_SP','CAP_AC'),name))
        if not tests:tests=['CAP_SP']
        self.test_combo.configure(values=tests)
        if self.test_var.get() not in tests:self.test_var.set(tests[0])
        selected=self.test_var.get();path=logs.get(selected,'')
        if not path and selected in ('CAP_SP','CAP_AC'):path=item.get('path','')
        if path!=self.current_path:self.current_path=path;self.offset=0;self.signature=None
        if not self.busy:
            self.busy=True;offset=self.offset;signature=self.signature
            def worker():
                try:self.queue.put((path,_read_update(path,offset,signature),None))
                except Exception as ex:self.queue.put((path,None,str(ex)))
            threading.Thread(target=worker,daemon=True).start()
        self.after_id=self.win.after(500,self._poll)


class PointLogBrowser(object):
    def __init__(self,app,page_id,source='run_points'):
        self.app=app;self.page_id=page_id;self.source=source;page=app.page(page_id)
        self.points=page.get(source,{}) if page else {};self.win=tk.Toplevel(app)
        self.win.title('Scan point logs');self.win.geometry('720x430')
        self.tree=ttk.Treeview(self.win,columns=('codes','state','path'),show='tree headings')
        for col,title,width in [('#0','Point',70),('codes','Register values',180),('state','State',90),('path','Spectre path',350)]:self.tree.heading(col,text=title);self.tree.column(col,width=width)
        self.tree.pack(fill='both',expand=True,padx=8,pady=8);self.tree.bind('<Double-1>',self.open_selected)
        self.tree.bind('<Button-3>',self.menu);ttk.Button(self.win,text='Open selected log',command=self.open_selected).pack(pady=5)
        self.after_id=None;self.win.bind('<Destroy>',self._destroyed,add='+');self._refresh()

    def _destroyed(self,event):
        if event.widget is self.win and self.after_id is not None:
            try:self.win.after_cancel(self.after_id)
            except tk.TclError:pass
            self.after_id=None

    def _refresh(self):
        self.after_id=None
        if not self.win.winfo_exists():return
        points=self.points
        keys=set(points)
        for iid in self.tree.get_children():
            if iid not in keys:self.tree.delete(iid)
        for key,item in sorted(points.items(),key=lambda kv:int(kv[0])):
            values=(', '.join('{}={}'.format(k,v) for k,v in sorted(item.get('codes',{}).items())),item.get('state','pending'),item.get('path',''))
            if self.tree.exists(key):self.tree.item(key,values=values)
            else:self.tree.insert('','end',iid=key,text=str(int(key)+1),values=values)
        self.after_id=self.win.after(500,self._refresh)

    def menu(self,event):
        row=self.tree.identify_row(event.y)
        if row:self.tree.selection_set(row)
        menu=tk.Menu(self.win,tearoff=False);menu.add_command(label='Open Spectre log',command=self.open_selected);menu.tk_popup(event.x_root,event.y_root)

    def open_selected(self,event=None):
        selected=self.tree.selection()
        if selected:PointLogViewer(self.app,self.page_id,int(selected[0]),self.source,points=self.points)


def open_row_log(app,row):
    page=app.page();item=point_for_row(page.get('result_points',{}),row)
    if item is None:
        app.error('This result has no matching saved scan-point log.');return None
    return PointLogViewer(app,page['id'],item['point'],'result_points',points=page['result_points'])
