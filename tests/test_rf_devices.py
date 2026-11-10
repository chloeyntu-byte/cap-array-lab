import tempfile
import unittest
from pathlib import Path
from caplab.touchstone import inspect
from caplab.devices import create_snp, create_passive, instance_skill, validate_device
from caplab.model import new_project

class Devices(unittest.TestCase):
    def test_touchstone_and_native_pcell(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'sample.s2p'
            path.write_text('# GHZ S RI R 50\n1 0 0 1 0 1 0 0 0\n2 0 0 1 0 1 0 0 0\n')
            meta = inspect(path)
            self.assertEqual((meta['ports'],meta['points'],meta['frequency_max_hz']), (2,2,2e9))
            project = new_project(); node = create_snp(project, path)
            self.assertEqual([p['name'] for p in node['symbol']['pins']], ['p1','m1','p2','m2'])
            self.assertIn('list("p" "int" 2)', instance_skill(node,0,0))
            self.assertIn('"dataFile"', instance_skill(node,0,0))
            path.write_text('# GHZ S RI R 50\n1 0 0\n')
            with self.assertRaises(ValueError): validate_device(node)
    def test_touchstone_v2_and_invalid(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)/'test.ts'
            p.write_text('[Version] 2.0\n# MHz S MA R 50\n[Number of Ports] 1\n[Number of Frequencies] 2\n[Reference] 75\n[Network Data]\n10 0.5 90\n20 0.5 90\n[End]\n')
            self.assertEqual(inspect(p)['references'],[75])
            p.write_text(p.read_text().replace('[End]',''))
            with self.assertRaises(ValueError): inspect(p)
    def test_passive_and_coupling(self):
        p=new_project(); a=create_passive(p,'indq'); b=create_passive(p,'indq')
        k=create_passive(p,'mind',props={'ind1':a['id'],'ind2':b['id'],'k':'0.4'})
        validate_device(k,p['instances'])
        self.assertEqual(k['symbol']['pins'],[])
        self.assertIn('"fq"',instance_skill(a,0,0))
        k['props']['ind2']=a['id']
        with self.assertRaises(ValueError): validate_device(k,p['instances'])

if __name__ == '__main__': unittest.main()
