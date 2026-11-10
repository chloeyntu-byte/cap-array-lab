import copy
import math
import tempfile
import unittest
from pathlib import Path
from caplab.model import number, frequencies, codes, expand_pin, new_project, add_instance, validate, cq, save_project, load_project
from caplab.backend import Backend, digest


def fixture():
    p=new_project();p['version']=1
    sym={'library':'L','cell':'C','bbox':[0,0,1,1], 'shapes':[],
         'pins':[{'name':n,'xy':[0,0],'direction':'inputOutput'} for n in ['P','N','VDD','VSS','SW<3:0>']]}
    add_instance(p,sym)
    return p


class ModelTests(unittest.TestCase):
    def test_engineering_units(self):
        self.assertEqual(frequencies('1G, 2.4GHz 100M'),[1e8,1e9,2.4e9])
        self.assertEqual(number('10m'),.01)
        for text in ['1/0','nan','inf','__import__("os")','1;exit()']:
            with self.assertRaises(ValueError): number(text)

    def test_codes_bounds_and_inclusive_ranges(self):
        self.assertEqual(codes('0,3 7:15:4',4),[0,3,7,11,15])
        self.assertEqual(codes('3:0',2),[3,2,1,0])
        for text in ['0:15:0','0:15:-1','16','-1','0:1000000000']:
            with self.assertRaises(ValueError): codes(text,4)

    def test_vector_indices_are_semantic_lsb(self):
        self.assertEqual(expand_pin('B<0:3>'),['B<0>','B<1>','B<2>','B<3>'])
        self.assertEqual(expand_pin('B<3:0>'),['B<3>','B<2>','B<1>','B<0>'])
        cfg=validate(fixture())
        self.assertEqual(cfg['nets']['I0.SW<0>'],'CAL_B<0>')

    def test_short_and_unconnected_pins_are_rejected(self):
        p=fixture(); p['edges']=[['I0.P','I0.N']]
        with self.assertRaisesRegex(ValueError,'shorted'): validate(p)
        p=fixture(); p['instances'][0]['roles']['SW<0>']=''
        with self.assertRaisesRegex(ValueError,'no role'): validate(p)

    def test_multiple_instances_share_roles_and_internal_wire(self):
        p=fixture(); second=copy.deepcopy(p['instances'][0]); second['id']='I1'; p['instances'].append(second)
        p['instances'][0]['roles']['P']=''
        p['edges']=[['I0.P','I1.P']]
        cfg=validate(p)
        self.assertEqual(cfg['nets']['I0.P'],cfg['nets']['I1.P'])
        self.assertEqual(cfg['nets']['I0.SW<3>'],cfg['nets']['I1.SW<3>'])

    def test_signed_admittance_and_lossless_behavior(self):
        y=complex(.001,2*math.pi*1e9*1e-12)
        c,q,status=cq(y,1e9)
        self.assertAlmostEqual(c/1e-12,1)
        self.assertAlmostEqual(q,y.imag/y.real)
        self.assertEqual(status,'Capacitive')
        self.assertIsNone(cq(complex(0,.1),1e9)[1])
        self.assertEqual(cq(complex(.001,-.1),1e9)[2],'Non-capacitive')
        self.assertEqual(cq(complex(-.001,.1),1e9)[2],'Negative conductance')

    def test_project_persistence_and_identity(self):
        p=fixture(); d=digest(p)
        p['instances'][0]['x']=999
        self.assertEqual(d,digest(p))
        p['instances'][0]['view']='av_extracted'
        self.assertNotEqual(d,digest(p))
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'project.json'; save_project(path,p); save_project(path,p)
            self.assertEqual(load_project(path),p)
            self.assertEqual(len(list(Path(folder).glob('*.bak-*'))),1)

    def test_partial_results_never_become_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'result.tsv'
            path.write_text('HISTORY\tInteractive.0\n')
            with self.assertRaisesRegex(RuntimeError,'sample count'):
                Backend.read_results(path,validate(fixture()))

if __name__=='__main__':
    unittest.main()
