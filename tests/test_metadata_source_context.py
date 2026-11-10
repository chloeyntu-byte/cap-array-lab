import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from caplab import native_metadata as metadata
from caplab.launch_context import ENV_NAMES

class MetadataContextTests(unittest.TestCase):
    def tearDown(self): metadata._WORKERS.clear()
    def test_workspace_and_native_snapshot_define_worker_identity(self):
        env = {name: {'state': 'unset'} for name in ENV_NAMES}
        other = copy.deepcopy(env); other['PATH'] = {'state':'value', 'value':'/other'}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(metadata, 'MetadataWorker') as factory:
            factory.side_effect = lambda *args: mock.Mock(owner=metadata.os.getpid(), _closed=mock.Mock(is_set=lambda:False), process=None)
            metadata.request(folder,'read',folder,native_environment=env,native_virtuoso='/a/virtuoso')
            metadata.request(folder,'read',folder,native_environment=copy.deepcopy(env),native_virtuoso='/a/virtuoso')
            metadata.request(folder,'read',folder,native_environment=other,native_virtuoso='/a/virtuoso')
            metadata.request(folder,'read',folder,native_environment=env,native_virtuoso='/b/virtuoso')
            metadata.request(folder,'read',folder)
            self.assertEqual(factory.call_count,4)
            self.assertIn(folder,metadata._WORKERS)
    def test_worker_uses_own_snapshot_and_standalone_clears_previous(self):
        env = {name: {'state':'unset'} for name in ENV_NAMES}
        env['PATH'] = {'state':'value','value':'/from-source'}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(metadata, 'ROOT', Path(folder)):
            Path(folder,'cds.lib').write_text('')
            with mock.patch.dict(os.environ, {'CAPLAB_NATIVE_ENV_JSON':'old', 'CAPLAB_NATIVE_VIRTUOSO':'/old'}, clear=False):
                with mock.patch.object(metadata.subprocess, 'Popen') as popen:
                    worker=metadata.MetadataWorker(folder,env,'/source/virtuoso')
                    worker._start(None)
                    child=popen.call_args[1]['env']
                    self.assertEqual(json.loads(child['CAPLAB_NATIVE_ENV_JSON']),env)
                    self.assertEqual(child['CAPLAB_NATIVE_VIRTUOSO'],'/source/virtuoso')
                    standalone=metadata.MetadataWorker(folder)
                    standalone._start(None)
                    child=popen.call_args[1]['env']
                    self.assertNotIn('CAPLAB_NATIVE_ENV_JSON',child)
                    self.assertNotIn('CAPLAB_NATIVE_VIRTUOSO',child)
    def test_source_cwd_with_private_mapping(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(metadata, 'ROOT', Path(folder)):
            mapping = Path(folder,'mapping'); mapping.mkdir(); (mapping/'cds.lib').write_text('')
            cwd = Path(folder,'source'); cwd.mkdir()
            worker = metadata.MetadataWorker(str(mapping), native_workdir=str(cwd))
            with mock.patch.object(metadata.subprocess, 'Popen') as popen:
                worker._start(None)
                self.assertEqual(popen.call_args[1]['cwd'],str(cwd))
                self.assertEqual(popen.call_args[1]['env']['CAPLAB_NATIVE_CDS_LIB'],str(mapping/'cds.lib'))
    def test_invalid_snapshot_rejected_before_worker(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(metadata, 'MetadataWorker') as factory:
            with self.assertRaises(ValueError): metadata.request(folder,'read',folder,native_environment={},native_virtuoso='/v')
            factory.assert_not_called()

if __name__ == '__main__': unittest.main()
