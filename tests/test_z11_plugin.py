import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from caplab.model import new_project
from caplab import z11_plugin


class Z11PluginTest(unittest.TestCase):
    def test_source_identity_and_bad_library(self):
        p=new_project();p['maestro_runtime']={'source':{'library':'L','cell':'C','view':'maestro','workspace':'/tmp'}}
        self.assertEqual(z11_plugin.source_identity(p),{'library':'L','cell':'C','view':'maestro','workspace':'/tmp'})
        p['maestro_runtime']['source']['library']='bad name'
        with self.assertRaisesRegex(ValueError,'library'):z11_plugin.source_identity(p)

    def test_discovery_explicit_and_failure(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ('z11_launch','z11_integration.py','z11_schematic.il'):Path(d,n).touch()
            self.assertEqual(z11_plugin.discover(d),d)
            with self.assertRaisesRegex(ValueError,'not found'):z11_plugin.discover(d+'/missing')

    def test_offline_missing_dependencies_explain_recovery(self):
        failed=mock.Mock(returncode=1,stderr=b'ModuleNotFoundError: matplotlib')
        with mock.patch.object(z11_plugin.subprocess,'run',return_value=failed),mock.patch.object(z11_plugin.subprocess,'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError,'Z11_PYTHON'):z11_plugin.launch_standalone()
            launch.assert_not_called()

    def test_reuse_is_bound_to_source_context(self):
        class Fake(object):
            def __init__(self,*a,**k):self.code=None
            def poll(self):return self.code
        p=new_project();p.update(workspace='/tmp',library='LIB',reference={'cell':'CELL'})
        with tempfile.TemporaryDirectory() as d, mock.patch.object(z11_plugin,'ROOT',Path(d)), mock.patch.object(z11_plugin,'discover',return_value='/test/z11'), mock.patch.object(z11_plugin,'_runtime_copy',return_value='/test/copy'), mock.patch.object(z11_plugin.subprocess,'Popen',side_effect=Fake) as launch:
            z11_plugin._RUNNING.clear();a=z11_plugin.launch_z11(p);b=z11_plugin.launch_z11(p)
            self.assertIs(a,b);self.assertEqual(launch.call_count,1)
            p['library']='LIB2';c=z11_plugin.launch_z11(p);self.assertIsNot(a,c)
            self.assertEqual(launch.call_count,2);z11_plugin._RUNNING.clear()

if __name__=='__main__':unittest.main()
