"""Native matrix coverage and bias-fixture boundary checks."""
import json
import math
from pathlib import Path
import tempfile
import unittest
from caplab.backend import Backend
from caplab.decomposition import decompose


class BackendDecompositionTests(unittest.TestCase):
    def test_complete_matrix_is_required_and_all_entries_survive(self):
        freq=1e9;omega=2*math.pi*freq
        matrix=(.001+omega*600e-15j,-omega*500e-15j,-omega*500e-15j,.0005+omega*800e-15j)
        split=decompose(*(list(matrix)+[freq]));total=split['total']
        samples=[freq,total['G_S'],total['B_S']]
        for y in matrix:samples.extend((y.real,y.imag))
        header='TEST\t1\t"nominal"\t"CAP_SP"\tdone\tnil\t"/task/1/CAP_SP"\nOUT\t"CODE_VALUE"\t"expr"\t0\n'
        cfg=dict(decomposition=True,frequencies=[freq],simulation_frequencies=[freq],codes=[0])
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'spectrum.tsv';path.write_text(header+'SPECTRUM\t'+'\t'.join(str(v) for v in samples)+'\n')
            rows=Backend.read_spectrum(path,cfg);self.assertEqual(rows[0]['Y12_B_S'],matrix[1].imag)
            self.assertEqual(rows[0]['Y22_G_S'],matrix[3].real)
            path.write_text(header+'SPECTRUM\t'+'\t'.join(str(v) for v in samples[:-2])+'\n')
            with self.assertRaisesRegex(RuntimeError,'complete two-port matrix'):Backend.read_spectrum(path,cfg)
            bad=list(samples);bad[1]*=10
            path.write_text(header+'SPECTRUM\t'+'\t'.join(str(v) for v in bad)+'\n')
            with self.assertRaisesRegex(RuntimeError,'disagree'):Backend.read_spectrum(path,cfg)

    def test_fixture_readback_checks_bias_and_probe_paths(self):
        cfg=dict(decomposition=True,nets={})
        expected={'PORT0.PLUS':'CAL_P','PORT0.MINUS':'CAL_BIAS_N','DC_BIAS.PLUS':'CAL_BIAS_N','DC_BIAS.MINUS':'CAL_N'}
        for node in ('P','N'):
            expected.update({'PORT_'+node+'.PLUS':'CAL_PROBE_'+node,'PORT_'+node+'.MINUS':'CAL_VSS','DC_BLOCK_'+node+'.PLUS':'CAL_PROBE_'+node,'DC_BLOCK_'+node+'.MINUS':'CAL_'+node})
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'connectivity.tsv'
            path.write_text(''.join(name.split('.')[0]+'\t'+name.split('.')[1]+'\t'+net+'\n' for name,net in expected.items()))
            Backend._check_connectivity({'instances':[],'registers':[]},cfg,Path(folder))
            expected['DC_BLOCK_P.MINUS']='CAL_N'
            path.write_text(''.join(name.split('.')[0]+'\t'+name.split('.')[1]+'\t'+net+'\n' for name,net in expected.items()))
            with self.assertRaisesRegex(RuntimeError,'fixture mismatch'):Backend._check_connectivity({'instances':[],'registers':[]},cfg,Path(folder))
