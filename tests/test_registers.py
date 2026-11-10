import unittest
from caplab.model import new_project,add_instance,validate
from caplab.registers import add_register,connect,scan_plan,upgrade_project,edit_register,assign_net,register_bus_name
from caplab.plotting import curve_groups
from caplab.backend import digest


def graph(count=2):
 p=new_project()
 sym={'library':'L','cell':'ARRAY','bbox':[0,0,1,1],'shapes':[],
      'pins':[{'name':n,'xy':[0,0],'direction':'inputOutput'} for n in ('A','Z','VPWR','VGND','EN<3:0>')]}
 for n in range(count):
  i=add_instance(p,sym)
  i['roles'].update(VPWR='VDD',VGND='VSS')
  if n==0:i['roles'].update(A='P',Z='N')
  else:
   connect(p,'I0.A',i['id']+'.A');connect(p,'I0.Z',i['id']+'.Z')
 return p


class RegisterTests(unittest.TestCase):
 def test_independent_and_fixed_scan(self):
  p=graph(3)
  for n,(name,text) in enumerate([('COARSE','0,15'),('FINE','1,7'),('TRIM','5')]):
   r=add_register(p,name,4,text);connect(p,r['id']+'.B<3:0>','I{}.EN<3:0>'.format(n))
  cfg=validate(p)
  self.assertEqual(cfg['scan_points'],[{'COARSE':0,'FINE':1,'TRIM':5},{'COARSE':0,'FINE':7,'TRIM':5},{'COARSE':15,'FINE':1,'TRIM':5},{'COARSE':15,'FINE':7,'TRIM':5}])
  self.assertEqual(cfg['nets']['I0.VPWR'],'CAL_VDD')
  self.assertNotEqual(cfg['nets']['I0.EN<0>'],cfg['nets']['I1.EN<0>'])
 def test_bus_fanout_and_scalar_sharing(self):
  p=graph();r=add_register(p,'BANK',4,'0:15')
  connect(p,'R0.B<3:0>','I0.EN<3:0>');connect(p,'I0.EN<3:0>','I1.EN<3:0>')
  c=validate(p)
  self.assertEqual(c['nets']['I0.EN<0>'],c['nets']['I1.EN<0>'])
 def test_bus_width_and_multiple_driver_rejected(self):
  p=graph(1);add_register(p,'A_CODE',4,'0');add_register(p,'B_CODE',3,'0')
  with self.assertRaisesRegex(ValueError,'width mismatch'):connect(p,'R1.B<2:0>','I0.EN<3:0>')
  edit_register(p,p['registers'][1],'B_CODE',4,'0')
  connect(p,'R0.B<3:0>','I0.EN<3:0>');connect(p,'R1.B<3:0>','I0.EN<3:0>')
  with self.assertRaisesRegex(ValueError,'shorted'):validate(p)
 def test_synchronized_fixed_and_mismatched_lengths(self):
  p=graph(3)
  for n,(name,text) in enumerate([('COARSE','0,3,15'),('FINE','15,7,0'),('TRIM','7')]):
   r=add_register(p,name,4,text);connect(p,r['id']+'.B<3:0>','I{}.EN<3:0>'.format(n))
  p['settings']['sweep_mode']='synchronous'
  c=validate(p)
  self.assertEqual(c['scan_points'][1],{'COARSE':3,'FINE':7,'TRIM':7})
  p['registers'][1]['codes']='0,3'
  with self.assertRaisesRegex(ValueError,'equal lengths'):validate(p)
 def test_names_and_oversized_scan(self):
  p=graph();add_register(p,'COARSE',8,'0:255')
  with self.assertRaisesRegex(ValueError,'Duplicate'):add_register(p,'COARSE',4,'0')
  with self.assertRaisesRegex(ValueError,'reserved'):add_register(p,'CAL_POINT',4,'0')
  add_register(p,'FINE',8,'0:255')
  with self.assertRaisesRegex(ValueError,'exceeds 4096'):scan_plan(p)
 def test_migration_preserves_legacy_register_connections(self):
  from test_model import fixture
  p=upgrade_project(fixture());cfg=validate(p)
  self.assertEqual(len(p['registers']),1)
  self.assertEqual(cfg['nets']['I0.SW<0>'],cfg['nets']['R0.B<0>'])
  self.assertEqual(len(cfg['scan_points']),16)
  self.assertIsNone(p['private'])
 def test_position_is_display_only_but_register_values_change_identity(self):
  p=graph(1);r=add_register(p,'BANK',4,'0,7')
  original=digest(p);r['x']+=900;r['y']-=200
  self.assertEqual(digest(p),original)
  r['codes']='0,15'
  self.assertNotEqual(digest(p),original)
 def test_scalar_bit_connection_and_width_change(self):
  p=graph(1);add_register(p,'BANK',4,'0,15')
  for bit in range(4):connect(p,'R0.B<{}>'.format(bit),'I0.EN<{}>'.format(bit))
  self.assertEqual(validate(p)['nets']['I0.EN<3>'],'CAL_R0_B<3>')
  edit_register(p,p['registers'][0],'BANK',3,'0,7')
  with self.assertRaisesRegex(ValueError,'Floating named net'):validate(p)
 def test_curve_grouping(self):
  rows=[{'COARSE':a,'FINE':b,'point':a*2+b,'frequency_Hz':1e9} for a in (0,1) for b in (0,1)]
  groups=curve_groups(rows,'COARSE',['COARSE','FINE'],'cartesian')
  self.assertEqual([len(g[1]) for g in groups],[2,2])
  self.assertEqual(len(curve_groups(rows,'COARSE',['COARSE','FINE'],'cartesian',{'FINE':'1'})),1)
  self.assertEqual(len(curve_groups(rows,'COARSE',['COARSE','FINE'],'synchronous')),1)

 def test_named_bus_without_wires_and_rename(self):
  p=graph(2);r=add_register(p,'DECIMAL',4,'0,15')
  assign_net(p,'R0.B<3:0>','BANK')
  assign_net(p,'I0.EN<3:0>','BANK<3:0>')
  assign_net(p,'I1.EN<3:0>','BANK')
  cfg=validate(p)
  self.assertEqual(p['edges'],[])
  self.assertEqual(register_bus_name(r),'BANK<3:0>')
  self.assertEqual(cfg['nets']['I0.EN<0>'],'CAL_R0_B<0>')
  self.assertEqual(cfg['nets']['I1.EN<3>'],'CAL_R0_B<3>')
  edit_register(p,r,'OTHER_DECIMAL',4,'0,15')
  self.assertEqual(register_bus_name(r),'BANK<3:0>')
 def test_bus_bit_order_and_atomic_assignment(self):
  p=graph(1);add_register(p,'DECIMAL',4,'0,15')
  assign_net(p,'R0.B<3:0>','BANK<3:0>')
  assign_net(p,'I0.EN<3:0>','BANK<0:3>')
  cfg=validate(p)
  self.assertEqual(cfg['nets']['I0.EN<3>'],cfg['nets']['R0.B<0>'])
  old=p['instances'][0]['roles'].copy()
  with self.assertRaisesRegex(ValueError,'width mismatch'):assign_net(p,'I0.EN<3:0>','BANK<2:0>')
  self.assertEqual(p['instances'][0]['roles'],old)
 def test_named_pin_grammar_and_stimulus_conflicts(self):
  p=graph(1);add_register(p,'CONTROL',4,'0')
  for name in ('bad net','foo/bar','x;exit()','CAL_HIDDEN'):
   with self.assertRaises(ValueError):assign_net(p,'I0.A',name)
  assign_net(p,'R0.B<0>','VDD')
  assign_net(p,'I0.EN<3:0>','CONTROL')
  with self.assertRaisesRegex(ValueError,'reserved stimulus'):validate(p)
 def test_unconnected_array_net_and_unused_register(self):
  p=graph(1);add_register(p,'CONTROL',4,'0')
  assign_net(p,'I0.EN<3:0>','ORPHAN')
  with self.assertRaisesRegex(ValueError,'Floating named net'):validate(p)
  for bit in range(4):assign_net(p,'I0.EN<{}>'.format(bit),'DC:0')
  with self.assertRaisesRegex(ValueError,'not connected'):validate(p)
 def test_migration_preserves_existing_bus_fanout_and_config(self):
  p=graph(2);r=add_register(p,'CONTROL',4,'0,15')
  # This is a pre-named-net v2 project with native B pins and saved wire edges.
  p['version']=2;p['registers'][0]['roles']={'VSS':'VSS'}
  p['edges']=[['R0.B<3:0>','I0.EN<3:0>'],['I0.EN<3:0>','I1.EN<3:0>']]
  p['config_template']='custom binding';p['private']={'cell':'existing'}
  q=upgrade_project(p);cfg=validate(q)
  self.assertEqual(q['edges'],[]);self.assertEqual(q['version'],3)
  self.assertEqual(cfg['nets']['I0.EN<0>'],cfg['nets']['I1.EN<0>'])
  self.assertEqual(q['config_template'],'custom binding')
  self.assertEqual(q['private'],{'cell':'existing'})
  self.assertEqual(p['version'],2)

if __name__=='__main__':unittest.main()
