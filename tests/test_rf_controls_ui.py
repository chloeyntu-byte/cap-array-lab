import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
import tkinter as tk
from caplab.ui import App
from caplab.model import new_project
from caplab.rf_ui import RFControls
from caplab.pointlogs import PointLogBrowser, open_row_log


@unittest.skipUnless(os.environ.get('DISPLAY'),'Tk display required')
class RFControlsUI(unittest.TestCase):
 def setUp(self):
  self.old=App.load_libraries;App.load_libraries=lambda *a,**kw:None
  self.app=App(new_project());self.errors=[];self.app.report_callback_exception=lambda *args:self.errors.append(args)
  self.app.task=lambda work,done,**kw:done(work(None));self.app.error=lambda msg,*a:self.errors.append(msg)
  self.app.update();self.controls=RFControls(self.app);self.controls.attach(tk.Frame(self.app),tk.Frame(self.app));self.controls.open();self.app.update()
 def tearDown(self):
  self.app.destroy();App.load_libraries=self.old;self.assertEqual(self.errors,[])
 def pump(self,seconds):
  until=time.monotonic()+seconds
  while time.monotonic()<until:self.app.update();time.sleep(.03)
 def test_multiple_snp_import_separate_positions(self):
  from caplab.devices import _symbol
  with tempfile.TemporaryDirectory() as d:
   one=Path(d)/'one.s1p';two=Path(d)/'two.s2p'
   one.write_text('# GHz S RI R 50\n1 0 0\n');two.write_text('# GHz S RI R 50\n1 0 0 0 0 0 0 0 0\n')
   def symbol(workspace,cell,ports,backend):
    return _symbol('nport',[v+str(i) for i in range(1,ports+1) for v in ('p','m')])
   with mock.patch('caplab.rf_ui.filedialog.askopenfilenames',return_value=[str(one),str(two)]),mock.patch('caplab.rf_ui.Backend.context',return_value='/tmp'),mock.patch('caplab.rf_ui.devices.native_symbol',side_effect=symbol):self.controls.import_snp()
   nodes=self.app.project['instances'];self.assertEqual([n['touchstone']['ports'] for n in nodes],[1,2])
   self.assertNotEqual((nodes[0]['x'],nodes[0]['y']),(nodes[1]['x'],nodes[1]['y']))
   self.assertEqual(self.app.selected,nodes[1]['id'])

 def test_late_device_result_cannot_change_replaced_project(self):
  from caplab.devices import _symbol
  pending=[];self.app.task=lambda work,done,**kw:pending.append((work,done))
  with mock.patch('caplab.rf_ui.Backend.context',return_value='/tmp'),mock.patch('caplab.rf_ui.devices.native_symbol',return_value=_symbol('res',['PLUS','MINUS'])):
   self.controls.add('res');symbol=pending[0][0](None)
  page=self.app.page();page['project']=new_project();self.app.project=page['project'];pending[0][1](symbol)
  self.assertEqual(page['project']['instances'],[])

 def test_delete_attached_wires_and_mutual_choices(self):
  from caplab import devices
  p=self.app.project
  a=devices.create_passive(p,'indq');b=devices.create_passive(p,'indq');c=devices.create_passive(p,'indq');c['enabled']=False
  m=devices.create_passive(p,'mind');p['wires']=[{'id':'W1','a':{'pin':a['id']+'.PLUS'},'b':{'pin':b['id']+'.PLUS'},'points':[]}]
  self.controls.refresh();self.controls.tree.selection_set(m['id']);self.controls.edit_selected();self.app.update()
  dialog=next(w for w in self.controls.window.winfo_children() if isinstance(w,tk.Toplevel))
  choices=[w.cget('values') for w in dialog.winfo_children() if w.winfo_class()=='TCombobox']
  self.assertEqual(len(choices),2);self.assertEqual(set(choices[0]),{a['id'],b['id']});dialog.destroy()
  self.controls.tree.selection_set(a['id']);self.controls.delete();self.assertEqual(p['wires'],[])

 def test_log_window_freezes_run_and_selects_test(self):
  with tempfile.TemporaryDirectory() as d:
   old=Path(d)/'old';new=Path(d)/'new';cm=Path(d)/'cm'
   old.write_text('old history');new.write_text('new history');cm.write_text('common mode')
   page=self.app.page();points={'0':{'point':0,'codes':{},'state':'running','path':'','logs':{}}};page['run_points']=points
   browser=PointLogBrowser(self.app,page['id']);self.assertEqual(browser.tree.item('0','text'),'1')
   from caplab.pointlogs import PointLogViewer
   viewer=PointLogViewer(self.app,page['id'],0,points=points);self.assertIn('point 1',viewer.win.title())
   page['run_points']={'0':{'point':0,'codes':{},'state':'done','path':str(new)}}
   points['0']['path']=str(old);points['0']['logs'].update(CAP_SP=str(old),CAP_LOSS_CM=str(cm))
   self.pump(1.1);self.assertIn('old history',viewer.text.get('1.0','end'));self.assertNotIn('new history',viewer.text.get('1.0','end'))
   viewer.test_var.set('CAP_LOSS_CM');self.pump(1.1);self.assertIn('common mode',viewer.text.get('1.0','end'))
   self.assertNotIn('new history',viewer.text.get('1.0','end'))

 def test_add_toggle_edit_and_logs(self):
  from caplab.devices import _symbol
  with mock.patch('caplab.rf_ui.Backend.context',return_value='/tmp'),mock.patch('caplab.rf_ui.devices.native_symbol',return_value=_symbol('res',['PLUS','MINUS'])):
   self.controls.add('res');self.app.update()
  n=self.app.project['instances'][0];self.assertEqual(n['kind'],'passive');self.assertTrue(n['enabled'])
  self.controls.toggle();self.assertFalse(n['enabled']);self.controls.toggle();self.assertTrue(n['enabled'])
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'spectre.out';path.write_text('hello\n')
   p=self.app.page();p['run_points']={'0':{'point':0,'codes':{'A':2},'state':'done','path':str(path)}};p['result_points']=dict(p['run_points'])
   browser=PointLogBrowser(self.app,p['id']);self.app.update();self.assertTrue(browser.tree.exists('0'))
   viewer=open_row_log(self.app,{'point':0});self.pump(.8);self.assertIn('hello',viewer.text.get('1.0','end'))
   with path.open('a') as f:f.write('world\n')
   self.pump(1.1);self.assertIn('world',viewer.text.get('1.0','end'))

if __name__=='__main__':unittest.main()
