import copy
import math
import unittest
from caplab.demo import make_workbook
from caplab.workbook import workbook, cached_rows
from caplab.resonance_view import resonance_data
from caplab.touchstone import inspect
from caplab.model import ROOT


class DemoTest(unittest.TestCase):
    def test_analytic_capacitance_and_q(self):
        book=workbook(make_workbook())
        rows=cached_rows(book['pages'][0]['result'])
        self.assertEqual(len(rows),24)
        for row in rows:
            self.assertAlmostEqual(row['C_F']*1e15,550+125*row['BANK'],places=7)
            self.assertAlmostEqual(row['Q'],80*row['frequency_Hz']/1e9,places=6)
            split=row['decomposition']
            self.assertTrue(split['valid'])
            self.assertAlmostEqual(split['ground']['C_F']*1e15,50,places=7)

    def test_resonance_matches_closed_form_and_preserves_data(self):
        p=make_workbook()['pages'][1]['project'];before=copy.deepcopy(p)
        data=resonance_data(p)
        self.assertEqual(len(data['waveforms']),808)
        for row in data['rows']:
            exact=1/(2*math.pi*math.sqrt(2e-9*(.55+.125*row['BANK'])*1e-12))
            self.assertLess(abs(row['frequency_Hz']/exact-1),.001)
            self.assertFalse(data['losses'][row['point']]['available'])
        self.assertEqual(p,before)

    def test_shipped_workbook_and_snp_are_portable(self):
        import json
        book=workbook(json.loads((ROOT/'examples/ideal_array.json').read_text(encoding='utf-8')))
        self.assertTrue(all(cached_rows(p['result']) for p in book['pages']))
        meta=inspect(ROOT/'examples/ideal_array.s2p')
        self.assertEqual((meta['ports'],meta['points']),(2,101))
        self.assertTrue(all(p['project']['workspace']=='' for p in book['pages']))
