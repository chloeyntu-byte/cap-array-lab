import copy
import tempfile
import unittest
from pathlib import Path
from caplab.model import load_project,save_project
from caplab.workbook import workbook,current,add_page,rename_page,delete_page,cache,cached_rows


class WorkbookTests(unittest.TestCase):
 def test_duplicate_isolates_graph_private_and_results(self):
  b=workbook();p=current(b);p['project']['private']={'cell':'old'}
  p['project']['settings']['vdd']='1'
  p['result']={'rows':[{'code':1}]}
  q=add_page(b,'copy',p);q['project']['settings']['vdd']='1.2';q['result']['rows'][0]['code']=2
  self.assertIsNone(q['project']['private'])
  self.assertEqual(p['project']['settings']['vdd'],'1')
  self.assertEqual(p['result']['rows'][0]['code'],1)
 def test_page_crud_and_last_page_protection(self):
  b=workbook();first=current(b)['id'];q=add_page(b)
  rename_page(b,q['id'],'Post-layout')
  with self.assertRaisesRegex(ValueError,'Duplicate'):rename_page(b,first,'Post-layout')
  delete_page(b,q['id']);self.assertEqual(b['active_page'],first)
  with self.assertRaisesRegex(ValueError,'at least'):delete_page(b,first)
 def test_result_snapshot_integrity_and_no_external_dependency(self):
  rows=[dict(point=0,COARSE=3,frequency_Hz=1e9,C_F=1e-12,Q=100.,G_S=1e-4,B_S=.01,status='Capacitive')]
  result=cache(rows,'a'*64,'cartesian')
  self.assertEqual(cached_rows(result),rows)
  result['rows'][0]['C_F']=2e-12
  self.assertEqual(cached_rows(result),[])
 def test_save_load_all_pages_results_and_active_page(self):
  b=workbook();add_page(b,'Another page');b['pages'][0]['project']['settings']['vdd']='1.1'
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'all.json';save_project(path,b);loaded=load_project(path)
   self.assertEqual(loaded,b)
 def test_invalid_or_duplicate_page_identity(self):
  b=workbook();b['pages'].append(copy.deepcopy(b['pages'][0]))
  with self.assertRaisesRegex(ValueError,'identifier'):workbook(b)

 def test_legacy_builtin_names_and_status_translate_but_custom_names_stay(self):
  from caplab.workbook import rows_hash
  b=workbook();p=current(b);p['name']='页面 1'
  row=dict(point=0,frequency_Hz=1e9,C_F=1e-12,Q=100.,G_S=1e-4,B_S=.01,status='电容区')
  p['result']={'rows':[row],'digest':'a'*64,'mode':'cartesian','sha256':rows_hash([row])}
  q=add_page(b,'我的后仿')
  loaded=workbook(b)
  self.assertEqual(loaded['pages'][0]['name'],'Page 1')
  self.assertEqual(loaded['pages'][1]['name'],'我的后仿')
  self.assertEqual(cached_rows(loaded['pages'][0]['result'])[0]['status'],'Capacitive')

 def test_known_builtin_page_names_and_legacy_unfiltered_values(self):
  b=workbook();b['pages'][0]['name']='单阵列'
  for name in ('多阵列','前仿','后仿','页面7','Custom 后仿'):
   add_page(b,name)
  b['pages'][0]['filters']={'COARSE':'全部','FINE':'All','TRIM':'7'}
  loaded=workbook(b)
  self.assertEqual([p['name'] for p in loaded['pages']],['Single array','Multiple arrays','Pre-layout','Post-layout','Page 7','Custom 后仿'])
  self.assertEqual(loaded['pages'][0]['filters'],{'TRIM':'7'})
  self.assertEqual(b['pages'][0]['filters'],{'COARSE':'全部','FINE':'All','TRIM':'7'})
 def test_distributed_examples_use_named_nets_and_sp_settings(self):
  from caplab.model import ROOT,validate
  for path in (ROOT/'examples').glob('*.json'):
   data=load_project(path)
   projects=[p['project'] for p in data['pages']] if data.get('kind')=='caplab-workbook' else [data]
   for project in projects:
    self.assertEqual(project['version'],3,path.name)
    self.assertEqual(project['edges'],[],path.name)
    self.assertEqual(project['settings']['analysis'],'sp',path.name)
    self.assertTrue(all(k in project['settings'] for k in ('simulation_start','simulation_stop','simulation_points','simulation_sweep')))
    validate(project)
   if data.get('kind')=='caplab-workbook' and path.name=='workbook.json':
    self.assertEqual([p['name'] for p in data['pages']],['Single array','Multiple arrays'])

if __name__=='__main__':unittest.main()
