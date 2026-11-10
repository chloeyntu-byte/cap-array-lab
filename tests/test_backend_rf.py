import copy
import json
import tempfile
import unittest
from pathlib import Path
from caplab.backend import Backend, digest
from caplab.canvas_tools import native_wire_plan, node_rotation_transform
from caplab.devices import create_passive
from caplab.model import new_project
from caplab.registers import validate_explicit, assign_net

class BackendRF(unittest.TestCase):
    def fixture(self):
        p=new_project();a=create_passive(p,'res',x=300,y=300);b=create_passive(p,'res',x=1000,y=300)
        for n,pin,role in ((a,'PLUS','P'),(a,'MINUS','X'),(b,'PLUS','X'),(b,'MINUS','N')):
            assign_net(p,n['id']+'.'+pin,role)
        p['wires']=[{'id':'W1','a':{'pin':a['id']+'.MINUS'},'b':{'pin':b['id']+'.PLUS'},'points':[]}]
        return p
    def test_wire_geometry_affects_digest_and_sense_gap(self):
        p=self.fixture();cfg=validate_explicit(p); before=digest(p)
        normal=native_wire_plan(p,cfg);sensed=native_wire_plan(p,cfg,isolate_instances=True)
        self.assertTrue(normal);self.assertTrue(sensed)
        self.assertNotEqual(normal[0]['points'],sensed[0]['points'])
        p['instances'][0]['x']+=10
        self.assertNotEqual(before,digest(p))
    def test_loss_test_identity_and_completeness(self):
        cfg={'codes':[0], 'frequencies':[1e9]}
        plan={'probes':[{}]}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); lines=[]
            for test,mode,key in (('CAP_SP',0,'CODE_VALUE'),('CAP_LOSS_CM',1,'LOSS_CODE_VALUE')):
                folder=root/test; (folder/'psf').mkdir(parents=True);(folder/'netlist').mkdir()
                (folder/'psf'/'spectre.out').write_text('spectre completes with 0 errors')
                (folder/'netlist'/'input.scs').write_text('parameters CAL_LOSS_MODE={}'.format(mode))
                lines += ['TEST\t1\t"corner"\t"{}"\tdone\tnil\t{}'.format(test,json.dumps(str(folder))),
                          'OUT\t"{}"\texpr\t0'.format(key),
                          'LOSS_INPUT\t1000000000\t1\t0\t0\t0',
                          'LOSS_PIN\t0\t1000000000\t1\t0\t1\t0']
            path=root/'results';path.write_text('\n'.join(lines))
            self.assertEqual(len(Backend.read_loss_spectrum(path,cfg,plan)['0']),1)
            path.write_text('\n'.join(lines+lines))
            with self.assertRaises(ValueError):Backend.read_loss_spectrum(path,cfg,plan)
            (root/'CAP_LOSS_CM'/'netlist'/'input.scs').write_text('parameters CAL_LOSS_MODE=0')
            path.write_text('\n'.join(lines))
            with self.assertRaises(ValueError):Backend.read_loss_spectrum(path,cfg,plan)

if __name__=='__main__':unittest.main()
