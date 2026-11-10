import copy
import tempfile
import unittest
from pathlib import Path
from caplab.model import new_project, save_project, load_project
from caplab.resonance_view import resonance_data
from caplab.plotting import curve_groups


def spectrum(point,code,root):
    rows=[]
    for f in (1e9,2e9,3e9,4e9):
        y=complex(0.1,(f-root)/1e9)
        row=dict(point=point,BANK=code,frequency_Hz=f,G_S=y.real,B_S=y.imag)
        for pair,value in [('11',y),('12',-y),('21',-y),('22',y)]:
            row['Y{}_G_S'.format(pair)]=value.real;row['Y{}_B_S'.format(pair)]=value.imag
        rows.append(row)
    return rows


class ResonanceDataTest(unittest.TestCase):
    def test_each_native_scan_identity_and_varying_resonance_curve(self):
        p=new_project();p['private']={'spectrum':{'version':2,'rows':spectrum(0,3,1.5e9)+spectrum(1,7,2.5e9)}}
        data=resonance_data(p)
        self.assertEqual(data['names'],['BANK'])
        self.assertEqual([r['point'] for r in data['rows']],[0,1])
        self.assertEqual([r['BANK'] for r in data['rows']],[3,7])
        self.assertEqual([r['frequency_Hz'] for r in data['rows']],[1.5e9,2.5e9])
        self.assertTrue(all(abs(r['Z_real_ohm']-10)<1e-10 for r in data['rows']))
        self.assertTrue(all(not v['available'] for v in data['losses'].values()))
        groups=curve_groups(data['rows'],'BANK',data['names'],'cartesian',include_frequency=False)
        self.assertEqual(len(groups),1);self.assertEqual(len(groups[0][1]),2)

    def test_old_single_port_and_no_crossing_are_explicit(self):
        p=new_project();p['private']={'spectrum':{'rows':spectrum(0,3,9e9)}}
        result=resonance_data(p);self.assertIsNone(result['rows'][0]['frequency_Hz'])
        self.assertIn('No resolved',result['rows'][0]['status'])
        for row in p['private']['spectrum']['rows']:
            for key in list(row):
                if key.startswith('Y'):del row[key]
        self.assertIn('two-port',resonance_data(p)['rows'][0]['status'])

    def test_derived_view_does_not_mutate_saved_data(self):
        p=new_project();p['private']={'spectrum':{'rows':spectrum(0,3,1.5e9)}}
        before=copy.deepcopy(p)
        resonance_data(p);self.assertEqual(p,before)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'rf.json';save_project(path,p);restored=load_project(path)
            self.assertEqual(resonance_data(restored),resonance_data(p))

    def test_corrupted_native_spectrum_is_not_used(self):
        import hashlib,json
        p=new_project();p['private']={'spectrum':{'rows':spectrum(0,3,1.5e9)}}
        private=p['private'];private['spectrum_data_sha256']=hashlib.sha256(json.dumps(private['spectrum'],sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()
        self.assertEqual(len(resonance_data(p)['rows']),1)
        private['spectrum']['rows'][0]['Y11_B_S']+=0.1
        with self.assertRaisesRegex(ValueError,'checksum'):resonance_data(p)


if __name__=='__main__':unittest.main()
