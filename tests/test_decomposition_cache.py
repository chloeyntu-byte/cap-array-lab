"""Saved two-port physics is recomputed from matrix samples, never faked."""
import copy
import hashlib
import json
import math
import unittest
from unittest.mock import patch
from caplab.backend import Backend,digest
from caplab.decomposition import decompose,result_names
from caplab.model import new_project
from caplab.registers import add_register
from caplab.spectrum import resample
from caplab.workbook import cache,cached_rows,rows_hash


def network(f):
    w=2*math.pi*f
    mutual=.0002+1j*w*1e-12
    p=.001+1j*w*1e-12
    n=.001+1j*w*3e-12
    return mutual+p,-mutual,-mutual,mutual+n


def saved():
    rows=[]
    for f in (1e9,3e9):
        matrix=network(f);split=decompose(*(list(matrix)+[f]))
        row=dict(point=0,BANK=7,frequency_Hz=f,G_S=split['total']['G_S'],B_S=split['total']['B_S'])
        for pair,value in zip(('11','12','21','22'),matrix):
            row['Y'+pair+'_G_S']=value.real;row['Y'+pair+'_B_S']=value.imag
        rows.append(row)
    return dict(version=2,analysis='sp',rows=rows)


class DecompositionCacheTests(unittest.TestCase):
    def test_matrix_interpolates_before_floating_reduction(self):
        row=resample(saved(),'2G')[0];expected=decompose(*(list(network(2e9))+[2e9]))
        self.assertEqual(result_names(row),['BANK'])
        self.assertEqual((row['point'],row['BANK']),(0,7))
        self.assertAlmostEqual(row['C_F']/expected['total']['C_F'],1.,places=13)
        self.assertAlmostEqual(row['decomposition']['ground']['Q'],expected['ground']['Q'],places=10)
        self.assertAlmostEqual(row['decomposition']['coupling']['C_F']+row['decomposition']['ground']['C_F'],row['C_F'],delta=1e-27)
        wrong=sum(r['B_S'] for r in saved()['rows'])/2/(2*math.pi*2e9)
        self.assertGreater(abs(wrong-row['C_F']),1e-18)

    def test_apply_saved_matrix_starts_no_native_process(self):
        p=new_project();p['settings']['frequencies']='2G';spectrum=saved()
        data=json.dumps(spectrum,sort_keys=True,separators=(',',':')).encode()
        p['private']=dict(digest=digest(p),spectrum_schema=2,spectrum=spectrum,spectrum_data_sha256=hashlib.sha256(data).hexdigest())
        with patch.object(Backend,'native',side_effect=AssertionError('Unexpected simulator')):
            rows=Backend().refresh(p)
        self.assertTrue(rows[0]['decomposition']['valid'])

    def test_incomplete_matrix_is_rejected(self):
        data=saved();del data['rows'][0]['Y12_B_S']
        with self.assertRaisesRegex(ValueError,'incomplete'):resample(data,'2G')
        data=saved()
        for row in data['rows']:
            for key in list(row):
                if key.startswith('Y'):del row[key]
        with self.assertRaisesRegex(ValueError,'incomplete'):resample(data,'2G')

    def test_cached_decomposition_survives_json_and_validates_additivity(self):
        rows=resample(saved(),'1G,2G');result=cache(rows,'a'*64,'cartesian')
        restored=json.loads(json.dumps(result,allow_nan=False))
        self.assertEqual(cached_rows(restored),rows)
        restored['rows'][0]['decomposition']['coupling']['C_F']*=2
        restored['sha256']=rows_hash(restored['rows'])
        self.assertEqual(cached_rows(restored),[])
        restored=copy.deepcopy(result);restored['rows'][0]['decomposition']['ground']['Q']=float('nan')
        restored['sha256']=rows_hash(restored['rows'])
        self.assertEqual(cached_rows(restored),[])

    def test_single_ended_inputs_are_distinct_from_ground_shunts(self):
        f=1e9;w=2*math.pi*f;y=1j*w*1e-12
        split=decompose(2*y,-y,-y,4*y,f)
        self.assertAlmostEqual(split['p_input']['C_F']/1e-12,2.)
        self.assertAlmostEqual(split['n_input']['C_F']/1e-12,4.)
        self.assertAlmostEqual(split['p_ground']['C_F']/1e-12,1.)
        self.assertAlmostEqual(split['ground']['C_F']/1e-12,.75)
        self.assertAlmostEqual(split['total']['C_F']/1e-12,1.75)

    def test_new_metric_identifiers_cannot_overwrite_register_values(self):
        for name in ('decomposition','Y11_G_S','y22_b_s'):
            with self.assertRaisesRegex(ValueError,'reserved'):add_register(new_project(),name,4,'0')


if __name__=='__main__':unittest.main()
