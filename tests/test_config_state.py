import tempfile
import unittest
from pathlib import Path
from caplab.backend import digest
from caplab.config_state import capture_saved_config, config_hash, record_config_baseline


class ConfigStateTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        path=Path(self.temp.name)/'config';path.mkdir()
        self.file=path/'expand.cfg'
        self.original='//Revision 1\nconfig CAL_A;\ndesign Lib.CAL_A:schematic;\ninst (Lib.CAL_A:schematic).X binding L.CAL_A:extracted;\nendconfig\n'
        self.file.write_text(self.original)
        self.p={'instances':[],'settings':{},'private':{'cellpath':self.temp.name,'library':'Lib','cell':'CAL_A', 'spectrum_schema':2,
                  'spectrum':{'value':1},'spectrum_sha256':'old','spectrum_data_sha256':'old','result_sha256':'old','history':'old'}}
        self.p['private']['digest']=digest(self.p)
        record_config_baseline(self.p)
    def test_saved_edit_captured_and_cache_invalidated(self):
        self.file.write_text(self.original.replace('extracted','schematic'))
        self.assertTrue(capture_saved_config(self.p))
        self.assertIn('design @CAL_TOP@:schematic',self.p['config_template'])
        self.assertIn('L.CAL_A:schematic',self.p['config_template'])
        self.assertEqual(self.p['private']['digest'],digest(self.p))
        self.assertEqual(self.p['private']['config_sha256'],config_hash(self.p['private']))
        self.assertEqual(self.p['private']['spectrum_schema'],2)
        for key in ('spectrum','spectrum_sha256','spectrum_data_sha256','result_sha256','history'):
            self.assertNotIn(key,self.p['private'])
        self.assertFalse(capture_saved_config(self.p))
    def test_existing_project_changes_still_require_rebuild(self):
        self.p['settings']['temperature']=40
        old=self.p['private']['digest']
        self.file.write_text(self.original.replace('extracted','schematic'))
        self.assertTrue(capture_saved_config(self.p))
        self.assertEqual(self.p['private']['digest'],old)
        self.assertNotEqual(old,digest(self.p))
    def test_unknown_baseline_invalidates_cache(self):
        self.p['private'].pop('config_sha256')
        self.assertTrue(capture_saved_config(self.p))
        self.assertNotIn('result_sha256',self.p['private'])
    def test_wrong_private_identity_is_rejected(self):
        self.file.write_text('config other;\ndesign something.else:schematic;\n')
        with self.assertRaises(ValueError):capture_saved_config(self.p)
    def test_no_private_is_noop(self):
        self.assertFalse(capture_saved_config({}))

if __name__ == '__main__': unittest.main()
